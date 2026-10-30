// The Portal's side of the M7 ledger rule and the in-app WebView signing
// (docs/plans/2026-10-25-samsung-health-integration.md §3.7, §6.2-§6.3),
// end to end through send():
//
//  * /chat/stream's stream_start names the turn key (`wellness_turn`) for an
//    operator with a health store; the Portal echoes it on /chat/save so the
//    box masks exactly that turn, and leaves the body byte-identical without.
//  * A `wellness_used` frame tags the reply being built: its history entry
//    keeps wellness_ref (the frame's ref) and wellness_used (for the footer);
//    the footer renders under the bubble, live and from local history.
//  * The Portal sends /chat/stream the current user turn only (context comes
//    from the snapshots), so no earlier health bubble, ref or frame ever goes
//    back to a provider.
//  * Inside the BlackBox Android app, AndroidBridge.wellnessSign signs
//    POST /chat/stream and POST /chat over the sha256 of the exact UTF-8
//    body; null means unsigned; a desktop browser sends what it always did.
//  * A wellness reply's "response ready" notification and the stream's debug
//    log carry no reply text (the app posts notifications natively and its
//    WebView console goes to logcat), nor does the snapshot-id linker's log.
//  * A wellness reply is never read aloud: auto-TTS skips it and the speak
//    buttons of its bubble (reply and reasoning) do not speak (the voice
//    service is a destination the health consent does not name, W26/W31).
//
// The fixture numbers are the synthetic wellness-fixture operator's.
//
// Run: bash scripts/run_web_tests.sh
import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { installDom, loadPurify, loadMarked, scriptVectors, REPO_ROOT } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

const prompt = document.createElement("textarea");
prompt.id = "prompt";
document.body.appendChild(prompt);
localStorage.setItem("bbx_operator", "wellness-fixture");
window.__provider = "anthropic";
window.__model = "";

const chat = await import("../../Portal/modules/chat-send.js");
const { send, renderHistory } = chat;
const { setHistoryData, getHistoryData } = await import("../../Portal/modules/state-management.js");
const { WELLNESS_SPEECH_OFF } = await import("../../Portal/modules/chat-bubbles.js");

const WT = "wt_0123456789abcdef";
const REF = "w-20261029-7f3a";
const REPLY = "Your sleeping heart rate was 47 bpm.";
const HEADER = "X-BlackBox-Origin";
const XSS = `<img src=x onerror="window.__pwned=1">`;
const USED = { ref: REF, categories: ["sleep", "heart"], as_of: "2026-10-29T07:10-04:00",
    provider: "anthropic", provider_label: "Anthropic" };

const encoder = new TextEncoder();
const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const settle = (ms = 40) => new Promise((r) => setTimeout(r, ms));
const sha256 = (s) => createHash("sha256").update(Buffer.from(s, "utf8")).digest("hex");

function sseResponse(text) {
    return new Response(encoder.encode(text), { status: 200, headers: { "Content-Type": "text/event-stream" } });
}
const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
    status, headers: { "Content-Type": "application/json" },
});

/** A wellness turn's stream: stream_start (with the key unless turn === null), the tool, the frames, the reply. */
function wellnessSse({ turn = WT, used = [USED], content = REPLY } = {}) {
    const start = { provider: "anthropic", model: "m-1", provenance: {} };
    if (turn !== null) start.wellness_turn = turn;
    return frame("stream_start", start)
        + frame("tool_start", "wellness_summary")
        + frame("tool_result", "wellness_summary: health data read")
        + used.map((u) => frame("wellness_used", u)).join("")
        + frame("content", content)
        + frame("done", { content })
        + frame("stream_end", { status: "ok" });
}

function stubFetch(routes) {
    const calls = [];
    globalThis.fetch = window.fetch = async (url, opts = {}) => {
        calls.push({ url: String(url), opts });
        for (const [prefix, handler] of routes) {
            if (String(url) === prefix || String(url).startsWith(prefix + "?") || String(url).startsWith(prefix + "/")) {
                return handler(url, opts);
            }
        }
        return json({});
    };
    return calls;
}

