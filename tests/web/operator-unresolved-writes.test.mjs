// Nothing is written while no operator is resolved (U16 adversarial review of
// P06-comms-web-02 / A09-data-local-model-05). On a box with several people a
// fresh browser now starts with NO operator (state-management chooseOperator),
// so every write that names an operator must refuse instead of going out as ""
// (an SMS filed in nobody's inbox, a contact book for "") or as a made-up name
// ("system"). Android's SmsInboxApi / ContactsBookApi refuse the same way.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

const settle = (ms = 30) => new Promise((r) => setTimeout(r, ms));
const json = (b, status = 200) => Promise.resolve({
    ok: status >= 200 && status < 300, status,
    json: () => Promise.resolve(b), text: () => Promise.resolve(JSON.stringify(b)),
});

document.body.insertAdjacentHTML("beforeend", `
  <div id="smsInboxModal" class="modal hide">
    <button id="btnCloseSMS"></button>
    <div id="smsThreadList"></div>
    <div id="smsConversation"></div>
    <div id="smsConvHeader"></div><div id="smsMessages"></div>
    <div id="smsFromSelectorRow"></div>
    <div id="smsCompose"><textarea id="smsInput"></textarea><button id="btnSMSSend"></button></div>
  </div>
  <button id="btnContactsManager"></button>
  <div id="contactsManagerModal" class="hide">
    <button id="btnAddContact"></button>
    <div id="contactsList"></div><div id="contactsEditForm"></div>
  </div>
  <div id="googleSSMLModal" class="hide">
    <textarea id="google-ssml-input"></textarea>
    <select id="google-voice-name-select"><option value="en-US-Neural2-A">A</option></select>
    <input id="google-ssml-pitch" value="0"><input id="google-ssml-rate" value="1">
    <button id="google-ssml-generate-btn"></button><div id="google-ssml-status"></div>
  </div>
  <div id="geminiProTTSModal" class="hide">
    <textarea id="gemini-pro-text-input"></textarea>
    <select id="gemini-pro-voice-select"></select>
    <button id="gemini-pro-generate-btn"></button><div id="gemini-pro-status"></div>
  </div>
  <button id="ctlPair"></button>
  <div id="pairModal" class="hide"><img id="pairQR"><pre id="pairJSON"></pre></div>`);

const state = await import("../../Portal/modules/state-management.js");
const sms = await import("../../Portal/modules/sms-inbox.js");
const contacts = await import("../../Portal/modules/contacts-manager.js");
const gen = await import("../../Portal/modules/generation-modals.js");
const ui = await import("../../Portal/modules/ui-setup.js");
sms.initSMSInbox();
contacts.initContactsManager();
gen.setupGoogleSSMLModal();
gen.setupGeminiProTTSModal();
{
    // initModalHandlers warns about every Portal control this page lacks.
    const warn = console.warn;
    console.warn = () => {};
    ui.initModalHandlers();
    await settle(100);
    console.warn = warn;
}

const toastText = () => document.getElementById("toast").textContent;
const $ = (id) => document.getElementById(id);

/** Record every request; [routes] maps a URL prefix to the JSON it answers. */
function serve(routes = {}) {
    const calls = [];
    globalThis.fetch = (url, opts = {}) => {
        const u = String(url);
        const body = typeof opts.body === "string" ? JSON.parse(opts.body) : undefined;
        calls.push({ url: u, method: opts.method || "GET", body });
        for (const [prefix, reply] of Object.entries(routes)) {
            if (u.startsWith(prefix)) return json(typeof reply === "function" ? reply(u, body) : reply);
        }
        return json({ success: true });
    };
    return calls;
}

/** The calls that would act under [operator] (a write, or a read naming it). */
const underOperator = (calls, operator) => calls.filter((c) =>
    (c.body && c.body.operator === operator) || c.url.includes(`operator=${encodeURIComponent(operator)}&`)
    || c.url.endsWith(`operator=${encodeURIComponent(operator)}`));

function unresolved() {
    localStorage.clear();
    state.setOperator("");
    $("toast").textContent = "";
}

async function closeInbox() {
    $("btnCloseSMS").click();  // stops the 10 s poll
    await settle();
}

