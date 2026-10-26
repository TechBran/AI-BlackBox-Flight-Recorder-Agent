// Finds what a Portal page or source file would load from another origin, for
// vendor-pins.test.mjs's "no CDN" guard (audit P07-html-css-14). The first
// guard read only double-quoted <script>/<link> attributes, so a single-quoted,
// unquoted or spaced attribute, or an inline module import, walked past it
// (U37 review). Pages are parsed by jsdom (no script runs, nothing is
// fetched), so quoting and spacing cannot hide a tag; inline scripts, inline
// styles and the Portal's own .js/.css files are scanned for module imports,
// importScripts/Worker URLs and CSS @import/url() that name another origin.
import { JSDOM } from "jsdom";

// An absolute http(s) or protocol-relative URL at the start of a string literal.
const ABS = String.raw`(?:https?:)?//`;
const Q = String.raw`["'\x60]`;
const SCRIPT_LOADS = [
    // import x from "…", import {a} from "…", export * from "…"
    new RegExp(String.raw`\b(?:import|export)\b[^;"'\x60]*?\bfrom\s*${Q}(${ABS}[^"'\x60]*)`, "g"),
    // import "…" (side-effect import)
    new RegExp(String.raw`\bimport\s*${Q}(${ABS}[^"'\x60]*)`, "g"),
    // import("…"), importScripts("…"), new Worker("…"), new SharedWorker("…")
    new RegExp(String.raw`\b(?:import|importScripts|Worker|SharedWorker)\s*\(\s*${Q}(${ABS}[^"'\x60]*)`, "g"),
];
const CSS_LOADS = [
    new RegExp(String.raw`@import\s*["'](${ABS}[^"']*)`, "gi"),
    new RegExp(String.raw`\burl\(\s*["']?(${ABS}[^"')\s]*)`, "gi"),
];

function scan(text, patterns) {
    const hits = [];
    for (const re of patterns) {
        for (const m of text.matchAll(re)) hits.push(m[1]);
    }
    return hits;
}

/** Cross-origin module/worker loads in JavaScript source text. */
export function scriptCrossOriginLoads(source) {
    return scan(source, SCRIPT_LOADS);
}

/** Cross-origin @import / url() loads in CSS text. */
export function cssCrossOriginLoads(css) {
    return scan(css, CSS_LOADS);
}

/**
 * Everything an HTML page would load from an origin other than its own:
 * <script src>, <link href> (any rel), <iframe>/<frame>/<embed src>,
 * <object data>, a cross-origin <base href> (it would rebase every relative
 * URL), absolute URLs in an import map, and the inline-script and CSS loads
 * above (in <style> blocks and style= attributes).
 * @param {string} html
 * @param {string} pageUrl - where the page is served, e.g. http://localhost:9091/ui/index.html
 * @returns {string[]} one "<what>: <url>" entry per load
 */
export function pageCrossOriginLoads(html, pageUrl) {
    const doc = new JSDOM(html, { url: pageUrl }).window.document;
    const origin = new URL(pageUrl).origin;
    const out = [];
    // A network fetch from another origin. about:blank, data: and blob: URLs
    // fetch nothing, so they are not a CDN dependency.
    const foreign = (raw) => {
        let url;
        try {
            url = new URL(raw, pageUrl);
        } catch {
            return true;  // unparseable: not a same-origin path
        }
        return /^(?:https?|wss?|ftp):$/.test(url.protocol) && url.origin !== origin;
    };
    const attrs = [
        ["script[src]", "src"], ["link[href]", "href"], ["base[href]", "href"],
        ["iframe[src]", "src"], ["frame[src]", "src"], ["embed[src]", "src"],
        ["object[data]", "data"],
    ];
    for (const [selector, attr] of attrs) {
        for (const el of doc.querySelectorAll(selector)) {
            const raw = el.getAttribute(attr).trim();
            if (foreign(raw)) out.push(`${selector}: ${raw}`);
        }
    }
    for (const el of doc.querySelectorAll("script:not([src])")) {
        const text = el.textContent;
        if ((el.getAttribute("type") || "").trim().toLowerCase() === "importmap") {
            for (const m of text.matchAll(new RegExp(String.raw`"(${ABS}[^"]*)"`, "g"))) out.push(`importmap: ${m[1]}`);
            continue;
        }
        for (const url of scriptCrossOriginLoads(text)) out.push(`inline script: ${url}`);
    }
    for (const el of doc.querySelectorAll("style")) {
        for (const url of cssCrossOriginLoads(el.textContent)) out.push(`<style>: ${url}`);
    }
    for (const el of doc.querySelectorAll("[style]")) {
        for (const url of cssCrossOriginLoads(el.getAttribute("style"))) out.push(`style=: ${url}`);
    }
    return out;
}