function fakeBridge({ sign } = {}) {
    const signed = [];
    let n = 0;
    return {
        signed,
        wellnessSign(method, path, hex) {
            signed.push({ method, path, hex });
            if (sign) return sign(method, path, hex);
            n += 1;
            return `v1;enr=en_0000abcd;ts=${1800000000 + n};nonce=${String(n).padStart(32, "0")};sig=${"c".repeat(64)}`;
        },
    };
}

function reset(t) {
    document.getElementById("history").innerHTML = "";
    document.querySelectorAll(".streaming-thinking-panel.floating").forEach((p) => p.remove());
    setHistoryData([]);
    delete window.AndroidBridge;
    delete globalThis.AndroidBridge;
    t.after(() => {
        delete window.AndroidBridge;
        delete globalThis.AndroidBridge;
    });
}

async function sendText(text, ms = 40) {
    prompt.value = text;
    await send();
    await settle(ms);
}

const saveCall = (calls) => calls.find((c) => c.url === "/chat/save");
const streamCall = (calls) => calls.find((c) => c.url === "/chat/stream");
const lastAssistant = () => getHistoryData().filter((e) => e.role === "assistant").at(-1);

// The /chat/save body the Portal sent before M7, for this turn.
const legacySave = (extra = {}) => JSON.stringify({
    operator: "wellness-fixture", user_message: "how did I sleep?", assistant_response: REPLY,
    reasoning: "", provenance: {}, model: "", tokens: {}, ...extra,
});

// ── The turn contract ──────────────────────────────────────────────────────

test("the turn key this suite uses is the box's shape", () => {
    const turnPy = readFileSync(resolve(REPO_ROOT, "Orchestrator", "wellness", "turn.py"), "utf8");
    assert.ok(turnPy.includes('return "wt_" + secrets.token_hex(8)'), "Orchestrator/wellness/turn.new_turn_key");
    assert.match(WT, /^wt_[0-9a-f]{16}$/);
});

test("stream_start's wellness_turn is echoed on /chat/save, appended to the old body", async (t) => {
    reset(t);
    const calls = stubFetch([["/chat/stream", () => sseResponse(wellnessSse())], ["/chat/save", () => json({ minted: true })]]);
    await sendText("how did I sleep?");
    assert.equal(saveCall(calls).opts.body, legacySave({ wellness_turn: WT }));
});

test("without wellness_turn the /chat/save body is byte-identical to before", async (t) => {
    reset(t);
    const calls = stubFetch([["/chat/stream", () => sseResponse(wellnessSse({ turn: null, used: [] }))],
        ["/chat/save", () => json({})]]);
    await sendText("how did I sleep?");
    assert.equal(saveCall(calls).opts.body, legacySave());
});

for (const [name, turn] of Object.entries({ "a number": 7, "an object": { k: WT }, "an empty string": "",
    "a runaway string": "wt_" + "a".repeat(300), "markup": XSS })) {
    test(`a wellness_turn that is ${name} is not echoed`, async (t) => {
        reset(t);
        const calls = stubFetch([["/chat/stream", () => sseResponse(wellnessSse({ turn, used: [] }))], ["/chat/save", () => json({})]]);
        await sendText("how did I sleep?");
        assert.equal(saveCall(calls).opts.body, legacySave());
    });
}

test("wellness_used tags the reply: its history entry keeps wellness_ref and wellness_used, the bubble its footer", async (t) => {
    reset(t);
    stubFetch([["/chat/stream", () => sseResponse(wellnessSse())], ["/chat/save", () => json({})]]);
    await sendText("how did I sleep?");
    const entry = lastAssistant();
    assert.deepEqual(Object.keys(entry).sort(), ["content", "role", "wellness_ref", "wellness_used"]);
    assert.equal(entry.content, REPLY);
    assert.equal(entry.wellness_ref, REF);
    assert.deepEqual(entry.wellness_used, { ref: REF, refs: [REF], categories: ["sleep", "heart"],
        as_of: "2026-10-29T07:10-04:00", provider: "anthropic", provider_label: "Anthropic", source_label: null });
    const user = getHistoryData().filter((e) => e.role === "user").at(-1);
    assert.ok(!("wellness_ref" in user) && !("wellness_used" in user), "the user's turn is untouched");
    const footer = document.querySelector("#history .bubble.assistant .wellness-footer");
    assert.ok(footer, "the footer is under the reply");
    assert.match(footer.textContent, /^Used your health data · your phone · as of .+ · Anthropic$/);
    assert.ok(!footer.textContent.includes("47"));
    assert.equal(footer.previousElementSibling.className, "streaming-response-panel");
});

