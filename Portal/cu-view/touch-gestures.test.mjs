// Unit tests for the CU live-view touchpad gesture state machine + viewport
// transform (design doc 2026-07-23 §4.1/§4.2, milestone M2). Both are PURE
// modules — no DOM, no network — driven here with synthetic touch events.
//
// Run: node --test Portal/cu-view/touch-gestures.test.mjs
//  (or: node Portal/cu-view/touch-gestures.test.mjs)
import test from "node:test";
import assert from "node:assert/strict";

import {
    TouchpadMachine, ViewportTransform, MASK, DEFAULTS,
} from "./touch-gestures.js";

const CTX1 = { zoom: 1, zoomedIn: false };

function machine(overrides = {}) {
    return new TouchpadMachine({ width: 1280, height: 720, ...overrides });
}

function pointerActions(actions) {
    return actions.filter((a) => a.kind === "pointer");
}

// ── tap vs drag discrimination ───────────────────────────────────────────

test("tap: deferred click fires at CURSOR position after the chain window", () => {
    const m = machine();
    // Cursor starts at display center.
    assert.deepEqual(m.cursor, { x: 640, y: 360 });
    let out = m.touchStart([{ id: 0, x: 100, y: 100 }], 1000, CTX1);
    out = out.concat(m.touchEnd([], 1080, CTX1));
    // Nothing yet — the click is deferred so tap-then-drag can claim it.
    assert.equal(pointerActions(out).length, 0);
    // Inside the chain window: still nothing.
    assert.equal(pointerActions(m.tick(1080 + DEFAULTS.dragChainMs)).length, 0);
    // Past the window: down+up at the CURSOR (640,360), NOT the touch point.
    const clicks = pointerActions(m.tick(1080 + DEFAULTS.dragChainMs + 1));
    assert.deepEqual(clicks, [
        { kind: "pointer", x: 640, y: 360, mask: MASK.LEFT },
        { kind: "pointer", x: 640, y: 360, mask: MASK.NONE },
    ]);
    // One-shot.
    assert.equal(m.tick(2000).length, 0);
});

test("tap: sub-slop jitter still counts as a tap", () => {
    const m = machine();
    m.touchStart([{ id: 0, x: 100, y: 100 }], 0, CTX1);
    m.touchMove([{ id: 0, x: 104, y: 103 }], 30, CTX1); // < tapSlopPx
    m.touchEnd([], 80, CTX1);
    const clicks = pointerActions(m.tick(80 + DEFAULTS.dragChainMs + 1));
    assert.equal(clicks.length, 2);
    assert.equal(clicks[0].mask, MASK.LEFT);
});

test("slow press-and-release without movement is NOT a tap", () => {
    const m = machine({ tapMaxMs: 250 });
    m.touchStart([{ id: 0, x: 100, y: 100 }], 0, CTX1);
    const out = m.touchEnd([], 400, CTX1); // held past tapMaxMs
    assert.equal(pointerActions(out).length, 0);
    assert.equal(pointerActions(m.tick(2000)).length, 0);
});

test("one-finger drag moves the cursor relatively and emits hover, never a click", () => {
    const m = machine();
    m.touchStart([{ id: 0, x: 100, y: 100 }], 0, CTX1);
    const out = m.touchMove([{ id: 0, x: 150, y: 130 }], 50, CTX1);
    // Full delta (including slop distance) applied: (640+50, 360+30).
    assert.deepEqual(m.cursor, { x: 690, y: 390 });
    const ptrs = pointerActions(out);
    assert.deepEqual(ptrs, [{ kind: "pointer", x: 690, y: 390, mask: MASK.NONE }]);
    const endOut = m.touchEnd([], 120, CTX1);
    assert.equal(pointerActions(endOut).length, 0);
    assert.equal(pointerActions(m.tick(9999)).length, 0); // no deferred click
});

test("cursor delta divides by zoom (constant on-screen cursor speed)", () => {
    const m = machine();
    const ctx = { zoom: 2, zoomedIn: true };
    m.touchStart([{ id: 0, x: 0, y: 0 }], 0, ctx);
    m.touchMove([{ id: 0, x: 50, y: 30 }], 40, ctx);
    assert.deepEqual(m.cursor, { x: 640 + 25, y: 360 + 15 });
});

test("cursor clamps to display bounds", () => {
    const m = machine();
    m.touchStart([{ id: 0, x: 0, y: 0 }], 0, CTX1);
    m.touchMove([{ id: 0, x: -5000, y: -5000 }], 50, CTX1);
    assert.deepEqual(m.cursor, { x: 0, y: 0 });
    m.touchMove([{ id: 0, x: 5000, y: 5000 }], 100, CTX1);
    assert.deepEqual(m.cursor, { x: 1279, y: 719 });
});

