// CLI-agent handlers in the Portal (audit rank 66: P03-live-agents-web-01,
// -04, -12).
//
//  * enhanceContent wrapped every streamed line starting with '-' or '+' in
//    red/green diff spans, so every markdown bullet list rendered as a diff.
//  * Each session restore built a NEW AgentChatHandler whose constructor added
//    document-level listeners, so Shift+Tab (and the mode indicator) cycled
//    the permission mode once per instance.
//  * A backend 'error' (e.g. "No active session" after a service restart) left
//    the UI streaming forever: isStreaming stayed true, the loader stayed up,
//    and the dead session kept receiving every next prompt.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

for (const [id, cls] of [["claudeCodeBanner", ""], ["permissionModeIndicator", ""], ["thinkingIndicator", "hide"],
    ["todoPanel", ""], ["agentStatusLine", ""]]) {
    const el = document.createElement("div");
    el.id = id;
    if (cls) el.className = cls;
    document.body.appendChild(el);
}

const json = (obj) => new Response(JSON.stringify(obj), { status: 200, headers: { "Content-Type": "application/json" } });
const agent = await import("../../Portal/modules/agent-handler.js");
const gemini = await import("../../Portal/modules/gemini-agent-handler.js");
const state = await import("../../Portal/modules/state-management.js");

test("streamed bullet lists and rules are never wrapped in diff spans", () => {
    const handler = new agent.AgentChatHandler();
    const text = "Steps:\n- install\n- run\n+ extra\n---\n@@ not a hunk";
    const out = handler.enhanceContent(text);
    assert.ok(!/diff-(add|remove|hunk|file)/.test(out), out);
    assert.equal(out, text);
    // file references are still highlighted
    assert.match(handler.enhanceContent("see app.js:12"), /class="file-ref"/);
    handler.dispose();

    const g = new gemini.GeminiAgentHandler();
    assert.equal(g.enhanceContent(text), text);
    g.dispose();
});

test("restoring a session twice reuses ONE handler: Shift+Tab cycles once", async () => {
    globalThis.fetch = window.fetch = async () => json({ session: null });
    localStorage.clear();
    state.saveOperatorSession("Ana", "sess-ana");
    state.setOperator("Ana");
    await agent.checkExistingAgentSession();
    const first = agent.getAgentHandler();
    await agent.checkExistingAgentSession();
    await agent.checkExistingAgentSession();
    assert.equal(agent.getAgentHandler(), first, "no second AgentChatHandler");

    const before = first.permissionMode;
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true, cancelable: true }));
    const after = first.permissionMode;
    assert.notEqual(after, before, "Shift+Tab cycles the mode");
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true, cancelable: true }));
    assert.equal(first.permissionMode, before, "and again cycles it back: exactly one listener");
});

test("concurrent restores for the same operator (boot + a re-select) share one probe", async () => {
    let probes = 0;
    globalThis.fetch = window.fetch = async () => { probes += 1; return json({ session: null }); };
    state.setOperator("Ana");
    await Promise.all([agent.checkExistingAgentSession(), agent.checkExistingAgentSession()]);
    assert.equal(probes, 1);
});

test("a disposed handler's listeners are gone", () => {
    const h = new agent.AgentChatHandler();
    let cycles = 0;
    h.cyclePermissionMode = () => { cycles += 1; };
    h.dispose();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true }));
    document.getElementById("permissionModeIndicator").click();
    assert.equal(cycles, 0);
});

test("a backend 'No active session' error ends the streaming state and forgets the session", () => {
    state.setOperator("Ana");
    state.saveOperatorSession("Ana", "sess-gone");
    const h = agent.getAgentHandler() || new agent.AgentChatHandler();
    h.currentOperator = "Ana";
    h.sessionId = "sess-gone";
    h.hasActiveSession = true;
    h.isStreaming = true;
    const bubble = document.createElement("div");
    bubble.className = "bubble assistant agent-streaming";
    bubble.innerHTML = '<div class="bubble-text"><div class="agent-loading">Starting Claude Code session...</div></div>';
    document.getElementById("history").appendChild(bubble);
    h.currentBubble = bubble;

    h.handleMessage({ type: "error", data: "No active session found to reconnect to" });

    assert.equal(h.isStreaming, false);
    assert.ok(!bubble.classList.contains("agent-streaming"));
    assert.equal(bubble.querySelector(".agent-loading"), null, "the loader is replaced");
    assert.match(bubble.querySelector(".agent-error").textContent, /No active session/);
    assert.equal(h.sessionId, null);
    assert.equal(h.hasActiveSession, false);
    assert.equal(state.getOperatorSession("Ana"), null, "the dead session id is not restored on reload");
});

test("a Gemini CLI error also ends its streaming state", () => {
    const g = new gemini.GeminiAgentHandler();
    g.isStreaming = true;
    const bubble = document.createElement("div");
    bubble.className = "bubble assistant agent-streaming";
    bubble.innerHTML = '<div class="agent-loading">Continuing conversation...</div>';
    g.currentBubble = bubble;
    g.handleMessage({ type: "error", data: "Gemini CLI crashed" });
    assert.equal(g.isStreaming, false);
    assert.ok(!bubble.classList.contains("agent-streaming"));
    assert.equal(bubble.querySelector(".agent-loading"), null);
    g.dispose();
});

