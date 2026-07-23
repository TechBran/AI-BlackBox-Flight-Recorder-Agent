/**
 * touch-gestures.js — PURE gesture state machine + viewport transform for the
 * CU live view's Splashtop-style touchpad mode (design 2026-07-23 §4.1/§4.2).
 *
 * No DOM, no timers, no network — the host (cu-view.js) feeds synthetic
 * touch lists + timestamps and applies the returned actions. That keeps the
 * whole gesture grammar unit-testable in node (touch-gestures.test.mjs).
 *
 * TouchpadMachine — indirect ("touchpad") pointer:
 *   - The CURSOR lives in DISPLAY space (native px of the CU session's Xvfb,
 *     e.g. 1280x720). Touches arrive in VIEW space (overlay client px).
 *   - One-finger drag  → relative cursor movement, Δcursor = Δtouch / zoom
 *     (constant on-screen cursor speed at any zoom), streamed as hover
 *     pointer events (mask 0) so menus/tooltips react.
 *   - Tap              → LEFT click at the CURSOR position (not the touch
 *     point). The click is DEFERRED by dragChainMs so that…
 *   - Tap-then-drag    → press-and-drag (button held while moving), and
 *   - Double tap       → double click — both claim the deferred tap.
 *     Deferred/press-commit actions are released by tick(now); the host runs
 *     a coarse interval for that.
 *   - Two-finger tap   → RIGHT click at the cursor.
 *   - Two-finger drag  → wheel scroll at fit zoom (natural scrolling:
 *     content follows the fingers), viewport PAN when zoomed in.
 *   - Pinch            → {kind:'pinch'} actions for the host's
 *     ViewportTransform. Classification (pinch vs scroll/pan) is sticky for
 *     the life of the two-finger contact.
 *
 * Emitted actions:
 *   {kind:'pointer', x, y, mask}   RFB pointerEvent in display coords
 *   {kind:'pan', dx, dy}           viewport pan (view px)
 *   {kind:'pinch', factor, cx, cy} zoom about a view-space centroid
 *
 * ViewportTransform — client-managed pinch-zoom/pan of the RFB canvas
 * (noVNC scaleViewport OFF, resizeSession NEVER — the agent screen must not
 * change): view = display*scale + t, scale ∈ [fitScale, maxScale], pan
 * clamped so the display never detaches from the viewport, plus the
 * Splashtop edge-push helper for cursor-against-the-edge auto-panning.
 */

export const MASK = Object.freeze({
    NONE: 0,
    LEFT: 1,
    MIDDLE: 2,
    RIGHT: 4,
    WHEEL_UP: 8,     // RFB button 4
    WHEEL_DOWN: 16,  // RFB button 5
});

export const DEFAULTS = Object.freeze({
    tapMaxMs: 250,        // touch shorter than this (and under slop) = tap
    tapSlopPx: 10,        // view-px movement budget before a touch is a drag
    dragChainMs: 300,     // window after a tap for tap-drag / double-tap
    wheelStepPx: 40,      // view px of two-finger travel per wheel tick
    pinchRatioThreshold: 0.06, // |dist/startDist - 1| beyond this = pinch
    twoTapMaxMs: 300,     // two-finger contact shorter than this = right click
});

const DEFAULT_CTX = Object.freeze({ zoom: 1, zoomedIn: false });

function clampNum(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
function dist(a, b) { return Math.hypot(a.x - b.x, a.y - b.y); }
function centroid(a, b) { return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }; }

export class TouchpadMachine {
    constructor(opts = {}) {
        const { width, height } = opts;
        if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
            throw new Error("TouchpadMachine requires positive {width, height} (display px)");
        }
        this.width = width;
        this.height = height;
        this.tapMaxMs = opts.tapMaxMs ?? DEFAULTS.tapMaxMs;
        this.tapSlopPx = opts.tapSlopPx ?? DEFAULTS.tapSlopPx;
        this.dragChainMs = opts.dragChainMs ?? DEFAULTS.dragChainMs;
        this.wheelStepPx = opts.wheelStepPx ?? DEFAULTS.wheelStepPx;
        this.pinchRatioThreshold = opts.pinchRatioThreshold ?? DEFAULTS.pinchRatioThreshold;
        this.twoTapMaxMs = opts.twoTapMaxMs ?? DEFAULTS.twoTapMaxMs;

        // Cursor starts centered — display-space, clamped for life.
        this.cursor = { x: Math.round(width / 2), y: Math.round(height / 2) };

