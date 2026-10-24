// The SMS inbox under its 10 s poll (audit rank 54: P06-comms-web-03, -09, -11, -12,
// O12-messaging-03 client half).
//
//  * -03: every tick rebuilt the whole thread list, prefs bar and "New Message" row
//    included: the provider select fell back to its first option, the model box went
//    blank (the next edit then saved the wrong provider) and a number being typed was
//    wiped. The bar and the row now render once; ticks diff the thread rows.
//  * -12: every tick re-rendered the open conversation and forced it to the bottom,
//    yanking anyone reading history. It re-renders only when something changed, and
//    follows new messages only when the reader is already at the bottom.
//  * -09: providers and models were a static list and a free-text box; they come from
//    the live catalogs (/models/<provider>, custom included when a server is set up).
//  * -11: a typed number was used raw as the thread key and the send target.
//  * O12-03: the box serves the newest page now; "Load earlier" pages back with the
//    cursor it returns.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

const settle = () => new Promise((r) => setTimeout(r, 40));
const $ = (id) => document.getElementById(id);
const json = (b, status = 200) => Promise.resolve({
    ok: status >= 200 && status < 300, status,
    json: () => Promise.resolve(b), text: () => Promise.resolve(JSON.stringify(b)),
});

document.body.insertAdjacentHTML("beforeend", `
  <div id="smsInboxModal" class="modal hide">
    <button id="btnCloseSMS"></button>
    <div id="smsThreadList"></div>
    <div id="smsConversation">
      <div id="smsConvHeader"></div><div id="smsMessages"></div>
      <div id="smsFromSelectorRow"></div>
      <div id="smsCompose"><textarea id="smsInput"></textarea><button id="btnSMSSend"></button></div>
    </div>
  </div>`);

const state = await import("../../Portal/modules/state-management.js");
const sms = await import("../../Portal/modules/sms-inbox.js");
sms.initSMSInbox();

const thread = (phone, extra = {}) => ({
    phone_number: phone, contact_name: "", last_message: "hi", last_timestamp: "2026-10-01T12:00:00+00:00",
    unread_count: 0, direction: "inbound", line_number: "", ...extra,
});
const msg = (id, extra = {}) => ({
    id, direction: id % 2 ? "inbound" : "outbound", body: `m${id}`, status: "delivered",
    timestamp: "2026-10-01T12:00:00+00:00", contact_name: "", ...extra,
});

/**
 * A scripted box. `box.threads` and `box.thread[phone]` (all of a thread's messages,
 * oldest first) can be changed between ticks; /sms/messages pages them like the box.
 */
function serve(box) {
    const calls = [];
    globalThis.fetch = (url, opts = {}) => {
        const u = String(url);
        const body = opts.body ? JSON.parse(opts.body) : null;
        calls.push({ url: u, method: opts.method || "GET", body });
        if (opts.method === "POST") {
            if (u === "/sms/send") return json(box.sendReply || { success: true, message_id: 1 });
            return json({ success: true });
        }
        if (u.startsWith("/sms/threads")) return json({ threads: box.threads || [] });
        if (u.startsWith("/sms/preferences")) return json(box.prefs || { sms_provider: "anthropic", sms_model: "" });
        if (u.startsWith("/sms/messages")) {
            const q = new URL(u, "http://x").searchParams;
            const all = (box.thread || {})[q.get("phone")] || [];
            const limit = Number(q.get("limit") || 50);
            const before = q.get("before_id");
            const upto = before ? all.findIndex((m) => m.id === Number(before)) : all.length;
            const end = upto < 0 ? 0 : upto;
            const page = all.slice(Math.max(0, end - limit), end);
            const hasMore = end - limit > 0;
            return json({ messages: page, has_more: hasMore, next_before_id: hasMore ? page[0].id : null });
        }
        if (u.startsWith("/models/")) {
            const p = u.slice("/models/".length).split("?")[0];
            const cat = (box.catalogs || {})[p];
            return cat ? json(cat) : json({}, 404);
        }
        if (u.startsWith("/asterisk/gateways")) return json({ gateways: [] });
        return json({});
    };
    return calls;
}