// ── tap-then-drag (press-and-drag) + double tap ──────────────────────────

test("tap-then-drag = press at cursor, drag with button held, release on end", () => {
    const m = machine();
    // Tap.
    m.touchStart([{ id: 0, x: 100, y: 100 }], 0, CTX1);
    m.touchEnd([], 80, CTX1);
    // Second touch within the chain window, then drag.
    m.touchStart([{ id: 1, x: 100, y: 100 }], 200, CTX1);
    const out = m.touchMove([{ id: 1, x: 140, y: 100 }], 260, CTX1);
    const ptrs = pointerActions(out);
    // Button-down at the cursor's ORIGINAL position, then the drag move.
    assert.deepEqual(ptrs[0], { kind: "pointer", x: 640, y: 360, mask: MASK.LEFT });
    assert.deepEqual(ptrs[1], { kind: "pointer", x: 680, y: 360, mask: MASK.LEFT });
    const up = pointerActions(m.touchEnd([], 400, CTX1));
    assert.deepEqual(up, [{ kind: "pointer", x: 680, y: 360, mask: MASK.NONE }]);
    // The deferred tap was consumed by the drag — no stray click later.
    assert.equal(pointerActions(m.tick(9999)).length, 0);
});

test("double tap = double click (down,up,down,up) at cursor", () => {
    const m = machine();
    m.touchStart([{ id: 0, x: 100, y: 100 }], 0, CTX1);
    m.touchEnd([], 60, CTX1);
    m.touchStart([{ id: 1, x: 102, y: 101 }], 180, CTX1);
    const out = pointerActions(m.touchEnd([], 240, CTX1));
    assert.deepEqual(out.map((a) => a.mask),
        [MASK.LEFT, MASK.NONE, MASK.LEFT, MASK.NONE]);
    for (const a of out) { assert.equal(a.x, 640); assert.equal(a.y, 360); }
    assert.equal(pointerActions(m.tick(9999)).length, 0);
});

test("tap-then-long-hold commits the press via tick (press-and-hold)", () => {
    const m = machine({ tapMaxMs: 250 });
    m.touchStart([{ id: 0, x: 100, y: 100 }], 0, CTX1);
    m.touchEnd([], 60, CTX1);
    m.touchStart([{ id: 1, x: 100, y: 100 }], 180, CTX1);
    const held = pointerActions(m.tick(180 + 251));
    assert.deepEqual(held, [{ kind: "pointer", x: 640, y: 360, mask: MASK.LEFT }]);
    const up = pointerActions(m.touchEnd([], 800, CTX1));
    assert.deepEqual(up, [{ kind: "pointer", x: 640, y: 360, mask: MASK.NONE }]);
});

// ── two-finger gestures ──────────────────────────────────────────────────

test("two-finger tap = right click at cursor", () => {
    const m = machine();
    m.touchStart([{ id: 0, x: 200, y: 200 }], 0, CTX1);
    m.touchStart([{ id: 0, x: 200, y: 200 }, { id: 1, x: 260, y: 205 }], 20, CTX1);
    m.touchEnd([{ id: 1, x: 260, y: 205 }], 90, CTX1); // first finger up
    const out = pointerActions(m.touchEnd([], 110, CTX1)); // second up
    assert.deepEqual(out, [
        { kind: "pointer", x: 640, y: 360, mask: MASK.RIGHT },
        { kind: "pointer", x: 640, y: 360, mask: MASK.NONE },
    ]);
});

test("two-finger parallel drag at fit zoom = wheel scroll pulses at cursor", () => {
    const m = machine({ wheelStepPx: 40 });
    const t0 = [{ id: 0, x: 300, y: 300 }, { id: 1, x: 380, y: 300 }];
    m.touchStart([t0[0]], 0, CTX1);
    m.touchStart(t0, 10, CTX1);
    // Fingers move DOWN 90px in parallel → content follows fingers → wheel UP.
    const out = m.touchMove(
        [{ id: 0, x: 300, y: 390 }, { id: 1, x: 380, y: 390 }], 80, CTX1);
    const ptrs = pointerActions(out);
    // 90px / 40px step = 2 pulses, each down+up, at the (unmoved) cursor.
    assert.deepEqual(ptrs.map((a) => a.mask),
        [MASK.WHEEL_UP, MASK.NONE, MASK.WHEEL_UP, MASK.NONE]);
    for (const a of ptrs) { assert.equal(a.x, 640); assert.equal(a.y, 360); }
    // Lifting produces no right click — the gesture was classified as scroll.
    m.touchEnd([{ id: 1, x: 380, y: 390 }], 120, CTX1);
    const end = pointerActions(m.touchEnd([], 130, CTX1));
    assert.equal(end.length, 0);
});

