// The markdown stack the Portal vendors (audit rank 73: P07-html-css-14,
// P01-chat-core-web-13, P01-chat-core-web-11), run on the REAL vendored
// marked 18 + marked-highlight + highlight.js 11 bytes index.html loads.
//
//  * Code is highlighted at PARSE time by marked-highlight, on a separate
//    marked instance that only non-streaming renders use. Streamed renders
//    (one per animation frame while tokens arrive) never run highlight.js;
//    the old design re-swept `pre code` with hljs.highlightElement on timers
//    in five places, after the highlight option marked 8 removed went dead.
//  * Unlabeled fences are auto-detected (what highlightElement did), a fence
//    in a language highlight.js lacks renders as plain escaped code instead
//    of throwing, and everything still ends in the sanitizer.
//  * GFM tables, task lists, single-newline breaks, the fenceUnfencedJson pass
//    and the post-sanitize media URL upgrade behave as before the upgrade.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import {
    installDom, loadPurify, loadMarked, loadHighlightJs, unloadHighlightJs, mount, scriptVectors,
} from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();
loadHighlightJs();

const md = await import("../../Portal/modules/markdown-renderer.js");
const { renderMarkdown, markdownToSafeHtml, configureMarked, highlightCodeBlocks } = md;
configureMarked();

const settle = (ms = 40) => new Promise((r) => setTimeout(r, ms));

/** Count highlight.js work done while `fn` runs. */
function countHljs(fn) {
    const h = globalThis.hljs;
    const real = { highlight: h.highlight, highlightAuto: h.highlightAuto, highlightElement: h.highlightElement };
    const calls = { highlight: 0, highlightAuto: 0, highlightElement: 0 };
    for (const k of Object.keys(real)) {
        h[k] = function (...args) { calls[k] += 1; return real[k].apply(this, args); };
    }
    try {
        fn();
    } finally {
        Object.assign(h, real);
    }
    return calls;
}
const total = (c) => c.highlight + c.highlightAuto + c.highlightElement;

const JS_FENCE = "Here:\n\n```js\nconst answer = 42;\nfunction f(x) { return x * 2; }\n```\n";
const JSON_FREE_REPLY = "Use this:\n\n```js\nconst ok = true;\n```\n";

test("a labeled fence is highlighted at parse time on a final render", () => {
    const el = mount(renderMarkdown(JS_FENCE));
    const code = el.querySelector("pre > code");
    assert.ok(code, el.innerHTML);
    assert.ok(code.classList.contains("hljs"), code.className);
    assert.ok(code.classList.contains("language-js"), code.className);
    const kw = [...code.querySelectorAll("span.hljs-keyword")].map((s) => s.textContent);
    assert.ok(kw.includes("const") && kw.includes("function") && kw.includes("return"), code.innerHTML);
    assert.equal(code.textContent.trim(), "const answer = 42;\nfunction f(x) { return x * 2; }");
});

test("an unlabeled fence is auto-detected, as highlightElement did", () => {
    const el = mount(renderMarkdown("```\ndef add(a, b):\n    return a + b\n\nclass Box:\n    pass\n```"));
    const code = el.querySelector("pre > code");
    assert.ok(code.classList.contains("hljs"), code.className);
    assert.ok(code.querySelector("span[class^='hljs-']"), code.innerHTML);
    assert.ok(code.textContent.includes("def add(a, b):"));
});

test("a fence in a language highlight.js lacks is plain escaped code, never a throw", () => {
    const errors = [];
    const realError = console.error;
    console.error = (...a) => errors.push(a.join(" "));
    let el;
    try {
        el = mount(renderMarkdown("```notalang\n<b>bold?</b> & more\n```"));
    } finally {
        console.error = realError;
    }
    const code = el.querySelector("pre > code");
    assert.equal(code.textContent.trim(), "<b>bold?</b> & more");
    assert.equal(code.querySelector("b"), null);
    assert.equal(code.querySelector("span[class^='hljs-']"), null);
    assert.deepEqual(errors, []);
});

test("HTML inside a highlighted fence stays text", () => {
    const hostile = `<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>`;
    for (const lang of ["html", "xml", "js", ""]) {
        const el = mount(renderMarkdown("```" + lang + "\n" + hostile + "\n```"));
        assert.deepEqual(scriptVectors(el), [], `${lang}: ${el.innerHTML}`);
        assert.equal(el.querySelector("img"), null, lang);
        assert.equal(el.querySelector("pre code").textContent.trim(), hostile, lang);
    }
});

