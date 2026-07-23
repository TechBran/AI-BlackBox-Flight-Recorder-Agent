/**
 * keysyms.js — PURE X11 keysym tables + chord builder for the CU live-view
 * extra-keys bar and manual keyboard (design 2026-07-23 §4.3/§4.4).
 *
 * Delivery is RFB keysym events (rfb.sendKey), NOT POST /browser/key — the
 * REST path targets the wrong display for virtual sessions and its
 * [a-zA-Z0-9+_] allowlist can't express arrows/F-keys/Super (design D2).
 *
 * Values are from X11 keysymdef.h. Modifiers use the LEFT-hand variants
 * (what xdotool and physical keyboards send by default).
 */

export const KEYSYMS = Object.freeze({
    Escape: 0xff1b,
    Tab: 0xff09,
    Return: 0xff0d,
    BackSpace: 0xff08,
    Delete: 0xffff,
    Insert: 0xff63,
    Home: 0xff50,
    Left: 0xff51,
    Up: 0xff52,
    Right: 0xff53,
    Down: 0xff54,
    PageUp: 0xff55,
    PageDown: 0xff56,
    End: 0xff57,
    Control_L: 0xffe3,
    Shift_L: 0xffe1,
    Alt_L: 0xffe9,
    Super_L: 0xffeb,
    F1: 0xffbe, F2: 0xffbf, F3: 0xffc0, F4: 0xffc1,
    F5: 0xffc2, F6: 0xffc3, F7: 0xffc4, F8: 0xffc5,
    F9: 0xffc6, F10: 0xffc7, F11: 0xffc8, F12: 0xffc9,
});

/** Sticky-modifier name → keysym (the names double as UI state keys). */
export const MODIFIER_KEYSYMS = Object.freeze({
    Control: KEYSYMS.Control_L,
    Shift: KEYSYMS.Shift_L,
    Alt: KEYSYMS.Alt_L,
    Super: KEYSYMS.Super_L,
});

/**
 * Map a single character (from the manual-keyboard input) to a keysym.
 * X11 rules: printable ASCII + Latin-1 = the codepoint itself; anything
 * >= U+0100 = 0x01000000 | codepoint (Unicode keysym range). Newline/CR →
 * Return, Tab → Tab, other control chars → null (unsendable).
 */
export function keysymForChar(ch) {
    if (!ch) return null;
    const cp = ch.codePointAt(0);
    if (cp === 0x0a || cp === 0x0d) return KEYSYMS.Return;
    if (cp === 0x09) return KEYSYMS.Tab;
    if ((cp >= 0x20 && cp <= 0x7e) || (cp >= 0xa0 && cp <= 0xff)) return cp;
    if (cp >= 0x100) return 0x01000000 | cp;
    return null;
}

/**
 * Build the RFB key-event sequence for a key press with sticky modifiers:
 * modifiers down in order, key down+up, modifiers up in reverse. Unknown
 * modifier names are skipped (never sent as garbage keysyms).
 * Returns [{keysym, down}, ...].
 */
export function buildKeySequence(keysym, mods = []) {
    const known = mods.filter((m) => MODIFIER_KEYSYMS[m] !== undefined);
    const seq = [];
    for (const m of known) seq.push({ keysym: MODIFIER_KEYSYMS[m], down: true });
    seq.push({ keysym, down: true }, { keysym, down: false });
    for (let i = known.length - 1; i >= 0; i--) {
        seq.push({ keysym: MODIFIER_KEYSYMS[known[i]], down: false });
    }
    return seq;
}

/**
 * Extra-keys bar layout (cloned from the cli-agents extra-keys pattern:
 * pinned Esc outside the scrollable strip, sticky modifiers, selectable
 * rows). `mod` entries render as sticky-modifier buttons; everything else
 * fires buildKeySequence(keysym, engagedMods) on tap.
 */
export const EXTRA_KEY_ROWS = Object.freeze([
    {
        id: "core",
        keys: [
            { label: "Esc", aria: "Escape", keysym: KEYSYMS.Escape, pinned: true },
            { label: "Tab", aria: "Tab", keysym: KEYSYMS.Tab },
            { label: "Ctrl", aria: "Control (sticky)", mod: "Control" },
            { label: "Alt", aria: "Alt (sticky)", mod: "Alt" },
            { label: "Sup", aria: "Super (sticky)", mod: "Super" },
            { label: "Shift", aria: "Shift (sticky)", mod: "Shift" },
        ],
    },
    {
        id: "nav",
        keys: [
            { label: "↑", aria: "Arrow up", keysym: KEYSYMS.Up },
            { label: "↓", aria: "Arrow down", keysym: KEYSYMS.Down },
            { label: "←", aria: "Arrow left", keysym: KEYSYMS.Left },
            { label: "→", aria: "Arrow right", keysym: KEYSYMS.Right },
            { label: "Home", aria: "Home", keysym: KEYSYMS.Home },
            { label: "End", aria: "End", keysym: KEYSYMS.End },
            { label: "PgUp", aria: "Page up", keysym: KEYSYMS.PageUp },
            { label: "PgDn", aria: "Page down", keysym: KEYSYMS.PageDown },
            { label: "Del", aria: "Delete", keysym: KEYSYMS.Delete },
            { label: "⏎", aria: "Enter", keysym: KEYSYMS.Return },
            { label: "⌫", aria: "Backspace", keysym: KEYSYMS.BackSpace },
        ],
    },
    {
        id: "fn",
        collapsible: true,
        keys: [
            { label: "F1", aria: "F1", keysym: KEYSYMS.F1 },
            { label: "F2", aria: "F2", keysym: KEYSYMS.F2 },
            { label: "F3", aria: "F3", keysym: KEYSYMS.F3 },
            { label: "F4", aria: "F4", keysym: KEYSYMS.F4 },
            { label: "F5", aria: "F5", keysym: KEYSYMS.F5 },
            { label: "F6", aria: "F6", keysym: KEYSYMS.F6 },
            { label: "F7", aria: "F7", keysym: KEYSYMS.F7 },
            { label: "F8", aria: "F8", keysym: KEYSYMS.F8 },
            { label: "F9", aria: "F9", keysym: KEYSYMS.F9 },
            { label: "F10", aria: "F10", keysym: KEYSYMS.F10 },
            { label: "F11", aria: "F11", keysym: KEYSYMS.F11 },
            { label: "F12", aria: "F12", keysym: KEYSYMS.F12 },
        ],
    },
]);