test("two tools in one turn: the first ref is the message's, both refs kept", async (t) => {
    reset(t);
    const second = { ...USED, ref: "w-20261029-a1b2", categories: ["activity"] };
    stubFetch([["/chat/stream", () => sseResponse(wellnessSse({ used: [USED, second] }))], ["/chat/save", () => json({})]]);
    await sendText("how did I sleep and move?");
    const entry = lastAssistant();
    assert.equal(entry.wellness_ref, REF);
    assert.deepEqual(entry.wellness_used.refs, [REF, "w-20261029-a1b2"]);
    assert.deepEqual(entry.wellness_used.categories, ["sleep", "heart", "activity"]);
    assert.equal(document.querySelectorAll("#history .wellness-footer").length, 1);
});

test("a wellness_used frame with no usable ref tags nothing", async (t) => {
    reset(t);
    stubFetch([["/chat/stream", () => sseResponse(wellnessSse({ used: [{ ...USED, ref: XSS }, { categories: ["sleep"] }] }))],
        ["/chat/save", () => json({})]]);
    await sendText("how did I sleep?");
    assert.deepEqual(Object.keys(lastAssistant()).sort(), ["content", "role"]);
    assert.equal(document.querySelector("#history .wellness-footer"), null);
});

test("hostile frame strings stay text in the footer", async (t) => {
    reset(t);
    stubFetch([["/chat/stream", () => sseResponse(wellnessSse({ used: [{ ...USED, provider_label: XSS }] }))],
        ["/chat/save", () => json({})]]);
    await sendText("how did I sleep?");
    const bubble = document.querySelector("#history .bubble.assistant");
    assert.ok(bubble.querySelector(".wellness-footer").textContent.endsWith(XSS));
    assert.deepEqual(scriptVectors(bubble.querySelector(".wellness-footer")), []);
    assert.equal(bubble.querySelector(".wellness-footer img"), null);
});

test("an ordinary turn has no footer and its entry no new fields", async (t) => {
    reset(t);
    const sse = frame("stream_start", { model: "m-1" }) + frame("content", "Hello.") + frame("done", { content: "Hello." })
        + frame("stream_end", {});
    stubFetch([["/chat/stream", () => sseResponse(sse)], ["/chat/save", () => json({})]]);
    await sendText("hi");
    assert.deepEqual(Object.keys(lastAssistant()).sort(), ["content", "role"]);
    assert.equal(document.querySelector("#history .wellness-footer"), null);
});

test("/chat/stream gets the current user turn only: no earlier bubble, ref or frame", async (t) => {
    reset(t);
    setHistoryData([
        { role: "user", content: "how did I sleep yesterday?" },
        { role: "assistant", content: REPLY, wellness_ref: REF, wellness_used: USED },
    ]);
    const calls = stubFetch([["/chat/stream", () => sseResponse(wellnessSse())], ["/chat/save", () => json({})]]);
    await sendText("and today?");
    const body = JSON.parse(streamCall(calls).opts.body);
    assert.deepEqual(body.messages, [{ role: "user", content: "and today?" }]);
    for (const key of ["wellness_ref", "wellness_used", "wellness_turn", REF, WT]) {
        assert.ok(!streamCall(calls).opts.body.includes(key), `${key} never goes out`);
    }
    assert.ok(!streamCall(calls).opts.body.includes("47"));
});

