// Grep gate over the Portal's production code (audit rank 37/38, P01-chat-core-web-14,
// P03-live-agents-web-11, P06-comms-web-02): no operator name and no pinned retired
// model id may ship. The operator comes from GET /operators and "Auto" (an empty
// model) lets the server pick its configured default; a name or id in either place
// tags a fresh box's ledger with someone who is not there, or sends a stale model the
// box never chose. Comments may still credit people by name.
//
// The scan reads what a browser runs or renders, with comments blanked: every line of
// JS (string literals, template literals of any length, identifiers), HTML markup text
// and attributes, inline <script> and CSS. The name is matched in any case.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { PORTAL, REPO_ROOT } from "./helpers/portal-dom.mjs";
import { stripComments } from "./helpers/sink-scan.mjs";

// Served to browsers, so scanned. vendor/ is third-party, dev/ holds design mocks,
// uploads/ is user data.
const SKIP_DIRS = new Set(["node_modules", "vendor", "dev", "uploads"]);
const SOURCE = /\.(?:m?js|html|css|json|webmanifest)$/;
const TEST_FILE = /\.test\.m?js$/;

// The owner's seed operator name is the one that leaked into every fallback.
const OPERATOR_NAME = /(?<![A-Za-z])brandon(?![A-Za-z])/i;
// Not served by /models/anthropic or /models/xai (2026-10-17): the undated 4.5 aliases
// and the retired Grok 3 Mini beta. A dated live id (claude-sonnet-4-5-20250929) is
// not one of them.
const STALE_MODEL_ID = /(?<![\w.-])(?:claude-sonnet-4-5|claude-haiku-4-5|grok-3-mini-beta)(?![\w.-])/;

function productionFiles(dir = PORTAL, out = []) {
    for (const name of readdirSync(dir)) {
        const path = join(dir, name);
        if (statSync(path).isDirectory()) {
            if (!SKIP_DIRS.has(name)) productionFiles(path, out);
        } else if (SOURCE.test(name) && !TEST_FILE.test(name)) {
            out.push(path);
        }
    }
    return out;
}

/** A comment's text blanked, its line breaks kept (so line numbers still match). */
const blank = (comment) => comment.replace(/[^\n]/g, "");
const blankCssComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, blank);

/** What a browser runs or renders from [text] (a file named [name]), comments blanked. */
function codeText(name, text) {
    if (/\.m?js$/.test(name)) return stripComments(text, { keepNewlines: true });
    if (/\.css$/.test(name)) return blankCssComments(text);
    if (/\.html$/.test(name)) {
        return text
            .replace(/<!--[\s\S]*?-->/g, blank)
            .replace(/(<script\b[^>]*>)([\s\S]*?)(<\/script\s*>)/gi,
                (_, open, body, close) => open + stripComments(body, { keepNewlines: true }) + close)
            .replace(/(<style\b[^>]*>)([\s\S]*?)(<\/style\s*>)/gi,
                (_, open, body, close) => open + blankCssComments(body) + close);
    }
    return text;
}

/** 1-based numbers of the lines of [text] whose code matches [rx]. */
function matchingLines(name, text, rx) {
    return codeText(name, text).split("\n").flatMap((line, i) => (rx.test(line) ? [i + 1] : []));
}

/** `file:line: code` for every production line whose code matches [rx]. */
function hits(rx) {
    const out = [];
    for (const path of productionFiles()) {
        const lines = readFileSync(path, "utf8").split("\n");
        for (const n of matchingLines(path, lines.join("\n"), rx)) {
            out.push(`${relative(REPO_ROOT, path)}:${n}: ${lines[n - 1].trim().slice(0, 140)}`);
        }
    }
    return out;
}

test("the scan covers the Portal modules, pages, styles and onboarding", () => {
    const files = productionFiles().map((p) => relative(PORTAL, p));
    for (const f of ["index.html", "modules/state-management.js", "modules/agent-handler.js",
        "modules/sms-inbox.js", "styles/main.css", "onboarding/onboarding.css"]) {
        assert.ok(files.includes(f), `${f} is scanned`);
    }
    assert.ok(!files.some((f) => TEST_FILE.test(f)), "tests are not production code");
});

test("no operator name is hard-coded in Portal production code", () => {
    assert.deepEqual(hits(OPERATOR_NAME), []);
});

test("window.__operator is gone: every reader asks getOperator()", () => {
    assert.deepEqual(hits(/__operator/), []);
});

test("no picker or SMS default pins an id that is not on the live catalogs", () => {
    // Pickers offer Auto or a live id; the SMS inbox sends "" (the box's default).
    assert.deepEqual(hits(STALE_MODEL_ID), []);
});

// ── the gate's own reach (planted samples, never production files) ───────
test("the gate sees the name wherever a browser would use it", () => {
    const html = [
        `<select><option value=Brandon>Brandon</option></select>`,  // 1 unquoted markup
        `<p title="Owner: BRANDON">x</p>`,                           // 2 attribute, any case
        `<script>const op = localStorage.op || 'brandon';</script>`, // 3 inline script
        `<style>.who::after { content: "Brandon"; }</style>`,       // 4 inline CSS
    ].join("\n");
    assert.deepEqual(matchingLines("page.html", html, OPERATOR_NAME), [1, 2, 3, 4]);

    const js = [
        "const a = getOperator() || 'Brandon';",        // 1 quoted literal
        "const t = `Hello,",                           // 2 template literal ...
        "Brandon",                                     // 3 ... the name on its own line
        "`;",                                          // 4
        `const u = "x // y" || 'Brandon';`,            // 5 a // inside a string is not a comment
        "const brandon = 1;",                          // 6 an identifier
        "const s = 'Brandon__claude__root';",          // 7 inside a session-style id
    ].join("\n");
    assert.deepEqual(matchingLines("mod.js", js, OPERATOR_NAME), [1, 3, 5, 6, 7]);
    assert.deepEqual(matchingLines("tokens.css", ".x::before { content: 'Brandon'; }", OPERATOR_NAME), [1]);
});

test("the gate leaves comments alone, so people can still be credited", () => {
    const js = [
        "// Brandon asked for this (2026-07-24)",
        "/* Brandon's",
        "   layout pass */ const a = 1;",
        "const b = 2; // per Brandon",
        "/**",
        " * Brandon: keep the confirm dialog.",
        " */",
        "const url = 'https://example.test/x'; // Brandon",
    ].join("\n");
    assert.deepEqual(matchingLines("mod.js", js, OPERATOR_NAME), []);
    const html = "<!-- Brandon's fit pass -->\n<script>\n// Brandon\nconst x = 1; /* Brandon */\n</script>";
    assert.deepEqual(matchingLines("page.html", html, OPERATOR_NAME), []);
    assert.deepEqual(matchingLines("a.css", "/* per Brandon\n 2026-05-20 */\n.a { color: red; }", OPERATOR_NAME), []);
});

test("the gate tells a stale alias from a live dated id", () => {
    const js = [
        "const a = 'claude-sonnet-4-5';",
        "const b = 'claude-sonnet-4-5-20250929';",
        "const c = `anthropic:claude-haiku-4-5`;",
        "const d = { id: \"grok-3-mini-beta\" };",
        "const e = 'claude-sonnet-4-6';",
    ].join("\n");
    assert.deepEqual(matchingLines("mod.js", js, STALE_MODEL_ID), [1, 3, 4]);
});