/** Give #smsMessages the layout jsdom does not compute: [perBubble] px per bubble, a 200 px view. */
function layout(perBubble) {
    const el = $("smsMessages");
    Object.defineProperty(el, "scrollHeight", {
        configurable: true, get: () => el.querySelectorAll(".sms-bubble-in, .sms-bubble-out").length * perBubble,
    });
    Object.defineProperty(el, "clientHeight", { configurable: true, get: () => 200 });
}

const bodies = () => Array.from($("smsMessages").querySelectorAll(".sms-bubble-body")).map((b) => b.textContent);

async function open(t) {
    t.mock.timers.enable({ apis: ["setInterval"] });
    localStorage.clear();
    sessionStorage.clear();
    state.setOperator("Ana");
    await sms.openSMSInbox();
    await settle();
}

async function tick(t) {
    t.mock.timers.tick(10000);
    await settle();
}

const close = () => $("btnCloseSMS").click();

test("a poll keeps the saved provider and model and the number being typed", async (t) => {
    const box = {
        threads: [thread("5550100111")],
        prefs: { sms_provider: "google", sms_model: "gemini-9-pro" },
        catalogs: { google: { models: [{ id: "gemini-9-pro", name: "Gemini 9 Pro" }], default_id: "gemini-9-pro" } },
    };
    const calls = serve(box);
    await open(t);
    try {
        const sel = $("smsProviderSelect");
        const model = $("smsModelSelect");
        assert.equal(sel.value, "google");
        assert.equal(model.value, "gemini-9-pro");
        $("smsNewMsgBtn").click();
        const phone = $("smsNewPhone");
        phone.value = "+1 (555) 01";

        box.threads = [thread("5550100111", { last_message: "newer", unread_count: 1 })];
        await tick(t);

        assert.equal($("smsProviderSelect"), sel, "the bar is not rebuilt");
        assert.equal(sel.value, "google");
        assert.equal($("smsModelSelect").value, "gemini-9-pro");
        assert.equal($("smsNewPhone"), phone);
        assert.equal(phone.value, "+1 (555) 01", "the number being typed survives");
        assert.notEqual($("smsNewMsg").style.display, "none", "the New Message row stays open");
        assert.match(document.querySelector('.sms-thread[data-phone="5550100111"]').textContent, /newer/);
        assert.deepEqual(calls.filter((c) => c.url === "/sms/preferences" && c.method === "POST"), [],
            "a poll never saves a preference");
    } finally {
        close();
    }
});

test("an unchanged poll leaves the thread rows alone; a change touches only its row", async (t) => {
    const box = { threads: [thread("5550100111"), thread("5550100222", { last_message: "two" })] };
    serve(box);
    await open(t);
    try {
        const rows = () => Array.from(document.querySelectorAll(".sms-thread"));
        const [a, b] = rows();
        const seen = [];
        const obs = new MutationObserver((m) => seen.push(...m));
        obs.observe($("smsThreadList"), { subtree: true, childList: true, characterData: true, attributes: true });
        await tick(t);
        await settle();
        assert.equal(obs.takeRecords().length + seen.length, 0, "nothing in the list changed");

        box.threads = [thread("5550100222", { last_message: "two, again", unread_count: 2 }), thread("5550100111")];
        await tick(t);
        const after = rows();
        assert.deepEqual(after.map((r) => r.dataset.phone), ["5550100222", "5550100111"], "server order");
        assert.equal(after[0], b, "a changed row is updated in place");
        assert.equal(after[1], a, "an unchanged row is the same element");
        assert.match(b.textContent, /two, again/);
        assert.ok(b.classList.contains("has-unread"));

        box.threads = [thread("5550100111")];
        await tick(t);
        assert.deepEqual(rows().map((r) => r.dataset.phone), ["5550100111"], "a vanished thread is removed");
        obs.disconnect();
    } finally {
        close();
    }
});

