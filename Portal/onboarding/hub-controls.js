// Shared operational controls — extracted from steps/done.js so BOTH the
// done screen (final wizard step) and the console hub (?mode=manage) reuse
// ONE battle-tested implementation. Frontend-only; no backend change.
//
// Three exports, parameterized by element refs (NOT fixed ids) so each
// surface passes its own DOM:
//   - openLogsModal()                  — live SSE journalctl modal (self-contained)
//   - mountRestartControl(btn, statusEl) — E9 status-aware Restart state machine
//   - mountUpdatesBadge(hostEl)        — T9 update-available badge
//
// completeAndOpen (Open Portal POST /complete) stays in done.js — it's
// done-specific (the hub is post-completion, a plain /ui link suffices).

// One restart at a time across the whole page — module-level is correct here
// (the spec calls this out). A second concurrent restart would race the same
// systemd service anyway.
let restartBusy = false;

// ── E10: View Logs modal — live SSE journalctl stream ───────────────────
// Opens a full-screen modal with a dark console-style log area that
// consumes /onboarding/logs/stream via EventSource. Auto-scrolls to
// bottom on each new line UNLESS the user has scrolled up (they're
// inspecting history — don't yank them back). Close via X / Esc /
// click-outside closes the EventSource, which triggers the backend's
// CancelledError handler killing the journalctl process — no orphan
// tail subprocesses. Self-contained: only one #ob-logs-modal exists at a
// time (a second call just reveals the existing one), so the fixed id is safe.
export function openLogsModal() {
    // Create modal if not present
    let modal = document.getElementById("ob-logs-modal");
    if (modal) {
        modal.hidden = false;
        return;  // already open; just reveal
    }
    modal = document.createElement("div");
    modal.id = "ob-logs-modal";
    modal.className = "ob-logs-modal";
    modal.innerHTML = `
        <div class="ob-logs-modal-backdrop"></div>
        <div class="ob-logs-modal-panel" role="dialog" aria-label="BlackBox service logs">
            <div class="ob-logs-modal-header">
                <span class="ob-logs-modal-title">BlackBox Service Logs</span>
                <button type="button" class="ob-logs-modal-close" aria-label="Close">&times;</button>
            </div>
            <pre id="ob-logs-modal-body" class="ob-logs-modal-body"></pre>
            <div class="ob-logs-modal-footer">
                <span id="ob-logs-modal-status" class="ob-logs-status ob-logs-status-connecting">Connecting&hellip;</span>
                <span id="ob-logs-modal-count" class="ob-logs-count">0 lines</span>
                <button type="button" id="ob-logs-modal-copy" class="ob-logs-copy">Copy all</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    const body = modal.querySelector("#ob-logs-modal-body");
    const status = modal.querySelector("#ob-logs-modal-status");
    const count = modal.querySelector("#ob-logs-modal-count");
    const copy = modal.querySelector("#ob-logs-modal-copy");
    const closeBtn = modal.querySelector(".ob-logs-modal-close");
    const backdrop = modal.querySelector(".ob-logs-modal-backdrop");

    let lineCount = 0;
    let autoScroll = true;

    // Detect user-scrolled-up (don't auto-yank if they're reading history)
    body.addEventListener("scroll", () => {
        const distFromBottom = body.scrollHeight - body.scrollTop - body.clientHeight;
        autoScroll = distFromBottom < 50;  // within 50px of bottom = stay pinned
    });

    // SSE connection
    const eventSource = new EventSource("/onboarding/logs/stream?lines=200");
    eventSource.addEventListener("start", () => {
        status.textContent = "Connected";
        status.className = "ob-logs-status ob-logs-status-connected";
    });
    eventSource.onmessage = (e) => {
        body.textContent += e.data + "\n";
        lineCount++;
        count.textContent = lineCount + " lines";
        if (autoScroll) {
            body.scrollTop = body.scrollHeight;
        }
    };
    eventSource.onerror = () => {
        status.textContent = "Disconnected";
        status.className = "ob-logs-status ob-logs-status-disconnected";
    };

    function closeModal() {
        eventSource.close();
        modal.remove();
        document.removeEventListener("keydown", escHandler);
    }

    closeBtn.addEventListener("click", closeModal);
    backdrop.addEventListener("click", closeModal);
    const escHandler = (e) => { if (e.key === "Escape") closeModal(); };
    document.addEventListener("keydown", escHandler);

    copy.addEventListener("click", async () => {
        try {
            await navigator.clipboard.writeText(body.textContent);
            copy.textContent = "Copied ✓";
            setTimeout(() => { copy.textContent = "Copy all"; }, 1500);
        } catch {
            copy.textContent = "Copy failed";
        }
    });
}

// ── E9: status-aware Restart Service control ────────────────────────────
// Three states:
//   A — up to date: passive "Service up to date ✓" text, button still clickable
//   B — needs restart: visible amber button + helper text
//   C — restarting: disabled spinner button, polling /health
//
// On click: POST /onboarding/restart (fire-and-forget — the restart will
// SIGTERM the service mid-response). Wait 5s, then poll /health every 2s
// for up to 120s. When it returns 200, poll /restart-status until drift
// clears, show "Restarted ✓" briefly, then fade back to State A.
//
// Parameterized by btn + statusEl so done (#ob-done-restart) and hub
// (#ob-hub-restart) each mount against their own elements.
export async function mountRestartControl(btn, statusEl) {
    if (!btn || !statusEl) return;

    try {
        const r = await fetch("/onboarding/restart-status");
        if (!r.ok) {
            // Endpoint missing or errored — silently hide the button.
            // Don't block the customer from clicking Open Portal.
            return;
        }
        const data = await r.json();
        renderRestartState(btn, statusEl, data);
    } catch (e) {
        // Network error — silently skip. Wizard finalize still works.
        console.warn("restart-status probe failed:", e);
    }
}

function renderRestartState(btn, statusEl, data) {
    // E11 followup (Brandon 2026-05-17): button is ALWAYS visible + clickable.
    // Customer should be able to restart anytime, not only when drift is detected
    // (covers cases where they edit .env directly, or just want a fresh boot for
    // sanity). Status text differentiates the two cases — actionable warn vs
    // passive 'up to date' — but the button itself is always pressable.
    btn.hidden = false;
    btn.disabled = false;
    btn.classList.remove("ob-cta-restart-done");
    const label = btn.querySelector(".ob-cta-restart-label");
    if (label) label.textContent = "Restart Service";
    statusEl.hidden = false;

    if (data && data.needs_restart) {
        // State B: actionable — amber warn styling on both button + status
        btn.classList.remove("ob-cta-restart-passive");
        btn.classList.add("ob-cta-restart-warn");
        statusEl.classList.remove("ob-restart-status-passive", "ob-restart-status-done");
        statusEl.classList.add("ob-restart-status-warn");
        // Compose human-readable reason from the drift list when available
        const drifted = (data.drifted_keys || []).length;
        statusEl.textContent = drifted > 0
            ? `${drifted} setting${drifted === 1 ? "" : "s"} changed — restart so they take effect`
            : "Settings changed — restart so they take effect";
    } else {
        // State A: passive — neutral styling, but button still clickable
        btn.classList.remove("ob-cta-restart-warn");
        btn.classList.add("ob-cta-restart-passive");
        statusEl.classList.remove("ob-restart-status-warn", "ob-restart-status-done");
        statusEl.classList.add("ob-restart-status-passive");
        statusEl.innerHTML = "Service up to date &check;";
    }

    // Wire (idempotent — replace any prior handler). Done in both states
    // because the button is now always clickable.
    btn.onclick = () => doRestart(btn, statusEl);
}

async function doRestart(btn, statusEl) {
    if (restartBusy) return;
    restartBusy = true;

    // E9 followup (Brandon's MSO2 Ultra report 2026-05-17): customer clicked
    // button, nothing happened. Journal confirmed /onboarding/restart was
    // never hit. Suspected root cause: if any State-C UI mutation threw
    // BEFORE the fetch (e.g., btn.querySelector returns null in some render
    // state, .textContent on null throws TypeError), the function would
    // exit early WITHOUT entering try/finally, leaving restartBusy=true
    // permanently. All subsequent clicks then no-op silently. Defensive fix:
    // wrap ALL state mutations inside the try block so finally always runs.

    try {
        // State C: restarting (mutations inside try so finally always runs)
        btn.disabled = true;
        const label = btn.querySelector(".ob-cta-restart-label");
        if (label) label.textContent = "Restarting service…";
        statusEl.classList.remove("ob-restart-status-warn", "ob-restart-status-done");
        statusEl.classList.add("ob-restart-status-passive");
        statusEl.textContent = "This takes about 60 to 90 seconds. The page will reconnect automatically.";

        // Fire-and-forget — the response may not arrive (server SIGTERMs mid-flight)
        try {
            await fetch("/onboarding/restart", { method: "POST" });
        } catch (e) {
            // Expected: server disconnects before responding. Continue with health poll.
            console.log("restart POST disconnected (expected):", e.message);
        }

        // Wait 5s for service to start shutting down
        await sleep(5000);

        // Poll /health every 2s for up to 120s
        const healthy = await pollHealth(120_000, 2_000);
        if (!healthy) {
            throw new Error("Service did not come back within 120 seconds");
        }

        // Confirm drift cleared via /restart-status
        const clearedDrift = await pollRestartCleared(15_000, 1_500);

        // State "done": show "Restarted ✓" briefly, then fade to State A
        const doneLabel = btn.querySelector(".ob-cta-restart-label");
        if (doneLabel) doneLabel.textContent = "Restarted";
        btn.classList.add("ob-cta-restart-done");
        statusEl.classList.remove("ob-restart-status-warn", "ob-restart-status-passive");
        statusEl.classList.add("ob-restart-status-done");
        statusEl.innerHTML = clearedDrift
            ? "Service restarted &check;"
            : "Service is back online &check;";

        await sleep(3000);
        // Re-probe and render whatever state we're in now (typically State A)
        const r = await fetch("/onboarding/restart-status");
        if (r.ok) {
            const data = await r.json();
            renderRestartState(btn, statusEl, data);
        } else {
            // Fall back to passive
            renderRestartState(btn, statusEl, { needs_restart: false });
        }
    } catch (e) {
        // Surface error inline. Customer can still click Open Portal — chat just won't pick up new keys.
        btn.disabled = false;
        const retryLabel = btn.querySelector(".ob-cta-restart-label");
        if (retryLabel) retryLabel.textContent = "Retry Restart";
        statusEl.classList.remove("ob-restart-status-passive", "ob-restart-status-done");
        statusEl.classList.add("ob-restart-status-warn");
        statusEl.textContent = `Restart didn't complete: ${e.message}. Try again or open Portal anyway.`;
    } finally {
        restartBusy = false;
    }
}

