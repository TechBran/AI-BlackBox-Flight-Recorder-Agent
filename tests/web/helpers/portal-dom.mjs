// Shared jsdom harness for the Portal web tests.
//
// Builds ONE jsdom window per test process (node --test runs every file in its
// own process), exposes it on globalThis the way a browser would, and loads the
// SAME library bytes the Portal ships: the vendored DOMPurify from
// Portal/vendor/dompurify/ and marked from the exact-pinned devDependency
// (byte-identical to the marked@11.1.1 CDN file index.html pins with SRI).
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
export const VENDORED_PURIFY = resolve(PORTAL, "vendor", "dompurify", "purify.min.js");
export const MARKED_UMD = resolve(HERE, "..", "node_modules", "marked", "marked.min.js");

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

/** Load marked (UMD) into the jsdom window and expose it globally. */
export function loadMarked() {
    const w = installDom().window;
    if (!w.marked) w.eval(readFileSync(MARKED_UMD, "utf8"));
    globalThis.marked = w.marked;
    return w.marked;
}

/** Remove DOMPurify from every global the Portal could reach it through. */
export function unloadPurify() {
    delete globalThis.DOMPurify;
    if (_dom) delete _dom.window.DOMPurify;
}

/** Remove marked from every global the Portal could reach it through. */
export function unloadMarked() {
    delete globalThis.marked;
    if (_dom) delete _dom.window.marked;
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
