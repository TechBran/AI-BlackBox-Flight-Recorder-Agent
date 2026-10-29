// The Claude Code and Gemini CLI agent handlers on one BaseAgentHandler (audit
// rank 75, P03-live-agents-web-17): the frames each sends, where each keeps its
// session and history, and its banner are pinned here so the shared base
// cannot blur the two agents together.
//
//  * A follow-up turn kept the previous turn's "streaming content seen" flag,
//    so a reply that came only as the CLI's complete "assistant" message (the
//    non-streaming path, agent_routes.py sends it as assistant_message) was
//    dropped as a duplicate and the follow-up bubble stayed empty.
//
// Run: cd tests/web && npm ci && node --test
import test, { after } from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

for (const id of ["claudeCodeBanner", "claudeAgentStatus", "claudeStatusCompact", "endAgentSession",
    "geminiCodeBanner", "geminiAgentStatus", "geminiStatusCompact", "endGeminiSession",
    "permissionModeIndicator", "todoPanel", "agentStatusLine"]) {
    const el = document.createElement("div");
    el.id = id;
    document.body.appendChild(el);
}
const indicator = document.createElement("div");
indicator.id = "thinkingIndicator";
indicator.className = "hide";
document.body.appendChild(indicator);

class FakeSocket {
    static all = [];
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; FakeSocket.all.push(this); }
    send(data) { this.sent.push(JSON.parse(data)); }
    close() { this.readyState = 3; }
    open() { this.readyState = 1; this.onopen(); }
}
FakeSocket.CONNECTING = 0;
FakeSocket.OPEN = 1;
FakeSocket.CLOSING = 2;
FakeSocket.CLOSED = 3;
globalThis.WebSocket = window.WebSocket = FakeSocket;

localStorage.clear();
const agent = await import("../../Portal/modules/agent-handler.js");
const gemini = await import("../../Portal/modules/gemini-agent-handler.js");
const state = await import("../../Portal/modules/state-management.js");
const { statusLine } = await import("../../Portal/modules/status-line.js");
after(() => statusLine.endSession());   // its duration timer runs for an open Claude session

const UUID = /\/([0-9a-f-]{36})$/;

test("Claude Code: the prompt and input frames, on /ws/agent/", async () => {
    localStorage.clear();
    state.setOperator("Ana");
    const h = new agent.AgentChatHandler();
    await h.startSession("hello", "Ana");
    const ws = FakeSocket.all.at(-1);
    assert.match(ws.url, /\/ws\/agent\/[0-9a-f-]{36}$/);
    assert.equal(state.getOperatorSession("Ana"), ws.url.match(UUID)[1], "kept under the operator");
    ws.open();
    assert.deepEqual(ws.sent[0], {
        type: "prompt", text: "hello", operator: "Ana", working_dir: null, model: "sonnet",
        permission_mode: "yolo", skip_permissions: true,
    });
    h.handleMessage({ type: "content", data: "Hi Ana" });
    h.handleMessage({ type: "completed" });
    const entry = state.getHistoryData().filter((m) => m.role === "assistant").at(-1);
    assert.equal(entry.isAgent, true);
    assert.match(entry.content, /Hi Ana/);

    await h.sendInput("more");
    assert.deepEqual(ws.sent[1], { type: "input", text: "more", permission_mode: "yolo", skip_permissions: true });
    h.dispose();
});

