/**
 * switcher.js — PURE state helpers for the CU live-view switcher rail
 * (N2 — main-desktop switcher). The served client (cu-view.js) renders a
 * compact rail listing [Main desktop] + every live session from ONE
 * /cu/sessions payload (which carries the additive "main" availability key)
 * and swaps the RFB stream in place when an entry is tapped.
 *
 * No DOM, no network, no timers — everything here is a pure function of a
 * /cu/sessions payload, node-tested in switcher.test.mjs. cu-view.js owns
 * the impure half (fetch/poll, RFB reconnect, history.replaceState).
 */

/** Reserved id for the REAL desktop stream (/cu/view/main, never a session). */
export const MAIN_ID = "main";

const DEFAULT_W = 1280;
const DEFAULT_H = 720;
const SHORT_ID_LEN = 9;

/** Compact session id for the rail label. */
export function shortId(id) {
    const s = String(id);
    return s.length > SHORT_ID_LEN ? `${s.slice(0, SHORT_ID_LEN)}…` : s;
}

/** Parse a probe "WxH" resolution string → {width, height} | null. */
export function parseResolution(res) {
    if (typeof res !== "string") return null;
    const m = /^(\d+)\s*[x×]\s*(\d+)$/.exec(res.trim());
    if (!m) return null;
    return { width: Number(m[1]), height: Number(m[2]) };
}

/**
 * Build the rail's entry list from a /cu/sessions payload.
 *
 * @param {object|null} payload - /cu/sessions response
 *        ({sessions:[to_public…], main:{available, display?, resolution?,
 *        reason?}}) or null on fetch failure.
 * @param {string} currentId - the page's current stream target ("main" or a
 *        session id) — exactly one entry gets current=true when it matches.
 * @returns {Array<{id:string, kind:'main'|'session', label:string,
 *        available:boolean, reason:string, current:boolean}>}
 *        [Main desktop] always first (disabled with the probe's reason when
 *        the real desktop is not streamable), then every listed session in
 *        payload order (disabled when its live_view pipeline is absent).
 */
export function buildSwitcherEntries(payload, currentId) {
    const sessions = (payload && Array.isArray(payload.sessions))
        ? payload.sessions : [];
    const main = (payload && payload.main && typeof payload.main === "object")
        ? payload.main : { available: false, reason: "main desktop status unknown" };

    const mainAvailable = main.available === true;
    const entries = [{
        id: MAIN_ID,
        kind: "main",
        label: "Main desktop"
            + (typeof main.resolution === "string" && main.resolution
                ? ` · ${main.resolution}` : ""),
        available: mainAvailable,
        reason: mainAvailable
            ? "" : String(main.reason || "main desktop unavailable"),
        current: currentId === MAIN_ID,
    }];

    for (const s of sessions) {
        if (!s || !s.session_id) continue;
        const live = s.live_view === true;
        const res = (s.width && s.height) ? ` · ${s.width}×${s.height}` : "";
        entries.push({
            id: s.session_id,
            kind: "session",
            label: `${s.backend || "cu"} · ${shortId(s.session_id)}${res}`,
            available: live,
            reason: live ? "" : "live view unavailable for this session",
            current: currentId === s.session_id,
        });
    }
    return entries;
}

/**
 * Where a rail tap swaps the stream to: the view page path (for the
 * history.replaceState deep-link update) and the RFB WS proxy path (the
 * caller prefixes ws(s)://host). The reserved "main" id passes through
 * verbatim; real session ids are URL-encoded.
 */
export function resolveSwapTarget(id) {
    const enc = id === MAIN_ID ? MAIN_ID : encodeURIComponent(String(id));
    return { id, viewPath: `/cu/view/${enc}`, wsPath: `/cu/view/${enc}/ws` };
}

/**
 * Session metadata for a stream target — the viewer's sizing source of truth
 * (§3.4). Main: parsed from the probe's resolution (1280x720 fallback —
 * informational absence never gates). Session: its listed native geometry.
 * Unknown session id → null (caller keeps its previous dims).
 */
export function sessionMetaFor(payload, id) {
    if (id === MAIN_ID) {
        const main = (payload && payload.main) || {};
        const dims = parseResolution(main.resolution)
            || { width: DEFAULT_W, height: DEFAULT_H };
        return { ...dims, backend: "main", operator: "" };
    }
    const sessions = (payload && Array.isArray(payload.sessions))
        ? payload.sessions : [];
    const s = sessions.find((x) => x && x.session_id === id);
    if (!s) return null;
    return {
        width: s.width || DEFAULT_W,
        height: s.height || DEFAULT_H,
        backend: s.backend || "cu",
        operator: s.operator || "",
    };
}

/**
 * Reconnect gate: is the current target still worth redialing? A null
 * payload (fetch failed — restart race) always answers true so a transient
 * outage retries instead of declaring the stream ended. Main gates on the
 * probe's availability; sessions on still being listed.
 */
export function targetStillListed(payload, id) {
    if (payload == null) return true;
    if (id === MAIN_ID) return !!(payload.main && payload.main.available);
    const sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
    return sessions.some((s) => s && s.session_id === id);
}