test("hostile markup around a fence is still neutralized on the highlighted path", () => {
    const payloads = [
        `<img src=x onerror="window.__pwned=1">`,
        `<svg onload="window.__pwned=1"><circle r="5"/></svg>`,
        `<a href="javascript:window.__pwned=1">click</a>`,
        `<iframe srcdoc="<script>parent.__pwned=1</script>"></iframe>`,
        `<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>`,
    ];
    for (const p of payloads) {
        const el = mount(renderMarkdown(`${p}\n\n${JS_FENCE}\n${p}`));
        assert.deepEqual(scriptVectors(el), [], `${p} -> ${el.innerHTML}`);
        // A <style> opens a CommonMark raw-text HTML block that runs to
        // </style>, so it swallows the fence; every other payload ends at the
        // blank line and the fence is still highlighted.
        if (!/<style/i.test(p)) assert.ok(el.querySelector("pre code span.hljs-keyword"), `the fence is still highlighted after ${p}`);
    }
});

test("a streaming render never runs highlight.js; the final render highlights once per block", () => {
    const text = `${JS_FENCE}\nand\n\n\`\`\`python\nprint("hi")\n\`\`\`\n`;
    let streamed;
    const during = countHljs(() => {
        for (let i = 1; i <= text.length; i += 7) streamed = renderMarkdown(text.slice(0, i), { streaming: true });
        streamed = renderMarkdown(text, { streaming: true });
    });
    assert.equal(total(during), 0, JSON.stringify(during));
    const s = mount(streamed);
    assert.ok(s.querySelector("pre code.language-js"), s.innerHTML);
    assert.equal(s.querySelector("span[class^='hljs-']"), null, "streamed code is not highlighted");

    let final;
    const after = countHljs(() => { final = renderMarkdown(text); });
    assert.equal(after.highlight, 2, JSON.stringify(after));
    assert.equal(after.highlightElement, 0);
    assert.ok(mount(final).querySelector("pre code.language-python span[class^='hljs-']"));
});

test("markdownToSafeHtml (agent renderers) highlights a final render, not a streaming one", () => {
    const streaming = countHljs(() => {
        assert.equal(mount(markdownToSafeHtml(JS_FENCE, { streaming: true })).querySelector("span.hljs-keyword"), null);
    });
    assert.equal(total(streaming), 0);
    assert.ok(mount(markdownToSafeHtml(JS_FENCE)).querySelector("pre code.hljs span.hljs-keyword"));
});

test("highlighting does not leak into the global marked instance", () => {
    configureMarked();
    assert.equal(marked.defaults.breaks, true);
    assert.equal(marked.defaults.gfm, true);
    for (const dead of ["highlight", "headerIds", "mangle", "sanitize", "langPrefix"]) {
        assert.equal(Object.hasOwn(marked.defaults, dead), false, `${dead} is not a marked option`);
    }
    assert.ok(!marked.defaults.walkTokens, "marked-highlight is not installed globally");
    assert.doesNotMatch(marked.parse(JS_FENCE), /hljs/);
});

test("GFM tables render with their alignment", () => {
    const el = mount(renderMarkdown("| Name | Count |\n|:-----|------:|\n| apples | 3 |\n| pears | 12 |\n"));
    const table = el.querySelector("table");
    assert.ok(table, el.innerHTML);
    assert.deepEqual([...table.querySelectorAll("thead th")].map((th) => th.textContent), ["Name", "Count"]);
    assert.equal(table.querySelectorAll("tbody tr").length, 2);
    assert.equal(table.querySelector("thead th").getAttribute("align"), "left");
    assert.equal(table.querySelectorAll("thead th")[1].getAttribute("align"), "right");
    assert.equal(table.querySelector("tbody tr:last-child td:last-child").textContent, "12");
});

test("single newlines break, and task lists, strikethrough and autolinks survive", () => {
    const el = mount(renderMarkdown("line one\nline two\n\n- [x] done\n- [ ] todo\n\n~~gone~~ see https://example.com/x"));
    assert.equal(el.querySelector("p").querySelectorAll("br").length, 1);
    const boxes = el.querySelectorAll("li input[type='checkbox']");
    assert.equal(boxes.length, 2, el.innerHTML);
    assert.equal(boxes[0].hasAttribute("checked"), true);
    assert.equal(boxes[1].hasAttribute("checked"), false);
    assert.equal(el.querySelector("del").textContent, "gone");
    assert.equal(el.querySelector("a[href='https://example.com/x']").textContent, "https://example.com/x");
});

test("an unfenced JSON blob is fenced, highlighted as json, and keeps its markdown characters", () => {
    const blob = '{"results": [{"id": 1, "name": "a_b_c"}], "note": "**not bold** # not a heading", "ok": true}';
    const el = mount(renderMarkdown(`Tool said:\n${blob}\nDone.`));
    const code = el.querySelector("pre code.language-json");
    assert.ok(code, el.innerHTML);
    assert.ok(code.classList.contains("hljs"));
    assert.ok(code.querySelector("span.hljs-attr"), code.innerHTML);
    assert.equal(code.textContent.trim(), blob);
    assert.equal(el.querySelector("strong"), null, "markdown inside the JSON is not rendered");
    assert.ok(el.textContent.includes("Done."));
});

