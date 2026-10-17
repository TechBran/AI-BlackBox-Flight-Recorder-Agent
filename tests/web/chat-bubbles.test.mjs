// Stored-XSS regression tests for chat bubbles (audit P01-chat-core-web-01,
// X04-security-clients-infra-06). appendBubble renders EVERY stored assistant
// turn on page load (renderHistory), and updateThinkingBubble renders polling /
// inline-media replies — both used to assign the raw string to innerHTML
// whenever it contained <img, <audio, <video, <pre, <code or "bubble-text".
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked, unloadPurify, scriptVectors } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

const { appendBubble, updateThinkingBubble, createAnimatedThinkingBubble } =
    await import("../../Portal/modules/chat-bubbles.js");

const settle = () => new Promise((r) => setTimeout(r, 30));

// Each string trips one of the old raw-HTML sniffing branches.
const HOSTILE = {
    containsImage: `Look: <img src=x onerror="window.__pwned=1">`,
    containsAudio: `<audio src=x onerror="window.__pwned=1"></audio><img src=y onerror=alert(1)>`,
    containsVideo: `<video><source onerror="window.__pwned=1"></video>`,
    alreadyHtmlPre: `<pre>x</pre><svg onload="window.__pwned=1"></svg>`,
    alreadyHtmlCode: `Use the <code>tag</code> <a href="javascript:window.__pwned=1">here</a>`,
    alreadyHtmlHljs: `<span class="hljs">x</span><iframe src="javascript:alert(1)"></iframe>`,
    alreadyHtmlBubbleText: `<div class="bubble-text"><img src=x onerror=alert(1)></div>`,
    markdown: `**hi** <img src=x onerror="window.__pwned=1">`,
};

for (const [branch, payload] of Object.entries(HOSTILE)) {
    test(`appendBubble (history restore) neutralizes the ${branch} branch`, async () => {
        const bubble = appendBubble("assistant", payload);
        await settle(); // deferred hljs / highlight / snapshot passes run on timers
        assert.deepEqual(scriptVectors(bubble), [], bubble.innerHTML);
    });

    test(`updateThinkingBubble (polling / inline media) neutralizes the ${branch} branch`, async () => {
        const thinking = createAnimatedThinkingBubble();
        document.getElementById("history").appendChild(thinking);
        updateThinkingBubble(thinking, payload);
        await settle();
        assert.deepEqual(scriptVectors(thinking), [], thinking.innerHTML);
    });
}

test("appendBubble keeps legitimate media playable", async () => {
    const bubble = appendBubble("assistant",
        `<div class="audio-result"><audio controls src="/ui/uploads/m.wav"></audio></div>`
        + `<video controls preload="metadata" src="/ui/uploads/v.mp4" class="generated-video"></video>`
        + `<img src="/ui/uploads/i.png" alt="Generated image" class="generated-image">`);
    await settle();
    assert.ok(bubble.querySelector("audio[controls][src='/ui/uploads/m.wav']"));
    assert.ok(bubble.querySelector("video.generated-video[controls]"));
    assert.ok(bubble.querySelector("img.generated-image[src='/ui/uploads/i.png']"));
    // the image download affordance still attaches
    assert.ok(bubble.querySelector("a.download-btn[download='i.png']"));
});

test("appendBubble still renders markdown for plain assistant text", async () => {
    const bubble = appendBubble("assistant", "**bold** text");
    await settle();
    assert.ok(bubble.querySelector(".bubble-text strong"));
});

test("user bubbles stay text-only", async () => {
    const bubble = appendBubble("user", HOSTILE.containsImage);
    assert.equal(bubble.querySelector(".bubble-text img"), null);
    assert.equal(bubble.querySelector(".bubble-text").textContent, HOSTILE.containsImage);
});

test("without DOMPurify, stored HTML replies render as escaped text (fail closed)", async () => {
    unloadPurify();
    try {
        const bubble = appendBubble("assistant", HOSTILE.containsImage);
        await settle();
        assert.equal(bubble.querySelector(".bubble-text img"), null);
        assert.equal(bubble.querySelector(".bubble-text").textContent, HOSTILE.containsImage);
        const thinking = createAnimatedThinkingBubble();
        document.getElementById("history").appendChild(thinking);
        updateThinkingBubble(thinking, HOSTILE.alreadyHtmlCode);
        await settle();
        assert.equal(thinking.querySelector("a"), null);
    } finally {
        loadPurify();
    }
});