test("Gemini CLI: the prompt frames, on /ws/gemini-agent/, its session under <operator>-gemini", async () => {
    localStorage.clear();
    state.setOperator("Ana");
    const g = new gemini.GeminiAgentHandler();
    await g.startSession("hello", "Ana");
    const ws = FakeSocket.all.at(-1);
    assert.match(ws.url, /\/ws\/gemini-agent\/[0-9a-f-]{36}$/);
    assert.equal(state.getOperatorSession("Ana-gemini"), ws.url.match(UUID)[1]);
    assert.equal(state.getOperatorSession("Ana"), null, "never the Claude session's slot");
    ws.open();
    assert.deepEqual(ws.sent[0], {
        type: "prompt", text: "hello", operator: "Ana", working_dir: null, yolo_mode: true, resume_session: null,
    });
    g.handleMessage({ type: "content", data: "Hi from Gemini" });
    g.handleMessage({ type: "completed" });
    const entry = state.getHistoryData().filter((m) => m.role === "assistant").at(-1);
    assert.equal(entry.isGeminiAgent, true);
    assert.equal(entry.isAgent, undefined);

    // The CLI exits after each turn; the follow-up reconnects and re-prompts.
    ws.close();
    await g.sendInput("more");
    const next = FakeSocket.all.at(-1);
    assert.notEqual(next, ws);
    assert.equal(next.url, ws.url);
    next.open();
    assert.deepEqual(next.sent[0], { type: "prompt", text: "more", operator: "Ana", yolo_mode: true });

    // A resumed prompt tells the backend to resume.
    next.close();
    g.hasActiveSession = true;
    await g.startSession("again", "Ana");
    FakeSocket.all.at(-1).open();
    assert.equal(FakeSocket.all.at(-1).sent[0].resume_session, true);
    g.dispose();
});

test("each agent drives its own banner", () => {
    const h = new agent.AgentChatHandler();
    const g = new gemini.GeminiAgentHandler();
    h.updateBannerStatus("Responding...", true);
    g.updateBannerStatus("Session Ready");
    assert.equal(document.getElementById("claudeAgentStatus").textContent, "Responding...");
    assert.ok(document.getElementById("claudeCodeBanner").classList.contains("working"));
    assert.equal(document.getElementById("geminiAgentStatus").textContent, "Session Ready");
    assert.ok(document.getElementById("geminiCodeBanner").classList.contains("session-ready"));
    assert.ok(!document.getElementById("geminiCodeBanner").classList.contains("working"));
    h.dispose();
    g.dispose();
});

test("a Gemini 'No session found' error forgets the Gemini session only", () => {
    localStorage.clear();
    state.saveOperatorSession("Ana", "claude-sess");
    state.saveOperatorSession("Ana-gemini", "gemini-sess");
    const g = new gemini.GeminiAgentHandler();
    g.currentOperator = "Ana";
    g.sessionId = "gemini-sess";
    g.hasActiveSession = true;
    g.handleMessage({ type: "error", data: "No session found" });
    assert.equal(g.sessionId, null);
    assert.equal(state.getOperatorSession("Ana-gemini"), null);
    assert.equal(state.getOperatorSession("Ana"), "claude-sess");
    g.dispose();
});

test("a follow-up reply that arrives only as the complete assistant message is shown", async () => {
    localStorage.clear();
    state.setOperator("Ana");
    const h = new agent.AgentChatHandler();
    await h.startSession("first", "Ana");
    const ws = FakeSocket.all.at(-1);
    ws.open();
    h.handleMessage({ type: "content", data: "Streamed reply" });
    h.handleMessage({ type: "assistant_message", data: "Streamed reply" });   // the duplicate
    h.handleMessage({ type: "completed" });
    assert.equal(h.outputBuffer, "Streamed reply", "the duplicate is dropped");

    await h.sendInput("second");
    h.handleMessage({ type: "assistant_message", data: "Whole second reply" });
    assert.equal(h.outputBuffer, "Whole second reply");
    h.dispose();
});

test("the Gemini tool icons include the Claude Code ones", () => {
    const h = new agent.AgentChatHandler();
    const g = new gemini.GeminiAgentHandler();
    for (const tool of ["Bash", "Read", "Edit", "WebFetch"]) assert.equal(g.getToolIcon(tool), h.getToolIcon(tool));
    assert.equal(g.getToolIcon("read_file"), "📄");
    assert.equal(h.getToolIcon("read_file"), "🔧", "Claude Code has no read_file tool");
    h.dispose();
    g.dispose();
});
