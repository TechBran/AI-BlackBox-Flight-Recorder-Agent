// Static guards for the Portal's HTML sinks (audit P01-chat-core-web-01/02/03,
// X04-security-clients-infra-06, P04-media-tasks-fx-web-05, P06-comms-web-14).
// Grep-level on purpose: they keep the single-sanitizer contract from being
// quietly re-broken by a new branch that assigns raw model text to innerHTML.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { resolve, relative } from "node:path";
import { PORTAL, REPO_ROOT } from "./helpers/portal-dom.mjs";
import { stripComments, innerHtmlAssignments, isSafeRhs } from "./helpers/sink-scan.mjs";

// Many Portal sources are CRLF; normalize so line-based patterns behave the same.
const readSource = (path) => readFileSync(path, "utf8").replace(/\r\n/g, "\n");
const src = (rel) => readSource(resolve(PORTAL, rel));

/** Every Portal .js file (modules, top-level pages, onboarding, cu-view). */
function portalJsFiles(dir = PORTAL) {
    const files = [];
    for (const ent of readdirSync(dir, { withFileTypes: true })) {
        if (ent.name === "vendor" || ent.name === "uploads" || ent.name === "node_modules") continue;
        const p = resolve(dir, ent.name);
        if (ent.isDirectory()) files.push(...portalJsFiles(p));
        else if (ent.name.endsWith(".js")) files.push(p);
    }
    return files;
}

for (const file of ["modules/chat-bubbles.js", "modules/markdown-renderer.js"]) {
    test(`${file}: every innerHTML assignment is static markup or sanitizer output`, () => {
        const code = stripComments(src(file));
        const bad = innerHtmlAssignments(code).filter((a) => !isSafeRhs(a.rhs));
        assert.deepEqual(bad.map((a) => `line ${a.line}: innerHTML ${a.op} ${a.rhs.slice(0, 120)}`), []);
        assert.doesNotMatch(code, /\.outerHTML\s*=|insertAdjacentHTML|document\.write/);
    });
}

test("the guard itself catches the old raw branches", () => {
    const old = "if (containsMedia) {\n    span.innerHTML = contentString;\n    tempDiv.innerHTML = newContentString;\n}";
    assert.equal(innerHtmlAssignments(old).filter((a) => !isSafeRhs(a.rhs)).length, 2);
    assert.equal(isSafeRhs("sanitizeAssistantHtml(contentString)"), true);
    assert.equal(isSafeRhs("`<b>${x}</b>`"), false);
});

test("DOMPurify is only ever called from Portal/modules/sanitize.js (one choke point, no fail-open fallbacks)", () => {
    const offenders = [];
    for (const file of portalJsFiles()) {
        const rel = relative(REPO_ROOT, file);
        if (rel === "Portal/modules/sanitize.js") continue;
        const code = stripComments(readSource(file));
        if (/DOMPurify/.test(code)) offenders.push(rel);
    }
    assert.deepEqual(offenders, []);
    assert.doesNotMatch(stripComments(src("modules/sanitize.js")), /typeof DOMPurify\s*!==?\s*['"]undefined['"]\s*\?/);
});

test("raw marked output never reaches innerHTML (use markdownToSafeHtml / renderMarkdown)", () => {
    const offenders = [];
    for (const file of portalJsFiles()) {
        const code = stripComments(readSource(file));
        for (const a of innerHtmlAssignments(code)) {
            if (/\bmarked\.parse\(/.test(a.rhs) || /^parsed\b/.test(a.rhs)) {
                offenders.push(`${relative(REPO_ROOT, file)}:${a.line}`);
            }
        }
        if (/\bmarked\.parse\(/.test(code) && !file.endsWith("markdown-renderer.js")) {
            offenders.push(`${relative(REPO_ROOT, file)}: calls marked.parse outside markdown-renderer.js`);
        }
    }
    assert.deepEqual(offenders, []);
});

test("extractSpeakableText parses with DOMParser, not innerHTML on a live element", () => {
    const code = stripComments(src("modules/tts-stt.js"));
    const fn = code.slice(code.indexOf("export function extractSpeakableText"));
    const body = fn.slice(0, fn.indexOf("\n}\n") + 2);
    assert.doesNotMatch(body, /innerHTML/);
    assert.match(body, /htmlToText\(/);
});
