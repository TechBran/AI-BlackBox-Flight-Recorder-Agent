// System Menu > Health inside the BlackBox Android app (M7 health design,
// docs/plans/2026-10-25-samsung-health-integration.md §6.2-§6.3).
//
// In the app's WebView, the message channel window.BlackBoxWellness answers
// a sign request ({id, method, path, sha256}) with the X-BlackBox-Origin
// value for one request (the device key never enters JavaScript), or null
// (helpers/wellness-channel.mjs; the channel itself is
// wellness-webview-channel.test.mjs). The Health card then signs
// GET /wellness/status, so the box answers with the settings view, and shows
// what it carries: "What BlackBox may use" (categories), "Where it may go"
// (providers), the trouble cards with their fix, the instinct prompt and the
// disk-encryption line, read-only (narrowing from the Portal is M7.3), plus
// the disclosure log from a signed GET /wellness/disclosures?operator=X (the
// app signs a GET only when it names the Portal's operator). Every string
// stays text. A null from the app, or no channel at all (a desktop
// browser), is an unsigned request and the four-field card of
// wellness-section.test.mjs, whatever the reply carries. A signed read the
// box refuses (401, 409, 410: it refuses a proof it cannot verify rather than
// downgrading it) is read again, once, unsigned: never less than a desktop.
//
// Run: bash scripts/run_web_tests.sh
import test from "node:test";
import assert from "node:assert/strict";
import { createHash, createHmac } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { installDom, PORTAL, REPO_ROOT, scriptVectors } from "./helpers/portal-dom.mjs";
import { fakeChannel, installChannel } from "./helpers/wellness-channel.mjs";

installDom();

const ME = "wellness-fixture";
const PEER = "wellness-fixture-peer";
const XSS = `<img src=x onerror="window.__pwned=1">`;
const NOW = Date.parse("2026-10-29T12:00:00Z");
const EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
const HEADER = "X-BlackBox-Origin";

{
    const page = readFileSync(resolve(PORTAL, "index.html"), "utf8");
    const section = new DOMParser().parseFromString(page, "text/html").getElementById("wellnessSection");
    document.body.insertAdjacentHTML("beforeend", `<div id="menuModal" class="modal hide"><div class="menu-body"></div></div>`);
    document.querySelector("#menuModal .menu-body").appendChild(document.importNode(section, true));
}

const state = await import("../../Portal/modules/state-management.js");
const wellness = await import("../../Portal/modules/wellness-section.js");
const bubbles = await import("../../Portal/modules/chat-bubbles.js");

const $ = (id) => document.getElementById(id);
const flush = async () => {
    for (let i = 0; i < 8; i++) await new Promise((r) => setImmediate(r));
};

/**
 * Resolve once `pred()` holds. A real SubtleCrypto digest finishes on Node's
 * threadpool, not within a fixed number of ticks, so a signed read is waited
 * for, never counted in ticks.
 */
async function waitFor(pred, what, timeoutMs = 5000) {
    const until = Date.now() + timeoutMs;
    while (!pred()) {
        if (Date.now() > until) throw new Error(`timed out waiting for ${what}`);
        await new Promise((r) => setTimeout(r, 5));
    }
    await flush();
}

function manualTimers() {
    let now = 0;
    let seq = 0;
    const pending = new Map();
    return {
        setTimeout(fn, ms) { const id = ++seq; pending.set(id, { at: now + ms, fn }); return id; },
        clearTimeout(id) { pending.delete(id); },
        async advance(ms) {
            const until = now + ms;
            for (;;) {
                const due = [...pending.entries()].filter(([, t]) => t.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
                if (!due) break;
                pending.delete(due[0]);
                now = due[1].at;
                due[1].fn();
                await flush();
            }
            now = until;
        },
    };
}

const reply = (body, status = 200) => Promise.resolve({
    ok: status >= 200 && status < 300, status,
    json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)),
});

