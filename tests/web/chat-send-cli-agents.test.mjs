// send() for the two CLI agents and the one history renderer (audit
// P01-chat-core-web-20, rank 75).
//
//  * The Claude Code and Gemini CLI branches of send() were two ~180-line
//    copies (upload loop, ATTACHED FILES block, composer styling); they are
//    one sendToCliAgent now, pinned here per agent. The Claude copy also
//    toasted a leftover "[DEBUG v103] Agent mode" on every send.
//  * History rendered two ways: app-init's renderer restored pending turns,
//    reasoning and the Computer Use log, chat-bubbles' did not, and a Computer
//    Use task that finished in the background was rendered with the latter,
//    so its activity panel only appeared after a reload.
//
// Run: cd tests/web && npm ci && node --test
import test, { after } from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

const prompt = document.createElement("textarea");
prompt.id = "prompt";
document.body.appendChild(prompt);
localStorage.setItem("bbx_operator", "tester");

const settle = (ms = 30) => new Promise((r) => setTimeout(r, ms));
const json = (body, status = 200) => Promise.resolve(new Response(JSON.stringify(body), {
    status, headers: { "Content-Type": "application/json" },
}));

class FakeSocket {
    static all = [];
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; FakeSocket.all.push(this); }
    send(data) { this.sent.push(JSON.parse(data)); }
    close() { this.readyState = 3; }
    open() { this.readyState = 1; this.onopen(); }
}
FakeSocket.OPEN = 1;
FakeSocket.CONNECTING = 0;
globalThis.WebSocket = window.WebSocket = FakeSocket;

// /upload/session: answers with the folder the form named.
const uploads = [];
globalThis.XMLHttpRequest = window.XMLHttpRequest = class {
    constructor() { this.upload = { addEventListener() {} }; this._l = {}; }
    addEventListener(type, fn) { this._l[type] = fn; }
    open(method, url) { this.url = url; }
    send(form) {
        const session = form.get("session_id");
        const file = form.get("file");
        uploads.push({ url: this.url, session, name: file.name });
        setTimeout(() => {
            this.status = 200;
            this.responseText = JSON.stringify({
                filename: file.name, path: `/srv/uploads/sessions/${session}/${file.name}`, url: `/ui/uploads/sessions/${session}/${file.name}`,
            });
            this._l.load && this._l.load();
        }, 0);
    }
};

const toasts = [];
const core = await import("../../Portal/modules/core-utils.js");
const chat = await import("../../Portal/modules/chat-send.js");
const { addAttachedFile, getAttachedFiles } = await import("../../Portal/modules/file-upload.js");
const state = await import("../../Portal/modules/state-management.js");
const agent = await import("../../Portal/modules/agent-handler.js");
const gemini = await import("../../Portal/modules/gemini-agent-handler.js");
const { statusLine } = await import("../../Portal/modules/status-line.js");
after(() => statusLine.endSession());

// Every toast: each one replaces #toast's text node, so every record counts
// (a toast overwritten in the same tick is still seen).
const toastObserver = new MutationObserver((records) => {
    for (const r of records) for (const n of r.addedNodes) toasts.push(n.textContent);
});
toastObserver.observe(document.getElementById("toast"), { childList: true });
after(() => toastObserver.disconnect());
void core;

function resetPage() {
    document.getElementById("history").innerHTML = "";
    state.setHistoryData([]);
    uploads.length = 0;
    toasts.length = 0;
    FakeSocket.all = [];
    globalThis.fetch = window.fetch = () => json({});
}

const AGENTS = [
    { provider: "agents", folder: /^session-\d+-[a-z0-9]+$/, path: "/ws/agent/", slot: (op) => op },
    { provider: "gemini-agents", folder: /^gemini-session-\d+-[a-z0-9]+$/, path: "/ws/gemini-agent/", slot: (op) => `${op}-gemini` },
];

