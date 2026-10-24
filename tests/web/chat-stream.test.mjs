// The Portal's /chat/stream turn, end to end through send() (audit
// P01-chat-core-web-04, -18, -09, -16, -17, -07, -08, rank 65).
//
//  * Frames split across reads used to be dropped (the final "done" frame,
//    which carries the whole reply, most of all).
//  * Any streaming failure fell back to POST /chat, which ran the SAME turn a
//    second time (a second billable call, tools run twice) and re-uploaded the
//    attachments, under the partial streaming bubble. The fallback is now only
//    for a server that never accepted the turn; a turn that dies after the
//    server answered drops its partial bubble and floating panel and offers
//    the retry chip.
//  * The floating reasoning panel was orphaned on errors and its fixed id made
//    the next turn's reasoning stream into the stale one.
//  * .doc/.docx were silently dropped by the streaming branch; the usage event
//    was discarded, so /chat/save always got tokens {}.
//  * The polling path swallowed a 'failed' task for 180 s; stripMdAndFences
//    deleted every code fence in a polled reply.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

const prompt = document.createElement("textarea");
prompt.id = "prompt";
document.body.appendChild(prompt);
localStorage.setItem("bbx_operator", "tester");
window.__provider = "gemini";
window.__model = "";

const chat = await import("../../Portal/modules/chat-send.js");
const { send, shouldFallBackToStandard, classifyAttachment, normalizeReplyFromAny } = chat;
const { addAttachedFile } = await import("../../Portal/modules/file-upload.js");
const { setHistoryData } = await import("../../Portal/modules/state-management.js");

const encoder = new TextEncoder();
const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const settle = (ms = 40) => new Promise((r) => setTimeout(r, ms));

/** An SSE body that yields `text` in `size`-byte reads, optionally dying after `failAfter` reads. */
function sseResponse(text, { size = 3, failAfter = null } = {}) {
    const bytes = encoder.encode(text);
    let offset = 0;
    let reads = 0;
    const body = new ReadableStream({
        pull(controller) {
            if (failAfter !== null && reads >= failAfter) {
                controller.error(new TypeError("network error"));
                return;
            }
            if (offset >= bytes.length) {
                controller.close();
                return;
            }
            controller.enqueue(bytes.slice(offset, offset + size));
            offset += size;
            reads += 1;
        },
    }, { highWaterMark: 0 }); // pull only on read: the error never discards unread chunks
    return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
    status, headers: { "Content-Type": "application/json" },
});

/** Route fetch by URL; record every call. */
function stubFetch(routes) {
    const calls = [];
    globalThis.fetch = window.fetch = async (url, opts = {}) => {
        calls.push({ url: String(url), opts });
        for (const [prefix, handler] of routes) {
            if (String(url).startsWith(prefix)) return handler(url, opts);
        }
        return json({});
    };
    return calls;
}

function resetPage() {
    document.getElementById("history").innerHTML = "";
    document.querySelectorAll(".streaming-thinking-panel.floating").forEach((p) => p.remove());
    setHistoryData([]);
}

async function sendText(text) {
    prompt.value = text;
    await send();
    await settle();
}

const floatingPanels = () => document.querySelectorAll("body > .streaming-thinking-panel.floating:not(.floating-exit)");

test("the fallback rule: only a turn the server never accepted may be re-sent to /chat", () => {
    assert.equal(shouldFallBackToStandard({ stage: "request" }), true, "fetch rejected: nothing reached the server");
    assert.equal(shouldFallBackToStandard({ stage: "http", status: 404 }), true, "server refused the stream");
    assert.equal(shouldFallBackToStandard({ stage: "http", status: 503 }), true);
    assert.equal(shouldFallBackToStandard({ stage: "stream" }), false, "the turn is already running server-side");
    assert.equal(shouldFallBackToStandard({ stage: "stalled" }), false);
    assert.equal(shouldFallBackToStandard(new Error("anything else")), false);
    assert.equal(shouldFallBackToStandard(null), false);
});

