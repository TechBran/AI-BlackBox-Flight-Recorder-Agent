// Unit tests for the CU viewer's adapter over noVNC's private pointer seam
// (rfb-input.js; audit P07-html-css-14/-16). Pure module — no DOM. The
// contract against the REAL vendored noVNC lives in
// tests/web/novnc-rfb-seam.test.mjs.
//
// Run: node --test Portal/cu-view/rfb-input.test.mjs
import test from "node:test";
import assert from "node:assert/strict";

import { hasPointerSeam, sendPointerEvent, POINTER_MASK_BITS } from "./rfb-input.js";
import { MASK } from "./touch-gestures.js";

/** An RFB stand-in recording every _sendMouse call. */
function fakeRfb() {
    const calls = [];
    return { calls, _sendMouse(x, y, mask) { calls.push([x, y, mask]); } };
}

test("forwards rounded display coords and the mask to _sendMouse", () => {
    const rfb = fakeRfb();
    assert.equal(sendPointerEvent(rfb, 10.4, 20.6, MASK.LEFT), true);
    assert.equal(sendPointerEvent(rfb, 10.5, 20.5, MASK.NONE), true);
    assert.deepEqual(rfb.calls, [[10, 21, 1], [11, 21, 0]]);
});

test("every mask the gesture machine emits passes through unchanged", () => {
    const rfb = fakeRfb();
    for (const m of Object.values(MASK)) sendPointerEvent(rfb, 1, 1, m);
    sendPointerEvent(rfb, 1, 1, 32);   // horizontal wheel (button 6)
    sendPointerEvent(rfb, 1, 1, 64);   // horizontal wheel (button 7)
    const sent = rfb.calls.map((c) => c[2]);
    assert.deepEqual(sent, [...Object.values(MASK), 32, 64]);
    assert.ok(sent.every((m) => (m & ~POINTER_MASK_BITS) === 0));
});

test("bits noVNC would route elsewhere (or throw on) are never sent", () => {
    // noVNC 1.7 sends bits 7-14 as an ExtendedPointerEvent and throws on bit 15.
    const rfb = fakeRfb();
    sendPointerEvent(rfb, 0, 0, 0x8000 | 0x0100 | MASK.RIGHT);
    assert.deepEqual(rfb.calls, [[0, 0, MASK.RIGHT]]);
});

test("a throwing seam (connect race) is contained", () => {
    const rfb = { _sendMouse() { throw new Error("socket closed"); } };
    assert.equal(sendPointerEvent(rfb, 1, 2, MASK.LEFT), false);
});

test("no RFB yet: nothing happens", () => {
    assert.equal(sendPointerEvent(null, 1, 2, MASK.LEFT), false);
    assert.equal(hasPointerSeam(null), false);
});

test("a missing seam is reported once per RFB instance, never thrown", () => {
    const reports = [];
    const onMissing = (r) => reports.push(r);
    const a = {};
    const b = {};
    assert.equal(hasPointerSeam(a), false);
    for (let i = 0; i < 3; i++) assert.equal(sendPointerEvent(a, 1, 2, MASK.LEFT, onMissing), false);
    sendPointerEvent(b, 1, 2, MASK.LEFT, onMissing);
    assert.deepEqual(reports, [a, b]);
    assert.equal(sendPointerEvent({}, 1, 2, MASK.LEFT), false, "no callback is fine too");
});