test("renderHistory restores the footer from local history; older entries render as before", async (t) => {
    reset(t);
    setHistoryData([
        { role: "user", content: "how did I sleep?" },
        { role: "assistant", content: REPLY, wellness_ref: REF, wellness_used: USED },
        { role: "assistant", content: "Thought about it.", thinking: "reasoning", wellness_ref: REF, wellness_used: USED },
        { role: "assistant", content: "An answer from before M7." },
    ]);
    renderHistory();
    await settle();
    const assistants = [...document.querySelectorAll("#history .bubble.assistant")];
    assert.equal(assistants.length, 3);
    assert.ok(assistants[0].querySelector(".wellness-footer"));
    assert.ok(assistants[1].querySelector(".wellness-footer"), "a reply with reasoning gets it too");
    assert.equal(assistants[2].querySelector(".wellness-footer"), null);
    assert.equal(assistants[2].querySelector(".bubble-text").textContent.trim(), "An answer from before M7.");
});

test("a masked reply in local history offers Show numbers only inside the app", async (t) => {
    reset(t);
    const masked = [{ role: "assistant", content: "Your sleeping heart rate was [·] bpm.", wellness_ref: REF, wellness_used: USED }];
    setHistoryData(masked);
    renderHistory();
    assert.equal(document.querySelector("#history .wellness-show-numbers"), null, "no bridge, no button");
    window.AndroidBridge = fakeBridge();
    setHistoryData(masked);
    renderHistory();
    assert.ok(document.querySelector("#history .wellness-show-numbers"));
});

test("a save that comes back masked (an artifact turn) keeps the ref and offers Show numbers in the app", async (t) => {
    reset(t);
    window.AndroidBridge = fakeBridge();
    const masked = "Your sleeping heart rate was [·] bpm. [download]";
    stubFetch([["/chat/stream", () => sseResponse(wellnessSse())],
        ["/chat/save", () => json({ minted: true, snap_id: "SNAP-20261029-0001", modified_response: masked })]]);
    await sendText("how did I sleep?");
    const entry = lastAssistant();
    assert.equal(entry.content, masked);
    assert.equal(entry.wellness_ref, REF);
    const bubble = document.querySelector("#history .bubble.assistant");
    assert.ok(bubble.querySelector(".wellness-show-numbers"));
    assert.equal(bubble.querySelectorAll(".wellness-footer").length, 1);
});

// ── Signing inside the app ─────────────────────────────────────────────────

test("inside the app /chat/stream carries X-BlackBox-Origin over the exact UTF-8 body", async (t) => {
    reset(t);
    const bridge = fakeBridge();
    window.AndroidBridge = bridge;
    const calls = stubFetch([["/chat/stream", () => sseResponse(wellnessSse())], ["/chat/save", () => json({})]]);
    await sendText("wie habe ich geschlafen? ünïcödé ✓");
    const stream = streamCall(calls);
    assert.equal(bridge.signed.length, 1);
    assert.deepEqual(bridge.signed[0], { method: "POST", path: "/chat/stream", hex: sha256(stream.opts.body) });
    assert.equal(stream.opts.headers[HEADER], `v1;enr=en_0000abcd;ts=1800000001;nonce=${"1".padStart(32, "0")};sig=${"c".repeat(64)}`);
    assert.equal(stream.opts.headers["Content-Type"], "application/json");
    assert.equal(typeof stream.opts.body, "string", "the body is sent as the string that was hashed");
    assert.equal(saveCall(calls).opts.headers[HEADER], undefined, "/chat/save needs no proof (the turn key is enough)");
});

test("a fresh proof for every turn", async (t) => {
    reset(t);
    const bridge = fakeBridge();
    window.AndroidBridge = bridge;
    const calls = stubFetch([["/chat/stream", () => sseResponse(wellnessSse())], ["/chat/save", () => json({})]]);
    await sendText("one");
    await sendText("two");
    const proofs = calls.filter((c) => c.url === "/chat/stream").map((c) => c.opts.headers[HEADER]);
    assert.equal(proofs.length, 2);
    assert.notEqual(proofs[0], proofs[1]);
    assert.equal(bridge.signed.length, 2);
});

