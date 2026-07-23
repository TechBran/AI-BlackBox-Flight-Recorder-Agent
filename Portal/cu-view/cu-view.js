/**
 * cu-view.js — CU Live View: Splashtop-style interactive remote-desktop
 * client over the session's RFB stream (design 2026-07-23 §3-§5, milestone
 * M2). Served at GET /cu/view/{sid}; assets under /ui/cu-view/; the RFB
 * WebSocket rides the Orchestrator proxy at /cu/view/{sid}/ws.
 *
 * Architecture:
 *   - noVNC RFB (vendored 1.5.0) renders at NATIVE resolution into
 *     #cuvScreen; scaleViewport OFF, resizeSession NEVER (the agent's
 *     screen must not change — design D6).
 *   - #cuvTouchLayer covers the canvas (touch-action:none) so noVNC's
 *     default direct-tap handling never sees raw input. ALL input — touch
 *     gestures, mouse, wheel — flows through this layer and maps through
 *     the ViewportTransform to display coords, so clicks stay correct at
 *     any zoom/pan.
 *   - Touch drives the PURE TouchpadMachine (touch-gestures.js): rendered
 *     cursor, relative drag, tap-at-cursor, tap-drag, two-finger
 *     right-click/scroll, pinch-zoom/pan. Mouse is direct (absolute).
 *   - Keys go out as RFB keysyms (keysyms.js) — extra-keys bar with sticky
 *     modifiers + manual keyboard toggle (NO auto-open anywhere, §4.4).
 *   - "Agent acting" dimming listens for postMessage forwards of the chat
 *     SSE cu_action/heartbeat events; degrades gracefully to nothing when
 *     no parent forwards them (§5 — client-side only, honest degradation).
 */

import RFB from "/cu/novnc/core/rfb.js";
import { TouchpadMachine, ViewportTransform, MASK } from "./touch-gestures.js";
import {
    KEYSYMS, MODIFIER_KEYSYMS, EXTRA_KEY_ROWS,
    keysymForChar, buildKeySequence,
} from "./keysyms.js";
import {
    MAIN_ID, buildSwitcherEntries, resolveSwapTarget, sessionMetaFor,
    targetStillListed,
} from "./switcher.js";

// ── Diagnostics ──────────────────────────────────────────────────────────

// Lifecycle beacon installed by index.html's inline script (Android-WebView
// black-screen hunt, 2026-07-23). Firing "module-boot" here proves the WHOLE
// ES-module import chain above (noVNC RFB + local modules) resolved in this
// browser — the inline script fires "page-load" even when it doesn't.
const diag = window.__cuvDiag || (() => {});
diag("module-boot");

// ── Session identity ─────────────────────────────────────────────────────

// Page URL is /cu/view/{sid} (no templating — the path IS the contract).
// MUTABLE (N2 — main-desktop switcher): tapping a rail entry swaps the
// stream in place and rewrites the URL via history.replaceState, so deep
// links keep working. The reserved id "main" targets the REAL desktop.
let sessionId = decodeURIComponent(
    location.pathname.replace(/\/+$/, "").split("/").pop() || "");
const VIEW_ONLY = new URLSearchParams(location.search).get("viewonly") === "1";