const SIGNED = {
    state: "active", source_label: "Fixture Phone (SM-TEST1)", last_sync: "2026-10-29T11:48:00Z", enrolled: true,
    troubles: ["stale_sync"],
    trouble_details: [{
        code: "stale_sync", title: "Health data hasn't synced recently",
        body: "Your phone hasn't sent health data since 07:10.", fix: "Open BlackBox on your phone",
    }],
    categories: { sleep: "ask", heart: "proactive", activity: "ask", blood_pressure: "off" },
    local_only: { sleep: false, heart: true, activity: false, blood_pressure: true },
    providers_allowed: ["anthropic", "custom:lan-box"],
    google_paid_key_confirmed: false,
    surfaces_mode: "auto",
    wear: { hr_mode: "continuous", nights_worn_7: 6 },
    sync: { last_push_at: "2026-10-29T11:48:00Z", outbox_hint: { next_min_interval_s: 300, batches_received: 41 } },
    instinct_prompt: null,
    instinct_prompt_default: "You have this person's own wearable data through the wellness_* tools.",
    replace_pending: false,
    policy_version: "2026-11-01",
    disclosures_count: 2,
    fde: { root_encrypted: true, method: "luks", store_encrypted: true },
};

const DISCLOSURES = [
    { time: "2026-10-29T07:12-04:00", surface: "android", provider: "anthropic", model: "claude-test",
      tool: "wellness_summary", categories: ["heart", "sleep"] },
    { time: "2026-10-28T21:03-04:00", surface: "android", provider: "custom", model: "lan-box::fixture-model",
      tool: "wellness_series", categories: ["activity"] },
];

// The scripted box and the app's channel. `refuseSigned` ({code, body}) is the
// box's answer to a status read that carries a proof (wellness_routes.wellness_status
// returns the refusal; an unsigned read gets box.status).
const box = { status: SIGNED, statusCode: 200, disclosures: DISCLOSURES, disclosuresCode: 200, refuseSigned: null };
const calls = [];
globalThis.fetch = window.fetch = (url, opts = {}) => {
    const u = String(url);
    calls.push({ url: u, method: opts.method || "GET", headers: { ...(opts.headers || {}) } });
    if (u.startsWith("/wellness/status")) {
        if (box.refuseSigned && opts.headers && opts.headers[HEADER]) return reply(box.refuseSigned.body, box.refuseSigned.code);
        return reply(box.status, box.statusCode);
    }
    if (u.startsWith("/wellness/disclosures")) return reply({ disclosures: box.disclosures }, box.disclosuresCode);
    if (u.startsWith("/devices/mesh")) return reply({ devices: [] });
    return Promise.reject(new TypeError("offline"));
};
const statusCalls = () => calls.filter((c) => c.url.startsWith("/wellness/status"));

/** A stand-in for the app's channel: records each sign request, answers a fresh proof. */
const fakeBridge = (opts) => fakeChannel(opts);

const menu = () => $("menuModal");
/** Open the System Menu and wait for the status read it starts (and its rendering). */
async function openMenu() {
    const before = statusCalls().length;
    menu().classList.remove("hide");
    await waitFor(() => statusCalls().length > before, "the status read");
}
async function closeMenu() { menu().classList.add("hide"); await flush(); }

function shownText(root) {
    const copy = root.cloneNode(true);
    copy.querySelectorAll("[hidden]").forEach((el) => el.remove());
    return copy.textContent.replace(/\s+/g, " ").trim();
}

async function setup(t, { status = SIGNED, bridge = fakeBridge(), operator = ME } = {}) {
    calls.length = 0;
    Object.assign(box, { status, statusCode: 200, disclosures: DISCLOSURES, disclosuresCode: 200, refuseSigned: null });
    await closeMenu();
    state.setOperator(operator);
    installChannel(bridge);
    const timers = manualTimers();
    const ctl = wellness.initWellnessSection({ timers, now: () => NOW });
    t.after(async () => {
        ctl.destroy();
        await closeMenu();
        installChannel(null);
        delete window.AndroidBridge;
    });
    return { timers, ctl, bridge };
}

const signedBlock = () => $("wellnessSigned");

