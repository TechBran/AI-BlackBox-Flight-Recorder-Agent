// Keeps the Portal's third-party supply chain consistent (audit
// P01-chat-core-web-03, P07-html-css-04, P07-html-css-14, P07-html-css-16):
// every file under Portal/vendor/ is byte-identical to its exact-pinned npm
// release (tests/web/package.json), its sha256 is recorded in
// Portal/vendor/VENDORED.md (noVNC: Portal/vendor/novnc/VENDORED.md), the
// integrity= attributes in Portal/index.html match the bytes, and no Portal
// page loads a script or stylesheet from another origin again (a LAN-only or
// offline box would lose markdown, highlighting and the CLI terminal).
import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { PORTAL, VENDOR, VENDORED_PURIFY, htmlTags } from "./helpers/portal-dom.mjs";

const read = (p) => readFileSync(p);
const sri384 = (buf) => "sha384-" + createHash("sha384").update(buf).digest("base64");
const sha256 = (buf) => createHash("sha256").update(buf).digest("hex");
const posix = (p) => p.split(sep).join("/");

const indexHtml = read(resolve(PORTAL, "index.html")).toString("utf8");
const vendoredMd = read(resolve(VENDOR, "VENDORED.md")).toString("utf8");
const novncMd = read(resolve(VENDOR, "novnc", "VENDORED.md")).toString("utf8");
const pkg = JSON.parse(read(new URL("./package.json", import.meta.url)));
const NODE_MODULES = new URL("./node_modules/", import.meta.url);
const npmFile = (name, path) => new URL(`${name}/${path}`, NODE_MODULES);

/**
 * Every vendored package: its directory under Portal/vendor/, the npm package
 * pinned in tests/web/package.json, and vendored file -> path in that package.
 * `tree: true` = the whole directory mirrors the package (noVNC core/ +
 * vendor/pako/ + LICENSE.txt).
 */
const PACKAGES = [
    { dir: "dompurify", npm: "dompurify",
      files: { "purify.min.js": "dist/purify.min.js", "LICENSE": "LICENSE", "LICENSE-MPL": "LICENSE-MPL" } },
    { dir: "marked", npm: "marked",
      files: { "marked.umd.js": "lib/marked.umd.js", "LICENSE": "LICENSE" } },
    { dir: "marked-highlight", npm: "marked-highlight",
      files: { "index.umd.js": "lib/index.umd.js", "LICENSE": "LICENSE" } },
    { dir: "highlightjs", npm: "@highlightjs/cdn-assets",
      files: { "highlight.min.js": "highlight.min.js", "styles/atom-one-dark.min.css": "styles/atom-one-dark.min.css", "LICENSE": "LICENSE" } },
    { dir: "xterm", npm: "@xterm/xterm",
      files: { "xterm.js": "lib/xterm.js", "xterm.css": "css/xterm.css", "LICENSE": "LICENSE" } },
    { dir: "xterm-addon-fit", npm: "@xterm/addon-fit",
      files: { "addon-fit.js": "lib/addon-fit.js", "LICENSE": "LICENSE" } },
    { dir: "novnc", npm: "@novnc/novnc", tree: true },
];

/** The scripts and stylesheets Portal/index.html must load from the vendor tree. */
const PAGE_ASSETS = [
    { kind: "script", path: "dompurify/purify.min.js" },
    { kind: "script", path: "marked/marked.umd.js" },
    { kind: "script", path: "marked-highlight/index.umd.js" },
    { kind: "script", path: "highlightjs/highlight.min.js" },
    { kind: "link", path: "highlightjs/styles/atom-one-dark.min.css" },
    { kind: "link", path: "xterm/xterm.css" },
    { kind: "script", path: "xterm/xterm.js" },
    { kind: "script", path: "xterm-addon-fit/addon-fit.js" },
];

/** Every file under `dir`, as paths relative to it (POSIX separators). */
function walkFiles(dir) {
    const out = [];
    const walk = (d) => {
        for (const ent of readdirSync(d, { withFileTypes: true })) {
            const p = resolve(d, ent.name);
            if (ent.isDirectory()) walk(p);
            else out.push(posix(relative(dir, p)));
        }
    };
    walk(dir);
    return out.sort();
}

/** The vendored files (documentation excluded), relative to Portal/vendor/. */
const vendoredFiles = walkFiles(VENDOR).filter((p) => !p.endsWith("VENDORED.md"));

