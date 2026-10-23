// The Portal resolves its operator from GET /operators, never from a seed name
// (audit A09-data-local-model-05 / P01-chat-core-web-14 / P03-live-agents-web-11 /
// P06-comms-web-02). The rule is the one Android's OperatorResolver applies:
//   - the persisted pick, while the box still lists it;
//   - otherwise the box's only person (a reserved overseer such as the Flight
//     Recorder is never anyone's operator by default);
//   - otherwise nobody: several people -> the operator picks; none -> guidance.
// Until an operator is resolved nothing is sent and nothing probes the server
// under a made-up name.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

const settle = () => new Promise((r) => setTimeout(r, 30));
const json = (b, status = 200) => Promise.resolve({
    ok: status >= 200 && status < 300, status,
    json: () => Promise.resolve(b), text: () => Promise.resolve(JSON.stringify(b)),
});

const state = await import("../../Portal/modules/state-management.js");

document.body.insertAdjacentHTML("beforeend", `<select id="operatorSelect"></select><textarea id="prompt"></textarea>`);
const select = () => document.getElementById("operatorSelect");
const toastText = () => document.getElementById("toast").textContent;

/** Serve GET /operators with [roster]; record every URL fetched. */
function serveOperators(roster) {
    const urls = [];
    globalThis.fetch = (url) => {
        urls.push(String(url));
        if (String(url) === "/operators") return json(roster);
        return json({});
    };
    return urls;
}

// ── the pure rule ────────────────────────────────────────────────────────
test("chooseOperator keeps a persisted pick the box still lists", () => {
    const r = state.chooseOperator({ persisted: "Ana", operators: ["Flight Recorder", "Ana", "Ben"], reserved: ["Flight Recorder"] });
    assert.deepEqual(r, { operator: "Ana", needsSelection: false, noOperators: false });
    // An explicit pick of the overseer is still a pick.
    assert.equal(state.chooseOperator({ persisted: "Flight Recorder", operators: ["Flight Recorder", "Ana"], reserved: ["Flight Recorder"] }).operator, "Flight Recorder");
});

test("chooseOperator auto-selects the only person, never the reserved overseer", () => {
    const r = state.chooseOperator({ persisted: "", operators: ["Flight Recorder", "Ana"], reserved: ["Flight Recorder"] });
    assert.deepEqual(r, { operator: "Ana", needsSelection: false, noOperators: false });
    // A stale pick (operator removed on the box) falls back to the same rule.
    assert.equal(state.chooseOperator({ persisted: "Gone", operators: ["Ana"], reserved: [] }).operator, "Ana");
});

test("chooseOperator leaves several people unresolved for the operator to pick", () => {
    const r = state.chooseOperator({ persisted: "Gone", operators: ["Flight Recorder", "Ana", "Ben"], reserved: ["Flight Recorder"] });
    assert.deepEqual(r, { operator: "", needsSelection: true, noOperators: false });
});

test("chooseOperator reports a box with nobody on it", () => {
    assert.deepEqual(state.chooseOperator({ persisted: "", operators: [], reserved: [] }),
        { operator: "", needsSelection: false, noOperators: true });
    assert.deepEqual(state.chooseOperator({ persisted: "", operators: ["Flight Recorder"], reserved: ["Flight Recorder"] }),
        { operator: "", needsSelection: false, noOperators: true });
    assert.deepEqual(state.chooseOperator(), { operator: "", needsSelection: false, noOperators: true });
});

// ── the selector ─────────────────────────────────────────────────────────
test("one person on the box: the selector picks them and persists the pick", async () => {
    localStorage.clear();
    const urls = serveOperators({ operators: ["Flight Recorder", "Ana"], default: "Ana", reserved: ["Flight Recorder"] });
    const active = await state.initOperatorSelector();
    assert.equal(active, "Ana");
    assert.equal(state.getOperator(), "Ana");
    assert.equal(select().value, "Ana");
    assert.ok(urls.includes("/operators"), "the roster comes from GET /operators");
});

