/**
 * rfb-input.js — the ONE place the CU viewer reaches into noVNC's private
 * pointer API (audit P07-html-css-14/-16, upgrade roadmap item 12).
 *
 * noVNC has no public "send this pointer event" call: RFB only turns events
 * on its OWN canvas into PointerEvent messages, and the viewer covers that
 * canvas with its gesture layer (touch-gestures.js) and drives the remote
 * pointer itself. So it calls RFB#_sendMouse(x, y, mask): display-absolute
 * coords (our canvas has internal scale 1, scaleViewport off, so display
 * coords pass straight through), gated by noVNC on the connection state and
 * viewOnly. The method is internal and any release may rename or change it,
 * so:
 *   - every call goes through sendPointerEvent below;
 *   - tests/web/novnc-rfb-seam.test.mjs runs it against the VENDORED RFB
 *     (Portal/vendor/novnc, a real handshake over a fake socket) and checks
 *     the PointerEvent bytes, so a re-vendor that breaks the seam fails a
 *     test instead of shipping a viewer that silently ignores every tap;
 *   - at runtime a missing seam is reported (once per RFB instance) through
 *     the caller's onMissing callback rather than swallowed.
 *
 * Pure: no DOM, no imports — node-tested in rfb-input.test.mjs.
 */

/** RFB pointer button bits the viewer sends (noVNC 1.7: bits 0-6 = buttons 1-7). */
export const POINTER_MASK_BITS = 0x7f;

const reported = new WeakSet();

/**
 * True when this RFB instance has the private pointer seam the viewer uses.
 * @param {object|null} rfb - A noVNC RFB instance
 * @returns {boolean}
 */
export function hasPointerSeam(rfb) {
    return !!rfb && typeof rfb._sendMouse === "function";
}

/**
 * Send one pointer event (position + button mask) through noVNC.
 * @param {object|null} rfb - A noVNC RFB instance
 * @param {number} x - Display-absolute x (rounded)
 * @param {number} y - Display-absolute y (rounded)
 * @param {number} mask - RFB button mask (bits 0-6; anything else is dropped)
 * @param {(rfb: object) => void} [onMissing] - Called once per instance when
 *   the seam is gone (the vendored noVNC changed under the viewer)
 * @returns {boolean} true when noVNC accepted the call (it may still drop it
 *   while not connected or in view-only mode, as it does for its own events)
 */
export function sendPointerEvent(rfb, x, y, mask, onMissing) {
    if (!rfb) return false;
    if (!hasPointerSeam(rfb)) {
        if (!reported.has(rfb)) {
            reported.add(rfb);
            if (typeof onMissing === "function") onMissing(rfb);
        }
        return false;
    }
    try {
        rfb._sendMouse(Math.round(x), Math.round(y), mask & POINTER_MASK_BITS);
        return true;
    } catch {
        return false;   // connect race: the socket went away mid-call
    }
}
