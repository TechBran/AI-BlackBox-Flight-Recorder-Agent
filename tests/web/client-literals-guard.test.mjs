// Grep gate over the Portal's production code (audit rank 37/38, P01-chat-core-web-14,
// P03-live-agents-web-11, P06-comms-web-02): no operator name and no pinned retired
// model id may ship as a string literal. The operator comes from GET /operators and
// "Auto" (an empty model) lets the server pick its configured default; a literal in
// either place tags a fresh box's ledger with someone who is not there, or sends a
// stale model the box never chose. Comments may still credit people by name.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { PORTAL, REPO_ROOT } from "./helpers/portal-dom.mjs";

// Served to browsers, so scanned. vendor/ is third-party, dev/ holds design mocks,
// uploads/ is user data.
const SKIP_DIRS = new Set(["node_modules", "vendor", "dev", "uploads"]);
const SOURCE = /\.(?:m?js|html)$/;
const TEST_FILE = /\.test\.m?js$/;

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

/** Code lines only: whole-line comments and HTML comments are prose, not literals. */
function codeLines(path) {
    const text = readFileSync(path, "utf8").replace(/<!--[\s\S]*?-->/g, (c) => c.replace(/[^\n]/g, " "));
    return text.split(/\r?\n/).map((line, i) => {
        const trimmed = line.trim();
        if (trimmed.startsWith("//") || trimmed.startsWith("*") || trimmed.startsWith("/*")) return [i + 1, ""];
        // A trailing `// ...` after code (never the `//` inside a URL like https://).
        return [i + 1, line.replace(/\s\/\/\s.*$/, "")];
    });
}

/** Every quoted string literal on a code line whose text matches [rx]. */
function literalHits(rx) {
    const hits = [];
    const literal = /(["'`])((?:\\.|(?!\1)[^\\\n])*)\1/g;
    for (const path of productionFiles()) {
        for (const [n, line] of codeLines(path)) {
            for (const m of line.matchAll(literal)) {
                if (rx.test(m[2])) hits.push(`${relative(REPO_ROOT, path)}:${n}: ${m[0]}`);
            }
        }
    }
    return hits;
}

test("the scan covers the Portal modules, pages and onboarding", () => {
    const files = productionFiles().map((p) => relative(PORTAL, p));
    for (const f of ["index.html", "modules/state-management.js", "modules/agent-handler.js", "modules/sms-inbox.js"]) {
        assert.ok(files.includes(f), `${f} is scanned`);
    }
    assert.ok(!files.some((f) => TEST_FILE.test(f)), "tests are not production code");
});

test("no operator name is hard-coded in Portal production code", () => {
    // The owner's seed operator name is the one that leaked into every fallback.
    assert.deepEqual(literalHits(/\bBrandon\b/), []);
});

test("window.__operator is gone: every reader asks getOperator()", () => {
    const hits = [];
    for (const path of productionFiles()) {
        for (const [n, line] of codeLines(path)) {
            if (line.includes("__operator")) hits.push(`${relative(REPO_ROOT, path)}:${n}`);
        }
    }
    assert.deepEqual(hits, []);
});

test("the SMS inbox never pins a model: empty means the server's default", () => {
    assert.deepEqual(literalHits(/^claude-sonnet-4-5$/), []);
});

test("no picker offers an id that is not on the live catalogs", () => {
    // Not served by /models/anthropic or /models/xai (2026-10-17): the undated 4.5
    // aliases and the retired Grok 3 Mini beta. Pickers offer Auto or a live id.
    assert.deepEqual(literalHits(/^(?:claude-sonnet-4-5|claude-haiku-4-5|grok-3-mini-beta)$/), []);
});

test("the gate itself sees a literal (self-check on a synthetic line)", () => {
    const literal = /(["'`])((?:\\.|(?!\1)[^\\\n])*)\1/g;
    const line = `const op = getOperator() || 'Brandon';`;
    assert.ok([...line.matchAll(literal)].some((m) => /\bBrandon\b/.test(m[2])));
});