async function pollHealth(timeoutMs, intervalMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        try {
            const r = await fetch("/health", { cache: "no-store" });
            if (r.ok) return true;
        } catch (e) {
            // Service still down — keep polling
        }
        await sleep(intervalMs);
    }
    return false;
}

async function pollRestartCleared(timeoutMs, intervalMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        try {
            const r = await fetch("/onboarding/restart-status", { cache: "no-store" });
            if (r.ok) {
                const data = await r.json();
                if (!data.needs_restart) return true;
            }
        } catch (e) {
            // Try again
        }
        await sleep(intervalMs);
    }
    return false;
}

// ── T9: update-available badge ─────────────────────────────────────────
// Fetches /update/status (cached 60s server-side). Renders nothing if:
//   - git not initialized (fresh install case — no update mechanism yet)
//   - commits_behind === 0 (already current)
//   - status fetch fails (no point alarming the user during onboarding)
// Renders the "N updates available" banner with Install/Skip buttons if
// 1-10 commits behind, or "Major update available — install later from
// System Menu" if >10 (audit I3 — avoid alarming first-run customers).
//
// Parameterized by hostEl (the container) — inner button lookups are scoped
// to hostEl.querySelector(...) so the done/hub instances never collide on ids.
export async function mountUpdatesBadge(hostEl) {
    if (!hostEl) return;
    let status;
    try {
        const r = await fetch("/update/status");
        if (!r.ok) return;
        status = await r.json();
    } catch (_) {
        return;
    }
    if (!status.git_initialized) return;
    if (!status.commits_behind) return;

    hostEl.hidden = false;

    const n = status.commits_behind;
    const isMajor = n > 10;
    hostEl.innerHTML = isMajor
        ? `
            <div class="ob-update-banner ob-update-banner-major">
                <strong>Major update available</strong>
                <p>${n} commits behind. Recommended to install from System Menu after onboarding completes — gives you time to test it.</p>
            </div>
        `
        : `
            <div class="ob-update-banner">
                <strong>⬆ ${n} update${n === 1 ? "" : "s"} available since this install</strong>
                <p>Apply now (restarts service, ~60–90s) or skip and use System Menu later.</p>
                <div class="ob-update-banner-actions">
                    <button type="button" class="ob-cta-secondary" data-ob-update-install>Install Now</button>
                    <button type="button" class="ob-link-button" data-ob-update-skip>Skip — install later</button>
                </div>
            </div>
        `;

    if (!isMajor) {
        hostEl.querySelector("[data-ob-update-install]").addEventListener("click", async () => {
            try {
                const r = await fetch("/update/start", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ confirm_sha: status.latest_sha }),
                });
                if (!r.ok) {
                    const t = await r.text();
                    window.alert("Couldn't start update: " + t.substring(0, 200));
                    return;
                }
                const { task_id } = await r.json();
                const mod = await import("/ui/modules/updates-manager.js");
                mod.openUpdateLogModal(task_id);
            } catch (e) {
                window.alert("Network error: " + e.message);
            }
        });
        hostEl.querySelector("[data-ob-update-skip]").addEventListener("click", () => {
            hostEl.hidden = true;
        });
    }
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}
