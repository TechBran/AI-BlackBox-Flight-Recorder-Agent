// The SMS inbox's AI model is "Auto" unless the operator names one (audit rank 38,
// X05-architecture-08 client half, A14-ui-misc-14). Auto is an EMPTY model: the box
// resolves its own configured default for the provider. The inbox used to fill
// claude-sonnet-4-5 in whenever the box had no preference, and kept the previous
// provider's model id across a provider switch (a Claude id sent to Gemini).
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

document.body.insertAdjacentHTML("beforeend", `
  <div id="smsInboxModal" class="modal hide">
    <button id="btnCloseSMS"></button>
    <div id="smsThreadList"></div>
    <div id="smsConvHeader"></div><div id="smsMessages"></div>
    <div id="smsCompose"><textarea id="smsInput"></textarea><button id="btnSMSSend"></button></div>
  </div>`);

const state = await import("../../Portal/modules/state-management.js");
const sms = await import("../../Portal/modules/sms-inbox.js");
sms.initSMSInbox();

/** Serve the inbox; [prefs] is GET /sms/preferences (null = the route fails). */
function serve(prefs) {
    const posts = [];
    globalThis.fetch = (url, opts) => {
        const u = String(url);
        if (opts && opts.method === "POST") { posts.push({ url: u, body: JSON.parse(opts.body) }); return json({ success: true }); }
        if (u.startsWith("/sms/threads")) return json({ threads: [] });
        if (u.startsWith("/sms/preferences")) return prefs === null ? json({}, 500) : json(prefs);
        return json({});
    };
    return posts;
}

async function openAndClose(fn) {
    await sms.openSMSInbox();
    await settle();
    try {
        await fn();
    } finally {
        document.getElementById("btnCloseSMS").click();  // stops the 10 s poll
    }
}

test("no preference on the box shows Auto, never a pinned model", async () => {
    state.setOperator("Ana");
    serve(null);
    await openAndClose(() => {
        assert.equal(document.getElementById("smsModelInput").value, "");
        assert.match(document.getElementById("smsModelInput").placeholder, /auto/i);
    });
    serve({ sms_provider: "anthropic", sms_model: "" });
    await openAndClose(() => {
        assert.equal(document.getElementById("smsModelInput").value, "");
    });
});

test("switching provider saves Auto, not the previous provider's model", async () => {
    state.setOperator("Ana");
    const posts = serve({ sms_provider: "anthropic", sms_model: "claude-opus-4-8" });
    await openAndClose(async () => {
        assert.equal(document.getElementById("smsModelInput").value, "claude-opus-4-8");
        const sel = document.getElementById("smsProviderSelect");
        sel.value = "openai";
        sel.dispatchEvent(new Event("change"));
        await settle();
        const saved = posts.find((p) => p.url === "/sms/preferences");
        assert.ok(saved, "the switch is saved");
        assert.deepEqual(saved.body, { operator: "Ana", sms_provider: "openai", sms_model: "" });
        assert.equal(document.getElementById("smsModelInput").value, "");
    });
});