        // States: idle | touch | drag | pressPending | pressHeld | pressDrag
        //         | two | twoEnd | ignore
        this.state = "idle";
        this._downAt = 0;
        this._downPos = null;   // view px at touch start
        this._last = null;      // view px of the last APPLIED position
        this._pendingTapAt = null; // t when a tap ended, awaiting chain/flush
        this._buttonDown = false;
        this._two = null;       // two-finger gesture record
    }

    // ── helpers ──────────────────────────────────────────────────────────

    _pointer(mask) {
        return {
            kind: "pointer",
            x: Math.round(this.cursor.x),
            y: Math.round(this.cursor.y),
            mask,
        };
    }

    _moveCursor(dxView, dyView, zoom) {
        const z = zoom > 0 ? zoom : 1;
        this.cursor.x = clampNum(this.cursor.x + dxView / z, 0, this.width - 1);
        this.cursor.y = clampNum(this.cursor.y + dyView / z, 0, this.height - 1);
    }

    _releaseButton(out) {
        if (this._buttonDown) {
            this._buttonDown = false;
            out.push(this._pointer(MASK.NONE));
        }
    }

    // ── event entry points (touches: [{id,x,y}] in view px) ──────────────

    touchStart(touches, t, _ctx = DEFAULT_CTX) {
        const out = [];
        if (touches.length === 1) {
            const p = touches[0];
            if (this.state === "idle") {
                if (this._pendingTapAt != null && t - this._pendingTapAt <= this.dragChainMs) {
                    // Chained touch after a tap: could be double-tap or
                    // press-and-drag — decide on move/end/tick.
                    this.state = "pressPending";
                    this._pendingTapAt = null;
                } else {
                    this.state = "touch";
                }
                this._downAt = t;
                this._downPos = { x: p.x, y: p.y };
                this._last = { x: p.x, y: p.y };
            }
            // Re-entry with one touch from twoEnd/ignore: wait for full lift.
        } else if (touches.length === 2) {
            // Entering two-finger from ANY one-finger state: abandon the
            // one-finger gesture (releasing a held button first).
            this._releaseButton(out);
            this._pendingTapAt = null;
            const [a, b] = touches;
            const d = dist(a, b);
            const c = centroid(a, b);
            this._two = {
                startDist: d, lastDist: d,
                startCentroid: c, lastCentroid: c,
                downAt: t, mode: null,
                accumY: 0, accumX: 0,
            };
            this.state = "two";
        } else {
            // 3+ fingers: abort everything until full lift.
            this._releaseButton(out);
            this._pendingTapAt = null;
            this._two = null;
            this.state = "ignore";
        }
        return out;
    }

    touchMove(touches, t, ctx = DEFAULT_CTX) {
        const out = [];
        switch (this.state) {
            case "touch": {
                const p = touches[0];
                if (dist(p, this._downPos) > this.tapSlopPx) {
                    this.state = "drag";
                    // Fall through logic: apply the full accumulated delta
                    // (from _downPos) so no movement is swallowed.
                    this._applyCursorMove(p, MASK.NONE, ctx, out);
                }
                break;
            }
            case "drag": {
                this._applyCursorMove(touches[0], MASK.NONE, ctx, out);
                break;
            }
            case "pressPending": {
                const p = touches[0];
                if (dist(p, this._downPos) > this.tapSlopPx) {
                    // Commit the press at the cursor's CURRENT position,
                    // then drag with the button held.
                    this._buttonDown = true;
                    out.push(this._pointer(MASK.LEFT));
                    this.state = "pressDrag";
                    this._applyCursorMove(p, MASK.LEFT, ctx, out);
                }
                break;
            }
            case "pressHeld": {
                this.state = "pressDrag";
                this._applyCursorMove(touches[0], MASK.LEFT, ctx, out);
                break;
            }
            case "pressDrag": {
                this._applyCursorMove(touches[0], MASK.LEFT, ctx, out);
                break;
            }
            case "two": {
                this._handleTwoMove(touches, ctx, out);
                break;
            }
            default:
                break; // twoEnd / ignore: remaining-finger movement is dead
        }
        return out;
    }

    touchEnd(remaining, t, _ctx = DEFAULT_CTX) {
        const out = [];
        switch (this.state) {
            case "touch":
                if (t - this._downAt <= this.tapMaxMs) {
                    // Tap — defer the click so tap-drag/double-tap can claim it.
                    this._pendingTapAt = t;
                }
                this.state = "idle";
                break;
            case "drag":
                this.state = "idle";
                break;
            case "pressPending":
                if (t - this._downAt <= this.tapMaxMs) {
                    // tap + tap = double click.
                    out.push(this._pointer(MASK.LEFT), this._pointer(MASK.NONE),
                             this._pointer(MASK.LEFT), this._pointer(MASK.NONE));
                } else {
                    // tap + long-still-hold released before tick committed it:
                    // treat as a single click.
                    out.push(this._pointer(MASK.LEFT), this._pointer(MASK.NONE));
                }
                this.state = "idle";
                break;
            case "pressHeld":
            case "pressDrag":
                this._releaseButton(out);
                this.state = "idle";
                break;
            case "two":
                if (remaining.length >= 1) {
                    this.state = "twoEnd"; // first finger up; await full lift
                } else {
                    this._finishTwo(t, out);
                }
                break;
            case "twoEnd":
                if (remaining.length === 0) this._finishTwo(t, out);
                break;
            case "ignore":
                if (remaining.length === 0) this.state = "idle";
                break;
            default:
                break;
        }
        return out;
    }

    touchCancel(_t) {
        const out = [];
        this._releaseButton(out);
        this._pendingTapAt = null;
        this._two = null;
        this.state = "idle";
        return out;
    }

    /** Release time-deferred actions. Host calls this on a coarse interval. */
    tick(t) {
        const out = [];
        if (this._pendingTapAt != null && t - this._pendingTapAt > this.dragChainMs) {
            // Un-chained tap: emit the click at the cursor.
            this._pendingTapAt = null;
            out.push(this._pointer(MASK.LEFT), this._pointer(MASK.NONE));
        }
        if (this.state === "pressPending" && t - this._downAt > this.tapMaxMs) {
            // tap + hold (no move yet): commit press-and-hold.
            this._buttonDown = true;
            this.state = "pressHeld";
            out.push(this._pointer(MASK.LEFT));
        }
        return out;
    }

    // ── internals ────────────────────────────────────────────────────────

    _applyCursorMove(p, mask, ctx, out) {
        const dx = p.x - this._last.x;
        const dy = p.y - this._last.y;
        this._last = { x: p.x, y: p.y };
        if (dx === 0 && dy === 0) return;
        this._moveCursor(dx, dy, ctx.zoom);
        out.push(this._pointer(mask));
    }

    _handleTwoMove(touches, ctx, out) {
        if (touches.length < 2 || !this._two) return;
        const [a, b] = touches;
        const d = dist(a, b);
        const c = centroid(a, b);
        const two = this._two;

        if (two.mode === null) {
            if (two.startDist > 0
                && Math.abs(d / two.startDist - 1) > this.pinchRatioThreshold) {
                two.mode = "pinch";
            } else if (dist(c, two.startCentroid) > this.tapSlopPx) {
                two.mode = ctx.zoomedIn ? "pan" : "scroll";
            }
        }

        if (two.mode === "pinch") {
            const factor = two.lastDist > 0 ? d / two.lastDist : 1;
            if (factor !== 1) out.push({ kind: "pinch", factor, cx: c.x, cy: c.y });
            const dx = c.x - two.lastCentroid.x;
            const dy = c.y - two.lastCentroid.y;
            if (dx !== 0 || dy !== 0) out.push({ kind: "pan", dx, dy });
        } else if (two.mode === "pan") {
            const dx = c.x - two.lastCentroid.x;
            const dy = c.y - two.lastCentroid.y;
            if (dx !== 0 || dy !== 0) out.push({ kind: "pan", dx, dy });
        } else if (two.mode === "scroll") {
            // Natural scrolling: content follows the fingers. Fingers moving
            // DOWN (+dy) reveal content above → wheel UP; fingers UP → wheel
            // DOWN. Wheel ticks fire at the cursor position.
            two.accumY += c.y - two.lastCentroid.y;
            two.accumX += c.x - two.lastCentroid.x;
            while (two.accumY >= this.wheelStepPx) {
                two.accumY -= this.wheelStepPx;
                out.push(this._pointer(MASK.WHEEL_UP), this._pointer(MASK.NONE));
            }
            while (two.accumY <= -this.wheelStepPx) {
                two.accumY += this.wheelStepPx;
                out.push(this._pointer(MASK.WHEEL_DOWN), this._pointer(MASK.NONE));
            }
            // Horizontal wheel = RFB buttons 6/7 (masks 32/64).
            while (two.accumX >= this.wheelStepPx) {
                two.accumX -= this.wheelStepPx;
                out.push(this._pointer(32), this._pointer(MASK.NONE));
            }
            while (two.accumX <= -this.wheelStepPx) {
                two.accumX += this.wheelStepPx;
                out.push(this._pointer(64), this._pointer(MASK.NONE));
            }
        }

        two.lastDist = d;
        two.lastCentroid = c;
    }

    _finishTwo(t, out) {
        const two = this._two;
        if (two && two.mode === null && t - two.downAt <= this.twoTapMaxMs) {
            // Two-finger tap = right click at the cursor.
            out.push(this._pointer(MASK.RIGHT), this._pointer(MASK.NONE));
        }
        this._two = null;
        this.state = "idle";
    }
}

