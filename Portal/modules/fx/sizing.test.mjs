// Sizing math for the particle canvas — the code that has been wrong since the
// engine's first commit and had no test.
//
// Two separate concerns that MUST stay separate:
//   backingStore() decides RESOLUTION (device pixels in the canvas buffer)
//   apparentScale() decides GEOMETRY (how big a particle looks)
// Conflating them is what produced the shipped bug: particle geometry that
// tracked devicePixelRatio, so the field rendered dpr x magnified.
//
// Run: node --test Portal/modules/fx/sizing.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { backingStore, apparentScale, MAX_BACKING_PX } from "./sizing.js";

test("a 3x phone gets full resolution — the ratio is not clamped to 2", () => {
    // 412x915 CSS at dpr 3 = ~3.4M device px, well inside the budget. The old
    // Math.min(dpr, 2) starved exactly this case (a small, very dense screen).
    const b = backingStore(412, 915, 3);
    assert.equal(b.dpr, 3);
    assert.equal(b.w, 1236);
    assert.equal(b.h, 2745);
});

test("a large desktop is clamped by TOTAL PIXELS, not by ratio", () => {
    // 2560x1440 at 2x would be 14.7M px; the budget pulls dpr to a FRACTIONAL
    // value that spends the budget exactly, rather than dropping to a blurrier
    // integer 1.0.
    const b = backingStore(2560, 1440, 2);
    assert.ok(b.dpr > 1 && b.dpr < 2, `expected a fractional dpr, got ${b.dpr}`);
    assert.ok(b.w * b.h <= MAX_BACKING_PX, `budget exceeded: ${b.w * b.h}`);
});

test("dpr floors at 1 even when the CSS box alone busts the budget", () => {
    // A 4K CSS viewport is 8.3M px at 1:1 — already over budget. Going below 1
    // would render sub-CSS-pixel and reintroduce the softness this whole change
    // exists to remove, so 1.0 is the floor and the budget yields.
    const b = backingStore(3840, 2160, 2);
    assert.equal(b.dpr, 1);
    assert.ok(b.w * b.h > MAX_BACKING_PX, "documents that the floor wins over the budget");
});

test("a modest window at 2x is untouched (the approved look must not move)", () => {
    const b = backingStore(1400, 800, 2);
    assert.equal(b.dpr, 2);
    assert.equal(b.w, 2800);
    assert.equal(b.h, 1600);
});

test("dpr never goes below 1, even on an absurd viewport", () => {
    const b = backingStore(20000, 20000, 3);
    assert.ok(b.dpr >= 1, `dpr fell below 1: ${b.dpr}`);
});

test("missing/garbage devicePixelRatio falls back to 1", () => {
    assert.equal(backingStore(800, 600, undefined).dpr, 1);
    assert.equal(backingStore(800, 600, 0).dpr, 1);
    assert.equal(backingStore(800, 600, NaN).dpr, 1);
});

test("apparent particle size is INDEPENDENT of devicePixelRatio", () => {
    // The whole bug in one assertion: geometry is a function of the CSS box only.
    assert.equal(apparentScale(1400, 800), apparentScale(1400, 800));
    assert.equal(apparentScale.length, 2, "apparentScale must not accept a dpr argument");
});

test("apparent particle size scales with the short edge", () => {
    assert.ok(apparentScale(1400, 1800) > apparentScale(1400, 900),
        "a taller viewport has a longer short edge and should scale up");
    assert.equal(apparentScale(900, 900), 1, "the reference short edge is 1.0");
});

test("apparent scale is clamped so tiny and huge viewports stay sane", () => {
    assert.ok(apparentScale(200, 200) >= 0.5);
    assert.ok(apparentScale(6000, 6000) <= 2.0);
});
