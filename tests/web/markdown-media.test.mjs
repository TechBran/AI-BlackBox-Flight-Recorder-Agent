// Media URLs in replies and the unfenced-JSON safeguard (audit
// P01-chat-core-web-12, -11, rank 65).
//
// convertMediaUrlsToHtml used to rewrite every /ui/uploads/*.ext URL into a
// multi-line <div> BEFORE marked.parse, so the markdown-image form models emit
// (![alt](/ui/uploads/x.png)) rendered as a literal "![alt](" + player + ")",
// and inside inline code or a ``` fence the raw <div ...> showed as text. The
// upgrade now runs on the SANITIZED DOM, after marked: <img>/<a>/text URLs
// become players, code is never touched, and nothing it builds is markup.
//
// fenceUnfencedJson rescanned the text from index 0 for every line-start brace
// (O(n^2) per render, on every streamed token); it is one forward pass now.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked, mount, scriptVectors } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

const { renderMarkdown, fenceUnfencedJson } = await import("../../Portal/modules/markdown-renderer.js");

const IMG = "/ui/uploads/cat_123.png";

test("the markdown image form becomes one clean image preview", () => {
    const el = mount(renderMarkdown(`Here it is:\n\n![Generated image](${IMG})\n\nEnjoy.`));
    const previews = el.querySelectorAll(".media-preview.media-image");
    assert.equal(previews.length, 1);
    assert.equal(previews[0].querySelector("img").getAttribute("src"), IMG);
    assert.equal(previews[0].querySelector("img").getAttribute("alt"), "Generated image");
    assert.equal(previews[0].querySelector("a.media-download-btn").getAttribute("href"), IMG);
    assert.ok(!el.textContent.includes("!["), el.innerHTML);
    assert.ok(!el.textContent.includes("]("), el.innerHTML);
    assert.ok(el.textContent.includes("Enjoy."));
    assert.equal(el.querySelectorAll("p .media-preview").length, 0, "a block preview never sits inside a <p>");
});

test("a bare URL, a [text](url) link and an autolinked absolute URL are upgraded", () => {
    const el = mount(renderMarkdown(
        `Video: /ui/uploads/clip.mp4\n\n[the song](/ui/uploads/song.mp3)\n\nhttp://localhost:9091/ui/uploads/pic.webp`));
    assert.equal(el.querySelector(".media-video video source").getAttribute("src"), "/ui/uploads/clip.mp4");
    assert.equal(el.querySelector(".media-video video source").getAttribute("type"), "video/mp4");
    assert.equal(el.querySelector(".media-audio audio source").getAttribute("src"), "/ui/uploads/song.mp3");
    assert.equal(el.querySelector(".media-audio audio source").getAttribute("type"), "audio/mpeg");
    assert.equal(el.querySelector(".media-image img").getAttribute("src"), "http://localhost:9091/ui/uploads/pic.webp");
    assert.ok(el.textContent.includes("Video:"), "the prose around a mid-sentence URL survives");
});

test("URLs inside inline code and fenced blocks are left as code", () => {
    const md = "Saved to `/ui/uploads/a.png` and:\n\n```\ncurl /ui/uploads/b.mp4\n```\n";
    const el = mount(renderMarkdown(md));
    assert.equal(el.querySelectorAll(".media-preview").length, 0, el.innerHTML);
    assert.equal(el.querySelector("p code").textContent, "/ui/uploads/a.png");
    assert.equal(el.querySelector("pre code").textContent.trim(), "curl /ui/uploads/b.mp4");
    assert.ok(!el.querySelector("pre code").textContent.includes("<div"));
});

test("server-built media markup is never re-wrapped or corrupted", () => {
    const html = `<img src="/ui/uploads/i.png" alt="Generated image" class="generated-image">`
        + `<audio controls src="/ui/uploads/m.wav"></audio>`;
    const el = mount(renderMarkdown(html));
    assert.ok(el.querySelector("img.generated-image[src='/ui/uploads/i.png']"), el.innerHTML);
    assert.ok(el.querySelector("audio[controls][src='/ui/uploads/m.wav']"));
    assert.equal(el.querySelectorAll(".media-preview").length, 0);
});

test("hostile text cannot ride the upgrade into markup", () => {
    const payloads = [
        `/ui/uploads/x.png"onerror="window.__pwned=1`,
        `![x](/ui/uploads/y.png" onerror="window.__pwned=1)`,
        `[click](javascript:alert(1)//ui/uploads/z.png)`,
        `<a href="javascript:alert(1)">/ui/uploads/q.png</a>`,
        `/ui/uploads/<img src=x onerror=alert(1)>.png`,
    ];
    for (const p of payloads) {
        const el = mount(renderMarkdown(p));
        assert.deepEqual(scriptVectors(el), [], `${p} -> ${el.innerHTML}`);
    }
});

