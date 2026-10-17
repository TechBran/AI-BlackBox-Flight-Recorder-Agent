// Static guards for the Portal's HTML sinks (audit P01-chat-core-web-01/02/03,
// X04-security-clients-infra-06, P04-media-tasks-fx-web-05, P06-comms-web-14).
// Grep-level on purpose: they keep the single-sanitizer contract from being
// quietly re-broken by a new branch that assigns raw model text to innerHTML.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { resolve, relative } from "node:path";
import { PORTAL, REPO_ROOT } from "./helpers/portal-dom.mjs";
import { stripComments, innerHtmlAssignments, isSafeRhs, rawHtmlApiUses } from "./helpers/sink-scan.mjs";

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

/** Every place `code` writes HTML that is NOT static markup or one sanitizer call. */
function unsafeHtmlWrites(code) {
    const stripped = stripComments(code);
    return [
        ...innerHtmlAssignments(stripped).filter((a) => !isSafeRhs(a.rhs))
            .map((a) => `line ${a.line}: innerHTML ${a.op} ${a.rhs.slice(0, 120)}`),
        ...rawHtmlApiUses(stripped).map((u) => `line ${u.line}: ${u.api}`),
    ];
}

for (const file of ["modules/chat-bubbles.js", "modules/markdown-renderer.js"]) {
    test(`${file}: every innerHTML assignment is static markup or sanitizer output`, () => {
        assert.deepEqual(unsafeHtmlWrites(src(file)), []);
    });
}

test("the guard itself catches the old raw branches", () => {
    const old = "if (containsMedia) {\n    span.innerHTML = contentString;\n    tempDiv.innerHTML = newContentString;\n}";
    assert.equal(innerHtmlAssignments(old).filter((a) => !isSafeRhs(a.rhs)).length, 2);
    assert.equal(isSafeRhs("sanitizeAssistantHtml(contentString)"), true);
    assert.equal(isSafeRhs("renderMarkdown(text || '')"), true);
    assert.equal(isSafeRhs("`<b>${x}</b>`"), false);
});

// Shapes that only LOOK sanitized, or reach the parser another way (bypass review of U2).
const GUARD_BYPASSES = {
    concatAfterCall: "span.innerHTML = sanitizeAssistantHtml(a) + contentString;",
    concatOnNextLine: "span.innerHTML = renderMarkdown(a)\n    + mediaHtml;",
    templateAfterCall: "span.innerHTML = plainTextHtml(a) + `${b}`;",
    orFallback: "span.innerHTML = sanitizeAssistantHtml(a) || contentString;",
    ternaryAfterCall: "span.innerHTML = markdownToSafeHtml(a) ? raw : '';",
    twoCalls: "span.innerHTML = sanitizeAssistantHtml(a)(contentString);",
    memberAfterCall: "span.innerHTML = sanitizeAssistantHtml(a).replace('x', raw);",
    bracketInner: "span['innerHTML'] = contentString;",
    bracketInnerDouble: 'span["innerHTML"] += contentString;',
    bracketOuter: "span[`outerHTML`] = contentString;",
    setHTMLUnsafe: "span.setHTMLUnsafe(contentString);",
    contextualFragment: "span.append(document.createRange().createContextualFragment(contentString));",
    parseHTMLUnsafe: "const d = Document.parseHTMLUnsafe(contentString);",
    insertAdjacent: "span.insertAdjacentHTML('beforeend', contentString);",
    outerHTML: "span.outerHTML = contentString;",
    documentWrite: "document.writeln(contentString);",
    srcdoc: "frame.srcdoc = contentString;",
};
for (const [name, code] of Object.entries(GUARD_BYPASSES)) {
    test(`the guard flags ${name}`, () => {
        assert.equal(unsafeHtmlWrites(code).length, 1, JSON.stringify(unsafeHtmlWrites(code)));
    });
}

test("the guard accepts the sanctioned shapes", () => {
    const ok = "span.innerHTML = sanitizeAssistantHtml(contentString);\n"
        + "span.innerHTML = renderMarkdown(\n    newContentString\n);\n"
        + "dots.innerHTML = '<span></span><span></span>';\n"
        + "x.innerHTML = '';\n"
        + "el.innerHTML = markdownToSafeHtml(a);\nconst y = 1;";
    assert.deepEqual(unsafeHtmlWrites(ok), []);
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

// Sinks the audit named (rank 26) that are not covered by a jsdom behaviour test.
// Each pattern matches the field interpolated straight into MARKUP: right after a
// tag's `>` (allowing a short literal prefix such as "/" or "• "), or as a quoted
// attribute value. Plain-text uses (toasts, input.value, textContent) are fine.
const RAW = (fields) => new RegExp(`(>|=["'])[^<"'\n]{0,3}\\$\\{(${fields})\\}`);
const NAMED_RAW_SINKS = [
    ["modules/grok-live.js", /<strong>You:<\/strong>\s*\$\{(?!escapeHtml\()/],
    ["modules/slash-commands.js", RAW("cmd\\.name|cmd\\.description \\|\\| ''|opt")],
    ["modules/cellular-manager.js", RAW("modem\\.operator_name \\|\\| 'No Carrier'|ev\\.detail \\|\\| ''|ev\\.type|tagStr|ip|r\\.interface \\|\\| '--'|r\\.gateway \\|\\| '--'")],
    ["modules/cellular-manager.js", /value="\$\{currentApn/],
    ["modules/media-manager.js", /onclick=|="\$\{file\.url\}"|\$\{err\.message\}/],
    ["modules/task-manager.js", /\$\{resultText\}|onclick="window\.open|src="\$\{(imgUrl|url|finalScreenshot)\}"/],
    ["modules/file-upload.js", RAW("file\\.name")],
];
for (const [file, pattern] of NAMED_RAW_SINKS) {
    test(`${file}: no raw interpolation of model/device/user text into markup`, () => {
        assert.doesNotMatch(stripComments(src(file)), pattern);
    });
}

// sanitize.js namespaces every id/name in model output as "user-content-<value>"
// (DOMPurify SANITIZE_NAMED_PROPS). That only stops DOM clobbering while the
// Portal never uses the prefix for its OWN elements or lookups.
test("no Portal page or module uses the user-content- id prefix reserved for model output", () => {
    const offenders = [];
    for (const file of portalJsFiles()) {
        if (/user-content-/.test(stripComments(readSource(file)))) offenders.push(relative(REPO_ROOT, file));
    }
    for (const page of readdirSync(PORTAL).filter((f) => f.endsWith(".html"))) {
        if (/user-content-/.test(readSource(resolve(PORTAL, page)))) offenders.push(`Portal/${page}`);
    }
    assert.deepEqual(offenders, []);
});