for (const [name, sign] of Object.entries({ "null": () => null, "a throw": () => { throw new Error("no key"); } })) {
    test(`a bridge answering ${name} sends the turn unsigned, and it still runs`, async (t) => {
        reset(t);
        window.AndroidBridge = fakeBridge({ sign });
        const calls = stubFetch([["/chat/stream", () => sseResponse(wellnessSse())], ["/chat/save", () => json({})]]);
        await sendText("how did I sleep?");
        assert.deepEqual(streamCall(calls).opts.headers, { "Content-Type": "application/json" });
        assert.ok(saveCall(calls), "the turn completed");
    });
}

test("a desktop browser (no bridge) sends exactly the old request", async (t) => {
    reset(t);
    const calls = stubFetch([["/chat/stream", () => sseResponse(wellnessSse())], ["/chat/save", () => json({})]]);
    await sendText("how did I sleep?");
    assert.deepEqual(streamCall(calls).opts.headers, { "Content-Type": "application/json" });
    assert.equal(streamCall(calls).opts.body, JSON.stringify({
        messages: [{ role: "user", content: "how did I sleep?" }], provider: "anthropic", model: "", operator: "wellness-fixture",
    }));
});

test("the standard /chat fallback is signed over its JSON body too", async (t) => {
    reset(t);
    const bridge = fakeBridge();
    window.AndroidBridge = bridge;
    const calls = stubFetch([
        ["/chat/stream", () => json({ detail: "busy" }, 503)],
        ["/tasks/status", () => json({ status: "completed", result_data: { reply: "Fallback reply." } })],
        ["/chat", () => json({ task_id: "t-1" })],
        ["/health", () => json({})],
    ]);
    await sendText("how did I sleep?");   // send() awaits the 1 s task poll
    const post = calls.find((c) => c.url === "/chat");
    assert.ok(post, "fell back to /chat");
    assert.equal(bridge.signed.length, 2, "the refused stream and the fallback each asked once");
    assert.deepEqual(bridge.signed[1], { method: "POST", path: "/chat", hex: sha256(post.opts.body) });
    assert.match(post.opts.headers[HEADER], /^v1;enr=en_0000abcd;ts=1800000002;/);
    assert.ok(document.getElementById("history").textContent.includes("Fallback reply."));
});

// ── Notifications and logs carry no reply text on a wellness turn ──────────

test("a wellness reply's 'response ready' notification has no reply text; an ordinary one keeps its preview", async (t) => {
    reset(t);
    const posted = [];
    const bridge = { ...fakeBridge(), showNotification(title, body) { posted.push({ title, body }); } };
    window.AndroidBridge = globalThis.AndroidBridge = bridge;
    stubFetch([["/chat/stream", () => sseResponse(wellnessSse())], ["/chat/save", () => json({})]]);
    await sendText("how did I sleep?");
    const ready = posted.filter((p) => p.title.includes("AI Response Ready"));
    assert.equal(ready.length, 1);
    assert.ok(!ready[0].body.includes("47"), ready[0].body);
    assert.equal(ready[0].body, "Your reply is ready.");
    posted.length = 0;
    const sse = frame("stream_start", { model: "m-1" }) + frame("content", "Hello there.") + frame("done", { content: "Hello there." })
        + frame("stream_end", {});
    stubFetch([["/chat/stream", () => sseResponse(sse)], ["/chat/save", () => json({})]]);
    await sendText("hi");
    assert.equal(posted.filter((p) => p.title.includes("AI Response Ready"))[0].body, "Hello there.");
});

test("the stream's debug log carries no reply text once a wellness tool answered", async (t) => {
    reset(t);
    const logged = [];
    const originals = {};
    for (const level of ["log", "info", "debug", "warn"]) {
        originals[level] = console[level];
        console[level] = (...args) => logged.push(args.map((a) => (typeof a === "string" ? a : JSON.stringify(a))).join(" "));
    }
    t.after(() => Object.assign(console, originals));
    stubFetch([["/chat/stream", () => sseResponse(wellnessSse({ content: "Your steps today: 12,345 steps." }))],
        ["/chat/save", () => json({})]]);
    await sendText("steps today?");
    Object.assign(console, originals);
    assert.ok(logged.some((l) => l.includes("[STREAM DEBUG] Event type: content")), "the events are still logged");
    const leaks = logged.filter((l) => l.includes("12,345"));
    assert.deepEqual(leaks, []);
});