test("a stream read in 3-byte pieces renders the full reply and saves usage tokens", async () => {
    resetPage();
    const reply = "Here is **the answer** — with ünïcödé ✓";
    const sse = frame("stream_start", { model: "m-1" })
        + frame("thinking", "pondering")
        + frame("thinking_end", "")
        + frame("content", "Here is ")
        + frame("content", "**the answer**")
        + frame("usage", { prompt_tokens: 120, completion_tokens: 34 })
        + frame("done", { content: reply, thinking: "pondering" })
        + frame("stream_end", { status: "ok" });
    const calls = stubFetch([
        ["/chat/stream", () => sseResponse(sse, { size: 3 })],
        ["/chat/save", () => json({ minted: false })],
    ]);
    await sendText("question one");
    const bubbles = document.querySelectorAll("#history .bubble.assistant");
    assert.equal(bubbles.length, 1);
    assert.ok(bubbles[0].querySelector(".response-content strong"), bubbles[0].innerHTML);
    assert.ok(bubbles[0].textContent.includes("ünïcödé ✓"));
    const save = calls.find((c) => c.url === "/chat/save");
    assert.ok(save, "the turn is saved");
    const body = JSON.parse(save.opts.body);
    assert.equal(body.assistant_response, reply);
    assert.deepEqual(body.tokens, { prompt: 120, completion: 34 });
    assert.equal(calls.filter((c) => c.url === "/chat").length, 0);
    await settle(400);
    assert.equal(floatingPanels().length, 0, "the floating reasoning panel is gone after the turn");
});

test("a mid-stream death never re-runs the turn: partial bubble and panel removed, retry offered", async () => {
    resetPage();
    const sse = frame("stream_start", { model: "m-1" })
        + frame("thinking", "step one")
        + frame("content", "partial answ")
        + frame("content", "er that will never finish ".repeat(20));
    const calls = stubFetch([
        ["/chat/stream", () => sseResponse(sse, { size: 40, failAfter: 4 })],
        ["/chat", () => json({ task_id: "should-not-happen" })],
    ]);
    await sendText("do the side-effecting thing");
    assert.equal(calls.filter((c) => c.url === "/chat").length, 0, "no second /chat call for a running turn");
    assert.equal(document.querySelectorAll("#history .streaming-bubble").length, 0, "partial bubble removed");
    assert.ok(!document.getElementById("history").textContent.includes("partial answ"));
    assert.equal(floatingPanels().length, 0, "floating panel removed");
    const users = document.querySelectorAll("#history .bubble.user");
    assert.equal(users.length, 1, "the user turn is shown once");
    assert.ok(users[0].querySelector(".bubble-retry"), "the retry chip is offered under the user turn");
    assert.ok(document.getElementById("history").textContent.includes("Chat error"));
});

