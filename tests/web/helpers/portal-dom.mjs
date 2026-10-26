// Shared jsdom harness for the Portal web tests.
//
// Builds ONE jsdom window per test process (node --test runs every file in its
// own process), exposes it on globalThis the way a browser would, and loads the
// SAME library bytes the Portal ships: each library is read from the file the
// <script src="/ui/vendor/..."> tag in Portal/index.html names, so a test runs
// what the page runs (and fails when the page stops loading a vendored copy).
// vendor-pins.test.mjs checks those bytes against the exact-pinned npm
// devDependencies and Portal/vendor/VENDORED.md.
//
// The real Portal ES modules are then imported from Portal/modules/ — nothing
// is copied or mocked except network (fetch rejects unless a test stubs it).
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = resolve(HERE, "..", "..", "..");
export const PORTAL = resolve(REPO_ROOT, "Portal");
export const VENDOR = resolve(PORTAL, "vendor");
export const VENDORED_PURIFY = resolve(VENDOR, "dompurify", "purify.min.js");

/** Every <script>/<link> tag in an HTML string, as {tag, src|href, integrity}. */
export function htmlTags(html) {
    return [...html.matchAll(/<(script|link)\b[^>]*>/gi)].map((m) => {
        const tag = m[0];
        const attr = (name) => (tag.match(new RegExp(`\\s${name}="([^"]*)"`, "i")) || [])[1];
        return { tag, name: m[1].toLowerCase(), src: attr("src"), href: attr("href"), rel: attr("rel"), integrity: attr("integrity") };
    });
}

/**
 * The file on disk a vendored <script src> / <link href> in Portal/index.html
 * points at, for the package directory `dir` under /ui/vendor/.
 * @param {string} dir - e.g. "marked", "xterm"
 * @param {"script"|"link"} [kind="script"]
 * @returns {string} absolute path
 */
export function vendoredFileFor(dir, kind = "script") {
    const html = readFileSync(resolve(PORTAL, "index.html"), "utf8");
    const prefix = `/ui/vendor/${dir}/`;
    const hit = htmlTags(html).find((t) => t.name === kind && (t.src || t.href || "").startsWith(prefix));
    if (!hit) throw new Error(`Portal/index.html does not load a vendored ${prefix} ${kind}`);
    return resolve(PORTAL, (hit.src || hit.href).slice("/ui/".length).split("?")[0]);
}

const BODY = `
  <div id="toast" class="hide"></div>
  <div id="history"></div>
  <div id="hintsContainer"></div>
`;

let _dom = null;

/**
 * Install (once) a browser-like global environment backed by jsdom.
 * @returns {JSDOM}
 */
