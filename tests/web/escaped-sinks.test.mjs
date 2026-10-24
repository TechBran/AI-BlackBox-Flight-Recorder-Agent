// Ledger / agent / contact text rendered by the Portal panels must stay text.
// Audit: P04-media-tasks-fx-web-05 (Timeline operator + preview), P06-comms-web-14
// (collapsed todo), rank 26 (contact attributes, slash commands, speakable text),
// P02-voice-web-01 (extractSpeakableText innerHTML), P04-media-tasks-fx-web-04/13.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked, scriptVectors } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

const XSS = `<img src=x onerror="window.__pwned=1">`;
const ATTR_BREAK = `x" autofocus onfocus="window.__pwned=1`;
const settle = () => new Promise((r) => setTimeout(r, 20));

function jsonResponse(body) {
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)) });
}

// ── core-utils escape helpers ────────────────────────────────────────────
test("escapeHtml / escapeAttr escape all five characters and tolerate null", async () => {
    const { escapeHtml, escapeAttr } = await import("../../Portal/modules/core-utils.js");
    assert.equal(escapeHtml(`<a href="x" title='y'>&</a>`), "&lt;a href=&quot;x&quot; title=&#39;y&#39;&gt;&amp;&lt;/a&gt;");
    assert.equal(escapeAttr(ATTR_BREAK), "x&quot; autofocus onfocus=&quot;window.__pwned=1");
    assert.equal(escapeHtml(null), "");
    assert.equal(escapeHtml(undefined), "");
    assert.equal(escapeHtml(0), "0");
    const probe = document.createElement("div");
    probe.innerHTML = `<input value="${escapeAttr(ATTR_BREAK)}">`;
    assert.equal(probe.querySelector("input").getAttribute("value"), ATTR_BREAK);
    assert.equal(probe.querySelector("input").hasAttribute("onfocus"), false);
});

// ── Timeline (ledger operator + user-turn preview) ───────────────────────
test("Timeline list and detail render ledger operator/preview as text", async () => {
    document.body.insertAdjacentHTML("beforeend",
        `<div id="timelineList"></div><div id="snapshotPopup" class="hide"><div id="snapshotPopupBody"></div></div>`);
    // The list comes from the snapshot index; a body from the per-id route.
    const rows = [
        { snap_id: "SNAP-20261017-2", operator: `Ops ${ATTR_BREAK}`, timestamp: "2026-10-17T00:00:00Z", type: "normal", snippet: "hello <b>there</b>" },
        { snap_id: "SNAP-20261017-1", operator: XSS, timestamp: "2026-10-17T00:00:00Z", type: "normal", snippet: XSS },
    ];
    globalThis.fetch = (url) => String(url).startsWith("/fossil/snapshot/")
        ? jsonResponse({ snap_id: "SNAP-20261017-1", content: `OPERATOR: ${XSS}\nuser: ${XSS}`,
                         metadata: { operator: XSS, timestamp: "2026-10-17T00:00:00Z", type: "normal" } })
        : jsonResponse({ results: rows, count: rows.length, has_more: false, operators: [XSS] });
    try {
        const tl = await import("../../Portal/modules/timeline-browser.js");
        await tl.loadTimelineData();
        tl.renderTimelineList();
        const list = document.getElementById("timelineList");
        assert.equal(list.querySelectorAll(".timeline-item").length, 2);
        assert.deepEqual(scriptVectors(list), [], list.innerHTML);
        assert.equal(list.querySelector("img"), null);
        assert.equal(list.querySelector("b"), null);
        const ops = [...list.querySelectorAll(".timeline-item-operator")].map((e) => e.textContent);
        assert.ok(ops.includes(XSS), ops.join(" | "));
        const previews = [...list.querySelectorAll(".timeline-item-preview")].map((e) => e.textContent);
        assert.ok(previews.includes(XSS));
        tl.showTimelineDetail(tl.getTimelineData().find((s) => s.operator === XSS));
        await settle();
        const body = document.getElementById("snapshotPopupBody");
        assert.deepEqual(scriptVectors(body), [], body.innerHTML);
        assert.equal(body.querySelector("img"), null);
        assert.ok(body.textContent.includes(XSS));
    } finally {
        globalThis.fetch = () => Promise.reject(new Error("no network in unit test"));
    }
});

// ── Todo tracker (agent TodoWrite text) ──────────────────────────────────
test("collapsed and expanded todo views render agent text as text", async () => {
    document.body.insertAdjacentHTML("beforeend", `<div id="todoPanelTest"></div>`);
    const { TodoTracker } = await import("../../Portal/modules/todo-tracker.js");
    const tracker = new TodoTracker();
    tracker.init("todoPanelTest", null);
    const todos = [
        { content: XSS, activeForm: XSS, status: "in_progress" },
        { content: "<svg onload=alert(1)>", status: `pending" onmouseover="window.__pwned=1` },
    ];
    tracker.updateTodos(todos);
    const panel = document.getElementById("todoPanelTest");
    assert.deepEqual(scriptVectors(panel), [], panel.innerHTML);
    tracker.isCollapsed = true;
    tracker.render();
    assert.deepEqual(scriptVectors(panel), [], panel.innerHTML);
    assert.equal(panel.querySelector("img"), null);
    assert.ok(panel.querySelector(".todo-current-mini").textContent.includes(XSS));
});

