// Unit tests for the CU live-view extra-keys keysym mapping (design doc
// 2026-07-23 §4.3, milestone M2/M3 slice). Pure module — no DOM.
//
// Run: node --test Portal/cu-view/keysyms.test.mjs
import test from "node:test";
import assert from "node:assert/strict";

import {
    KEYSYMS, MODIFIER_KEYSYMS, EXTRA_KEY_ROWS,
    keysymForChar, buildKeySequence,
} from "./keysyms.js";

// ── X11 keysym constants (authoritative values from keysymdef.h) ─────────

test("core keysym values match X11 keysymdef", () => {
    assert.equal(KEYSYMS.Escape, 0xff1b);
    assert.equal(KEYSYMS.Tab, 0xff09);
    assert.equal(KEYSYMS.Return, 0xff0d);
    assert.equal(KEYSYMS.BackSpace, 0xff08);
    assert.equal(KEYSYMS.Delete, 0xffff);
    assert.equal(KEYSYMS.Home, 0xff50);
    assert.equal(KEYSYMS.Left, 0xff51);
    assert.equal(KEYSYMS.Up, 0xff52);
    assert.equal(KEYSYMS.Right, 0xff53);
    assert.equal(KEYSYMS.Down, 0xff54);
    assert.equal(KEYSYMS.PageUp, 0xff55);
    assert.equal(KEYSYMS.PageDown, 0xff56);
    assert.equal(KEYSYMS.End, 0xff57);
});

test("modifier keysyms are the LEFT-hand variants", () => {
    assert.equal(MODIFIER_KEYSYMS.Control, 0xffe3);
    assert.equal(MODIFIER_KEYSYMS.Shift, 0xffe1);
    assert.equal(MODIFIER_KEYSYMS.Alt, 0xffe9);
    assert.equal(MODIFIER_KEYSYMS.Super, 0xffeb);
});

test("F1-F12 are sequential from 0xffbe", () => {
    for (let i = 1; i <= 12; i++) {
        assert.equal(KEYSYMS[`F${i}`], 0xffbe + (i - 1), `F${i}`);
    }
});

// ── char → keysym (typing path for the manual keyboard) ──────────────────

test("printable ASCII and Latin-1 map to their codepoints", () => {
    assert.equal(keysymForChar("a"), 0x61);
    assert.equal(keysymForChar("A"), 0x41);
    assert.equal(keysymForChar("/"), 0x2f);
    assert.equal(keysymForChar(" "), 0x20);
    assert.equal(keysymForChar("~"), 0x7e);
    assert.equal(keysymForChar("é"), 0xe9);
});

test("codepoints >= 0x100 use the X11 Unicode keysym rule (0x01000000 | cp)", () => {
    assert.equal(keysymForChar("€"), 0x01000000 | 0x20ac);
    assert.equal(keysymForChar("→"), 0x01000000 | 0x2192);
});

test("newline/tab map to their control keysyms; other control chars are null", () => {
    assert.equal(keysymForChar("\n"), KEYSYMS.Return);
    assert.equal(keysymForChar("\r"), KEYSYMS.Return);
    assert.equal(keysymForChar("\t"), KEYSYMS.Tab);
    assert.equal(keysymForChar("\x07"), null);
    assert.equal(keysymForChar(""), null);
    assert.equal(keysymForChar(undefined), null);
});

// ── sticky-modifier chord sequences ──────────────────────────────────────

test("bare key = down then up", () => {
    assert.deepEqual(buildKeySequence(KEYSYMS.Escape), [
        { keysym: 0xff1b, down: true },
        { keysym: 0xff1b, down: false },
    ]);
});

test("modifiers wrap the key: down in order, up in reverse", () => {
    const seq = buildKeySequence(KEYSYMS.Up, ["Control", "Shift"]);
    assert.deepEqual(seq, [
        { keysym: MODIFIER_KEYSYMS.Control, down: true },
        { keysym: MODIFIER_KEYSYMS.Shift, down: true },
        { keysym: KEYSYMS.Up, down: true },
        { keysym: KEYSYMS.Up, down: false },
        { keysym: MODIFIER_KEYSYMS.Shift, down: false },
        { keysym: MODIFIER_KEYSYMS.Control, down: false },
    ]);
});

test("unknown modifier names are skipped, not sent as garbage", () => {
    const seq = buildKeySequence(KEYSYMS.Tab, ["Hyper", "Control"]);
    assert.deepEqual(seq.map((s) => s.keysym),
        [MODIFIER_KEYSYMS.Control, KEYSYMS.Tab, KEYSYMS.Tab, MODIFIER_KEYSYMS.Control]);
});

// ── extra-keys bar layout contract ───────────────────────────────────────

test("row layout: core row pins Esc and carries the four sticky modifiers", () => {
    const core = EXTRA_KEY_ROWS.find((r) => r.id === "core");
    assert.ok(core, "core row exists");
    assert.equal(core.keys[0].label, "Esc");
    assert.equal(core.keys[0].pinned, true);
    assert.equal(core.keys[0].keysym, KEYSYMS.Escape);
    const mods = core.keys.filter((k) => k.mod).map((k) => k.mod);
    assert.deepEqual(mods.sort(), ["Alt", "Control", "Shift", "Super"].sort());
});

test("row layout: nav row has arrows + Home/End/PgUp/PgDn/Del/Enter/Backspace", () => {
    const nav = EXTRA_KEY_ROWS.find((r) => r.id === "nav");
    const syms = nav.keys.map((k) => k.keysym);
    for (const want of [KEYSYMS.Up, KEYSYMS.Down, KEYSYMS.Left, KEYSYMS.Right,
                        KEYSYMS.Home, KEYSYMS.End, KEYSYMS.PageUp, KEYSYMS.PageDown,
                        KEYSYMS.Delete, KEYSYMS.Return, KEYSYMS.BackSpace]) {
        assert.ok(syms.includes(want), `nav row includes 0x${want.toString(16)}`);
    }
});

test("row layout: fn row is collapsible and holds exactly F1-F12 in order", () => {
    const fn = EXTRA_KEY_ROWS.find((r) => r.id === "fn");
    assert.equal(fn.collapsible, true);
    assert.equal(fn.keys.length, 12);
    fn.keys.forEach((k, i) => {
        assert.equal(k.label, `F${i + 1}`);
        assert.equal(k.keysym, 0xffbe + i);
    });
});

test("every non-modifier key in every row has a numeric keysym", () => {
    for (const row of EXTRA_KEY_ROWS) {
        for (const k of row.keys) {
            if (k.mod) {
                assert.ok(MODIFIER_KEYSYMS[k.mod], `known modifier ${k.mod}`);
            } else {
                assert.equal(typeof k.keysym, "number", `${k.label} has keysym`);
            }
        }
    }
});