test("the snapshot-id linker logs no reply or reasoning text (a wellness reply citing a snapshot)", async (t) => {
    reset(t);
    const logged = [];
    const originals = {};
    for (const level of ["log", "info", "debug", "warn"]) {
        originals[level] = console[level];
        console[level] = (...args) => logged.push(args.map((a) => (typeof a === "string" ? a : JSON.stringify(a))).join(" "));
    }
    t.after(() => Object.assign(console, originals));
    const content = "Compared with SNAP-20261028-9001, your steps today: 12,345 steps.";
    // The box's order: the tool answers (wellness_used), then the model reasons over it and replies.
    const sse = frame("stream_start", { provider: "anthropic", model: "m-1", wellness_turn: WT })
        + frame("tool_start", "wellness_summary")
        + frame("tool_result", "wellness_summary: health data read")
        + frame("wellness_used", USED)
        + frame("thinking_start", {})
        + frame("thinking", "Per SNAP-20261028-9001 the user walked 12,345 steps.")
        + frame("thinking_end", {})
        + frame("content", content)
        + frame("done", { content })
        + frame("stream_end", { status: "ok" });
    stubFetch([["/chat/stream", () => sseResponse(sse)], ["/chat/save", () => json({})]]);
    await sendText("steps today?");
    await settle();
    assert.ok(document.querySelector("#history .clickable-snapshot"), "the id was linked (the linker ran)");
    renderHistory();                                  // and again from local history
    await settle();
    Object.assign(console, originals);
    assert.ok(logged.some((l) => l.includes("[SNAP-CLICK] Found SNAP pattern")), "the linker still logs that it found one");
    assert.deepEqual(logged.filter((l) => l.includes("12,345")), []);
});

// ── A health reply is never read aloud ─────────────────────────────────────

const ttsCalls = (calls) => calls.filter((c) => c.url.startsWith("/tts"));
// The voice service, scripted to fail fast (the positive controls only need
// to see that a request went out).
const TTS_ROUTES = [["/tts/catalog", () => json({}, 404)], ["/tts/queue", () => json({}, 404)],
    ["/tts/batch", () => json({ detail: "no voice in this test" }, 500)]];
const ordinarySse = (content = "Hello there.") => frame("stream_start", { model: "m-1" }) + frame("content", content)
    + frame("done", { content }) + frame("stream_end", {});

function click(el) {
    el.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true }));
}

test("auto-TTS never gets a health reply; an ordinary reply still goes to it", async (t) => {
    reset(t);
    const spoken = [];
    const saved = { enabled: window.autoTTSEnabled, trigger: window.triggerAutoTTS };
    window.autoTTSEnabled = true;
    window.triggerAutoTTS = (text) => spoken.push(text);
    t.after(() => { window.autoTTSEnabled = saved.enabled; window.triggerAutoTTS = saved.trigger; });
    window.AndroidBridge = fakeBridge();                // the credentialed WebView, where health replies arrive
    stubFetch([["/chat/stream", () => sseResponse(wellnessSse())], ["/chat/save", () => json({})]]);
    await sendText("how did I sleep?");
    assert.deepEqual(spoken, [], "the health reply is not handed to auto-TTS");
    stubFetch([["/chat/stream", () => sseResponse(ordinarySse())], ["/chat/save", () => json({})]]);
    await sendText("hi");
    assert.deepEqual(spoken, ["Hello there."], "an ordinary reply still is");
});

test("the speak button of a live health reply does not speak: no voice request, it says why", async (t) => {
    reset(t);
    const calls = stubFetch([["/chat/stream", () => sseResponse(wellnessSse())], ["/chat/save", () => json({})], ...TTS_ROUTES]);
    await sendText("how did I sleep?");
    const bubble = document.querySelector("#history .bubble.assistant");
    assert.equal(bubble.dataset.wellness, "1", "marked as a health reply");
    const speak = bubble.querySelector(".speak-btn");
    assert.ok(speak, "the button is still there");
    click(speak.querySelector("svg") || speak);         // a tap lands on the icon
    click(speak);
    await settle();
    assert.deepEqual(ttsCalls(calls), []);
    assert.equal(document.getElementById("toast").textContent, WELLNESS_SPEECH_OFF);
    assert.ok(!WELLNESS_SPEECH_OFF.includes("47"));
});