// ── SMS inbox ────────────────────────────────────────────────────────────
test("SMS: a new message is not sent while no operator is resolved", async () => {
    unresolved();
    const calls = serve({ "/sms/threads": { threads: [] }, "/asterisk/gateways": { gateways: [] } });
    await sms.openSMSInbox();
    await settle();
    try {
        $("smsNewMsgBtn").click();
        $("smsNewPhone").value = "+15550100000";
        $("btnNewMsgStart").click();
        await settle();
        $("smsInput").value = "hello";
        $("btnSMSSend").click();
        await settle();
        assert.deepEqual(calls.filter((c) => c.method === "POST"), [], "nothing is posted");
        assert.deepEqual(underOperator(calls, ""), [], "nothing is read or written as operator ''");
        assert.match(toastText(), /select an operator/i);
    } finally {
        await closeInbox();
    }
});

test("SMS: an open thread cannot send once the operator is unresolved", async () => {
    localStorage.clear();
    state.setOperator("Ana");
    const calls = serve({
        "/sms/threads": { threads: [{ phone_number: "+15550100000", last_message: "hi", unread_count: 0 }] },
        "/sms/messages": { messages: [] }, "/asterisk/gateways": { gateways: [] },
    });
    await sms.openSMSInbox();
    await settle();
    try {
        document.querySelector('.sms-thread[data-phone="+15550100000"]').click();
        await settle();
        state.setOperator("");  // e.g. the box dropped the pick on the next roster read
        $("smsInput").value = "hello";
        $("btnSMSSend").click();
        await settle();
        assert.deepEqual(calls.filter((c) => c.url === "/sms/send"), []);
        assert.equal($("smsInput").value, "hello", "the draft is kept");
        assert.match(toastText(), /select an operator/i);
    } finally {
        await closeInbox();
    }
});

test("SMS: the reply model is not saved for operator ''", async () => {
    unresolved();
    const calls = serve({ "/sms/threads": { threads: [] }, "/asterisk/gateways": { gateways: [] } });
    await sms.openSMSInbox();
    await settle();
    try {
        const sel = $("smsProviderSelect");
        sel.value = "openai";
        sel.dispatchEvent(new Event("change"));
        await settle();
        const input = $("smsModelInput");
        input.value = "gpt-5";
        input.dispatchEvent(new Event("change"));
        await settle();
        assert.deepEqual(calls.filter((c) => c.url === "/sms/preferences" && c.method === "POST"), []);
        assert.doesNotMatch(toastText(), /updated/i, "no success toast for a save that did not happen");
        assert.match(toastText(), /select an operator/i);
    } finally {
        await closeInbox();
    }
});

test("SMS: with an operator the inbox still sends as that operator", async () => {
    localStorage.clear();
    state.setOperator("Ana");
    const calls = serve({
        "/sms/threads": { threads: [] }, "/sms/messages": { messages: [] },
        "/asterisk/gateways": { gateways: [] },
    });
    await sms.openSMSInbox();
    await settle();
    try {
        $("smsNewMsgBtn").click();
        $("smsNewPhone").value = "+15550100000";
        $("btnNewMsgStart").click();
        await settle();
        $("smsInput").value = "hello";
        $("btnSMSSend").click();
        await settle();
        const sent = calls.find((c) => c.url === "/sms/send");
        assert.ok(sent, "the SMS is sent");
        assert.deepEqual(sent.body, { operator: "Ana", to: "+15550100000", message: "hello" });
    } finally {
        await closeInbox();
    }
});

test("SMS: the unread badge never polls as operator ''", async () => {
    unresolved();
    const calls = serve({ "/sms/unread": { unread: 3 } });
    assert.equal(await sms.pollUnread(), 0);
    assert.deepEqual(calls, []);
});

// ── contact book ─────────────────────────────────────────────────────────
test("contacts: a new contact is not saved while no operator is resolved", async () => {
    unresolved();
    const calls = serve({ "/contacts": { contacts: [] } });
    $("btnContactsManager").click();
    await settle();
    $("btnAddContact").click();
    const form = $("contactsEditForm");
    form.querySelector('[id="contactName"]').value = "Mallory";
    form.querySelector('[id="btnContactSave"]').click();
    await settle();
    assert.deepEqual(calls.filter((c) => c.method !== "GET"), [], "nothing is written");
    assert.deepEqual(underOperator(calls, ""), [], "no contact book is read for operator ''");
    assert.match(toastText(), /select an operator/i);
});

