// Keeps the sanitizer supply chain consistent (audit P01-chat-core-web-03,
// P07-html-css-04): the vendored DOMPurify bytes, Portal/vendor/VENDORED.md,
// the integrity= attributes in Portal/index.html and the tests/web pins must
// all agree, and nothing may load DOMPurify from a CDN again.
import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { resolve, sep } from "node:path";
import { PORTAL, VENDORED_PURIFY, MARKED_UMD } from "./helpers/portal-dom.mjs";

const read = (p) => readFileSync(p);
const sri384 = (buf) => "sha384-" + createHash("sha384").update(buf).digest("base64");
const sha256 = (buf) => createHash("sha256").update(buf).digest("hex");

const indexHtml = read(resolve(PORTAL, "index.html")).toString("utf8");
const vendoredMd = read(resolve(PORTAL, "vendor", "VENDORED.md")).toString("utf8");
const purifyBytes = read(VENDORED_PURIFY);
const pkg = JSON.parse(read(new URL("./package.json", import.meta.url)));
const npmPurify = JSON.parse(read(new URL("./node_modules/dompurify/package.json", import.meta.url)));

/** All <script>/<link rel=stylesheet> tags in index.html. */
function tags(html) {
    return [...html.matchAll(/<(script|link)\b[^>]*>/gi)].map((m) => m[0]);
}
const attr = (tag, name) => (tag.match(new RegExp(`\\s${name}="([^"]*)"`, "i")) || [])[1];

test("index.html loads DOMPurify from the vendored copy, with a matching SRI hash", () => {
    const purifyTags = tags(indexHtml).filter((t) => /purify/i.test(t));
    assert.equal(purifyTags.length, 1, purifyTags.join("\n"));
    assert.equal(attr(purifyTags[0], "src"), "/ui/vendor/dompurify/purify.min.js");
    assert.equal(attr(purifyTags[0], "integrity"), sri384(purifyBytes));
});

test("the vendored DOMPurify is the pinned version, byte-identical to the npm release", () => {
    const version = pkg.devDependencies.dompurify;
    assert.equal(npmPurify.version, version);
    assert.match(purifyBytes.toString("utf8", 0, 200), new RegExp(`DOMPurify ${version.replace(/\./g, "\\.")} `));
    const npmBytes = read(new URL("./node_modules/dompurify/dist/purify.min.js", import.meta.url));
    assert.equal(sha256(purifyBytes), sha256(npmBytes));
    // DOMPurify >= 3.2.4 (CVE-2025-26791 and the earlier mXSS fixes).
    const [maj, min, pat] = version.split(".").map(Number);
    assert.ok(maj > 3 || (maj === 3 && (min > 2 || (min === 2 && pat >= 4))), version);
});

test("VENDORED.md records the vendored file's version, sha256 and SRI", () => {
    assert.ok(vendoredMd.includes(`| DOMPurify | ${pkg.devDependencies.dompurify} |`));
    assert.ok(vendoredMd.includes(sha256(purifyBytes)), "sha256 recorded");
    assert.ok(vendoredMd.includes(sri384(purifyBytes)), "sha384 recorded");
});

test("every remaining CDN script/stylesheet for markdown + highlighting carries SRI + crossorigin", () => {
    const cdn = tags(indexHtml).filter((t) => /https:\/\/(cdn\.jsdelivr\.net|cdnjs\.cloudflare\.com)\/.*(marked|highlight)/.test(t));
    assert.equal(cdn.length, 3, cdn.join("\n"));
    for (const t of cdn) {
        assert.match(attr(t, "integrity") || "", /^sha384-[A-Za-z0-9+/]{64}$/, t);
        assert.equal(attr(t, "crossorigin"), "anonymous", t);
        assert.ok(vendoredMd.includes(attr(t, "integrity")), `VENDORED.md lists ${attr(t, "integrity")}`);
    }
});

test("the marked SRI pin matches the exact bytes the tests run against", () => {
    const markedTag = tags(indexHtml).find((t) => /marked@/.test(t));
    const cdnVersion = attr(markedTag, "src").match(/marked@([\d.]+)\//)[1];
    assert.equal(pkg.devDependencies.marked, cdnVersion);
    assert.equal(attr(markedTag, "integrity"), sri384(read(MARKED_UMD)));
});

test("no Portal page loads DOMPurify from a CDN", () => {
    // Every HTML page under Portal/ (runtime uploads excluded), so a moved or
    // newly added page cannot slip back onto the CDN copy.
    const pages = [];
    const walk = (dir) => {
        for (const ent of readdirSync(dir, { withFileTypes: true })) {
            const p = resolve(dir, ent.name);
            if (ent.isDirectory()) {
                if (!["uploads", "node_modules", "vendor"].includes(ent.name)) walk(p);
            } else if (ent.name.endsWith(".html")) {
                pages.push(p);
            }
        }
    };
    walk(PORTAL);
    assert.ok(pages.some((p) => p.endsWith(`${sep}index.html`)), "Portal/index.html found");
    for (const page of pages) {
        const html = read(page).toString("utf8");
        assert.doesNotMatch(html, /https?:\/\/[^"']*dompurify/i, page);
    }
});
