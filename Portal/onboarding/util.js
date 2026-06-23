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
