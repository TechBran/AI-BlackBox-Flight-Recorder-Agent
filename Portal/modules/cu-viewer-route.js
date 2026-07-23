/**
 * cu-viewer-route.js — stream-vs-fallback routing for the CU viewer
 * (design 2026-07-23 §7.1 / §8-M4 "Portal integration").
 *
 * Every Portal entry point that used to open the screenshot-poll modal
 * (cu-interact.js) directly now routes through openCuViewer():
 *
 *   - STREAM: the Splashtop-style live client (iframe of /cu/view/{sid},
 *     the M2 served asset) when the target is a live VIRTUAL session —
 *     i.e. it is listed in /cu/sessions with live_view=true (websockify +
 *     noVNC present; the same box-level state /cu/preflight reports as the
 *     `live_view` check).
 *   - FALLBACK (D8 — kept permanently, automatic): the screenshot-poll
 *     modal for native mode, remote devices, no-websockify boxes, ended /
 *     unlisted sessions, and any fetch failure. Never a dead panel
 *     (fresh-box gate).
 *
 * chooseCuViewer() is PURE (no DOM / no network) so the decision logic is
 * node-testable (cu-viewer-route.test.mjs); openCuViewer() is the thin
 * impure wrapper. Consumers import this module dynamically, so pulling it
 * in never front-loads cu-interact/cu-live-view.
 */

/**
 * Decide which CU viewer to open from a /cu/sessions payload.
 *
 * @param {object|null} status - /cu/sessions response
 *        ({active, count, cap, sessions:[to_public…]}) or null on fetch failure.
 * @param {object} [opts]
 * @param {string} [opts.sessionId] - Target CU session id (drawer selection /
 *        cu_session SSE). When given, only THAT session may stream — an
 *        unlisted id falls back rather than streaming someone else's session.
 * @param {string} [opts.deviceId]  - CU target device. Remote devices
 *        (VNC/Android) have no local virtual display: the stream only exists
 *        for the box's own Xvfb quartet, so they always use the fallback.
 * @returns {{mode:'stream', session:object}|{mode:'fallback', reason:string}}
 */
export function chooseCuViewer(status, opts = {}) {
    const deviceId = opts.deviceId;
    if (deviceId && deviceId !== "blackbox" && deviceId !== "local") {
        return { mode: "fallback", reason: "remote-device" };
    }

    const sessions = (status && Array.isArray(status.sessions)) ? status.sessions : [];
    if (!sessions.length) return { mode: "fallback", reason: "no-sessions" };

    const streamable = (s) => !!(s && s.live_view && s.view_url);

    if (opts.sessionId) {
        const match = sessions.find((s) => s && s.session_id === opts.sessionId);
        if (match) {
            return streamable(match)
                ? { mode: "stream", session: match }
                : { mode: "fallback", reason: "stream-unavailable" };
        }
        // The caller's session is not a live virtual session (native mode,
        // ended, or TTL-reaped). Never silently stream a DIFFERENT session.
        return { mode: "fallback", reason: "session-not-listed" };
    }

    const first = sessions.find(streamable);
    return first
        ? { mode: "stream", session: first }
        : { mode: "fallback", reason: "stream-unavailable" };
}

/**
 * Open the right CU viewer: streaming client when available, screenshot-poll
 * modal otherwise. Resolution is LIVE per open — sessions rotate, so nothing
 * is cached here.
 *
 * @param {object} [opts]
 * @param {string} [opts.sessionId]     - Target CU session id (optional).
 * @param {string} [opts.deviceId]      - CU device id; forwarded to the
 *        fallback modal as its device override (T12 semantics preserved).
 * @param {string} [opts.screenshotUrl] - Initial image for the fallback modal.
 * @returns {Promise<{mode:string, reason?:string, session?:object}>} the
 *        routing decision that was acted on (handy for logging/tests).
 */
export async function openCuViewer(opts = {}) {
    let status = null;
    try {
        const r = await fetch("/cu/sessions", { cache: "no-store" });
        if (r.ok) status = await r.json();
    } catch { /* offline / endpoint missing → fallback via empty status */ }

    const choice = chooseCuViewer(status, opts);

    if (choice.mode === "stream") {
        try {
            const { openStreamPanel } = await import("./cu-live-view.js");
            openStreamPanel(choice.session);
            return choice;
        } catch (e) {
            // Degraded, never dead: any stream-open failure lands on the modal.
            console.warn("[CU-Route] stream panel failed, using fallback:", e);
            choice.mode = "fallback";
            choice.reason = "stream-open-failed";
        }
    }

    const cuInteract = await import("./cu-interact.js");
    cuInteract.open(opts.screenshotUrl, opts.deviceId);
    return choice;
}
