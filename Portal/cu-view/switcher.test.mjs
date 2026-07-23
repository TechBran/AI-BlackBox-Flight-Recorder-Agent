// Unit tests for the CU live-view switcher rail state (N2 — main-desktop
// switcher). switcher.js is a PURE module — no DOM, no network — driven here
// with a fake /cu/sessions payload (sessions + additive "main" key).
//
// Run: node --test Portal/cu-view/switcher.test.mjs
import test from "node:test";
import assert from "node:assert/strict";

import {
    MAIN_ID, buildSwitcherEntries, resolveSwapTarget, sessionMetaFor,
    targetStillListed, parseResolution,
} from "./switcher.js";

// A representative /cu/sessions payload: two live sessions (one without a
// working live-view pipeline) + an available main desktop.
const PAYLOAD = {
    active: true, count: 2, cap: 3,
    sessions: [
        { session_id: "cu-virt-abcdef123456", operator: "Brandon",
          backend: "anthropic", width: 1280, height: 720,
          live_view: true, view_url: "/cu/view/cu-virt-abcdef123456" },
        { session_id: "s2", operator: "system", backend: "google",
          width: 1440, height: 900, live_view: false, view_url: "/cu/view/s2" },
    ],
    main: { available: true, display: ":0", resolution: "1920x1080" },
};

const PAYLOAD_MAIN_DOWN = {
    active: false, count: 0, cap: 3, sessions: [],
    main: { available: false, reason: "log into the desktop session" },
};

// ── entry list building ──────────────────────────────────────────────────

test("entries: [Main desktop] first, then every listed session", () => {
    const entries = buildSwitcherEntries(PAYLOAD, "cu-virt-abcdef123456");
    assert.equal(entries.length, 3);
    assert.equal(entries[0].id, MAIN_ID);
    assert.equal(entries[0].kind, "main");
    assert.equal(entries[1].id, "cu-virt-abcdef123456");
    assert.equal(entries[2].id, "s2");
});

test("entries: session label carries backend + short id + resolution", () => {
    const entries = buildSwitcherEntries(PAYLOAD, MAIN_ID);
    const s1 = entries[1];
    assert.ok(s1.label.includes("anthropic"), s1.label);
    assert.ok(s1.label.includes("1280×720"), s1.label);
    // Short id, not the full slug — the rail is compact.
    assert.ok(!s1.label.includes("cu-virt-abcdef123456"), s1.label);
    assert.ok(s1.label.includes("cu-virt-a"), s1.label);
});

test("entries: main label carries the real desktop resolution", () => {
    const entries = buildSwitcherEntries(PAYLOAD, MAIN_ID);
    assert.ok(entries[0].label.startsWith("Main desktop"), entries[0].label);
    assert.ok(entries[0].label.includes("1920x1080"), entries[0].label);
});

test("entries: a session without live_view is listed but not available", () => {
    const entries = buildSwitcherEntries(PAYLOAD, MAIN_ID);
    const s2 = entries.find((e) => e.id === "s2");
    assert.equal(s2.available, false);
    assert.ok(s2.reason.length > 0);
});

test("entries: null payload (fetch failure) still yields a disabled main row", () => {
    const entries = buildSwitcherEntries(null, "s1");
    assert.equal(entries.length, 1);
    assert.equal(entries[0].id, MAIN_ID);
    assert.equal(entries[0].available, false);
});

// ── current highlight ────────────────────────────────────────────────────

test("current: exactly the current target is highlighted", () => {
    const entries = buildSwitcherEntries(PAYLOAD, "cu-virt-abcdef123456");
    assert.deepEqual(entries.map((e) => e.current), [false, true, false]);
});

test("current: the main entry highlights when viewing /cu/view/main", () => {
    const entries = buildSwitcherEntries(PAYLOAD, MAIN_ID);
    assert.deepEqual(entries.map((e) => e.current), [true, false, false]);
});

// ── swap-target resolution ───────────────────────────────────────────────

test("swap target: session id maps to its view + ws proxy paths", () => {
    const t = resolveSwapTarget("s2");
    assert.equal(t.viewPath, "/cu/view/s2");
    assert.equal(t.wsPath, "/cu/view/s2/ws");
});

test("swap target: main maps to the reserved main stream paths", () => {
    const t = resolveSwapTarget(MAIN_ID);
    assert.equal(t.viewPath, "/cu/view/main");
    assert.equal(t.wsPath, "/cu/view/main/ws");
});

test("swap target: session ids are URL-encoded", () => {
    const t = resolveSwapTarget("weird id/1");
    assert.equal(t.wsPath, "/cu/view/weird%20id%2F1/ws");
});

// ── unavailable-main handling ────────────────────────────────────────────

test("main unavailable: disabled with the probe's reason as tooltip", () => {
    const entries = buildSwitcherEntries(PAYLOAD_MAIN_DOWN, "s1");
    assert.equal(entries[0].available, false);
    assert.equal(entries[0].reason, "log into the desktop session");
});

test("main unavailable: payload missing the main key entirely", () => {
    const entries = buildSwitcherEntries({ sessions: [] }, "s1");
    assert.equal(entries[0].available, false);
    assert.ok(entries[0].reason.length > 0);
});

// ── session metadata (sizing source of truth for a swap) ─────────────────

test("meta: session target uses its listed native resolution", () => {
    const meta = sessionMetaFor(PAYLOAD, "s2");
    assert.deepEqual(meta, { width: 1440, height: 900,
                             backend: "google", operator: "system" });
});

test("meta: main target parses the probe resolution", () => {
    const meta = sessionMetaFor(PAYLOAD, MAIN_ID);
    assert.equal(meta.width, 1920);
    assert.equal(meta.height, 1080);
    assert.equal(meta.backend, "main");
});

test("meta: main without a resolution falls back to 1280x720", () => {
    const meta = sessionMetaFor(PAYLOAD_MAIN_DOWN, MAIN_ID);
    assert.deepEqual({ w: meta.width, h: meta.height }, { w: 1280, h: 720 });
});

test("meta: unknown session id yields null (caller keeps previous dims)", () => {
    assert.equal(sessionMetaFor(PAYLOAD, "nope"), null);
});

test("parseResolution: WxH strings and garbage", () => {
    assert.deepEqual(parseResolution("2560x1440"), { width: 2560, height: 1440 });
    assert.equal(parseResolution("huge"), null);
    assert.equal(parseResolution(null), null);
});

// ── reconnect gate ───────────────────────────────────────────────────────

test("still listed: session present / absent; main by availability", () => {
    assert.equal(targetStillListed(PAYLOAD, "s2"), true);
    assert.equal(targetStillListed(PAYLOAD, "gone"), false);
    assert.equal(targetStillListed(PAYLOAD, MAIN_ID), true);
    assert.equal(targetStillListed(PAYLOAD_MAIN_DOWN, MAIN_ID), false);
});

test("still listed: null payload (fetch failed) never declares ended", () => {
    assert.equal(targetStillListed(null, "s2"), true);
    assert.equal(targetStillListed(null, MAIN_ID), true);
});
