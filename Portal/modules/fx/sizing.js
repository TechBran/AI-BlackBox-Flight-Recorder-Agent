/**
 * Canvas sizing math for the particle field.
 *
 * The bug this module exists to prevent (shipped from the engine's first commit
 * until 2026-07-24): particle GEOMETRY was allowed to track devicePixelRatio, so
 * the field rendered dpr x magnified at 1/dpr resolution on every HiDPI screen.
 *
 * The rule, and the reason these are two separate functions:
 *
 *   backingStore()  -> RESOLUTION only. How many device pixels the canvas buffer
 *                      holds. More = crisper. Never touches how big anything looks.
 *   apparentScale() -> GEOMETRY only. How big a particle looks, as a function of
 *                      the CSS box. Never sees devicePixelRatio at all.
 *
 * Keep them separate and the field looks identical on a 1x laptop and a 4x phone,
 * just sharper on the phone.
 */

/**
 * Backing-store budget, in device pixels (~6M px ≈ 24 MB RGBA).
 *
 * The old code clamped the RATIO (`Math.min(dpr, 2)`), which is the wrong axis:
 * it starves a small-but-dense phone (412x915 at 3x is only ~3.4M px — cheap and
 * it wants every one of them) while doing nothing for a 4K desktop at 2x
 * (~33M px). Clamping total pixels protects the actual cost.
 */
export const MAX_BACKING_PX = 6e6;

/** Reference short edge, in CSS px. A viewport this size renders at scale 1.0 —
 *  it is the size the current look was tuned at, so it must stay 900. */
const REFERENCE_SHORT_EDGE = 900;

/** Geometry clamp: below this the field reads as sparse dust, above it as blobs. */
const MIN_APPARENT = 0.5;
const MAX_APPARENT = 2.0;

/**
 * Resolve the canvas backing store for a CSS box.
 *
 * @param {number} cssW  container width in CSS px
 * @param {number} cssH  container height in CSS px
 * @param {number} rawDpr  window.devicePixelRatio (may be undefined/0/NaN)
 * @returns {{dpr:number, w:number, h:number}} dpr actually used and the integer
 *          device-pixel dimensions to assign to canvas.width / canvas.height.
 */
export function backingStore(cssW, cssH, rawDpr) {
    const wanted = Number.isFinite(rawDpr) && rawDpr > 0 ? rawDpr : 1;
    const cssArea = cssW * cssH;
    if (cssArea <= 0) return { dpr: 1, w: 0, h: 0 };

    const dpr = cssArea * wanted * wanted <= MAX_BACKING_PX
        ? wanted
        // Spend the whole budget rather than dropping to the next integer ratio:
        // a fractional dpr is still sharper than a smaller integer one.
        : Math.max(1, Math.sqrt(MAX_BACKING_PX / cssArea));

    return { dpr, w: Math.floor(cssW * dpr), h: Math.floor(cssH * dpr) };
}

/**
 * Geometry multiplier for particle sizes/speeds, from the CSS box alone.
 *
 * Deliberately takes NO dpr argument — a test asserts its arity, because
 * accepting one is exactly how the original bug would come back.
 *
 * @param {number} cssW container width in CSS px
 * @param {number} cssH container height in CSS px
 * @returns {number} multiplier, clamped to [0.5, 2.0]
 */
export function apparentScale(cssW, cssH) {
    const shortEdge = Math.min(cssW, cssH);
    const raw = shortEdge / REFERENCE_SHORT_EDGE;
    return Math.max(MIN_APPARENT, Math.min(MAX_APPARENT, raw));
}