test("the media URL upgrade still runs on highlighted output, and code stays code", () => {
    const el = mount(renderMarkdown("![cat](/ui/uploads/cat.png)\n\n```bash\ncurl -O /ui/uploads/clip.mp4\n```\n\nListen: /ui/uploads/song.mp3"));
    assert.equal(el.querySelectorAll(".media-preview.media-image").length, 1, el.innerHTML);
    assert.equal(el.querySelectorAll(".media-preview.media-audio").length, 1);
    assert.equal(el.querySelector("pre .media-preview"), null);
    const code = el.querySelector("pre code.language-bash");
    assert.ok(code.classList.contains("hljs"));
    assert.ok(code.textContent.includes("curl -O /ui/uploads/clip.mp4"));
});

test("server-built placeholder HTML passes through marked to the sanitizer", () => {
    const el = mount(renderMarkdown("Here you go.\n<div class=\"video-loading-placeholder\" data-task-id=\"t1\" style=\"padding: 12px;\">in progress</div>"));
    assert.ok(el.querySelector(".video-loading-placeholder[data-task-id='t1']"), el.innerHTML);
});

test("without highlight.js a final render still shows the code, escaped and unhighlighted", () => {
    unloadHighlightJs();
    try {
        const el = mount(renderMarkdown("```js\nif (a < b) {}\n```"));
        const code = el.querySelector("pre code");
        assert.equal(code.textContent.trim(), "if (a < b) {}");
        assert.equal(code.querySelector("span"), null);
    } finally {
        loadHighlightJs();
    }
});

test("highlightCodeBlocks highlights only blocks that are not highlighted yet", () => {
    const el = mount(
        "<pre><code class=\"language-js\">let x = 1;</code></pre>"
        + "<pre><code class=\"hljs language-js\"><span class=\"hljs-keyword\">const</span> y</code></pre>");
    const calls = countHljs(() => highlightCodeBlocks(el));
    assert.equal(calls.highlightElement, 1, JSON.stringify(calls));
    assert.ok(el.querySelector("pre code.language-js.hljs span.hljs-keyword"));
    assert.equal(el.querySelectorAll("pre")[1].textContent, "const y");
    highlightCodeBlocks(null);   // tolerated
});

test("chat bubbles show highlighted code at once, with no highlight sweep left on a timer", async () => {
    const { appendBubble } = await import("../../Portal/modules/chat-bubbles.js");
    let bubble;
    const sync = countHljs(() => { bubble = appendBubble("assistant", JSON_FREE_REPLY); });
    assert.equal(sync.highlight, 1, JSON.stringify(sync));
    assert.ok(bubble.querySelector(".bubble-text pre code.hljs span.hljs-keyword"), bubble.innerHTML);
    const later = { n: 0 };
    const real = globalThis.hljs.highlightElement;
    globalThis.hljs.highlightElement = function (...a) { later.n += 1; return real.apply(this, a); };
    try {
        await settle();
    } finally {
        globalThis.hljs.highlightElement = real;
    }
    assert.equal(later.n, 0, "no deferred re-highlight of already highlighted code");
});


test("a streamed chat reply is unhighlighted while streaming and highlighted when finalized", async () => {
    const chat = await import("../../Portal/modules/chat-send.js");
    const bubble = chat.createStreamingThinkingBubble();
    document.getElementById("history").appendChild(bubble);
    const text = JSON_FREE_REPLY;
    const during = { n: 0 };
    const h = globalThis.hljs;
    const real = { highlight: h.highlight, highlightAuto: h.highlightAuto, highlightElement: h.highlightElement };
    for (const k of Object.keys(real)) h[k] = function (...a) { during.n += 1; return real[k].apply(this, a); };
    try {
        for (let i = 1; i <= text.length; i += 5) {
            chat.appendResponseContent(bubble, text.slice(i - 1, i + 4), text.slice(0, i + 4));
            await settle(20);   // let the coalesced frame render
        }
    } finally {
        Object.assign(h, real);
    }
    const content = bubble.querySelector(".response-content");
    assert.ok(content.querySelector("pre code.language-js"), content.innerHTML);
    assert.equal(during.n, 0, "streaming renders never call highlight.js");
    assert.equal(content.querySelector("span.hljs-keyword"), null);

    chat.finalizeStreamingBubble(bubble, text, "");
    assert.ok(content.querySelector("pre code.hljs span.hljs-keyword"), content.innerHTML);
});