// The algorithm the single pass replaced (copied verbatim from the old
// markdown-renderer.js), kept here as the behavioral oracle.
function oldFenceUnfencedJson(text) {
    if (!text || typeof text !== "string") return text;
    const atLineStart = (s, index) => {
        for (let j = index - 1; j >= 0; j--) {
            if (s[j] === "\n") return true;
            if (s[j] !== " " && s[j] !== "\t") return false;
        }
        return true;
    };
    const isFenceMarkerAt = (s, lineStart, k) => {
        for (let j = lineStart; j < k; j++) if (s[j] !== " " && s[j] !== "\t") return false;
        return s[k] === "`" && s[k + 1] === "`" && s[k + 2] === "`";
    };
    const insideFence = (s, index) => {
        let open = false;
        let lineStart = 0;
        for (let k = 0; k < index; k++) {
            if (s[k] === "\n") lineStart = k + 1;
            else if (isFenceMarkerAt(s, lineStart, k)) {
                open = !open;
                while (k < index && s[k] !== "\n") k++;
                lineStart = k + 1;
            }
        }
        return open;
    };
    const jsonRunEnd = (s, start) => {
        let depth = 0, inStr = false, escaped = false;
        for (let i = start; i < s.length; i++) {
            const c = s[i];
            if (inStr) {
                if (escaped) escaped = false;
                else if (c === "\\") escaped = true;
                else if (c === '"') inStr = false;
            } else if (c === '"') inStr = true;
            else if (c === "{" || c === "[") depth++;
            else if (c === "}" || c === "]") {
                depth--;
                if (depth === 0) return i + 1;
                if (depth < 0) return -1;
            }
        }
        return -1;
    };
    const parses = (c) => { try { JSON.parse(c); return true; } catch { return false; } };
    let out = "", proseStart = 0, i = 0;
    while (i < text.length) {
        const c = text[i];
        if ((c === "{" || c === "[") && atLineStart(text, i) && !insideFence(text, i)) {
            const end = jsonRunEnd(text, i);
            if (end > i) {
                const cand = text.substring(i, end);
                if ((cand.includes("\n") || cand.length >= 80) && parses(cand)) {
                    out += text.substring(proseStart, i) + "\n```json\n" + cand + "\n```\n";
                    i = end;
                    proseStart = end;
                    continue;
                }
            }
        }
        i++;
    }
    return out + text.substring(proseStart);
}

const SAMPLES = [
    "",
    "plain prose only",
    'Result:\n{"results":[{"title":"a_b","url":"https://x/y"},{"title":"c","url":"z"}]}\nDone.',
    '[1] first citation\n[2] second citation\n  [3] indented',
    '```json\n{"already": "fenced",\n "x": 1}\n```\nafter {"inline": "brace"}',
    '   {\n  "indented": true\n}\ntext\n[ {"a":1},\n {"b":2} ]',
    '{"short":1}',
    '{"x": "' + "y".repeat(90) + '"}',
    '{"unbalanced": [1, 2\nstill streaming',
    '  ```\n{"inside": "open fence"}\n',
    'a {"mid": "line"} b\n{"tail":\n"x"}{"glued": 1}',
    '[link](https://example.com)\n| a | b |\n|---|---|\n{x}',
    '{"s": "brace } and \\" quote ```"}\n```\n{"after": "close?"}\n```',
];

test("fenceUnfencedJson matches the old algorithm on every sample", () => {
    for (const s of SAMPLES) {
        assert.equal(fenceUnfencedJson(s), oldFenceUnfencedJson(s), JSON.stringify(s));
    }
});

test("fenceUnfencedJson is linear: a long reply of citation lines stays fast", () => {
    const line = "[12] A citation line with words, numbers 42 and a [bracket]\n";
    const big = line.repeat(6000) + '{"results": [1, 2, 3],\n "ok": true}\n';
    const t0 = performance.now();
    const out = fenceUnfencedJson(big);
    const ms = performance.now() - t0;
    assert.ok(out.includes("```json"), "the trailing JSON is still fenced");
    assert.ok(ms < 250, `took ${ms.toFixed(1)} ms for ${big.length} chars (quadratic rescans are back?)`);
});