test("from local history, a health reply's speak buttons (reply and reasoning) do not speak", async (t) => {
    reset(t);
    const calls = stubFetch([...TTS_ROUTES]);
    setHistoryData([
        { role: "user", content: "how did I sleep?" },
        { role: "assistant", content: REPLY, wellness_ref: REF, wellness_used: USED },
        { role: "assistant", content: REPLY, thinking: "The tool said 47 bpm.", wellness_ref: REF, wellness_used: USED },
    ]);
    renderHistory();
    await settle();
    const [plain, reasoned] = document.querySelectorAll("#history .bubble.assistant");
    click(plain.querySelector(".speak-btn"));
    click(reasoned.querySelector(".speak-btn"));
    click(reasoned.querySelector('[data-bubble-action="thinking-speak"]'));
    await settle();
    assert.deepEqual(ttsCalls(calls), []);
});

test("an ordinary reply's speak button still speaks (the guard's positive control)", async (t) => {
    reset(t);
    const calls = stubFetch([["/chat/stream", () => sseResponse(ordinarySse())], ["/chat/save", () => json({})], ...TTS_ROUTES]);
    await sendText("hi");
    const bubble = document.querySelector("#history .bubble.assistant");
    assert.equal(bubble.dataset.wellness, undefined);
    click(bubble.querySelector(".speak-btn"));
    const until = Date.now() + 5000;
    while (!ttsCalls(calls).some((c) => c.url === "/tts/batch") && Date.now() < until) await settle(5);
    assert.ok(ttsCalls(calls).some((c) => c.url === "/tts/batch"), "the voice service was asked");
    await settle();
});

test("an ordinary reply's reasoning still speaks from local history (positive control)", async (t) => {
    reset(t);
    const calls = stubFetch([...TTS_ROUTES]);
    setHistoryData([{ role: "assistant", content: "Hello.", thinking: "Say hello." }]);
    renderHistory();
    await settle();
    click(document.querySelector('#history .bubble.assistant [data-bubble-action="thinking-speak"]'));
    const until = Date.now() + 5000;
    while (!ttsCalls(calls).some((c) => c.url === "/tts/batch") && Date.now() < until) await settle(5);
    assert.ok(ttsCalls(calls).some((c) => c.url === "/tts/batch"), "the voice service was asked");
    await settle();
});

test("a /chat reply the box masked live is never read aloud; an ordinary /chat reply still is", async (t) => {
    reset(t);
    const spoken = [];
    const saved = { enabled: window.autoTTSEnabled, trigger: window.triggerAutoTTS };
    window.autoTTSEnabled = true;
    window.triggerAutoTTS = (text) => spoken.push(text);
    t.after(() => { window.autoTTSEnabled = saved.enabled; window.triggerAutoTTS = saved.trigger; });
    window.AndroidBridge = fakeBridge();
    const fallback = (reply) => [
        ["/chat/stream", () => json({ detail: "busy" }, 503)],
        ["/tasks/status", () => json({ status: "completed", result_data: { reply } })],
        ["/chat", () => json({ task_id: "t-1" })],
        ["/health", () => json({})],
    ];
    stubFetch(fallback("Your sleeping heart rate was [·] bpm, a little below your usual."));
    await sendText("how did I sleep?");   // send() awaits the 1 s task poll
    const masked = [...document.querySelectorAll("#history .bubble.assistant")].at(-1);
    assert.ok(masked.textContent.includes("[·]"), "the masked reply is shown");
    assert.equal(masked.dataset.wellness, "1", "and marked as a health reply");
    assert.deepEqual(spoken, []);
    stubFetch(fallback("Fallback reply."));
    await sendText("hi");
    assert.deepEqual(spoken.map((x) => x.trim()), ["Fallback reply."]);
});