// ── An operator switch mid-probe restores the NEW operator's session ─────
// The shared in-flight probe was handed to every caller whoever it was for:
// switching operator while the previous operator's probe was in flight got
// the OLD probe back, so the old operator's session was attached under the
// new operator and the new one's was never looked up.
function slowSessions(probed, sessions) {
    globalThis.fetch = window.fetch = async (url) => {
        probed.push(String(url));
        await new Promise((r) => setTimeout(r, 30));
        return json({ session: null });
    };
    localStorage.clear();
    for (const [op, id] of sessions) state.saveOperatorSession(op, id);
}

test("switching operator during the boot probe restores the new operator's Claude session", async () => {
    const probed = [];
    slowSessions(probed, [["Ana", "sess-ana"], ["Ben", "sess-ben"]]);
    state.setOperator("Ana");
    const boot = agent.checkExistingAgentSession();
    state.setOperator("Ben");
    await agent.restoreAgentSessionForOperatorSwitch();
    await boot;
    const h = agent.getAgentHandler();
    assert.equal(h.currentOperator, "Ben");
    assert.equal(h.sessionId, "sess-ben");
    assert.ok(probed.includes("/agent/session/Ben"), "the new operator's session is looked up");
});

test("switching operator during the boot probe restores the new operator's Gemini session", async () => {
    const probed = [];
    slowSessions(probed, [["Ana-gemini", "g-ana"], ["Ben-gemini", "g-ben"]]);
    state.setOperator("Ana");
    const boot = gemini.checkExistingGeminiAgentSession();
    state.setOperator("Ben");
    await gemini.restoreGeminiSessionForOperatorSwitch();
    await boot;
    const g = gemini.getGeminiAgentHandler();
    assert.equal(g.currentOperator, "Ben");
    assert.equal(g.sessionId, "g-ben");
    assert.ok(probed.includes("/gemini-agent/session/Ben"));
});

// ── Every exit from the Claude handler's streaming state is finalized ─────
class FakeSocket {
    constructor(url) { this.url = url; this.readyState = 0; FakeSocket.last = this; }
    send() {}
    close() { this.readyState = 3; }
}
FakeSocket.CONNECTING = 0;
FakeSocket.OPEN = 1;
FakeSocket.CLOSING = 2;
FakeSocket.CLOSED = 3;

async function withFakeSocket(fn) {
    const real = globalThis.WebSocket;
    globalThis.WebSocket = window.WebSocket = FakeSocket;
    try { return await fn(); } finally { globalThis.WebSocket = window.WebSocket = real; }
}

function assertFinalized(h, bubble) {
    assert.equal(h.isStreaming, false);
    assert.ok(!bubble.classList.contains("agent-streaming"), "no stale .agent-streaming scroll target");
    assert.equal(bubble.querySelector(".agent-loading"), null, "the loader is replaced");
}

for (const [name, fire] of [
    ["errors", (ws) => ws.onerror(new Event("error"))],
    ["closes cleanly", (ws) => ws.onclose({ code: 1000 })],
]) {
    test(`a Claude follow-up socket that ${name} leaves the streaming state`, async () => {
        await withFakeSocket(async () => {
            const h = new agent.AgentChatHandler();
            h.sessionId = "s1";
            h.hasActiveSession = true;
            h.currentOperator = "Ana";
            await h.sendInput("continue please");
            const bubble = h.currentBubble;
            assert.ok(bubble.querySelector(".agent-loading"), "the follow-up shows its loader");
            fire(FakeSocket.last);
            assertFinalized(h, bubble);
            h.dispose();
        });
    });
}

test("giving up on reconnecting leaves the streaming state", () => {
    const h = new agent.AgentChatHandler();
    h.sessionId = "s2";
    h.isStreaming = true;
    const bubble = document.createElement("div");
    bubble.className = "bubble assistant agent-streaming";
    bubble.innerHTML = '<div class="bubble-text"><div class="agent-loading">Continuing conversation...</div></div>';
    h.currentBubble = bubble;
    h.reconnectAttempts = h.maxReconnectAttempts;
    h.attemptReconnect();
    assertFinalized(h, bubble);
    assert.match(bubble.textContent, /connection/i);
    h.dispose();
});

test("a follow-up socket a later turn replaced does not end that later turn", async () => {
    await withFakeSocket(async () => {
        for (const h of [new agent.AgentChatHandler(), new gemini.GeminiAgentHandler()]) {
            h.sessionId = "s3";
            h.hasActiveSession = true;
            h.currentOperator = "Ana";
            await h.sendInput("first");
            const first = FakeSocket.last;
            first.readyState = FakeSocket.CLOSING;          // closing as the next prompt goes out
            await h.sendInput("second");
            assert.notEqual(FakeSocket.last, first);
            const bubble = h.currentBubble;
            first.onclose({ code: 1000 });                   // the old socket finishes closing
            assert.equal(h.isStreaming, true, "the second turn is still streaming");
            assert.ok(bubble.querySelector(".agent-loading"));
            FakeSocket.last.onclose({ code: 1000 });
            assertFinalized(h, bubble);
            h.dispose();
        }
    });
});