test("inside the app the status read is signed: GET, the path with its query, sha256 of the empty body", async (t) => {
    const { bridge } = await setup(t);
    await openMenu();
    assert.deepEqual(bridge.signed, [{ method: "GET", path: `/wellness/status?operator=${ME}`, hex: EMPTY_SHA256 }]);
    const [call] = statusCalls();
    assert.equal(call.url, `/wellness/status?operator=${ME}`);
    assert.match(call.headers[HEADER], /^v1;enr=en_0000abcd;ts=1800000001;/);
});

test("the signed settings view: what BlackBox may use, where it may go, troubles, instinct prompt, encryption", async (t) => {
    await setup(t);
    await openMenu();
    assert.ok(signedBlock(), "the signed block is drawn");
    assert.equal(signedBlock().hidden, false);
    const text = shownText($("wellnessSection"));
    assert.match(text, /What BlackBox may use/);
    assert.match(text, /Sleep: Ask/);
    assert.match(text, /Heart: Proactive, local AI only/);
    assert.match(text, /Activity: Ask/);
    assert.match(text, /Blood pressure: Off/);
    assert.match(text, /Where it may go/);
    assert.match(text, /AI on your network/);
    assert.match(text, /Anthropic/);
    assert.match(text, /lan-box/);
    assert.match(text, /Health data hasn't synced recently/);
    assert.match(text, /Your phone hasn't sent health data since 07:10\./);
    assert.match(text, /To fix: Open BlackBox on your phone/);
    assert.doesNotMatch(text, /On your phone: Open BlackBox on your phone/, "the fix already says where");
    assert.match(text, /Disk encryption: on \(LUKS\)/);
    const instinct = signedBlock().querySelector(".wellness-instinct");
    assert.ok(instinct, "the instinct prompt is shown");
    assert.match(instinct.textContent, /You have this person's own wearable data/);
    assert.match(instinct.textContent, /default/i);
    assert.match(text, /BlackBox, Settings, Health/, "changes are made on the phone");
    // Show, don't edit (M7.3 brings narrowing from the Portal).
    assert.equal($("wellnessSection").querySelectorAll("input, select, textarea, [contenteditable]").length, 0);
});

test("the signed view shows only what it names: wear, sync and policy fields never render", async (t) => {
    const planted = {
        ...SIGNED,
        wear: { hr_mode: "SENTINEL-WEAR", nights_worn_7: 987654 },
        sync: { last_push_at: "SENTINEL-PUSH", outbox_hint: { batches_received: 876543 } },
        policy_version: "SENTINEL-POLICY",
        surfaces_mode: "SENTINEL-SURFACES",
        instinct_prompt_default: "the default prompt",
        replace_pending: "SENTINEL-REPLACE",
        operator: PEER,
        categories: { ...SIGNED.categories, sleep: "SENTINEL-CONSENT", made_up: "ask" },
    };
    await setup(t, { status: planted });
    await openMenu();
    const section = $("wellnessSection");
    const everything = section.innerHTML;
    for (const sentinel of ["SENTINEL", "987654", "876543", PEER, "made_up"]) {
        assert.ok(!everything.includes(sentinel), `${sentinel} must not reach the card`);
    }
    assert.doesNotMatch(shownText(section), /Sleep:/, "a consent word this Portal does not know is not shown");
});

test("hostile strings in the signed view stay text", async (t) => {
    const hostile = {
        ...SIGNED,
        trouble_details: [{ code: XSS, title: XSS, body: XSS, fix: XSS }],
        providers_allowed: [XSS, `custom:${XSS}`],
        instinct_prompt: `${XSS} be brief`,
        fde: { root_encrypted: XSS, method: XSS, store_encrypted: XSS },
    };
    await setup(t, { status: hostile });
    await openMenu();
    const section = $("wellnessSection");
    assert.deepEqual(scriptVectors(section), [], section.innerHTML);
    assert.equal(section.querySelector("img"), null);
    assert.ok(signedBlock().textContent.includes(XSS), "shown as typed, as text");
    assert.match(shownText(section), /Disk encryption: could not be checked/, "a non-boolean answer is not a yes");
    assert.ok(!signedBlock().querySelector(".wellness-instinct").textContent.includes("default"),
        "a prompt of their own is not called the default");
});

test("Google needs the paid-key tick to count", async (t) => {
    await setup(t, { status: { ...SIGNED, providers_allowed: ["google"], google_paid_key_confirmed: false } });
    await openMenu();
    assert.match(shownText($("wellnessSection")), /Google \(needs a paid key\)/);
});

test("nothing allowed but the network: says so", async (t) => {
    await setup(t, { status: { ...SIGNED, providers_allowed: [] } });
    await openMenu();
    const where = [...signedBlock().querySelectorAll(".wellness-providers li")].map((li) => li.textContent);
    assert.deepEqual(where, ["AI on your network"]);
});

test("a null from the app is an unsigned read: the four-field card only, whatever the reply carries", async (t) => {
    const { bridge } = await setup(t, { bridge: fakeBridge({ sign: () => null }) });
    await openMenu();
    assert.equal(bridge.signed.length, 1);
    assert.equal(statusCalls()[0].headers[HEADER], undefined, "no header on an unsigned read");
    assert.ok(!signedBlock() || signedBlock().hidden, "no signed view");
    assert.doesNotMatch(shownText($("wellnessSection")), /What BlackBox may use|Anthropic|synced recently/);
    assert.equal($("wellnessStateChip").textContent, "Active");
});

for (const [name, sign] of Object.entries({
    throws: () => { throw new Error("no key"); },
    "a number": () => 42,
    "a header break": () => "v1;enr=x\r\nInjected: 1",
    "an empty string": () => "",
    undefined: () => undefined,
})) {
    test(`an app that answers ${name} is unsigned, and the card still renders`, async (t) => {
        await setup(t, { bridge: fakeBridge({ sign }) });
        await openMenu();
        assert.equal(statusCalls()[0].headers[HEADER], undefined);
        assert.equal($("wellnessStateChip").textContent, "Active");
        assert.ok(!signedBlock() || signedBlock().hidden);
    });
}

test("a desktop browser (no channel) sends no header and gets the four-field card", async (t) => {
    await setup(t, { bridge: null });
    await openMenu();
    assert.deepEqual(statusCalls()[0].headers, {});
    assert.ok(!signedBlock() || signedBlock().hidden);
    assert.doesNotMatch(shownText($("wellnessSection")), /What BlackBox may use/);
});

test("an app without the channel (AndroidBridge only, or the old wellnessSign) is a desktop too", async (t) => {
    window.AndroidBridge = { wellnessOpenSettings() {}, wellnessSign: () => "v1;enr=en_0000abcd;ts=1;nonce=n;sig=s" };
    await setup(t, { bridge: null });
    await openMenu();
    assert.deepEqual(statusCalls()[0].headers, {});
    assert.ok(!signedBlock() || signedBlock().hidden);
});

test("an app that never answers: the read waits for the channel, then goes unsigned", async (t) => {
    const { bridge } = await setup(t, { bridge: fakeBridge({ mode: "never" }) });
    menu().classList.remove("hide");
    await waitFor(() => statusCalls().length === 1, "the unsigned read after the sign timeout", wellness.SIGN_TIMEOUT_MS + 3000);
    assert.equal(bridge.posted.length, 1, "it asked once");
    assert.deepEqual(statusCalls()[0].headers, {});
    assert.equal($("wellnessStateChip").textContent, "Active");
    assert.ok(!signedBlock() || signedBlock().hidden);
});

test("a signed request answered with the four fields (another operator's phone) shows no signed view", async (t) => {
    await setup(t, { status: { state: "active", source_label: "Fixture Phone (SM-TEST1)", last_sync: null, enrolled: true } });
    await openMenu();
    assert.ok(!signedBlock() || signedBlock().hidden);
});

test("every poll asks the app again: a fresh proof per request, never cached", async (t) => {
    const { timers, bridge } = await setup(t);
    await openMenu();
    await timers.advance(wellness.WELLNESS_POLL_MS);
    await waitFor(() => statusCalls().length === 2, "the second poll");
    assert.equal(bridge.signed.length, 2);
    const [a, b] = statusCalls().map((c) => c.headers[HEADER]);
    assert.ok(a && b && a !== b, "two requests, two proofs");
});

test("an operator switch drops the signed view at once", async (t) => {
    await setup(t);
    await openMenu();
    assert.equal(signedBlock().hidden, false);
    box.status = { state: null, source_label: null, last_sync: null, enrolled: false };
    state.setOperator(PEER);
    window.dispatchEvent(new CustomEvent("bbx:operator-changed", { detail: { operator: PEER } }));
    assert.equal(signedBlock().hidden, true, "at once, before the new read");
    await waitFor(() => statusCalls().length === 2, "the new operator's read");
    assert.equal(signedBlock().hidden, true);
    assert.doesNotMatch(signedBlock().textContent, /Anthropic|synced recently/, "the previous operator's settings are gone");
});

test("Who saw my health data: a signed GET /wellness/disclosures?operator= when opened, rows as text", async (t) => {
    const { bridge } = await setup(t, { });
    box.disclosures = [...DISCLOSURES, { time: XSS, surface: XSS, provider: XSS, model: XSS, tool: XSS, categories: [XSS] }];
    await openMenu();
    const details = signedBlock().querySelector("details.wellness-disclosures");
    assert.ok(details);
    assert.equal(calls.filter((c) => c.url.startsWith("/wellness/disclosures")).length, 0, "nothing until it is opened");
    details.open = true;
    details.dispatchEvent(new Event("toggle"));
    await waitFor(() => details.dataset.state === "loaded", "the disclosure log");
    const disc = calls.filter((c) => c.url.startsWith("/wellness/disclosures"));
    assert.equal(disc.length, 1);
    assert.equal(disc[0].url, `/wellness/disclosures?operator=${ME}`, "the app signs a GET that names the Portal's operator");
    assert.ok(disc[0].headers[HEADER]);
    assert.deepEqual(bridge.signed.at(-1), { method: "GET", path: `/wellness/disclosures?operator=${ME}`, hex: EMPTY_SHA256 });
    const rows = [...details.querySelectorAll("li")].map((li) => li.textContent);
    assert.equal(rows.length, 3);
    assert.match(rows[0], /BlackBox app/);
    assert.match(rows[0], /Anthropic/);
    assert.match(rows[0], /Anthropic \(claude-test\)/);
    assert.match(rows[0], /Heart, Sleep/);
    assert.match(rows[1], /lan-box \(fixture-model\)/);
    assert.deepEqual(scriptVectors(details), []);
    assert.equal(details.querySelector("img"), null);
});

test("Who saw my health data: 403 says Not available", async (t) => {
    await setup(t);
    box.disclosuresCode = 403;
    await openMenu();
    const details = signedBlock().querySelector("details.wellness-disclosures");
    details.open = true;
    details.dispatchEvent(new Event("toggle"));
    await waitFor(() => details.dataset.state === "loaded", "the refusal");
    assert.match(details.textContent, /Not available/);
});

test("lastSourceLabel: the phone label of the last status read, for that operator only", async (t) => {
    await setup(t);
    await openMenu();
    assert.equal(wellness.lastSourceLabel(ME), "Fixture Phone (SM-TEST1)");
    assert.equal(wellness.lastSourceLabel(PEER), null);
});

test("sha256Hex: the digest of the exact UTF-8 bytes, with or without SubtleCrypto", async () => {
    const hex = (s) => createHash("sha256").update(Buffer.from(s, "utf8")).digest("hex");
    assert.equal(await wellness.sha256Hex(""), EMPTY_SHA256);
    assert.equal(await wellness.sha256Hex("ünïcödé ✓ {\"a\":1}"), hex("ünïcödé ✓ {\"a\":1}"));
    assert.equal(await wellness.sha256Hex("x", undefined), hex("x"), "the default is the page's SubtleCrypto");
    // Plain http in the WebView has no SubtleCrypto: the page's own digest.
    assert.equal(await wellness.sha256Hex("x", null), hex("x"));
    assert.equal(await wellness.sha256Hex("x", { digest: () => Promise.reject(new Error("nope")) }), hex("x"));
});

test("the Portal's body hash, signed the way the phone signs, reproduces the box's golden vectors", async (t) => {
    // Orchestrator/tests/fixtures/wellness/origin_proof_vectors.json (synthetic
    // keys): sig = HMAC-SHA256(K, "v1|enr|ts|nonce|METHOD|path|sha256hex(body)").
    // The app does the HMAC; what the Portal owes it is that hex, over the
    // exact UTF-8 bytes of the string body it sends.
    const vectors = JSON.parse(readFileSync(resolve(REPO_ROOT, "Orchestrator", "tests", "fixtures", "wellness",
        "origin_proof_vectors.json"), "utf8")).filter((v) => v.content_encoding === null);
    assert.ok(vectors.length >= 4);
    t.after(() => installChannel(null));
    for (const v of vectors) {
        const key = Buffer.from(v.k_b64url, "base64url");
        installChannel(fakeChannel({
            sign(method, path, hex) {
                const canonical = `v1|${v.enr}|${v.ts}|${v.nonce}|${method}|${path.split("?")[0]}|${hex}`;
                const sig = createHmac("sha256", key).update(canonical, "utf8").digest("hex");
                return `v1;enr=${v.enr};ts=${v.ts};nonce=${v.nonce};sig=${sig}`;
            },
        }));
        const target = v.request_target ?? v.path;
        const proof = await wellness.originProof(v.method.toLowerCase(), target, v.body_utf8);
        assert.equal(proof, `v1;enr=${v.enr};ts=${v.ts};nonce=${v.nonce};sig=${v.expected_sig}`, `${v.method} ${target}`);
    }
});

test("originProof: the app's value, or null for anything that is not a header value", async (t) => {
    installChannel(null);
    assert.equal(await wellness.originProof("GET", "/wellness/status?operator=x"), null, "no channel");
    const bridge = fakeBridge();
    installChannel(bridge);
    t.after(() => installChannel(null));
    const value = await wellness.originProof("post", "/chat/stream", "{\"a\":\"ü\"}");
    assert.match(value, /^v1;enr=/);
    assert.deepEqual(bridge.signed, [{ method: "POST", path: "/chat/stream",
        hex: createHash("sha256").update(Buffer.from("{\"a\":\"ü\"}", "utf8")).digest("hex") }]);
    assert.deepEqual(await wellness.withOriginProof({ "Content-Type": "application/json" }, "POST", "/chat", "{}"),
        { "Content-Type": "application/json", [HEADER]: `v1;enr=en_0000abcd;ts=1800000002;nonce=${"2".padStart(32, "0")};sig=${"a".repeat(64)}` });
    installChannel(fakeBridge({ sign: () => null }));
    const base = { "Content-Type": "application/json" };
    assert.equal(await wellness.withOriginProof(base, "POST", "/chat", "{}"), base, "unsigned: the same headers object");
});

// ── A refused proof is never less than a desktop ───────────────────────────

const REFUSALS = {
    "401 bad_signature (a key this box does not know, a phone clock off)": {
        refusal: { code: 401, body: { error: "bad_signature" } },
        minimal: { state: "active", source_label: "Fixture Phone (SM-TEST1)", last_sync: "2026-10-29T11:48:00Z", enrolled: true },
        chip: "Active", says: /Fixture Phone \(SM-TEST1\)/,
    },
    "409 owner_changed": {
        refusal: { code: 409, body: { error: "owner_changed" } },
        minimal: { state: "ownership_changed", source_label: "Fixture Phone (SM-TEST1)", last_sync: "2026-10-29T11:48:00Z", enrolled: false },
        chip: "Needs attention", says: /no longer assigned to you in Devices/,
    },
    "410 revoked": {
        refusal: { code: 410, body: { error: "revoked" } },
        minimal: { state: "revoked", source_label: null, last_sync: null, enrolled: false },
        chip: "Disconnected", says: /Health is disconnected/,
    },
    "410 source_moved (Use for health picked another phone)": {
        refusal: { code: 410, body: { error: "source_moved" } },
        minimal: { state: "active", source_label: "Other Fixture Phone (SM-TEST2)", last_sync: "2026-10-29T11:50:00Z", enrolled: true },
        chip: "Active", says: /Other Fixture Phone \(SM-TEST2\)/,
    },
};

for (const [name, c] of Object.entries(REFUSALS)) {
    test(`a signed read the box refuses (${name}) is read again unsigned: the desktop's card, no signed view`, async (t) => {
        const { bridge } = await setup(t, { status: c.minimal });
        box.refuseSigned = c.refusal;
        await openMenu();
        await waitFor(() => statusCalls().length === 2, "the unsigned re-read");
        const [first, second] = statusCalls();
        assert.ok(first.headers[HEADER], "the first read went out signed");
        assert.deepEqual(second.headers, {}, "the re-read carries no proof");
        assert.equal(second.url, first.url);
        assert.equal(bridge.signed.length, 1, "the app is asked once");
        assert.equal($("wellnessStateChip").textContent, c.chip);
        const text = shownText($("wellnessSection"));
        assert.match(text, c.says);
        assert.doesNotMatch(text, /not available for this operator/i);
        assert.ok(!signedBlock() || signedBlock().hidden, "an unsigned answer never shows the settings view");
    });
}

test("a refused re-read is the refusal: no loop, 'not available for this operator'", async (t) => {
    await setup(t);
    box.refuseSigned = { code: 401, body: { error: "bad_signature" } };
    box.status = { error: "invalid_operator" };
    box.statusCode = 400;
    await openMenu();
    await waitFor(() => /not available for this operator/i.test(shownText($("wellnessSection"))), "the refusal");
    await flush();
    assert.equal(statusCalls().length, 2, "one signed read, one unsigned, no more");
});

test("a refusal that is not about the proof (400) is not read again", async (t) => {
    await setup(t);
    box.status = { error: "invalid_operator" };
    box.statusCode = 400;
    await openMenu();
    await waitFor(() => /not available for this operator/i.test(shownText($("wellnessSection"))), "the refusal");
    await flush();
    assert.equal(statusCalls().length, 1);
});

test("the next poll signs again after a refused proof (the phone may have re-enrolled)", async (t) => {
    const { timers, bridge } = await setup(t, { status: REFUSALS["401 bad_signature (a key this box does not know, a phone clock off)"].minimal });
    box.refuseSigned = { code: 401, body: { error: "bad_signature" } };
    await openMenu();
    await waitFor(() => statusCalls().length === 2, "the unsigned re-read");
    box.refuseSigned = null;
    box.status = SIGNED;
    await timers.advance(wellness.WELLNESS_POLL_MS);
    await waitFor(() => statusCalls().length === 3, "the next poll");
    assert.ok(statusCalls()[2].headers[HEADER], "signed again");
    assert.equal(bridge.signed.length, 2);
    await waitFor(() => signedBlock() && !signedBlock().hidden, "the settings view");
});

// ── The chat footer never borrows the selected operator's phone ────────────

test("a footer with no recorded source says 'your phone', even when the card knows a phone", async (t) => {
    await setup(t);
    await openMenu();
    assert.equal(wellness.lastSourceLabel(ME), "Fixture Phone (SM-TEST1)", "the card has a label for the selected operator");
    // A turn from earlier (this operator's or another's: the chat history is one
    // store) that recorded no source label.
    const bubble = document.createElement("div");
    bubble.className = "bubble assistant";
    const used = { ref: "w-20261029-7f3a", categories: ["sleep"], as_of: "2026-10-29T07:10-04:00",
        provider: "anthropic", provider_label: "Anthropic", source_label: null };
    const footer = bubbles.decorateWellnessBubble(bubble, { content: "You slept well.", wellness_ref: used.ref, wellness_used: used });
    assert.match(footer.textContent, /^Used your health data · your phone · /);
    assert.ok(!footer.textContent.includes("Fixture Phone"));
    const recorded = bubbles.decorateWellnessBubble(bubble, { content: "You slept well.", wellness_ref: used.ref,
        wellness_used: { ...used, source_label: "Recorded Phone" } });
    assert.match(recorded.textContent, /^Used your health data · Recorded Phone · /);
});