test("contacts: a contact is not deleted once the operator is unresolved", async () => {
    localStorage.clear();
    state.setOperator("Ana");
    const calls = serve({ "/contacts": { contacts: [{ id: "c1", name: "Mallory" }] } });
    $("btnContactsManager").click();
    await settle();
    state.setOperator("");
    const confirm = globalThis.confirm;
    globalThis.confirm = window.confirm = () => true;
    try {
        document.querySelector("#contactsList .contact-delete-btn").click();
        await settle();
    } finally {
        globalThis.confirm = window.confirm = confirm;
    }
    assert.deepEqual(calls.filter((c) => c.method === "DELETE"), []);
    assert.match(toastText(), /select an operator/i);
});

test("contacts: with an operator a save goes to that operator's book", async () => {
    localStorage.clear();
    state.setOperator("Ana");
    const calls = serve({ "/contacts": { contacts: [] } });
    $("btnContactsManager").click();
    await settle();
    $("btnAddContact").click();
    const form = $("contactsEditForm");
    form.querySelector('[id="contactName"]').value = "Mallory";
    form.querySelector('[id="btnContactSave"]').click();
    await settle();
    const saved = calls.find((c) => c.url === "/contacts" && c.method === "POST");
    assert.ok(saved);
    assert.equal(saved.body.operator, "Ana");
});

// ── generation modals (the Portal twins of Android's GoogleSsml / GeminiProTts) ──
const TTS_ROUTES = {
    "/generate/": { task_id: "t1" },
    "/tasks/status/": { status: "completed", result_url: "" },
};

test("Google SSML and Gemini Pro TTS refuse instead of inventing an operator", async () => {
    unresolved();
    const calls = serve(TTS_ROUTES);
    $("google-ssml-input").value = "Hello there";
    $("google-ssml-generate-btn").click();
    await settle();
    $("gemini-pro-text-input").value = "Hello there";
    $("gemini-pro-generate-btn").click();
    await settle();
    assert.deepEqual(calls.filter((c) => c.url.startsWith("/generate/")), [],
        "no generation is started for a made-up operator");
    assert.match(toastText(), /select an operator/i);
});

test("Google SSML and Gemini Pro TTS run as the resolved operator", async () => {
    localStorage.clear();
    state.setOperator("Ana");
    const calls = serve(TTS_ROUTES);
    $("google-ssml-input").value = "Hello there";
    $("google-ssml-generate-btn").click();
    await settle();
    $("gemini-pro-text-input").value = "Hello there";
    $("gemini-pro-generate-btn").click();
    await settle();
    const started = calls.filter((c) => c.url.startsWith("/generate/"));
    assert.deepEqual(started.map((c) => [c.url, c.body.operator]),
        [["/generate/google_ssml", "Ana"], ["/generate/gemini_tts", "Ana"]]);
});

// ── pairing a phone ──────────────────────────────────────────────────────
// The QR names the operator the phone starts as, so the token is minted for the
// operator who opened the dialog, never the box default on a box with several people.
const PAIR_ROUTES = {
    "/pair/start": { token: "tok123", exp: 1 },
    "/health": { pairing: { default_origin: "http://box:9091/ui", default_operator: "Ben" } },
};

test("pairing is minted for the Portal's own operator, not the box default", async () => {
    localStorage.clear();
    state.setOperator("Ana");
    const calls = serve(PAIR_ROUTES);
    $("ctlPair").click();
    await settle();
    const start = calls.find((c) => c.url === "/pair/start");
    assert.ok(start, "a token is minted");
    assert.equal(start.method, "POST");
    assert.deepEqual(start.body, { operator: "Ana" });
    assert.equal(JSON.parse($("pairJSON").textContent).operator, "Ana");
    assert.ok(!$("pairModal").classList.contains("hide"));
});

test("pairing waits for an operator instead of handing out the box default", async () => {
    unresolved();
    $("pairModal").classList.add("hide");
    const calls = serve(PAIR_ROUTES);
    $("ctlPair").click();
    await settle();
    assert.deepEqual(calls.filter((c) => c.url === "/pair/start"), []);
    assert.ok($("pairModal").classList.contains("hide"), "no QR is shown");
    assert.match(toastText(), /select an operator/i);
});