export class ViewportTransform {
    constructor({ viewW, viewH, dispW, dispH, maxScale = 3 }) {
        if (!(viewW > 0 && viewH > 0 && dispW > 0 && dispH > 0)) {
            throw new Error("ViewportTransform requires positive view/display dimensions");
        }
        this.viewW = viewW;
        this.viewH = viewH;
        this.dispW = dispW;
        this.dispH = dispH;
        this.maxScale = maxScale;
        this.scale = 1;
        this.tx = 0;
        this.ty = 0;
        this.reset();
    }

    /** Fit-to-screen scale, capped at 1 (never upscale by default — keeps the
     *  native display crisp; pinch past 1 is always available). */
    get fitScale() {
        return Math.min(this.viewW / this.dispW, this.viewH / this.dispH, 1);
    }

    get zoomedIn() { return this.scale > this.fitScale * 1.001; }

    reset() {
        this.scale = this.fitScale;
        this.tx = (this.viewW - this.dispW * this.scale) / 2;
        this.ty = (this.viewH - this.dispH * this.scale) / 2;
    }

    displayToView(x, y) {
        return { x: x * this.scale + this.tx, y: y * this.scale + this.ty };
    }

    viewToDisplay(x, y) {
        return { x: (x - this.tx) / this.scale, y: (y - this.ty) / this.scale };
    }