test("two-finger drag upward = wheel DOWN pulses (natural scrolling)", () => {
    const m = machine({ wheelStepPx: 40 });
    m.touchStart([{ id: 0, x: 300, y: 300 }], 0, CTX1);
    m.touchStart([{ id: 0, x: 300, y: 300 }, { id: 1, x: 380, y: 300 }], 10, CTX1);
    const out = m.touchMove(
        [{ id: 0, x: 300, y: 255 }, { id: 1, x: 380, y: 255 }], 80, CTX1);
    assert.deepEqual(pointerActions(out).map((a) => a.mask),
        [MASK.WHEEL_DOWN, MASK.NONE]);
});

test("two-finger parallel drag while ZOOMED IN = viewport pan, no wheel", () => {
    const m = machine();
    const ctx = { zoom: 2, zoomedIn: true };
    m.touchStart([{ id: 0, x: 300, y: 300 }], 0, ctx);
    m.touchStart([{ id: 0, x: 300, y: 300 }, { id: 1, x: 380, y: 300 }], 10, ctx);
    const out = m.touchMove(
        [{ id: 0, x: 330, y: 340 }, { id: 1, x: 410, y: 340 }], 80, ctx);
    assert.equal(pointerActions(out).length, 0);
    const pans = out.filter((a) => a.kind === "pan");
    assert.equal(pans.length, 1);
    assert.equal(pans[0].dx, 30);
    assert.equal(pans[0].dy, 40);
});

test("pinch (distance change) classifies as pinch and emits factor + centroid", () => {
    const m = machine();
    m.touchStart([{ id: 0, x: 300, y: 300 }], 0, CTX1);
    m.touchStart([{ id: 0, x: 300, y: 300 }, { id: 1, x: 400, y: 300 }], 10, CTX1);
    // Spread from 100px apart to 150px apart.
    const out = m.touchMove(
        [{ id: 0, x: 275, y: 300 }, { id: 1, x: 425, y: 300 }], 60, CTX1);
    const pinches = out.filter((a) => a.kind === "pinch");
    assert.equal(pinches.length, 1);
    assert.ok(Math.abs(pinches[0].factor - 1.5) < 1e-9);
    assert.equal(pinches[0].cx, 350);
    assert.equal(pinches[0].cy, 300);
    assert.equal(pointerActions(out).length, 0); // never wheel once pinch
    // Classification is sticky: a follow-up parallel move stays pinch/pan.
    const out2 = m.touchMove(
        [{ id: 0, x: 285, y: 310 }, { id: 1, x: 435, y: 310 }], 100, CTX1);
    assert.equal(pointerActions(out2).length, 0);
});

test("second finger landing mid press-drag releases the held button", () => {
    const m = machine();
    m.touchStart([{ id: 0, x: 100, y: 100 }], 0, CTX1);
    m.touchEnd([], 60, CTX1);
    m.touchStart([{ id: 1, x: 100, y: 100 }], 150, CTX1);
    m.touchMove([{ id: 1, x: 160, y: 100 }], 200, CTX1); // press-drag active
    const out = pointerActions(
        m.touchStart([{ id: 1, x: 160, y: 100 }, { id: 2, x: 220, y: 100 }], 240, CTX1));
    assert.equal(out.length, 1);
    assert.equal(out[0].mask, MASK.NONE); // button released
});

test("touchCancel releases any held button and resets", () => {
    const m = machine();
    m.touchStart([{ id: 0, x: 100, y: 100 }], 0, CTX1);
    m.touchEnd([], 60, CTX1);
    m.touchStart([{ id: 1, x: 100, y: 100 }], 150, CTX1);
    m.touchMove([{ id: 1, x: 160, y: 100 }], 200, CTX1);
    const out = pointerActions(m.touchCancel(220));
    assert.equal(out.length, 1);
    assert.equal(out[0].mask, MASK.NONE);
    assert.equal(m.state, "idle");
    assert.equal(pointerActions(m.tick(9999)).length, 0);
});

// ── ViewportTransform (pinch-zoom + pan math) ────────────────────────────

test("reset fits and centers the display in the viewport", () => {
    const vp = new ViewportTransform({ viewW: 800, viewH: 600, dispW: 1280, dispH: 720 });
    assert.equal(vp.fitScale, 0.625);           // min(800/1280, 600/720)
    assert.equal(vp.scale, 0.625);
    assert.equal(vp.tx, 0);                      // (800 - 1280*0.625)/2
    assert.equal(vp.ty, 75);                     // (600 - 720*0.625)/2
    assert.equal(vp.zoomedIn, false);
});

