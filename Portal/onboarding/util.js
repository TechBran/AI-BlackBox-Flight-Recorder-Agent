// Shared onboarding-wizard utilities. Keep this dependency-free and free of
// import-time side effects (no DOM, no state) so any step module — or the
// hub/status modules — can import it without ordering concerns. DOM helpers
// live in hint.js.

// Escape an arbitrary string for safe interpolation into a CSS attribute
// selector (e.g. `[data-slug="${cssEscape(slug)}"]`). Backslash-escapes
// every char outside [A-Za-z0-9_-]. Sufficient for the wizard's slug/id
// values; not a full CSS.escape polyfill (we don't need leading-digit or
// control-char handling here).
export function cssEscape(s) {
    return String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
}

// Escape a value for use as HTML element content or inside a QUOTED
// attribute value. The wizard is served from its own /onboarding/ mount, so
// it cannot import /ui/modules/core-utils.js; this must escape exactly like
// core-utils' escapeHtml/escapeAttr (Portal/onboarding/escape.test.mjs pins
// it): all five of & < > " ', null/undefined -> "". Never for unquoted
// attributes or inline handlers.
const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHtml(value) {
    if (value === null || value === undefined) return "";
    return String(value).replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]);
}

export function escapeAttr(value) {
    return escapeHtml(value);
}

// GET (or `init`) a JSON endpoint: the parsed body, or null on ANY failure
// (network, non-2xx, bad JSON) — a step renders its fallback instead of
// throwing. One copy for the steps (they had six; local_models' needed the
// fetch options, e.g. { cache: "no-store" }).
export async function fetchJson(url, init) {
    try {
        const r = await fetch(url, init);
        if (!r.ok) return null;
        return await r.json();
    } catch (_) {
        return null;
    }
}

// The reason a failed response gives: its JSON `detail` when that is a
// non-empty string (FastAPI's error field), else "HTTP <status>".
export async function safeDetail(r) {
    try {
        const body = await r.json();
        return body && typeof body.detail === "string" && body.detail ? body.detail : `HTTP ${r.status}`;
    } catch (_) {
        return `HTTP ${r.status}`;
    }
}