export function installDom() {
    if (_dom) return _dom;
    _dom = new JSDOM(`<!doctype html><html><head></head><body>${BODY}</body></html>`, {
        url: "http://localhost:9091/ui/",
        runScripts: "outside-only",
        pretendToBeVisual: true,
    });
    const w = _dom.window;
    globalThis.window = w;
    for (const k of [
        "document", "navigator", "localStorage", "sessionStorage", "location",
        "HTMLElement", "Element", "Node", "NodeFilter", "DOMParser", "DocumentFragment",
        "MutationObserver", "Event", "CustomEvent", "KeyboardEvent", "getComputedStyle",
        "requestAnimationFrame", "cancelAnimationFrame", "Image", "Audio", "FileReader",
        "Blob", "URL", "WebSocket", "XMLHttpRequest", "HTMLAudioElement",
        // jsdom's addEventListener({signal}) accepts only jsdom's own
        // AbortSignal, as a browser's accepts only the page's.
        "AbortController", "AbortSignal",
    ]) {
        if (w[k] !== undefined) {
            Object.defineProperty(globalThis, k, { value: w[k], configurable: true, writable: true });
        }
    }
    if (typeof globalThis.Audio === "undefined") {
        globalThis.Audio = class { constructor() { this.paused = true; } play() { return Promise.resolve(); } pause() {} };
    }
    // Layout APIs jsdom does not implement (the Portal calls them on render).
    w.Element.prototype.scrollIntoView = w.Element.prototype.scrollIntoView || function () {};
    w.Element.prototype.scrollTo = w.Element.prototype.scrollTo || function () {};
    w.scrollTo = () => {};
    w.matchMedia = w.matchMedia || (() => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
    globalThis.matchMedia = w.matchMedia;
    globalThis.fetch = w.fetch = () => Promise.reject(new Error("no network in unit test"));
    return _dom;
}

/** Load the vendored DOMPurify into the jsdom window and expose it globally. */
export function loadPurify() {
    const w = installDom().window;
    if (!w.DOMPurify) w.eval(readFileSync(VENDORED_PURIFY, "utf8"));
    globalThis.DOMPurify = w.DOMPurify;
    return w.DOMPurify;
}

/** Evaluate the vendored classic script index.html loads from /ui/vendor/<dir>/. */
function evalVendored(w, dir) {
    w.eval(readFileSync(vendoredFileFor(dir), "utf8"));
}

/**
 * Load the markdown stack index.html ships (marked + marked-highlight, UMD)
 * into the jsdom window and expose it globally. highlight.js is separate
 * (loadHighlightJs): without it the renderer leaves code unhighlighted.
 */
export function loadMarked() {
    const w = installDom().window;
    if (!w.marked) evalVendored(w, "marked");
    if (!w.markedHighlight) evalVendored(w, "marked-highlight");
    globalThis.marked = w.marked;
    globalThis.markedHighlight = w.markedHighlight;
    return w.marked;
}

/** Load the vendored highlight.js into the jsdom window and expose it globally. */
export function loadHighlightJs() {
    const w = installDom().window;
    if (!w.hljs) evalVendored(w, "highlightjs");
    globalThis.hljs = w.hljs;
    return w.hljs;
}

/** Remove highlight.js from every global the Portal could reach it through. */
export function unloadHighlightJs() {
    delete globalThis.hljs;
    if (_dom) delete _dom.window.hljs;
}

/**
 * Load the vendored xterm.js and its fit addon (UMD) into the jsdom window:
 * window.Terminal and window.FitAddon, exactly as index.html provides them.
 */
export function loadXterm() {
    const w = installDom().window;
    if (!w.Terminal) evalVendored(w, "xterm");
    if (!w.FitAddon) evalVendored(w, "xterm-addon-fit");
    return { Terminal: w.Terminal, FitAddon: w.FitAddon };
}

/** Remove DOMPurify from every global the Portal could reach it through. */
export function unloadPurify() {
    delete globalThis.DOMPurify;
    if (_dom) delete _dom.window.DOMPurify;
}

/** Remove marked (and marked-highlight) from every global the Portal could reach it through. */
export function unloadMarked() {
    delete globalThis.marked;
    delete globalThis.markedHighlight;
    if (_dom) {
        delete _dom.window.marked;
        delete _dom.window.markedHighlight;
    }
}

/**
 * Parse an HTML string into a fresh detached container of the live document
 * (the same thing a Portal sink does with innerHTML) and return it.
 */
export function mount(html) {
    const el = document.createElement("div");
    el.innerHTML = html;
    return el;
}

/**
 * Every way a parsed fragment could still run script: on* attributes,
 * javascript:/vbscript:/data:text/html URLs in URL-bearing attributes, and the
 * elements that execute or embed foreign documents.
 * @returns {string[]} human-readable list of problems (empty = clean)
 */
export function scriptVectors(root) {
    const problems = [];
    const all = root.querySelectorAll("*");
    for (const el of all) {
        const tag = el.tagName.toLowerCase();
        if (["script", "iframe", "frame", "object", "embed", "base", "form", "style", "meta", "link"].includes(tag)) {
            problems.push(`<${tag}> element`);
        }
        for (const attr of Array.from(el.attributes)) {
            const name = attr.name.toLowerCase();
            const value = attr.value.replace(/[\s\u0000-\u001f]+/g, "").toLowerCase();
            if (name.startsWith("on")) problems.push(`<${tag} ${name}>`);
            if (["href", "src", "xlink:href", "action", "formaction", "poster", "srcdoc", "data"].includes(name)
                && (value.startsWith("javascript:") || value.startsWith("vbscript:") || value.startsWith("data:text/html"))) {
                problems.push(`<${tag} ${name}="${attr.value}">`);
            }
        }
    }
    return problems;
}