    /** Zoom by `factor` about the view-space point (cx, cy) — that display
     *  point stays put. Scale clamps to [fitScale, maxScale]. */
    zoomAt(cx, cy, factor) {
        const s0 = this.scale;
        const s1 = clampNum(s0 * factor, this.fitScale, this.maxScale);
        if (s1 !== s0) {
            const r = s1 / s0;
            this.tx = cx - (cx - this.tx) * r;
            this.ty = cy - (cy - this.ty) * r;
            this.scale = s1;
        }
        this.clampPan();
    }

    panBy(dx, dy) {
        this.tx += dx;
        this.ty += dy;
        this.clampPan();
    }

    /** Axis-wise: content smaller than the viewport centers; larger content
     *  may never expose a gap between its edge and the viewport edge. */
    clampPan() {
        const sw = this.dispW * this.scale;
        const sh = this.dispH * this.scale;
        this.tx = sw <= this.viewW
            ? (this.viewW - sw) / 2
            : clampNum(this.tx, this.viewW - sw, 0);
        this.ty = sh <= this.viewH
            ? (this.viewH - sh) / 2
            : clampNum(this.ty, this.viewH - sh, 0);
    }

    resize(viewW, viewH) {
        this.viewW = viewW;
        this.viewH = viewH;
        if (this.scale < this.fitScale) this.scale = this.fitScale;
        this.clampPan();
    }

    /** Splashtop edge-push: when the (display-space) cursor is dragged into
     *  the margin band at a zoomed-in viewport edge, return the pan delta
     *  (view px) that brings it back inside the band. {0,0} when not zoomed
     *  (clampPan would cancel it anyway) or when the cursor is comfortable. */
    edgePushDelta(cursorDispX, cursorDispY, margin = 36) {
        if (!this.zoomedIn) return { dx: 0, dy: 0 };
        const v = this.displayToView(cursorDispX, cursorDispY);
        let dx = 0;
        let dy = 0;
        if (v.x < margin) dx = margin - v.x;
        else if (v.x > this.viewW - margin) dx = (this.viewW - margin) - v.x;
        if (v.y < margin) dy = margin - v.y;
        else if (v.y > this.viewH - margin) dy = (this.viewH - margin) - v.y;
        return { dx, dy };
    }
}