test("an unchanged conversation is not re-rendered or scrolled", async (t) => {
    const box = { threads: [thread("5550100111")], thread: { "5550100111": [msg(1), msg(2), msg(3)] } };
    serve(box);
    await open(t);
    try {
        layout(500);
        document.querySelector('.sms-thread[data-phone="5550100111"]').click();
        await settle();
        const view = $("smsMessages");
        assert.equal(view.scrollTop, 1500, "a thread opens at its newest message");
        const bubble = view.querySelector(".sms-bubble-in");
        view.scrollTop = 120;  // reading history
        await tick(t);
        assert.equal(view.querySelector(".sms-bubble-in"), bubble, "not re-rendered");
        assert.equal(view.scrollTop, 120, "not yanked to the bottom");
    } finally {
        close();
    }
});

test("a new message follows the reader only when they are at the bottom", async (t) => {
    const box = { threads: [thread("5550100111")], thread: { "5550100111": [msg(1), msg(2)] } };
    serve(box);
    await open(t);
    try {
        layout(500);
        document.querySelector('.sms-thread[data-phone="5550100111"]').click();
        await settle();
        const view = $("smsMessages");
        assert.equal(view.scrollTop, 1000);

        view.scrollTop = 100;  // scrolled up
        box.thread["5550100111"].push(msg(3));
        await tick(t);
        assert.deepEqual(bodies(), ["m1", "m2", "m3"], "the new message is shown");
        assert.equal(view.scrollTop, 100, "the reader stays where they were");

        view.scrollTop = 1300;  // at the bottom (1500 of content, a 200 px view)
        box.thread["5550100111"].push(msg(4));
        await tick(t);
        assert.equal(view.scrollTop, 2000, "at the bottom, the view follows");
    } finally {
        close();
    }
});

test("a long thread opens on its newest page and Load earlier pages back", async (t) => {
    const all = Array.from({ length: 130 }, (_, i) => msg(i + 1));
    const box = { threads: [thread("5550100111")], thread: { "5550100111": all } };
    const calls = serve(box);
    await open(t);
    try {
        layout(50);
        document.querySelector('.sms-thread[data-phone="5550100111"]').click();
        await settle();
        const view = $("smsMessages");
        assert.deepEqual(bodies(), all.slice(30).map((m) => m.body), "the newest 100, oldest first");
        const more = $("smsLoadEarlier");
        assert.ok(more, "there is an older page");

        view.scrollTop = 0;
        more.click();
        await settle();
        const page = calls.filter((c) => c.url.startsWith("/sms/messages") && c.url.includes("before_id="));
        assert.equal(page.length, 1);
        assert.match(page[0].url, /before_id=31\b/, "the cursor is the oldest message shown");
        assert.deepEqual(bodies(), all.map((m) => m.body), "the whole thread, in order");
        assert.equal($("smsLoadEarlier"), null, "nothing older remains");
        assert.equal(view.scrollTop, 1500, "the reader keeps their place above the old top");

        box.thread["5550100111"] = [...all, msg(131)];
        await tick(t);  // the poll keeps the page already loaded and adds the new one
        assert.deepEqual(bodies(), [...all, msg(131)].map((m) => m.body));
    } finally {
        close();
    }
});

test("a typed number opens its thread by the box's key and sends to a dialable number", async (t) => {
    const box = { threads: [], thread: {} };
    const calls = serve(box);
    await open(t);
    try {
        $("smsNewMsgBtn").click();
        $("smsNewPhone").value = "+1 (555) 010-0123";
        $("btnNewMsgStart").click();
        await settle();
        const read = calls.find((c) => c.url.startsWith("/sms/messages"));
        assert.match(read.url, /phone=5550100123/, "the thread key, as the box files it");
        $("smsInput").value = "hello";
        $("btnSMSSend").click();
        await settle();
        const sent = calls.find((c) => c.url === "/sms/send");
        assert.equal(sent.body.to, "+15550100123", "digits, the leading + kept");

        box.threads = [thread("5550100123")];
        await tick(t);
        assert.ok(document.querySelector('.sms-thread[data-phone="5550100123"]').classList.contains("active"),
            "the open thread's row is highlighted");
        assert.match($("smsConvHeader").textContent, /5550100123/);
    } finally {
        close();
    }
});