// ── Contacts (names/notes from SMS auto-create, tools, other operators) ──
test("contact cards and the edit form keep contact fields as text/values", async () => {
    document.body.insertAdjacentHTML("beforeend", `
        <button id="btnContactsManager"></button>
        <div id="contactsManagerModal" class="hide"><div id="contactsList"></div><div id="contactsEditForm"></div></div>`);
    const contact = {
        id: `c1" onclick="window.__pwned=1`, name: ATTR_BREAK, phone: XSS, email: `a@b.c" onfocus="x`,
        relationship: XSS, notes: `Tom & Jerry <3 ${XSS}`, tags: [XSS, ATTR_BREAK],
    };
    globalThis.fetch = (url) => jsonResponse(String(url).startsWith("/contacts?") ? { contacts: [contact] } : {});
    // A contact book is always some operator's: with none resolved the modal shows guidance instead.
    localStorage.setItem("bbx_operator", "Ana");
    try {
        const cm = await import("../../Portal/modules/contacts-manager.js");
        cm.initContactsManager();
        document.getElementById("btnContactsManager").click();
        await settle();
        const list = document.getElementById("contactsList");
        assert.equal(list.querySelectorAll(".contact-card").length, 1, list.innerHTML);
        assert.deepEqual(scriptVectors(list), [], list.innerHTML);
        assert.equal(list.querySelector(".contact-card-name").textContent, ATTR_BREAK);
        assert.equal(list.querySelector(".contact-card").getAttribute("data-id"), contact.id);
        list.querySelector(".contact-edit-btn").click();
        await settle();
        const form = document.getElementById("contactsEditForm");
        assert.deepEqual(scriptVectors(form), [], form.innerHTML);
        assert.equal(document.getElementById("contactName").value, contact.name);
        assert.equal(document.getElementById("contactPhone").value, contact.phone);
        assert.equal(document.getElementById("contactEmail").value, contact.email);
        assert.equal(document.getElementById("contactRelationship").value, contact.relationship);
        // P06-comms-web-07: notes used to be escaped twice and saved corrupted.
        assert.equal(document.getElementById("contactNotes").value, contact.notes);
        assert.equal(document.getElementById("contactTags").value, contact.tags.join(", "));
    } finally {
        globalThis.fetch = () => Promise.reject(new Error("no network in unit test"));
        localStorage.removeItem("bbx_operator");
    }
});

// ── Slash-command dropdown (command files are agent-writable) ────────────
test("slash command names, descriptions and options render as text", async () => {
    const { SlashCommandDropdown } = await import("../../Portal/modules/slash-commands.js");
    const dd = new SlashCommandDropdown();
    dd.commands = [{ name: XSS, description: `<a href="javascript:alert(1)">d</a>`, options: [XSS, "plain"] }];
    const input = document.createElement("input");
    document.body.appendChild(input);
    dd.show(input);
    assert.deepEqual(scriptVectors(dd.dropdown), [], dd.dropdown.innerHTML);
    assert.equal(dd.dropdown.querySelector(".slash-command-name").textContent, `/${XSS}`);
    dd.level = 1;
    dd.expandedCommand = dd.commands[0];
    dd.render();
    assert.deepEqual(scriptVectors(dd.dropdown), [], dd.dropdown.innerHTML);
    assert.equal(dd.dropdown.querySelector(".slash-option-value").textContent, XSS);
});

// ── Speakable text extraction (every speaker button / auto-TTS) ──────────
test("extractSpeakableText never parses reply HTML in the live document", async () => {
    const { extractSpeakableText } = await import("../../Portal/modules/tts-stt.js");
    const desc = Object.getOwnPropertyDescriptor(window.Element.prototype, "innerHTML");
    const liveWrites = [];
    Object.defineProperty(window.Element.prototype, "innerHTML", {
        configurable: true,
        get: desc.get,
        set(v) {
            if (this.ownerDocument === window.document && /<img/i.test(String(v))) liveWrites.push(String(v));
            return desc.set.call(this, v);
        },
    });
    try {
        const out = extractSpeakableText(`Hello ${XSS} <b>world</b> <audio src="/ui/uploads/a.mp3"></audio>`);
        assert.equal(out, "Hello world");
        assert.deepEqual(liveWrites, []);
    } finally {
        Object.defineProperty(window.Element.prototype, "innerHTML", desc);
    }
});
