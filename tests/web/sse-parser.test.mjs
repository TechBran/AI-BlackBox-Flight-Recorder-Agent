// The Portal's SSE reader (audit P01-chat-core-web-04, rank 65).
//
// The old /chat/stream loop split EACH reader.read() chunk on "\n\n" by itself,
// so a frame that straddled two reads was lost twice over: the first half
// failed JSON.parse and the second half had no "event:"/"data:" prefix. The
// big frames (the "done" frame carries the whole reply) were the likeliest to
// straddle. It also kept only the LAST data: line of a multi-line event.
// parseSseStream keeps a carry-over buffer and follows the SSE line rules, the
// same contract as Android's SSEClient (BufferedReader + StringBuilder).
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";

const { createSseParser, parseSseStream } = await import("../../Portal/modules/sse.js");

const encoder = new TextEncoder();

/** A ReadableStream reader that yields `bytes` in pieces of `size` bytes. */
function chunkedReader(text, size, { failAfter = null } = {}) {
    const bytes = encoder.encode(text);
    let offset = 0;
    let reads = 0;
    return new ReadableStream({
        pull(controller) {
            if (failAfter !== null && reads >= failAfter) {
                controller.error(new Error("connection reset"));
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
    }, { highWaterMark: 0 }).getReader(); // pull only on read: an error never discards unread chunks
}

const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

const STREAM = [
    ": keepalive\n\n",
    frame("stream_start", { model: "m-1" }),
    frame("content", "Hello "),
    ": keepalive\n\n",
    frame("content", "wörld — ✓ 你好"),
    frame("done", { content: "Hello wörld — ✓ 你好", thinking: "x".repeat(5000) }),
    frame("stream_end", { status: "ok" }),
].join("");

async function collect(text, size) {
    const events = [];
    await parseSseStream(chunkedReader(text, size), (ev) => events.push(ev));
    return events;
}

test("frames split across reads are reassembled (3-byte chunks)", async () => {
    const events = await collect(STREAM, 3);
    assert.deepEqual(events.map((e) => e.event), ["stream_start", "content", "content", "done", "stream_end"]);
    assert.equal(JSON.parse(events[2].data), "wörld — ✓ 你好", "multi-byte UTF-8 split across reads survives");
    const done = JSON.parse(events[3].data);
    assert.equal(done.content, "Hello wörld — ✓ 你好");
    assert.equal(done.thinking.length, 5000, "the large final frame is never lost");
});

test("every chunk size yields the same events", async () => {
    const whole = await collect(STREAM, 1 << 20);
    for (const size of [1, 2, 5, 7, 64, 1000]) {
        assert.deepEqual(await collect(STREAM, size), whole, `chunk size ${size}`);
    }
});

test("comment frames (keepalives) dispatch nothing but still count as bytes", async () => {
    let chunks = 0;
    const events = [];
    await parseSseStream(chunkedReader(": keepalive\n\n: keepalive\n\n", 4), (ev) => events.push(ev),
        { onChunk: () => { chunks += 1; } });
    assert.deepEqual(events, []);
    assert.ok(chunks > 0, "onChunk fires for comment-only bytes (the stall watchdog's heartbeat)");
});

test("multi-line data: joins every line with a newline", () => {
    const events = [];
    const p = createSseParser((ev) => events.push(ev));
    p.feed("event: content\ndata: first\ndata:second\ndata:  third\n\n");
    assert.deepEqual(events, [{ event: "content", data: "first\nsecond\n third", id: "" }]);
});

test("CRLF and lone CR line endings, including a CR/LF pair split across reads", () => {
    const events = [];
    const p = createSseParser((ev) => events.push(ev));
    p.feed("event: a\r\ndata: 1\r");
    p.feed("\n\r\nevent: b\rdata: 2\r\r");
    assert.deepEqual(events.map((e) => [e.event, e.data]), [["a", "1"]],
        "a CR that ends a read waits: it may be half of a CRLF");
    p.end();
    assert.deepEqual(events.map((e) => [e.event, e.data]), [["a", "1"], ["b", "2"]]);
});

test("an event without event: is a 'message'; one without data: is not dispatched", () => {
    const events = [];
    const p = createSseParser((ev) => events.push(ev));
    p.feed("data: plain\n\nevent: empty\n\nid: 7\nevent: x\ndata: y\n\n");
    assert.deepEqual(events, [
        { event: "message", data: "plain", id: "" },
        { event: "x", data: "y", id: "7" },
    ]);
});

test("end of stream flushes a complete last event and drops a truncated one", () => {
    const events = [];
    const p = createSseParser((ev) => events.push(ev));
    p.feed("event: done\ndata: {\"ok\":true}\n");
    p.end();
    assert.deepEqual(events.map((e) => e.event), ["done"], "complete lines, missing only the blank line");

    const cut = [];
    const q = createSseParser((ev) => cut.push(ev));
    q.feed("event: done\ndata: {\"content\":\"half");
    q.end();
    assert.deepEqual(cut, [], "a frame cut mid-line is never dispatched as if it were whole");
});

test("a read error after some frames rejects, keeping the events that arrived", async () => {
    const events = [];
    const reader = chunkedReader(frame("content", "a") + frame("content", "b"), 8, { failAfter: 4 });
    await assert.rejects(parseSseStream(reader, (ev) => events.push(ev)), /connection reset/);
    assert.deepEqual(events.map((e) => JSON.parse(e.data)), ["a"]);
});

test("an aborted signal stops reading and rejects with AbortError", async () => {
    const ctrl = new AbortController();
    const events = [];
    const reader = chunkedReader(frame("content", "a") + frame("content", "b"), 4);
    const run = parseSseStream(reader, (ev) => {
        events.push(ev);
        ctrl.abort();
    }, { signal: ctrl.signal });
    await assert.rejects(run, (err) => err.name === "AbortError");
    assert.equal(events.length, 1);
});