test("a number that is not one is refused before anything is read or sent", async (t) => {
    const calls = serve({ threads: [] });
    await open(t);
    try {
        for (const raw of ["12", "call me", "+1 555 010 0123 ext 4"]) {
            $("smsNewMsgBtn").click();
            $("smsNewPhone").value = raw;
            $("btnNewMsgStart").click();
            await settle();
            assert.match($("toast").textContent, /3 to 15 digits/i, raw);
            $("toast").textContent = "";
            $("smsNewMsg").style.display = "none";
        }
        assert.deepEqual(calls.filter((c) => c.url.startsWith("/sms/messages") || c.url === "/sms/send"), []);
    } finally {
        close();
    }
});

// The gateway takes 3 to 15 digits, so a short code (a carrier's 611, a service's
// 5- or 6-digit code) can still be texted from the inbox, as before the number check.
test("a short code opens its thread and is sent to as typed", async (t) => {
    const calls = serve({ threads: [], thread: {} });
    await open(t);
    try {
        $("smsNewMsgBtn").click();
        $("smsNewPhone").value = "22000";
        $("btnNewMsgStart").click();
        await settle();
        assert.match(calls.find((c) => c.url.startsWith("/sms/messages")).url, /phone=22000\b/);
        $("smsInput").value = "STOP";
        $("btnSMSSend").click();
        await settle();
        assert.equal(calls.find((c) => c.url === "/sms/send").body.to, "22000");
    } finally {
        close();
    }
});

test("a failed send keeps the draft and says why", async (t) => {
    const box = { threads: [thread("5550100111")], thread: { "5550100111": [] },
        sendReply: { success: false, error: "Not connected to AMI" } };
    serve(box);
    await open(t);
    try {
        document.querySelector('.sms-thread[data-phone="5550100111"]').click();
        await settle();
        $("smsInput").value = "keep me";
        $("btnSMSSend").click();
        await settle();
        assert.equal($("smsInput").value, "keep me");
        assert.match($("toast").textContent, /Not connected to AMI/);
    } finally {
        close();
    }
});

test("providers and models come from the live catalogs", async (t) => {
    const box = {
        threads: [],
        prefs: { sms_provider: "anthropic", sms_model: "", sms_model_resolved: "claude-new-1" },
        catalogs: {
            anthropic: { models: [{ id: "claude-new-1", name: "Claude New 1" }], default_id: "claude-new-1" },
            custom: { models: [{ id: "lan::gemma", name: "gemma", server: "lan" }] },
        },
    };
    const calls = serve(box);
    await open(t);
    try {
        const providers = Array.from($("smsProviderSelect").options).map((o) => o.value);
        assert.ok(providers.includes("custom"), "a configured LAN server is offered");
        for (const p of ["anthropic", "google", "openai", "xai"]) assert.ok(providers.includes(p), p);
        const models = Array.from($("smsModelSelect").options).map((o) => [o.value, o.textContent]);
        assert.deepEqual(models[0], ["", "(Auto - Claude New 1)"], "Auto names the box default");
        assert.ok(models.some(([id]) => id === "claude-new-1"), "the live id is offered");

        const sel = $("smsProviderSelect");
        sel.value = "custom";
        sel.dispatchEvent(new Event("change"));
        await settle();
        assert.ok(Array.from($("smsModelSelect").options).some((o) => o.value === "lan::gemma"));
        const saved = calls.filter((c) => c.url === "/sms/preferences" && c.method === "POST");
        assert.deepEqual(saved.at(-1).body, { operator: "Ana", sms_provider: "custom", sms_model: "" });

        const model = $("smsModelSelect");
        model.value = "lan::gemma";
        model.dispatchEvent(new Event("change"));
        await settle();
        assert.deepEqual(calls.filter((c) => c.url === "/sms/preferences" && c.method === "POST").at(-1).body,
            { operator: "Ana", sms_provider: "custom", sms_model: "lan::gemma" });
    } finally {
        close();
    }
});