for (const a of AGENTS) {
    test(`${a.provider}: a first prompt uploads its files to a new session folder and names them`, async () => {
        resetPage();
        localStorage.removeItem("bbx_agent_sessions");
        window.__provider = a.provider;
        addAttachedFile(new File(["x"], "notes.txt", { type: "text/plain" }));
        prompt.value = "read my notes";
        await chat.send();
        await settle(700);   // the upload, then the 500 ms "uploaded" pause

        assert.equal(uploads.length, 1);
        assert.match(uploads[0].session, a.folder);
        assert.equal(getAttachedFiles().length, 0, "the attachments are cleared");
        assert.equal(prompt.value, "");
        const ws = FakeSocket.all.at(-1);
        assert.ok(ws.url.includes(a.path), ws.url);
        ws.open();
        const sent = ws.sent[0];
        assert.equal(sent.type, "prompt");
        assert.ok(sent.text.startsWith("read my notes\n\n--- ATTACHED FILES ---\n"), sent.text);
        assert.ok(sent.text.includes(`- notes.txt: /srv/uploads/sessions/${uploads[0].session}/notes.txt`));
        const user = [...document.querySelectorAll("#history .bubble.user")].at(-1);
        assert.match(user.textContent, /read my notes/);
        assert.ok(!toasts.some((t) => t.includes("DEBUG")), toasts.join(" | "));
    });

    test(`${a.provider}: a follow-up's files go to the open session's own folder`, async () => {
        resetPage();
        window.__provider = a.provider;
        const handler = a.provider === "agents" ? agent.getAgentHandler() : gemini.getGeminiAgentHandler();
        assert.ok(handler, "the first prompt made the handler");
        handler.hasActiveSession = true;
        handler.isStreaming = false;
        const ws = FakeSocket.all.at(-1) || handler.ws;
        if (ws) ws.readyState = 1;
        addAttachedFile(new File(["y"], "data.csv", { type: "text/csv" }));
        prompt.value = "and this";
        await chat.send();
        await settle(700);

        assert.equal(uploads.length, 1);
        assert.equal(uploads[0].session, handler.sessionId);
        const user = [...document.querySelectorAll("#history .bubble.user")].at(-1);
        assert.match(user.textContent, /and this\s*\[Attached 1 file\(s\) to session folder\]/);
        const frame = (handler.ws.sent || []).at(-1) || FakeSocket.all.at(-1).sent.at(-1);
        assert.ok(frame.text.includes("--- ATTACHED FILES ---"), JSON.stringify(frame));
        assert.ok(frame.text.includes(`- data.csv: /srv/uploads/sessions/${handler.sessionId}/data.csv`));
    });
}

test("history renders pending turns, reasoning, the Computer Use log and failed turns alike", () => {
    resetPage();
    state.setHistoryData([
        { role: "user", content: "hello", failed: true, sendKey: "k1", failedAt: 1 },
        { role: "assistant", content: "**done**", thinking: "mulling it over", cuLog: [{ type: "action", action: "click", params: "x=1", step: 2 }] },
        { role: "assistant", content: "...", pending: true, pendingTimestamp: Date.now() },
    ]);
    chat.renderHistory();
    const hist = document.getElementById("history");
    assert.ok(hist.querySelector(".bubble.user .bubble-retry"), "the failed turn keeps its retry chip");
    assert.ok(hist.querySelector(".cu-activity-panel"), "the Computer Use log is restored");
    assert.match(hist.querySelector(".cu-activity-panel").textContent, /click/);
    assert.ok(hist.querySelector(".streaming-thinking-panel"), "the reasoning panel is restored");
    assert.equal(hist.querySelectorAll(".bubble").length >= 3, true);
});

test("a Computer Use task that finished in the background shows its activity panel at once", async () => {
    resetPage();
    globalThis.fetch = window.fetch = (url) => String(url).startsWith("/chat/cu-status")
        ? json({ status: "complete", user_message: "open the site", final_response: "Opened it.",
            cu_log: [{ type: "action", action: "navigate", params: "example.org", step: 1 }] })
        : json({});
    await chat.checkCUBackgroundStatus();
    const hist = document.getElementById("history");
    assert.match(hist.textContent, /open the site/);
    assert.match(hist.textContent, /Opened it\./);
    assert.ok(hist.querySelector(".cu-activity-panel"), "no reload needed for the CU log");
});