test("a server that refuses the stream falls back to /chat once, without re-uploading", async () => {
    resetPage();
    let uploads = 0;
    globalThis.XMLHttpRequest = window.XMLHttpRequest = class {
        constructor() { this.upload = { addEventListener() {} }; this._l = {}; }
        addEventListener(type, fn) { this._l[type] = fn; }
        open() {}
        send() {
            uploads += 1;
            setTimeout(() => {
                this.status = 200;
                this.responseText = JSON.stringify({ url: "/ui/uploads/report.docx" });
                this._l.load && this._l.load();
            }, 0);
        }
    };
    const docx = new File(["PK"], "report.docx",
        { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" });
    addAttachedFile(docx);
    const calls = stubFetch([
        ["/chat/stream", () => json({ detail: "not here" }, 404)],
        ["/chat", () => json({ task_id: "t-1" })],
        ["/tasks/status/t-1", () => json({ status: "completed", result_data: { reply: "fallback reply" } })],
        ["/health", () => json({})],
    ]);
    prompt.value = "summarize the attachment";
    await send();
    await settle(1500); // one 1 s poll
    assert.equal(calls.filter((c) => c.url === "/chat").length, 1);
    assert.equal(uploads, 1, "the attachment is uploaded once, not again for the fallback");
    const chatCall = calls.find((c) => c.url === "/chat");
    const messages = JSON.parse(chatCall.opts.body.get("messages"));
    assert.ok(messages[0].content.some((p) => p.type === "document_url"
        && p.document_url.url === "/ui/uploads/report.docx"), "the .docx reaches the model");
    assert.equal(document.querySelectorAll("#history .bubble.user").length, 1);
    assert.ok(document.getElementById("history").textContent.includes("fallback reply"));
});

test("a failed polled task surfaces its error_message at once, not a 180 s timeout", async () => {
    resetPage();
    stubFetch([
        ["/chat/stream", () => { throw new TypeError("Failed to fetch"); }],
        ["/chat", () => json({ task_id: "t-2" })],
        ["/tasks/status/t-2", () => json({ status: "failed", error_message: "provider quota exhausted" })],
    ]);
    const t0 = Date.now();
    prompt.value = "anything";
    await send();
    assert.ok(Date.now() - t0 < 5000, "gave up after the first failed poll");
    const text = document.getElementById("history").textContent;
    assert.ok(text.includes("provider quota exhausted"), text);
    assert.ok(!text.includes("timed out"));
});

test("each turn's reasoning streams into its OWN floating panel", async () => {
    resetPage();
    // An orphan from an earlier failed turn must not capture the next turn's text.
    const orphan = document.createElement("div");
    orphan.className = "streaming-thinking-panel floating";
    orphan.id = "floatingReasoningPanel";
    orphan.innerHTML = '<div class="thinking-content"></div>';
    document.body.appendChild(orphan);
    let release;
    const gate = new Promise((r) => { release = r; });
    const sse1 = frame("stream_start", {}) + frame("thinking", "mine-only");
    const body = new ReadableStream({
        async start(controller) {
            controller.enqueue(encoder.encode(sse1));
            await gate;
            controller.enqueue(encoder.encode(frame("done", { content: "ok" }) + frame("stream_end", {})));
            controller.close();
        },
    });
    stubFetch([
        ["/chat/stream", () => new Response(body, { status: 200 })],
        ["/chat/save", () => json({})],
    ]);
    prompt.value = "think";
    const sending = send();
    await settle(60);
    assert.equal(orphan.querySelector(".thinking-content").textContent, "", "the stale panel is not written to");
    const mine = [...floatingPanels()].filter((p) => p !== orphan);
    assert.equal(mine.length, 1);
    assert.ok(mine[0].textContent.includes("mine-only"));
    release();
    await sending;
    orphan.remove();
});

test("classifyAttachment routes every type the picker offers", () => {
    const f = (name, type) => new File(["x"], name, { type });
    assert.equal(classifyAttachment(f("a.png", "image/png")), "image_url");
    assert.equal(classifyAttachment(f("a.mp4", "video/mp4")), "video_url");
    assert.equal(classifyAttachment(f("a.wav", "audio/wav")), "audio_url");
    assert.equal(classifyAttachment(f("a.pdf", "application/pdf")), "document_url");
    assert.equal(classifyAttachment(f("a.txt", "text/plain")), "document_url");
    assert.equal(classifyAttachment(f("a.doc", "application/msword")), "document_url");
    assert.equal(classifyAttachment(f("a.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")), "document_url");
    assert.equal(classifyAttachment(f("a.bin", "application/octet-stream")), null);
    // A browser with no MIME entry for an extension the picker accepts.
    assert.equal(classifyAttachment(f("Report.DOCX", "")), "document_url");
    assert.equal(classifyAttachment(f("notes.txt", "application/octet-stream")), "document_url");
    assert.equal(classifyAttachment(f("a.exe", "")), null);
});

test("normalizeReplyFromAny keeps interior code fences and unwraps only a whole-fenced envelope", () => {
    const mixed = "Intro text.\n\n```python\nprint('hi')\n```\n\nOutro.";
    assert.equal(normalizeReplyFromAny(mixed), mixed);
    assert.equal(normalizeReplyFromAny({ reply: mixed }), mixed);
    assert.equal(normalizeReplyFromAny('```json\n{"reply": "hello"}\n```'), "hello");
    assert.equal(normalizeReplyFromAny('```\n{"ui_reply": "hi there"}\n```'), "hi there");
    const codeOnly = "```bash\nls -la\n```";
    assert.equal(normalizeReplyFromAny(codeOnly), codeOnly, "a reply that IS a code block stays one");
});