function currentWsUrl() {
    return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`
        + resolveSwapTarget(sessionId).wsPath;
}

const LS_MODE = "cuv-touch-mode";          // 'touchpad' | 'direct'
const LS_KEYS_VISIBLE = "cuv-extra-keys-visible";
const LS_FN_ROW = "cuv-fn-row-visible";

const AGENT_ACTING_MS = 1500;
const MAX_RECONNECT_ATTEMPTS = 8;
const TICK_MS = 60;
const SWITCHER_POLL_MS = 4000;
const MOD_LONGPRESS_MS = 500;              // cli-agents extra-keys parity
const MOD_ORDER = ["Control", "Alt", "Super", "Shift"];

// ── DOM handles ──────────────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);
const stageEl = $("cuvStage");
const wrapEl = $("cuvCanvasWrap");
const screenEl = $("cuvScreen");
const touchEl = $("cuvTouchLayer");
const cursorEl = $("cuvCursor");
const statusDot = $("cuvStatusDot");
const sessionLabel = $("cuvSessionLabel");
const resBadge = $("cuvResBadge");
const zoomBadge = $("cuvZoomBadge");
const inputLiveBadge = $("cuvInputLive");
const agentActingEl = $("cuvAgentActing");
const endedCard = $("cuvEndedCard");
const endedTitle = $("cuvEndedTitle");
const endedMsg = $("cuvEndedMsg");
const endedSessions = $("cuvEndedSessions");
const extraKeysEl = $("cuvExtraKeys");
const kbSink = $("cuvKbSink");
const switcherEl = $("cuvSwitcher");

// ── State ────────────────────────────────────────────────────────────────

let rfb = null;
let vp = null;                 // ViewportTransform
let machine = null;            // TouchpadMachine
let session = { width: 1280, height: 720, backend: "cu", operator: "" };
let touchMode = readTouchMode();
let reconnectAttempts = 0;
let reconnectTimer = null;
let ended = false;
let agentActingTimer = null;
// Sticky modifiers: name -> 'armed' | 'locked' (absent = off).
const stickyMods = new Map();
const modButtons = new Map();  // name -> button element
// Direct-mode mouse button mask (RFB bit layout).
let mouseMask = 0;

function readTouchMode() {
    const saved = localStorage.getItem(LS_MODE);
    if (saved === "touchpad" || saved === "direct") return saved;
    return matchMedia("(pointer: coarse)").matches ? "touchpad" : "direct";
}

// ── Status chrome ────────────────────────────────────────────────────────

function setStatus(state, label) {
    statusDot.dataset.state = state;
    statusDot.title = label || state;
    inputLiveBadge.hidden = !(state === "connected" && !VIEW_ONLY);
}

function updateBadges() {
    resBadge.textContent = `${session.width}×${session.height}`;
    if (vp) zoomBadge.textContent = `${Math.round(vp.scale * 100)}%`;
}

function updateSessionLabel() {
    const watch = VIEW_ONLY ? " (watch-only)" : "";
    if (sessionId === MAIN_ID) {
        sessionLabel.textContent = `Main desktop${watch}`;
        document.title = "CU Live — Main desktop";
        return;
    }
    const shortId = sessionId.length > 14 ? `${sessionId.slice(0, 14)}…` : sessionId;
    sessionLabel.textContent = `${session.backend} · ${shortId}${watch}`;
    document.title = `CU Live — ${session.backend} ${shortId}`;
}

// ── Viewport transform application ───────────────────────────────────────

function applyTransform() {
    if (!vp) return;
    wrapEl.style.transform = `translate(${vp.tx}px, ${vp.ty}px) scale(${vp.scale})`;
    updateBadges();
    positionCursor();
}

function positionCursor() {
    if (!vp || !machine) return;
    const v = vp.displayToView(machine.cursor.x, machine.cursor.y);
    // Offset by the arrow tip's hotspot (~4,2 in the 24-unit viewBox @ 22px).
    cursorEl.style.left = `${v.x - 3.7}px`;
    cursorEl.style.top = `${v.y - 1.8}px`;
}

function setTouchMode(mode) {
    touchMode = mode;
    localStorage.setItem(LS_MODE, mode);
    touchEl.dataset.mode = mode;
    cursorEl.hidden = mode !== "touchpad";
    $("cuvModeBtn").setAttribute("aria-pressed", String(mode === "touchpad"));
}

// ── RFB input plumbing ───────────────────────────────────────────────────

function sendPointer(x, y, mask) {
    if (!rfb || ended) return;
    // _sendMouse is the 1.5.x seam (design D2 note): it gates on connection
    // state + viewOnly and forwards display-absolute coords. Our canvas has
    // internal scale 1 (scaleViewport off), so display coords pass through.
    try { rfb._sendMouse(Math.round(x), Math.round(y), mask); } catch { /* connect race */ }
}

function sendKeyEvent(keysym, down) {
    if (!rfb || ended || VIEW_ONLY || keysym == null) return;
    try { rfb.sendKey(keysym, null, down); } catch { /* connect race */ }
}

/** Send a key wrapped in the engaged sticky modifiers; consume armed ones. */
function sendKeyWithMods(keysym) {
    const mods = MOD_ORDER.filter((m) => stickyMods.has(m));
    for (const step of buildKeySequence(keysym, mods)) {
        sendKeyEvent(step.keysym, step.down);
    }
    consumeArmedMods();
}

function consumeArmedMods() {
    for (const [name, state] of [...stickyMods]) {
        if (state === "armed") {
            stickyMods.delete(name);
            renderModState(name);
        }
    }
}

function renderModState(name) {
    const btn = modButtons.get(name);
    if (!btn) return;
    const state = stickyMods.get(name);
    if (state) btn.dataset.modState = state;
    else delete btn.dataset.modState;
    btn.setAttribute("aria-pressed", state ? "true" : "false");
}

// ── Gesture layer (touch → TouchpadMachine, mouse → direct) ──────────────

function viewTouches(touchList) {
    const rect = stageEl.getBoundingClientRect();
    return Array.from(touchList, (t) => ({
        id: t.identifier, x: t.clientX - rect.left, y: t.clientY - rect.top,
    }));
}

function gestureCtx() {
    return { zoom: vp ? vp.scale : 1, zoomedIn: vp ? vp.zoomedIn : false };
}

function handleActions(actions) {
    if (!actions || actions.length === 0) return;
    let cursorMoved = false;
    let transformed = false;
    for (const a of actions) {
        if (a.kind === "pointer") {
            if (!VIEW_ONLY) sendPointer(a.x, a.y, a.mask);
            cursorMoved = true;
        } else if (a.kind === "pan") {
            vp.panBy(a.dx, a.dy);
            transformed = true;
        } else if (a.kind === "pinch") {
            vp.zoomAt(a.cx, a.cy, a.factor);
            transformed = true;
        }
    }
    if (cursorMoved && vp.zoomedIn) {
        // Splashtop edge-push: dragging the cursor against a zoomed-in edge
        // pans the viewport to follow.
        const push = vp.edgePushDelta(machine.cursor.x, machine.cursor.y);
        if (push.dx !== 0 || push.dy !== 0) {
            vp.panBy(push.dx, push.dy);
            transformed = true;
        }
    }
    if (transformed) applyTransform();
    else if (cursorMoved) positionCursor();
}

function directTouchPoint(touchList) {
    const rect = stageEl.getBoundingClientRect();
    const t = touchList[0];
    return vp.viewToDisplay(t.clientX - rect.left, t.clientY - rect.top);
}

function bindTouchLayer() {
    touchEl.addEventListener("touchstart", (ev) => {
        ev.preventDefault();
        const now = performance.now();
        if (touchMode === "touchpad") {
            handleActions(machine.touchStart(viewTouches(ev.touches), now, gestureCtx()));
        } else if (!VIEW_ONLY && ev.touches.length === 1) {
            const p = directTouchPoint(ev.touches);
            sendPointer(p.x, p.y, MASK.LEFT);
        }
    }, { passive: false });

    touchEl.addEventListener("touchmove", (ev) => {
        ev.preventDefault();
        const now = performance.now();
        if (touchMode === "touchpad") {
            handleActions(machine.touchMove(viewTouches(ev.touches), now, gestureCtx()));
        } else if (!VIEW_ONLY && ev.touches.length === 1) {
            const p = directTouchPoint(ev.touches);
            sendPointer(p.x, p.y, MASK.LEFT);
        }
    }, { passive: false });

    const onTouchEnd = (ev) => {
        ev.preventDefault();
        const now = performance.now();
        if (touchMode === "touchpad") {
            handleActions(machine.touchEnd(viewTouches(ev.touches), now, gestureCtx()));
        } else if (!VIEW_ONLY && ev.touches.length === 0 && ev.changedTouches.length) {
            const p = directTouchPoint(ev.changedTouches);
            sendPointer(p.x, p.y, MASK.NONE);
        }
    };
    touchEl.addEventListener("touchend", onTouchEnd, { passive: false });
    touchEl.addEventListener("touchcancel", (ev) => {
        ev.preventDefault();
        if (touchMode === "touchpad") handleActions(machine.touchCancel(performance.now()));
        else if (!VIEW_ONLY) sendPointer(machine.cursor.x, machine.cursor.y, MASK.NONE);
    }, { passive: false });

    // Mouse (fine pointer): always direct/absolute, mapped through the
    // viewport transform so zoom/pan never skews the click position.
    const mouseDisplayPos = (ev) => {
        const rect = stageEl.getBoundingClientRect();
        return vp.viewToDisplay(ev.clientX - rect.left, ev.clientY - rect.top);
    };
    const buttonsToMask = (buttons) =>
        (buttons & 1 ? MASK.LEFT : 0)
        | (buttons & 2 ? MASK.RIGHT : 0)
        | (buttons & 4 ? MASK.MIDDLE : 0);

    touchEl.addEventListener("mousedown", (ev) => {
        if (VIEW_ONLY) return;
        ev.preventDefault();
        mouseMask = buttonsToMask(ev.buttons);
        const p = mouseDisplayPos(ev);
        sendPointer(p.x, p.y, mouseMask);
    });
    touchEl.addEventListener("mousemove", (ev) => {
        if (VIEW_ONLY) return;
        const p = mouseDisplayPos(ev);
        sendPointer(p.x, p.y, mouseMask);
    });
    touchEl.addEventListener("mouseup", (ev) => {
        if (VIEW_ONLY) return;
        ev.preventDefault();
        mouseMask = buttonsToMask(ev.buttons);
        const p = mouseDisplayPos(ev);
        sendPointer(p.x, p.y, mouseMask);
    });
    touchEl.addEventListener("contextmenu", (ev) => ev.preventDefault());
    touchEl.addEventListener("wheel", (ev) => {
        ev.preventDefault();
        if (ev.ctrlKey) {
            // Trackpad pinch arrives as ctrl+wheel: zoom about the pointer.
            const rect = stageEl.getBoundingClientRect();
            vp.zoomAt(ev.clientX - rect.left, ev.clientY - rect.top,
                      ev.deltaY < 0 ? 1.1 : 1 / 1.1);
            applyTransform();
            return;
        }
        if (VIEW_ONLY) return;
        const p = mouseDisplayPos(ev);
        const mask = ev.deltaY < 0 ? MASK.WHEEL_UP : MASK.WHEEL_DOWN;
        sendPointer(p.x, p.y, mouseMask | mask);
        sendPointer(p.x, p.y, mouseMask);
    }, { passive: false });

    // Deferred gesture actions (tap-click release, press-and-hold commit).
    setInterval(() => handleActions(machine.tick(performance.now())), TICK_MS);
}

// ── Extra-keys bar (cli-agents-extra-keys pattern clone) ─────────────────

function makeKeyBtn(label, aria) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cuv-key-btn";
    btn.textContent = label;
    btn.title = aria || label;
    btn.setAttribute("aria-label", aria || label);
    // Focus-preservation rule (live-verified on the cli-agents bar):
    // preventDefault on pointerdown — click still fires, but the button
    // never steals focus from the keyboard sink / dismisses the IME.
    // NEVER touchstart+preventDefault (kills the click on touch).
    btn.addEventListener("pointerdown", (ev) => ev.preventDefault());
    return btn;
}

function makeModBtn(spec) {
    const btn = makeKeyBtn(spec.label, spec.aria);
    btn.title = `${spec.mod} — tap: next key only · hold: lock`;
    btn.setAttribute("aria-pressed", "false");
    let holdTimer = null;
    let longPressFired = false;
    const clearHold = () => { if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; } };
    btn.addEventListener("contextmenu", (ev) => ev.preventDefault());
    btn.addEventListener("pointerdown", () => {
        longPressFired = false;
        clearHold();
        holdTimer = setTimeout(() => {
            holdTimer = null;
            longPressFired = true;
            // Long-press: toggle locked.
            if (stickyMods.get(spec.mod) === "locked") stickyMods.delete(spec.mod);
            else stickyMods.set(spec.mod, "locked");
            renderModState(spec.mod);
        }, MOD_LONGPRESS_MS);
    });
    btn.addEventListener("pointerup", clearHold);
    btn.addEventListener("pointercancel", clearHold);
    btn.addEventListener("pointerleave", clearHold);
    btn.addEventListener("click", () => {
        if (longPressFired) { longPressFired = false; return; }
        // Tap: off → armed; armed/locked → off.
        if (stickyMods.has(spec.mod)) stickyMods.delete(spec.mod);
        else stickyMods.set(spec.mod, "armed");
        renderModState(spec.mod);
    });
    modButtons.set(spec.mod, btn);
    return btn;
}

function buildExtraKeys() {
    for (const row of EXTRA_KEY_ROWS) {
        const rowEl = document.createElement("div");
        rowEl.className = "cuv-keys-row";
        rowEl.dataset.rowId = row.id;
        rowEl.setAttribute("role", "toolbar");
        rowEl.setAttribute("aria-label", `Remote keys — ${row.id}`);

        const strip = document.createElement("div");
        strip.className = "cuv-keys-strip";

        for (const spec of row.keys) {
            let btn;
            if (spec.mod) {
                btn = makeModBtn(spec);
            } else {
                btn = makeKeyBtn(spec.label, spec.aria);
                btn.addEventListener("click", () => sendKeyWithMods(spec.keysym));
            }
            if (spec.pinned) {
                // Pinned keys live OUTSIDE the scroll strip (dead-Esc lesson).
                btn.classList.add("cuv-keys-pinned");
                rowEl.appendChild(btn);
                const divider = document.createElement("div");
                divider.className = "cuv-keys-divider";
                divider.setAttribute("aria-hidden", "true");
                rowEl.appendChild(divider);
            } else {
                strip.appendChild(btn);
            }
        }

        if (row.id === "core") {
            // Fn-row selector rides the core row's strip end.
            const fnToggle = makeKeyBtn("Fn", "Toggle F1-F12 row");
            fnToggle.setAttribute("aria-pressed", "false");
            fnToggle.addEventListener("click", () => {
                const fnRow = extraKeysEl.querySelector('[data-row-id="fn"]');
                const show = fnRow.hidden;
                fnRow.hidden = !show;
                fnToggle.setAttribute("aria-pressed", String(show));
                localStorage.setItem(LS_FN_ROW, show ? "1" : "0");
            });
            strip.appendChild(fnToggle);
        }

        rowEl.appendChild(strip);
        if (row.collapsible) rowEl.hidden = localStorage.getItem(LS_FN_ROW) !== "1";
        extraKeysEl.appendChild(rowEl);
    }
    const fnRow = extraKeysEl.querySelector('[data-row-id="fn"]');
    if (fnRow && !fnRow.hidden) {
        const fnToggle = [...extraKeysEl.querySelectorAll("button")]
            .find((b) => b.textContent === "Fn");
        if (fnToggle) fnToggle.setAttribute("aria-pressed", "true");
    }
}

function setExtraKeysVisible(visible) {
    extraKeysEl.hidden = !visible;
    $("cuvKeysBtn").setAttribute("aria-pressed", String(visible));
    localStorage.setItem(LS_KEYS_VISIBLE, visible ? "1" : "0");
}

// ── Manual keyboard (§4.4 — NO auto-open, ever) ──────────────────────────

// Prime the sink so deleteContentBackward fires even on IMEs that suppress
// keydown (value is one space; never rendered — the input is invisible).
function primeKbSink() { kbSink.value = " "; }

function bindKeyboard() {
    const kbBtn = $("cuvKbBtn");
    kbBtn.addEventListener("click", () => {
        if (document.activeElement === kbSink) {
            kbSink.blur();
        } else {
            primeKbSink();
            kbSink.focus(); // the ONLY focus call in this file
        }
    });
    kbSink.addEventListener("focus", () => kbBtn.setAttribute("aria-pressed", "true"));
    kbSink.addEventListener("blur", () => kbBtn.setAttribute("aria-pressed", "false"));

    let composing = false;
    kbSink.addEventListener("compositionstart", () => { composing = true; });
    kbSink.addEventListener("compositionend", (ev) => {
        composing = false;
        for (const ch of ev.data || "") sendKeyWithMods(keysymForChar(ch));
        primeKbSink();
    });
    kbSink.addEventListener("input", (ev) => {
        if (composing) return;
        if (ev.inputType === "insertText" && ev.data) {
            for (const ch of ev.data) sendKeyWithMods(keysymForChar(ch));
        } else if (ev.inputType === "deleteContentBackward") {
            sendKeyWithMods(KEYSYMS.BackSpace);
        } else if (ev.inputType === "insertLineBreak") {
            sendKeyWithMods(KEYSYMS.Return);
        }
        primeKbSink();
    });
    kbSink.addEventListener("keydown", (ev) => {
        // Special keys the input event can't express. Printable chars flow
        // through the input event instead (IME-safe).
        const sym = specialKeysym(ev.key);
        if (sym != null) {
            ev.preventDefault();
            sendKeyWithMods(sym);
            primeKbSink();
        }
    });

    // Physical keyboard (desktop): forward when the sink is NOT focused —
    // the stage is the implicit focus target. Modifier keys pass through as
    // real down/up so native chords (Ctrl+C…) work alongside sticky mods.
    window.addEventListener("keydown", (ev) => {
        if (document.activeElement === kbSink || ended || VIEW_ONLY) return;
        const handled = forwardPhysicalKey(ev, true);
        if (handled) ev.preventDefault();
    });
    window.addEventListener("keyup", (ev) => {
        if (document.activeElement === kbSink || ended || VIEW_ONLY) return;
        if (forwardPhysicalKey(ev, false)) ev.preventDefault();
    });
}

const SPECIAL_KEYSYMS = {
    Enter: KEYSYMS.Return, Backspace: KEYSYMS.BackSpace, Tab: KEYSYMS.Tab,
    Escape: KEYSYMS.Escape, Delete: KEYSYMS.Delete, Insert: KEYSYMS.Insert,
    ArrowUp: KEYSYMS.Up, ArrowDown: KEYSYMS.Down,
    ArrowLeft: KEYSYMS.Left, ArrowRight: KEYSYMS.Right,
    Home: KEYSYMS.Home, End: KEYSYMS.End,
    PageUp: KEYSYMS.PageUp, PageDown: KEYSYMS.PageDown,
    Control: MODIFIER_KEYSYMS.Control, Shift: MODIFIER_KEYSYMS.Shift,
    Alt: MODIFIER_KEYSYMS.Alt, Meta: MODIFIER_KEYSYMS.Super,
    F1: KEYSYMS.F1, F2: KEYSYMS.F2, F3: KEYSYMS.F3, F4: KEYSYMS.F4,
    F5: KEYSYMS.F5, F6: KEYSYMS.F6, F7: KEYSYMS.F7, F8: KEYSYMS.F8,
    F9: KEYSYMS.F9, F10: KEYSYMS.F10, F11: KEYSYMS.F11, F12: KEYSYMS.F12,
};

function specialKeysym(key) {
    return Object.prototype.hasOwnProperty.call(SPECIAL_KEYSYMS, key)
        ? SPECIAL_KEYSYMS[key] : null;
}

function forwardPhysicalKey(ev, down) {
    const special = specialKeysym(ev.key);
    if (special != null) {
        sendKeyEvent(special, down);
        return true;
    }
    if (ev.key.length === 1) {
        const sym = keysymForChar(ev.key);
        if (sym != null) {
            sendKeyEvent(sym, down);
            return true;
        }
    }
    return false;
}

// ── Agent-acting soft guard (§5) ─────────────────────────────────────────

function bindAgentActing() {
    // The Portal (or any embedder) forwards chat-SSE cu_action / heartbeat
    // events via postMessage. With no embedder (Android WebView direct URL),
    // nothing arrives and nothing dims — honest degradation.
    window.addEventListener("message", (ev) => {
        const d = ev.data;
        if (!d || typeof d !== "object") return;
        if (d.type !== "cu_action" && d.type !== "heartbeat"
            && d.type !== "cu-agent-acting") return;
        if (d.session_id && d.session_id !== sessionId) return;
        agentActingEl.classList.add("cuv-active");
        if (agentActingTimer) clearTimeout(agentActingTimer);
        agentActingTimer = setTimeout(
            () => agentActingEl.classList.remove("cuv-active"), AGENT_ACTING_MS);
    });
}

// ── Connection lifecycle ─────────────────────────────────────────────────

// Latest /cu/sessions payload (sessions + additive "main" availability key).
// Fed by boot, the switcher poll, and reconnect probes; the switcher rail
// and swap-target metadata read from it.
let lastStatus = null;

async function fetchStatus() {
    try {
        const resp = await fetch("/cu/sessions", { cache: "no-store" });
        if (!resp.ok) return null;
        return await resp.json();
    } catch {
        return null;
    }
}

function connect() {
    endedCard.hidden = true;
    ended = false;
    setStatus(reconnectAttempts > 0 ? "reconnecting" : "connecting");
    if (rfb) {
        const old = rfb;
        rfb = null; // detach FIRST so old's disconnect event is a no-op
        try { old.disconnect(); } catch { /* already down */ }
    }
    screenEl.textContent = "";

    const inst = new RFB(screenEl, currentWsUrl(), {});
    rfb = inst;
    inst.viewOnly = VIEW_ONLY;
    inst.scaleViewport = false;  // OUR transform is the only scaling (§4.2)
    inst.clipViewport = false;
    inst.resizeSession = false;  // NEVER resize the agent's screen (D6)
    inst.showDotCursor = false;
    inst.background = "#0b0b0d";

    inst.addEventListener("connect", () => {
        if (rfb !== inst) return;
        reconnectAttempts = 0;
        setStatus("connected");
        diag("rfb-connect", sessionId);
    });
    inst.addEventListener("desktopname", (ev) => {
        if (rfb === inst && ev.detail?.name) statusDot.title = ev.detail.name;
    });
    inst.addEventListener("disconnect", () => {
        // Stale-instance guard: a superseded RFB's disconnect must never
        // clobber the live one or trigger a reconnect storm.
        if (rfb !== inst) return;
        diag("rfb-disconnect", `${sessionId} ended=${ended}`);
        rfb = null;
        if (ended) return;
        scheduleReconnect();
    });
}

async function scheduleReconnect() {
    if (reconnectTimer) return;
    setStatus("reconnecting");
    const status = await fetchStatus();
    if (status) {
        lastStatus = status;
        renderSwitcher();
    }
    // Null status (fetch failed — restart race?) always retries; main gates
    // on the probe's availability, sessions on still being listed.
    if (!targetStillListed(status, sessionId)) {
        if (sessionId === MAIN_ID) {
            showEnded("Main desktop unavailable",
                      String(status?.main?.reason || "log into the desktop session"),
                      status ? status.sessions : null);
        } else {
            showEnded("Session ended", "This CU session is no longer running.",
                      status ? status.sessions : null);
        }
        return;
    }
    if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
        showEnded("Connection lost",
                  "The session is still listed but the stream would not reconnect.",
                  status ? status.sessions : null);
        return;
    }
    const delay = Math.min(1000 * 2 ** reconnectAttempts, 10000);
    reconnectAttempts += 1;
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
    }, delay);
}

function showEnded(title, msg, sessions) {
    ended = true;
    setStatus("ended", title);
    endedTitle.textContent = title;
    endedMsg.textContent = msg;
    endedSessions.textContent = "";
    for (const s of sessions || []) {
        if (s.session_id === sessionId || !s.live_view) continue;
        const a = document.createElement("a");
        a.href = s.view_url || `/cu/view/${encodeURIComponent(s.session_id)}`;
        a.textContent = `Open ${s.backend} · ${s.width}×${s.height} (${s.operator})`;
        endedSessions.appendChild(a);
    }
    endedCard.hidden = false;
}

// ── Switcher rail (N2 — main-desktop switcher) ───────────────────────────

// Entry-list building / swap-target resolution / metadata lookup are PURE
// (switcher.js, node-tested); this block is the thin impure renderer + the
// in-place stream swap.

let switcherSig = "";  // skip DOM rebuilds (and mid-tap button churn) when unchanged

function renderSwitcher() {
    if (!switcherEl) return;
    const entries = buildSwitcherEntries(lastStatus, sessionId);
    const sig = JSON.stringify(entries);
    if (sig === switcherSig) return;
    switcherSig = sig;
    switcherEl.textContent = "";
    for (const e of entries) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "cuv-switch-btn";
        btn.textContent = e.label;
        btn.setAttribute("role", "tab");
        btn.setAttribute("aria-selected", String(e.current));
        if (e.current) btn.dataset.current = "1";
        if (!e.available) {
            // Disabled with the reason as the tooltip (e.g. main.available=false
            // → "log into the desktop session").
            btn.disabled = true;
            btn.title = e.reason;
        } else {
            btn.title = e.current ? "Currently streaming" : `Switch to ${e.label}`;
            btn.addEventListener("click", () => swapStream(e.id));
        }
        // Same focus-preservation rule as the extra-keys bar: never steal
        // focus from the keyboard sink on tap.
        btn.addEventListener("pointerdown", (ev) => ev.preventDefault());
        switcherEl.appendChild(btn);
    }
}

/** Size the native-res screen target + rebuild the viewport transform and
 *  touchpad machine for the CURRENT session geometry. Shared by boot and
 *  swapStream — a swap resets zoom/pan while the cursor layer (overlay +
 *  gesture machinery) stays alive. */
function applySessionGeometry() {
    screenEl.style.width = `${session.width}px`;
    screenEl.style.height = `${session.height}px`;
    wrapEl.style.width = `${session.width}px`;
    wrapEl.style.height = `${session.height}px`;

    const rect = stageEl.getBoundingClientRect();
    vp = new ViewportTransform({
        viewW: Math.max(1, rect.width), viewH: Math.max(1, rect.height),
        dispW: session.width, dispH: session.height,
    });
    machine = new TouchpadMachine({ width: session.width, height: session.height });

    updateSessionLabel();
    updateBadges();
    applyTransform();
}

/** Swap the stream in place to another rail entry: disconnect the current
 *  RFB, reconnect to the chosen target's WS proxy, rewrite the URL via
 *  history.replaceState (deep links keep working), reset zoom/pan. */
function swapStream(targetId) {
    if (targetId === sessionId) return;
    sessionId = targetId;
    const target = resolveSwapTarget(targetId);
    history.replaceState(null, "", target.viewPath + location.search);

    const meta = sessionMetaFor(lastStatus, targetId);
    if (meta) session = meta;  // unknown id: keep previous dims until listed
    applySessionGeometry();

    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
    reconnectAttempts = 0;
    ended = false;
    connect();  // detaches + disconnects the superseded RFB first
    renderSwitcher();
}

function startSwitcherPoll() {
    setInterval(async () => {
        const status = await fetchStatus();
        if (status) {
            lastStatus = status;
            renderSwitcher();
        }
    }, SWITCHER_POLL_MS);
}

// ── Boot ─────────────────────────────────────────────────────────────────

async function boot() {
    // Session metadata (native resolution, backend) — /cu/sessions is the
    // sizing source of truth (§3.4; the "main" key carries the real desktop's
    // probed resolution). Fallback keeps the viewer alive if the fetch races
    // a restart.
    lastStatus = await fetchStatus();
    const meta = sessionMetaFor(lastStatus, sessionId);
    if (meta) {
        session = meta;
    } else {
        console.warn("[CU-VIEW] session metadata unavailable — assuming 1280x720");
    }

    // Native-res screen target; our wrapper transform does all scaling.
    applySessionGeometry();
    setTouchMode(touchMode);

    new ResizeObserver(() => {
        const r = stageEl.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) {
            vp.resize(r.width, r.height);
            applyTransform();
        }
    }).observe(stageEl);

    // Toolbar.
    $("cuvFitBtn").addEventListener("click", () => { vp.reset(); applyTransform(); });
    $("cuvModeBtn").addEventListener("click", () => {
        setTouchMode(touchMode === "touchpad" ? "direct" : "touchpad");
    });
    $("cuvKeysBtn").addEventListener("click", () => setExtraKeysVisible(extraKeysEl.hidden));
    $("cuvRetryBtn").addEventListener("click", () => {
        reconnectAttempts = 0;
        connect();
    });

    buildExtraKeys();
    // Default: visible on coarse pointers, else hidden; persisted choice wins.
    const savedKeys = localStorage.getItem(LS_KEYS_VISIBLE);
    setExtraKeysVisible(savedKeys === null
        ? matchMedia("(pointer: coarse)").matches
        : savedKeys === "1");

    bindTouchLayer();
    bindKeyboard();
    bindAgentActing();
    renderSwitcher();
    startSwitcherPoll();
    connect();
}

boot();
