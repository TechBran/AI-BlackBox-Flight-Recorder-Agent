// Unit tests for the screenshot-fallback pinch-zoom/pan transform mapping
// (desktop-first CU 2026-07-23, part C). The math is ViewportTransform from
// Portal/cu-view/touch-gestures.js (imported by cu-fallback-zoom.js, not
// duplicated) — these tests pin the fallback's coordinate contract: clicks
// map through the CURRENT transform to correct screenshot coords.
//
// Run: node --test Portal/modules/cu-fallback-zoom.test.mjs
import test from "node:test";
import assert from "node:assert/strict";

import {
    createFallbackViewport, fallbackTransformCss, mapViewToScreenshot,
} from "./cu-fallback-zoom.js";

// A 640x360 fitted <img> showing a 1280x720 screenshot.
const VIEW_W = 640, VIEW_H = 360, SHOT_W = 1280, SHOT_H = 720;

test("untransformed state is identity: pre-pinch mapping unchanged", () => {
    const vt = createFallbackViewport(VIEW_W, VIEW_H);
    assert.equal(vt.scale, 1);
    assert.equal(vt.tx, 0);
    assert.equal(vt.ty, 0);
    assert.deepEqual(mapViewToScreenshot(vt, 0, 0, SHOT_W, SHOT_H), { x: 0, y: 0 });
    // Proportional, same as the legacy rect-ratio math.
    assert.deepEqual(mapViewToScreenshot(vt, 320, 180, SHOT_W, SHOT_H), { x: 640, y: 360 });
    assert.deepEqual(mapViewToScreenshot(vt, 160, 90, SHOT_W, SHOT_H), { x: 320, y: 180 });
});

test("mapping clamps inside the screenshot bounds", () => {
    const vt = createFallbackViewport(VIEW_W, VIEW_H);
    assert.deepEqual(mapViewToScreenshot(vt, VIEW_W, VIEW_H, SHOT_W, SHOT_H),
        { x: SHOT_W - 1, y: SHOT_H - 1 });
    assert.deepEqual(mapViewToScreenshot(vt, -50, -50, SHOT_W, SHOT_H), { x: 0, y: 0 });
});

test("zoom anchor stays put: the point under the fingers maps identically", () => {
    const vt = createFallbackViewport(VIEW_W, VIEW_H);
    const before = mapViewToScreenshot(vt, 320, 180, SHOT_W, SHOT_H);
    vt.zoomAt(320, 180, 2);
    const after = mapViewToScreenshot(vt, 320, 180, SHOT_W, SHOT_H);
    assert.deepEqual(after, before);
});

test("2x zoom about the origin: view center → screenshot first quadrant", () => {
    const vt = createFallbackViewport(VIEW_W, VIEW_H);
    vt.zoomAt(0, 0, 2);
    assert.equal(vt.scale, 2);
    // view(320,180) → base(160,90) → screenshot(320,180)
    assert.deepEqual(mapViewToScreenshot(vt, 320, 180, SHOT_W, SHOT_H), { x: 320, y: 180 });
});

test("pan shifts the mapping; clampPan never detaches the image", () => {
    const vt = createFallbackViewport(VIEW_W, VIEW_H);
    vt.zoomAt(0, 0, 2);
    vt.panBy(-100, -50);
    // view(0,0) → base((0+100)/2, (0+50)/2) = (50,25) → screenshot(100,50)
    assert.deepEqual(mapViewToScreenshot(vt, 0, 0, SHOT_W, SHOT_H), { x: 100, y: 50 });
    // A runaway pan clamps to the far edge (viewW - dispW*scale).
    vt.panBy(-99999, -99999);
    assert.equal(vt.tx, VIEW_W - VIEW_W * 2);
    assert.equal(vt.ty, VIEW_H - VIEW_H * 2);
    // Bottom-right of the view now maps exactly to the screenshot's far corner.
    assert.deepEqual(mapViewToScreenshot(vt, VIEW_W, VIEW_H, SHOT_W, SHOT_H),
        { x: SHOT_W - 1, y: SHOT_H - 1 });
});

test("scale clamps to [1, maxScale] — pinch-out below fit is a no-op", () => {
    const vt = createFallbackViewport(VIEW_W, VIEW_H, 3);
    vt.zoomAt(10, 10, 0.5);
    assert.equal(vt.scale, 1);
    assert.equal(vt.tx, 0);   // clampPan re-centers at fit
    vt.zoomAt(10, 10, 100);
    assert.equal(vt.scale, 3);
});

test("fallbackTransformCss renders the live state for the <img>", () => {
    const vt = createFallbackViewport(VIEW_W, VIEW_H);
    vt.zoomAt(0, 0, 2);
    vt.panBy(-10, -20);
    assert.equal(fallbackTransformCss(vt),
        `translate(${vt.tx}px, ${vt.ty}px) scale(2)`);
});

test("non-16:9 view (letterboxed layout) still maps proportionally", () => {
    // e.g. a 500x281 img box (max-width constrained)
    const vt = createFallbackViewport(500, 281);
    const m = mapViewToScreenshot(vt, 250, 140.5, SHOT_W, SHOT_H);
    assert.deepEqual(m, { x: 640, y: 360 });
});
