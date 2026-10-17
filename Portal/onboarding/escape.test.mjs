// One HTML-escape helper for the onboarding wizard (audit P08-onboarding-installer-10,
// M3 U20 item 5).
//
// The wizard is served from /onboarding/ (its own static mount), so its modules
// cannot reach /ui/modules/core-utils.js by a relative import; util.js is the
// wizard's dependency-free seam instead. Every wizard module used to carry its own
// escapeHtml copy (15 of them) rendering server, model and operator data; they now
// import util.js, which must escape exactly like core-utils. The same holds for
// the /ui modules that kept private copies (voice-lab, voice-library,
// devices-section): they use core-utils.
//
// Run: node --test Portal/onboarding/escape.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const portal = join(here, "..");
const util = await import("./util.js");
const core = await import("../modules/core-utils.js");

const HOSTILE = [
    `<img src=x onerror="alert(1)">`,
    `x" autofocus onfocus="alert(1)`,
    `x' onmouseover='alert(1)`,
    `</textarea><script>alert(1)</script>`,
    `a & b &amp; c`,
    `‮<b>rtl</b>`,
    "",
    0,
    42,
    null,
    undefined,
    { toString: () => "<obj>" },
];

test("util.escapeHtml / escapeAttr escape all five characters and tolerate null", () => {
    assert.equal(util.escapeHtml(`<a href="x" title='y'>&</a>`),
        "&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt;&amp;&lt;/a&gt;");
    assert.equal(util.escapeHtml(null), "");
    assert.equal(util.escapeHtml(undefined), "");
    assert.equal(util.escapeHtml(0), "0");
    assert.equal(util.escapeAttr(`x" onfocus="y`), "x&quot; onfocus=&quot;y");
});

test("util escapes exactly like the Portal's core-utils", () => {
    for (const v of HOSTILE) {
        assert.equal(util.escapeHtml(v), core.escapeHtml(v), String(v));
        assert.equal(util.escapeAttr(v), core.escapeAttr(v), String(v));
    }
});

function jsFiles(dir) {
    return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
        const p = join(dir, e.name);
        if (e.isDirectory()) return e.name === "fonts" ? [] : jsFiles(p);
        return e.name.endsWith(".js") ? [p] : [];
    });
}

const PRIVATE_COPY = /\bfunction\s+escape(Html|Attr)\s*\(|\bconst\s+escape(Html|Attr)\s*=/;

test("no wizard module keeps a private escape copy", () => {
    const offenders = jsFiles(here)
        .filter((p) => !p.endsWith(`${"/"}util.js`))
        .filter((p) => PRIVATE_COPY.test(readFileSync(p, "utf8")));
    assert.deepEqual(offenders, []);
});

test("the /ui modules that kept copies use core-utils", () => {
    for (const rel of ["voice-lab.js", "voice-library.js", "modules/devices-section.js"]) {
        const src = readFileSync(join(portal, rel), "utf8");
        assert.ok(!PRIVATE_COPY.test(src), `${rel} defines its own escape helper`);
        assert.match(src, /import\s*\{[^}]*\bescapeHtml\b[^}]*\}\s*from\s*'\.\/(modules\/)?core-utils\.js'/, rel);
    }
});

test("every wizard module that renders escaped text imports it from util.js", () => {
    for (const p of jsFiles(here)) {
        if (p.endsWith(`${"/"}util.js`)) continue;
        const src = readFileSync(p, "utf8");
        if (!/\bescapeHtml\(/.test(src)) continue;
        assert.match(src, /import\s*\{[^}]*\bescapeHtml\b[^}]*\}\s*from\s*"\.\.?\/util\.js"/, p);
    }
});