test("no LAN server, no custom option; a stored model the catalog lacks is shown as itself", async (t) => {
    serve({
        threads: [],
        prefs: { sms_provider: "openai", sms_model: "gpt-retired-3" },
        catalogs: { openai: { models: [{ id: "gpt-9", name: "GPT-9" }], default_id: "gpt-9" }, custom: { models: [] } },
    });
    await open(t);
    try {
        const providers = Array.from($("smsProviderSelect").options).map((o) => o.value);
        assert.ok(!providers.includes("custom"));
        assert.equal($("smsProviderSelect").value, "openai");
        assert.equal($("smsModelSelect").value, "gpt-retired-3", "never shown as Auto or another id");
    } finally {
        close();
    }
});

// /models/custom probes every LAN server live (never cached; an unreachable one takes
// seconds). The saved provider used to wait for that probe, so the bar showed the
// first provider (Anthropic) and no models meanwhile.
test("the saved provider and its models show before the LAN custom catalog answers", async (t) => {
    const box = {
        threads: [],
        prefs: { sms_provider: "xai", sms_model: "" },
        catalogs: { xai: { models: [{ id: "grok-9", name: "Grok 9" }], default_id: "grok-9" }, custom: { models: [] } },
    };
    serve(box);
    await state.fetchAvailableModels("custom");  // start with no LAN server known
    box.catalogs.custom = { models: [{ id: "lan::gemma", name: "gemma", server: "lan" }] };
    const served = globalThis.fetch;
    let release;
    const gate = new Promise((r) => { release = r; });
    globalThis.fetch = (url, opts) =>
        String(url).startsWith("/models/custom") ? gate.then(() => served(url, opts)) : served(url, opts);
    await open(t);
    try {
        const providers = () => Array.from($("smsProviderSelect").options).map((o) => o.value);
        assert.equal($("smsProviderSelect").value, "xai", "shown while the custom probe is out");
        assert.ok(Array.from($("smsModelSelect").options).some((o) => o.value === "grok-9"), "its models too");
        assert.ok(!providers().includes("custom"));
        release();
        await settle();
        assert.ok(providers().includes("custom"), "custom joins the list when its catalog answers");
        assert.equal($("smsProviderSelect").value, "xai", "the saved provider is kept");
    } finally {
        release();
        close();
    }
});

test("the header names a contact, and a number with no contact only once", async (t) => {
    // The box stores a number with no contact under the number itself as its "name".
    const box = {
        threads: [thread("5550100333", { contact_name: "+15550100333" }), thread("5550100444", { contact_name: "Pat" })],
        thread: {
            "5550100333": [msg(1, { contact_name: "+15550100333" })],
            "5550100444": [msg(2, { contact_name: "Pat" })],
        },
    };
    serve(box);
    await open(t);
    try {
        const header = $("smsConvHeader");
        document.querySelector('.sms-thread[data-phone="5550100333"]').click();
        await settle();
        assert.equal(header.querySelector(".sms-conv-name").textContent, "5550100333");
        assert.equal(header.querySelector(".sms-conv-phone"), null);

        document.querySelector('.sms-thread[data-phone="5550100444"]').click();
        await settle();
        assert.equal(header.querySelector(".sms-conv-name").textContent, "Pat");
        assert.equal(header.querySelector(".sms-conv-phone").textContent, "5550100444");
    } finally {
        close();
    }
});