test("zoomAt keeps the display point under the centroid fixed", () => {
    const vp = new ViewportTransform({ viewW: 800, viewH: 600, dispW: 1280, dispH: 720 });
    const before = vp.viewToDisplay(400, 300);
    vp.zoomAt(400, 300, 2);
    assert.ok(Math.abs(vp.scale - 1.25) < 1e-9);
    const after = vp.displayToView(before.x, before.y);
    assert.ok(Math.abs(after.x - 400) < 1e-6);
    assert.ok(Math.abs(after.y - 300) < 1e-6);
    assert.equal(vp.zoomedIn, true);
});

test("scale clamps to [fitScale, maxScale]", () => {
    const vp = new ViewportTransform({ viewW: 800, viewH: 600, dispW: 1280, dispH: 720, maxScale: 3 });
    vp.zoomAt(400, 300, 1000);
    assert.equal(vp.scale, 3);
    vp.zoomAt(400, 300, 0.0001);
    assert.equal(vp.scale, vp.fitScale);
});

test("pan clamps so the display never detaches from the viewport", () => {
    const vp = new ViewportTransform({ viewW: 800, viewH: 600, dispW: 1280, dispH: 720 });
    vp.zoomAt(400, 300, 2); // scale 1.25 → content 1600x900
    vp.panBy(100000, 100000);
    assert.equal(vp.tx, 0);
    assert.equal(vp.ty, 0);
    vp.panBy(-100000, -100000);
    assert.equal(vp.tx, 800 - 1600);
    assert.equal(vp.ty, 600 - 900);
});

test("view/display round-trip under zoom+pan", () => {
    const vp = new ViewportTransform({ viewW: 800, viewH: 600, dispW: 1280, dispH: 720 });
    vp.zoomAt(250, 220, 2.4);
    vp.panBy(-37, 51);
    const p = { x: 431, y: 275 };
    const rt = vp.displayToView(vp.viewToDisplay(p.x, p.y).x, vp.viewToDisplay(p.x, p.y).y);
    assert.ok(Math.abs(rt.x - p.x) < 1e-6);
    assert.ok(Math.abs(rt.y - p.y) < 1e-6);
});

test("edgePushDelta pans toward a cursor near the edge only when zoomed in", () => {
    const vp = new ViewportTransform({ viewW: 800, viewH: 600, dispW: 1280, dispH: 720 });
    // At fit zoom: no push ever.
    assert.deepEqual(vp.edgePushDelta(0, 0), { dx: 0, dy: 0 });
    vp.zoomAt(400, 300, 2); // 1.25, tx=-400, ty=-150
    // Cursor at display (200,300): view x = 200*1.25-400 = -150 → off-screen left.
    const push = vp.edgePushDelta(200, 300, 36);
    assert.ok(push.dx > 0); // pan content right to reveal the cursor
    assert.equal(push.dy, 0); // y at view 225 — inside the margin band
    // Cursor comfortably inside: no push.
    assert.deepEqual(vp.edgePushDelta(640, 360, 36), { dx: 0, dy: 0 });
});

// ── CRITICAL: click coords map through zoom/pan to display coords ────────

test("integration: click coordinates stay display-native under zoom+pan", () => {
    const vp = new ViewportTransform({ viewW: 800, viewH: 600, dispW: 1280, dispH: 720 });
    vp.zoomAt(400, 300, 2); // scale 1.25
    const m = machine();
    const ctx = { zoom: vp.scale, zoomedIn: vp.zoomedIn };
    // Drag the finger 100 view-px right: cursor advances 100/1.25 = 80 display px.
    m.touchStart([{ id: 0, x: 100, y: 100 }], 0, ctx);
    m.touchMove([{ id: 0, x: 200, y: 100 }], 50, ctx);
    assert.deepEqual(m.cursor, { x: 720, y: 360 });
    m.touchEnd([], 90, ctx);
    // Then TAP (anywhere on the touchpad) → click lands at the cursor.
    m.touchStart([{ id: 1, x: 500, y: 400 }], 1000, ctx);
    m.touchEnd([], 1060, ctx);
    const clicks = pointerActions(m.tick(1060 + DEFAULTS.dragChainMs + 1));
    // The click goes out in DISPLAY coordinates — exactly the cursor.
    assert.deepEqual(clicks[0], { kind: "pointer", x: 720, y: 360, mask: MASK.LEFT });
    // And that display point is currently visible in the transformed view.
    const v = vp.displayToView(720, 360);
    assert.ok(v.x >= 0 && v.x <= 800 && v.y >= 0 && v.y <= 600);
    // Round-trip sanity: view position maps back to the same display pixel.
    const back = vp.viewToDisplay(v.x, v.y);
    assert.ok(Math.abs(back.x - 720) < 1e-6 && Math.abs(back.y - 360) < 1e-6);
});
