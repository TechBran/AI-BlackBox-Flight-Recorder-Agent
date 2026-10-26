// When a rendering library is missing, the Portal must degrade SAFELY and
// LOUDLY (audit P01-chat-core-web-03: "log a loud startup error if DOMPurify is
// missing"). marked is vendored, but a failed or blocked script load can lose it
// while the vendored DOMPurify is present; generated-media placeholders must
// still render there, because the pollers find them by class.
//
// Own file on purpose: the warnings are once per page load (module state), and
// node --test runs every file in a fresh process.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked, unloadPurify, unloadMarked, scriptVectors } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

const errors = [];
const realError = console.error;
console.error = (...args) => { errors.push(args.map(String).join(" ")); };
process.on("exit", () => { console.error = realError; });

const settle = () => new Promise((r) => setTimeout(r, 30));
const { renderMarkdown, markdownToSafeHtml } = await import("../../Portal/modules/markdown-renderer.js");
const { sanitizeAssistantHtml } = await import("../../Portal/modules/sanitize.js");
const { appendBubble } = await import("../../Portal/modules/chat-bubbles.js");
const toastText = () => document.getElementById("toast").textContent;

test("with both libraries present nothing is reported", () => {
    renderMarkdown("**hi**");
    markdownToSafeHtml("**hi**");
    sanitizeAssistantHtml("<b>hi</b>");
    assert.deepEqual(errors, []);
});

test("a missing markdown renderer is reported once, loudly (console + toast)", () => {
    unloadMarked();
    try {
        renderMarkdown("**one**");
        renderMarkdown("**two**");
        markdownToSafeHtml("**three**");
        const hits = errors.filter((e) => /marked/i.test(e));
        assert.equal(hits.length, 1, JSON.stringify(errors));
        assert.match(toastText(), /markdown/i);
    } finally {
        loadMarked();
    }
});

test("offline (no marked): generated-media placeholders still render, sanitized", async () => {
    unloadMarked();
    try {
        const reply = "Here you go.\n<div class=\"video-loading-placeholder\" data-task-id=\"t1\" style=\"padding: 12px;\">"
            + "Video generation in progress</div>\n<svg onload=\"window.__pwned=1\"></svg>";
        const bubble = appendBubble("assistant", reply);
        await settle();
        assert.ok(bubble.querySelector(".video-loading-placeholder[data-task-id='t1']"), bubble.innerHTML);
        assert.deepEqual(scriptVectors(bubble), [], bubble.innerHTML);
        assert.ok(bubble.querySelector(".bubble-text").textContent.includes("Here you go."));
    } finally {
        loadMarked();
    }
});

test("a missing sanitizer is reported once, loudly, whichever renderer hits it first", () => {
    const before = errors.length;
    unloadPurify();
    try {
        renderMarkdown("**one**");
        markdownToSafeHtml("**two**");
        const hits = () => errors.slice(before).filter((e) => /DOMPurify/.test(e));
        assert.equal(hits().length, 1, JSON.stringify(errors));
        assert.match(toastText(), /sanitizer/i);
        sanitizeAssistantHtml("<b>three</b>");
        renderMarkdown("**four**");
        assert.equal(hits().length, 1, "still reported only once per page load");
    } finally {
        loadPurify();
    }
});
