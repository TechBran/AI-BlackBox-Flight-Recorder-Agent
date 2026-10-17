// Shared onboarding-wizard utilities. Keep this dependency-free and
// side-effect-free (pure functions only) so any step module — or the
// future hub/status modules — can import it without ordering concerns.

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