test("several people and no pick: nothing is selected and the operator is asked", async () => {
    localStorage.clear();
    serveOperators({ operators: ["Flight Recorder", "Ana", "Ben"], default: "Ben", reserved: ["Flight Recorder"] });
    const active = await state.initOperatorSelector();
    assert.equal(active, "");
    assert.equal(state.getOperator(), "", "the server default is never silently adopted");
    assert.equal(select().value, "");
    const placeholder = select().querySelector('option[value=""]');
    assert.ok(placeholder, "a placeholder option asks for a pick");
    assert.match(toastText(), /select/i);
    // The operator's pick resolves it.
    select().value = "Ben";
    select().onchange();
    assert.equal(state.getOperator(), "Ben");
});

test("a stale persisted pick is cleared instead of being sent", async () => {
    localStorage.setItem(state.OP_STORE, "Removed Operator");
    serveOperators({ operators: ["Ana", "Ben"], default: "Ana", reserved: [] });
    await state.initOperatorSelector();
    assert.equal(state.getOperator(), "");
});

test("no operators: guidance, no invented name", async () => {
    localStorage.clear();
    serveOperators({ operators: [], default: "", reserved: [] });
    const active = await state.initOperatorSelector();
    assert.equal(active, "");
    assert.equal(state.getOperator(), "");
    assert.match(toastText(), /no operators/i);
});

test("an unreachable box keeps the persisted pick", async () => {
    localStorage.setItem(state.OP_STORE, "Ana");
    globalThis.fetch = () => Promise.reject(new Error("offline"));
    const active = await state.initOperatorSelector();
    assert.equal(active, "Ana");
    assert.equal(state.getOperator(), "Ana");
});

test("a reply that is not a roster keeps the persisted pick (an error is not an empty box)", async () => {
    // A JSON error body (a 5xx, or the origin guard's 403) has no operators array:
    // reading it as "nobody on the box" would clear a valid pick.
    for (const [body, status] of [[{ detail: "boom" }, 500], [{ detail: "Forbidden origin" }, 403], [{ status: "ok" }, 200]]) {
        localStorage.setItem(state.OP_STORE, "Ana");
        document.getElementById("toast").textContent = "";
        globalThis.fetch = () => json(body, status);
        const active = await state.initOperatorSelector();
        assert.equal(active, "Ana", `status ${status}`);
        assert.equal(state.getOperator(), "Ana", `status ${status}: the pick is kept`);
        assert.doesNotMatch(toastText(), /no operators/i, `status ${status}: no false guidance`);
    }
});

test("switching operator in the selector is visible to every reader at once", async () => {
    localStorage.clear();
    serveOperators({ operators: ["Ana", "Ben"], default: "Ana", reserved: [] });
    await state.initOperatorSelector();
    let seen = null;
    window.addEventListener("bbx:operator-changed", (e) => { seen = e.detail.operator; }, { once: true });
    select().value = "Ana";
    select().onchange();
    assert.equal(state.getOperator(), "Ana");
    assert.equal(seen, "Ana");
});

// ── nothing goes out under an unresolved operator ────────────────────────
test("send() refuses while no operator is resolved", async () => {
    const { send } = await import("../../Portal/modules/chat-send.js");
    localStorage.clear();
    const urls = [];
    globalThis.fetch = (url) => { urls.push(String(url)); return json({}); };
    document.getElementById("prompt").value = "hello";
    await send();
    assert.match(toastText(), /select an operator/i);
    assert.deepEqual(urls, [], "no request leaves without an operator");
    assert.equal(document.getElementById("prompt").value, "hello", "the draft is kept");
});

test("agent-session restore probes the resolved operator, URL-encoded, and skips when none", async () => {
    const { checkExistingAgentSession } = await import("../../Portal/modules/agent-handler.js");
    const { checkExistingGeminiAgentSession } = await import("../../Portal/modules/gemini-agent-handler.js");
    const urls = [];
    globalThis.fetch = (url) => { urls.push(String(url)); return json({ session: null }); };

    localStorage.clear();
    await checkExistingAgentSession();
    await checkExistingGeminiAgentSession();
    assert.deepEqual(urls, [], "no probe under an invented operator");

    state.setOperator("Ana Lee");
    await checkExistingAgentSession();
    await checkExistingGeminiAgentSession();
    assert.deepEqual(urls, ["/agent/session/Ana%20Lee", "/gemini-agent/session/Ana%20Lee"]);
});

