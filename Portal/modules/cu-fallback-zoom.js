/**
 * cu-fallback-zoom.js — PURE pinch-zoom/pan helpers for the screenshot-poll
 * fallback viewer (cu-interact.js), desktop-first CU 2026-07-23 (C).
 *
 * Reuses ViewportTransform from Portal/cu-view/touch-gestures.js (imported,
 * NOT duplicated) so the fallback shares the streaming client's exact
 * zoom-about-a-point / pan-clamp math. The transform is VIEW-ONLY: it is
 * CSS-applied to the fallback <img> (transform-origin 0 0) — the remote
 * screen and the polled screenshots never change.
 *
 * Coordinate model: the fallback's "display" is the fitted <img> layout box
 * itself. The img is laid out at the wrap's size (the wrap shrink-wraps it),
 * so disp == view, fitScale == 1, and the untransformed state is exactly the
 * pre-pinch viewer. Pinch scales up from 1; clampPan keeps the img attached
 * to the viewport on every axis.
 *
 * No DOM, no network — node-tested in cu-fallback-zoom.test.mjs.
 */

import { ViewportTransform } from '../cu-view/touch-gestures.js';

/**
 * Build the fallback viewport over the img's (untransformed) layout box.
 * @param {number} viewW - img layout width (offsetWidth), px
 * @param {number} viewH - img layout height (offsetHeight), px
 * @param {number} [maxScale] - pinch ceiling
 * @returns {ViewportTransform}
 */
export function createFallbackViewport(viewW, viewH, maxScale = 4) {
    return new ViewportTransform({ viewW, viewH, dispW: viewW, dispH: viewH, maxScale });
}

/** CSS transform string for the current viewport state (origin must be 0 0). */
export function fallbackTransformCss(vt) {
    return `translate(${vt.tx}px, ${vt.ty}px) scale(${vt.scale})`;
}

/**
 * Map a wrap-relative pointer position through the CURRENT transform to
 * screenshot coordinates — so clicks land on the correct remote pixel at any
 * zoom/pan. Inverse-transform to the img's base box, normalize, then scale to
 * the screenshot's native resolution, clamped inside its bounds.
 *
 * @param {ViewportTransform} vt
 * @param {number} viewX - pointer x relative to the wrap, px
 * @param {number} viewY - pointer y relative to the wrap, px
 * @param {number} shotW - screenshot native width (e.g. 1280)
 * @param {number} shotH - screenshot native height (e.g. 720)
 * @returns {{x:number, y:number}} screenshot coords
 */
export function mapViewToScreenshot(vt, viewX, viewY, shotW, shotH) {
    const p = vt.viewToDisplay(viewX, viewY);
    const nx = Math.min(Math.max(p.x / vt.dispW, 0), 1);
    const ny = Math.min(Math.max(p.y / vt.dispH, 0), 1);
    return {
        x: Math.min(Math.round(nx * shotW), shotW - 1),
        y: Math.min(Math.round(ny * shotH), shotH - 1),
    };
}