/** `| \`path\` | \`sha256\` |` rows of both VENDORED.md files, as path -> hash. */
function checksumRows() {
    const rows = new Map();
    const add = (md, base) => {
        for (const m of md.matchAll(/^\|\s*`([^`]+)`\s*\|\s*`([0-9a-f]{64})`\s*\|/gm)) {
            const path = base + m[1];
            assert.ok(!rows.has(path), `${path} is recorded twice`);
            rows.set(path, m[2]);
        }
    };
    add(vendoredMd, "");
    add(novncMd, "novnc/");
    return rows;
}

test("every vendored package is its exact npm pin, byte for byte", () => {
    for (const p of PACKAGES) {
        const pin = pkg.devDependencies[p.npm];
        assert.match(pin || "", /^\d+\.\d+\.\d+$/, `${p.npm} is exact-pinned in tests/web/package.json`);
        const installed = JSON.parse(read(npmFile(p.npm, "package.json")));
        assert.equal(installed.version, pin, `${p.npm}: node_modules holds the pin (run npm ci)`);
        const files = p.tree
            ? Object.fromEntries(walkFiles(resolve(VENDOR, p.dir)).filter((f) => f !== "VENDORED.md").map((f) => [f, f]))
            : p.files;
        for (const [vendored, upstream] of Object.entries(files)) {
            const mine = read(resolve(VENDOR, p.dir, vendored));
            assert.equal(sha256(mine), sha256(read(npmFile(p.npm, upstream))), `${p.dir}/${vendored} == ${p.npm}@${pin}/${upstream}`);
        }
    }
});

test("vendored noVNC is the whole RFB closure of its release (core/ + vendor/pako/)", () => {
    const upstream = walkFiles(fileURLToPath(npmFile("@novnc/novnc", "")))
        .filter((f) => f.startsWith("core/") || f.startsWith("vendor/pako/"));
    const mine = walkFiles(resolve(VENDOR, "novnc")).filter((f) => f.startsWith("core/") || f.startsWith("vendor/"));
    assert.deepEqual(mine, upstream);
});

test("no file under Portal/vendor/ is unaccounted for", () => {
    const owned = new Set();
    for (const p of PACKAGES) {
        if (p.tree) vendoredFiles.filter((f) => f.startsWith(`${p.dir}/`)).forEach((f) => owned.add(f));
        else Object.keys(p.files).forEach((f) => owned.add(`${p.dir}/${f}`));
    }
    assert.deepEqual(vendoredFiles.filter((f) => !owned.has(f)), [], "every vendored file belongs to a pinned package");
});

test("VENDORED.md records the sha256 of every vendored file, and nothing stale", () => {
    const rows = checksumRows();
    for (const f of vendoredFiles) {
        assert.ok(rows.has(f), `${f} has a checksum row`);
        assert.equal(rows.get(f), sha256(read(resolve(VENDOR, f))), `${f}: recorded sha256 == bytes on disk`);
    }
    for (const path of rows.keys()) {
        assert.ok(existsSync(resolve(VENDOR, path)), `checksum row for a missing file: ${path}`);
    }
});

test("VENDORED.md records each package's pinned version", () => {
    for (const p of PACKAGES) {
        const pin = pkg.devDependencies[p.npm];
        const row = vendoredMd.split("\n").find((l) => l.startsWith("|") && l.includes(`\`${p.dir}/\``));
        assert.ok(row, `VENDORED.md has a package row for ${p.dir}/`);
        assert.equal(row.split("|")[2].trim(), pin, `${p.dir}/ row version`);
    }
    assert.match(novncMd, new RegExp(`\\*\\*Tag:\\*\\* \`v${pkg.devDependencies["@novnc/novnc"].replace(/\./g, "\\.")}\``));
});

test("index.html loads every vendored library from /ui/vendor/, each with a matching SRI hash", () => {
    const tags = htmlTags(indexHtml);
    for (const { kind, path } of PAGE_ASSETS) {
        const url = `/ui/vendor/${path}`;
        const hits = tags.filter((t) => t.name === kind && (kind === "script" ? t.src : t.href) === url);
        assert.equal(hits.length, 1, `exactly one ${kind} for ${url}`);
        if (kind === "link") assert.equal(hits[0].rel, "stylesheet", url);
        assert.equal(hits[0].integrity, sri384(read(resolve(VENDOR, path))), `${url} integrity`);
    }
    // Every vendored asset the page names is one of the above (no stale extra tag).
    const named = tags.map((t) => t.src || t.href || "").filter((u) => u.startsWith("/ui/vendor/"));
    assert.deepEqual(named.sort(), PAGE_ASSETS.map((a) => `/ui/vendor/${a.path}`).sort());
});

test("the vendored DOMPurify is a release with the published mXSS fixes", () => {
    const version = pkg.devDependencies.dompurify;
    assert.match(read(VENDORED_PURIFY).toString("utf8", 0, 200), new RegExp(`DOMPurify ${version.replace(/\./g, "\\.")} `));
    // DOMPurify >= 3.2.4 (CVE-2025-26791 and the earlier mXSS fixes).
    const [maj, min, pat] = version.split(".").map(Number);
    assert.ok(maj > 3 || (maj === 3 && (min > 2 || (min === 2 && pat >= 4))), version);
});

test("no Portal page loads a script or stylesheet from another origin", () => {
    // Every HTML page under Portal/ (runtime uploads excluded), so a moved or
    // newly added page cannot slip back onto a CDN.
    const pages = walkFiles(PORTAL)
        .filter((p) => p.endsWith(".html") && !/^(uploads|vendor)\//.test(p) && !p.includes("node_modules/"));
    assert.ok(pages.includes("index.html"), "Portal/index.html found");
    for (const page of pages) {
        const html = read(resolve(PORTAL, page)).toString("utf8");
        for (const t of htmlTags(html)) {
            const url = t.src || t.href;
            if (!url) continue;
            assert.doesNotMatch(url, /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i, `${page}: ${t.tag}`);
        }
        assert.doesNotMatch(html, /https?:\/\/[^"']*(dompurify|marked|highlight\.js|xterm)/i, page);
    }
});