test("a persisted agent session is restored for the operator it belongs to", async () => {
    const agent = await import("../../Portal/modules/agent-handler.js");
    globalThis.fetch = () => json({ session: null });
    localStorage.clear();
    state.saveOperatorSession("Ana", "sess-ana");
    state.saveOperatorSession("Ben", "sess-ben");
    state.setOperator("Ben");
    await agent.checkExistingAgentSession();
    const handler = agent.getAgentHandler();
    assert.ok(handler, "a handler is pre-loaded for the persisted session");
    assert.equal(handler.currentOperator, "Ben");
    assert.equal(handler.sessionId, "sess-ben");
});

test("startSession never continues another operator's agent session", async () => {
    const { AgentChatHandler } = await import("../../Portal/modules/agent-handler.js");
    const sockets = [];
    const RealWS = globalThis.WebSocket;
    globalThis.WebSocket = class {
        constructor(url) { this.url = url; this.readyState = 0; sockets.push(this); }
        send() {}
        close() { this.readyState = 3; }
    };
    globalThis.WebSocket.OPEN = 1;
    try {
        localStorage.clear();
        state.saveOperatorSession("Ben", "sess-ben");
        const h = new AgentChatHandler();
        h.currentOperator = "Ana";
        h.sessionId = "sess-ana";
        h.hasActiveSession = true;
        await h.startSession("hi", "Ben");
        assert.equal(h.currentOperator, "Ben");
        assert.equal(h.sessionId, "sess-ben", "Ben's own session is the one restored");
        assert.ok(sockets.at(-1).url.endsWith("/ws/agent/sess-ben"));
        assert.equal(h.belongsTo("Ben"), true);
        assert.equal(h.belongsTo("Ana"), false);
    } finally {
        globalThis.WebSocket = RealWS;
    }
});

test("switching operator restores the new operator's session, not the old one's", async () => {
    const agent = await import("../../Portal/modules/agent-handler.js");
    const urls = [];
    globalThis.fetch = (url) => { urls.push(String(url)); return json({ session: null }); };
    localStorage.clear();
    state.saveOperatorSession("Ana", "sess-ana");
    state.saveOperatorSession("Ben", "sess-ben");
    state.setOperator("Ana");
    await agent.checkExistingAgentSession();
    assert.equal(agent.getAgentHandler().sessionId, "sess-ana");

    state.setOperator("Ben");
    await agent.restoreAgentSessionForOperatorSwitch();
    const handler = agent.getAgentHandler();
    assert.equal(handler.currentOperator, "Ben");
    assert.equal(handler.sessionId, "sess-ben");
    assert.ok(urls.includes("/agent/session/Ben"));
    // Ana's session is let go, not ended: it is still hers to resume.
    assert.equal(state.getOperatorSession("Ana"), "sess-ana");
});

test("startSession refuses without an operator", async () => {
    const { AgentChatHandler } = await import("../../Portal/modules/agent-handler.js");
    const { GeminiAgentHandler } = await import("../../Portal/modules/gemini-agent-handler.js");
    localStorage.clear();
    const sockets = [];
    const RealWS = globalThis.WebSocket;
    globalThis.WebSocket = class { constructor(url) { sockets.push(url); } send() {} close() {} };
    try {
        await new AgentChatHandler().startSession("hi", "");
        assert.match(toastText(), /select an operator/i);
        await new GeminiAgentHandler().startSession("hi", "");
        assert.match(toastText(), /select an operator/i);
        assert.deepEqual(sockets, []);
    } finally {
        globalThis.WebSocket = RealWS;
    }
});

test("task notifications for another operator are filtered, an unresolved one is not", async () => {
    const { showNotification } = await import("../../Portal/modules/notifications.js");
    const shown = [];
    window.AndroidBridge = { showNotification: (title) => shown.push(title) };
    globalThis.AndroidBridge = window.AndroidBridge;
    try {
        state.setOperator("Ana");
        showNotification("for ben", { operator: "Ben" });
        showNotification("for ana", { operator: "Ana" });
        state.setOperator("");
        showNotification("unresolved", { operator: "Ben" });
        assert.deepEqual(shown, ["for ana", "unresolved"]);
    } finally {
        delete window.AndroidBridge;
        delete globalThis.AndroidBridge;
    }
    await settle();
});
