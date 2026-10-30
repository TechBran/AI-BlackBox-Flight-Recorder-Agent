// System Menu > Devices: the Health entry point (M7.2 of the M7 health design,
// docs/plans 2026-10-25-samsung-health-integration.md §3.1, §6.2).
//
//  * "Use for health" appears only on the current operator's OWN Android rows
//    and POSTs /wellness/select {operator, device_id}; the toast sends the
//    operator to the phone, where setup finishes.
//  * The "Health source" badge comes from the additive mesh fields
//    health_source / health_state, which the box fills only when ?operator=
//    equals the row's owner (rule W17). The Portal reads them from a mesh read
//    made AS the current operator and believes them only on that operator's own
//    rows, so "All operators" still badges your own phone and never anyone
//    else's.
//  * Improvements to the same file: owner changes ask in the page instead of
//    window.confirm (the Android WebView has no dialog host, so confirm()
//    returned false unseen and unassign / re-home silently did nothing on the
//    phone), and a refresh asked for while one is loading runs after it
//    instead of being dropped (a filter change or a post-edit refresh used to
//    leave the list on the old answer).
//
// Run: bash scripts/run_web_tests.sh
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { installDom, PORTAL, scriptVectors } from "./helpers/portal-dom.mjs";

installDom();

const ME = "wellness-fixture";
const PEER = "wellness-fixture-peer";
const OVERSEER = "wellness-fixture-overseer";
const XSS = `<img src=x onerror="window.__pwned=1">`;
const ATTR_BREAK = `x" autofocus onfocus="window.__pwned=1`;
const PHONE_TOAST = "Finish on your phone: BlackBox, Settings, Health";

// No browser dialogs anywhere in this section (the WebView cannot show them).
const dialogs = [];
for (const kind of ["confirm", "alert", "prompt"]) {
    const fn = (text) => { dialogs.push(`${kind}: ${text}`); throw new Error(`window.${kind} called`); };
    window[kind] = fn;
    globalThis[kind] = fn;
}

// The section's real markup, from the page the Portal serves, inside a stand-in System Menu.
{
    const page = readFileSync(resolve(PORTAL, "index.html"), "utf8");
    const section = new DOMParser().parseFromString(page, "text/html").querySelector(".devices-section");
    assert.ok(section, "Portal/index.html has the Devices section");
    document.body.insertAdjacentHTML("beforeend", `<div id="menuModal" class="modal hide"><div class="menu-body"></div></div>`);
    document.querySelector("#menuModal .menu-body").appendChild(document.importNode(section, true));
}

const $ = (id) => document.getElementById(id);
const flush = async () => {
    for (let i = 0; i < 6; i++) await new Promise((r) => setImmediate(r));
};

const reply = (body, status = 200) => Promise.resolve({
    ok: status >= 200 && status < 300, status,
    json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)),
});

const FOLD = { id: "fold", name: "Fixture Fold", tailnet: "fold.fixture.ts.net", type: "android", online: true, owner: ME, is_primary: true, default_provider: null };
const LAPTOP = { id: "laptop", name: "Fixture Laptop", tailnet: "laptop.fixture.ts.net", type: "windows", online: true, owner: ME, is_primary: false, default_provider: null };
const TAB = { id: "tab", name: "Peer Tablet", tailnet: "tab.fixture.ts.net", type: "android", online: true, owner: PEER, is_primary: true, default_provider: null };
const SPARE = { id: "spare", name: "Spare Phone", tailnet: "spare.fixture.ts.net", type: "android", online: false, owner: null, is_primary: false, default_provider: null };
const WATCH = { id: "overseer-phone", name: "Overseer Phone", tailnet: "ov.fixture.ts.net", type: "android", online: true, owner: OVERSEER, is_primary: false, default_provider: null };

/**
 * A scripted box. `rows` is the registry joined with the tailnet; `health[id]`
 * is what the box fills on a row ONLY for a read made as its owner (as the real
 * route does); `forged[id]` is planted on a row whatever the read, to prove the
 * Portal does not believe health fields on rows that are not the operator's own.
 */
const box = { rows: [], health: {}, forged: {}, select: { status: 200, body: { state: "awaiting_phone" } }, hold: null };
const calls = [];

function meshFor(operator) {
    let rows = box.rows.map((r) => ({ ...r }));
    if (operator) rows = rows.filter((r) => r.owner == null || r.owner.toLowerCase() === operator.toLowerCase());
    return rows.map((r) => ({
        ...r,
        ...(operator && r.owner === operator ? box.health[r.id] || {} : {}),
        ...(box.forged[r.id] || {}),
    }));
}

globalThis.fetch = window.fetch = (url, opts = {}) => {
    const u = String(url);
    const method = opts.method || "GET";
    const body = opts.body ? JSON.parse(opts.body) : null;
    calls.push({ url: u, method, body });
    if (u === "/operators") return reply({ operators: [ME, PEER, OVERSEER], reserved: [OVERSEER] });
    if (u.startsWith("/devices/mesh")) {
        const operator = new URL(u, "http://box").searchParams.get("operator") || "";
        const answer = { devices: meshFor(operator), operator: operator || null };
        if (box.hold) return new Promise((r) => box.hold.push(() => r({ ok: true, status: 200, json: () => Promise.resolve(answer) })));
        return reply(answer);
    }
    if (u === "/wellness/select" && method === "POST") return reply(box.select.body, box.select.status);
    if (method === "POST" && u.startsWith("/devices/")) return reply({ ok: true });
    return Promise.reject(new TypeError("offline"));
};
const meshCalls = () => calls.filter((c) => c.url.startsWith("/devices/mesh")).map((c) => c.url);
const posts = () => calls.filter((c) => c.method === "POST").map((c) => ({ url: c.url, body: c.body }));

const state = await import("../../Portal/modules/state-management.js");
const devices = await import("../../Portal/modules/devices-section.js");
devices.initDevicesSection();

const wellnessEvents = [];
window.addEventListener("bbx:wellness-changed", () => wellnessEvents.push(Date.now()));

/** Every rendered card, by device name. */
function cards() {
    const out = {};
    for (const card of document.querySelectorAll("#devicesList .device-card")) {
        out[card.querySelector(".device-name").textContent] = card;
    }
    return out;
}
const useBtn = (card) => card && card.querySelector("button.device-wellness-use");
const badge = (card) => card && card.querySelector(".device-wellness-badge");

/** Reset the box, pick the operator, and load the section the way the operator sees it (menu opened, "All operators"). */
async function load({ rows = [FOLD, LAPTOP, TAB, SPARE, WATCH], health = {}, forged = {}, operator = ME } = {}) {
    Object.assign(box, { rows, health, forged, hold: null, select: { status: 200, body: { state: "awaiting_phone" } } });
    state.setOperator(operator);
    $("menuModal").classList.add("hide");
    await flush();
    calls.length = 0;
    $("menuModal").classList.remove("hide");
    await flush();
    return cards();
}

test("Use for health shows only on the current operator's own Android rows", async () => {
    const c = await load();
    assert.ok(useBtn(c["Fixture Fold"]), "own Android phone");
    assert.equal(useBtn(c["Fixture Laptop"]), null, "own, but not Android");
    assert.equal(useBtn(c["Peer Tablet"]), null, "someone else's phone");
    assert.equal(useBtn(c["Spare Phone"]), null, "an unassigned phone (assign it to yourself first)");
    assert.equal(useBtn(c["Overseer Phone"]), null);
    assert.equal(useBtn(c["Fixture Fold"]).textContent, "Use for health");
    assert.deepEqual(dialogs, []);
});

test("the Health source badge is read as the current operator and believed only on its own rows", async () => {
    const c = await load({
        health: { fold: { health_source: true, health_state: "active" }, tab: { health_source: true, health_state: "active" } },
        forged: { spare: { health_source: true, health_state: "active" }, tab: { health_source: true, health_state: "paused" } },
    });
    assert.deepEqual(meshCalls(), ["/devices/mesh", `/devices/mesh?operator=${ME}`],
        "the 'All operators' list, then one read as the operator for its own health fields");
    assert.equal(badge(c["Fixture Fold"]).textContent.trim(), "Health source · Active");
    assert.equal(badge(c["Peer Tablet"]), null, "never another operator's enrollment (W17)");
    assert.equal(badge(c["Spare Phone"]), null, "a row that is not yours is not believed");
    assert.equal(useBtn(c["Fixture Fold"]), null, "an active source is managed on the phone, not re-picked");
});

test("a source still waiting for the phone keeps Use for health, to send the nudge again", async () => {
    const c = await load({ health: { fold: { health_source: true, health_state: "awaiting_phone" } } });
    assert.equal(badge(c["Fixture Fold"]).textContent.trim(), "Health source · Finishing setup");
    assert.ok(useBtn(c["Fixture Fold"]));
});

test("an operator with no phone of their own makes no second mesh read", async () => {
    await load({ rows: [TAB, SPARE] });
    assert.deepEqual(meshCalls(), ["/devices/mesh"]);
});

test("a filter already set to the current operator reads the mesh once", async () => {
    await load({ health: { fold: { health_source: true, health_state: "paused" } } });
    calls.length = 0;
    const sel = $("devicesOperatorFilter");
    sel.value = ME;
    sel.dispatchEvent(new Event("change"));
    await flush();
    assert.deepEqual(meshCalls(), [`/devices/mesh?operator=${ME}`], "that read already carries the health fields");
    assert.equal(badge(cards()["Fixture Fold"]).textContent.trim(), "Health source · Paused");
    sel.value = "";
    sel.dispatchEvent(new Event("change"));
    await flush();
});

test("Use for health POSTs /wellness/select {operator, device_id} and sends the operator to the phone", async () => {
    const c = await load();
    wellnessEvents.length = 0;
    useBtn(c["Fixture Fold"]).click();
    await flush();
    assert.deepEqual(posts(), [{ url: "/wellness/select", body: { operator: ME, device_id: "fold" } }]);
    assert.equal($("toast").textContent, PHONE_TOAST);
    assert.ok($("toast").classList.contains("success"));
    assert.equal(wellnessEvents.length, 1, "the Health card is told to refresh");
    assert.ok(meshCalls().length >= 3, "the list reloads to show the new badge");
    assert.deepEqual(dialogs, []);
});

test("a refused select says why in words and re-enables the button", async () => {
    const cases = [
        [{ status: 409, body: { error: "owner_mismatch" } }, "This phone is not assigned to you in Devices"],
        [{ status: 424, body: { error: "not_paired", fix: "Open the BlackBox app once on this phone" } }, "Open the BlackBox app once on this phone"],
        [{ status: 424, body: { detail: { error: "not_paired", fix: "Open the BlackBox app once on this phone" } } }, "Open the BlackBox app once on this phone"],
        [{ status: 409, body: { error: "already_enrolled" } }, /already set up/],
        [{ status: 422, body: { detail: "operator is not a live operator" } }, "operator is not a live operator"],
        [{ status: 404, body: { detail: "Not Found" } }, /not available on this BlackBox/],
        [{ status: 500, body: null }, /HTTP 500/],
    ];
    for (const [select, expected] of cases) {
        const c = await load();
        box.select = select;
        const btn = useBtn(c["Fixture Fold"]);
        btn.click();
        await flush();
        const text = $("toast").textContent;
        if (expected instanceof RegExp) assert.match(text, expected, JSON.stringify(select));
        else assert.equal(text, expected, JSON.stringify(select));
        assert.ok($("toast").classList.contains("error"));
        assert.equal(btn.disabled, false, "the operator can try again");
    }
});

test("hostile device names and states stay text", async () => {
    const hostile = { ...FOLD, name: `${XSS} ${ATTR_BREAK}`, tailnet: XSS };
    await load({ rows: [hostile, TAB], health: { fold: { health_source: true, health_state: XSS } } });
    const list = $("devicesList");
    assert.deepEqual(scriptVectors(list), [], list.innerHTML);
    assert.equal(list.querySelector("img"), null);
    const card = list.querySelector(".device-card");
    assert.equal(card.querySelector(".device-name").textContent, `${XSS} ${ATTR_BREAK}`);
    assert.equal(badge(card).textContent.trim(), "Health source", "an unknown state is not printed");
});

test("a reserved operator (the box overseer) gets no Use for health", async () => {
    const c = await load({ operator: OVERSEER });
    assert.equal(useBtn(c["Overseer Phone"]), null);
    assert.equal(document.querySelectorAll("#devicesList button.device-wellness-use").length, 0);
});

test("no operator selected: no health controls at all", async () => {
    await load({ operator: "", health: { fold: { health_source: true, health_state: "active" } } });
    assert.equal(document.querySelectorAll("#devicesList .device-wellness-row").length, 0);
});

test("switching operator moves the health controls to the new operator's rows", async () => {
    await load({ health: { fold: { health_source: true, health_state: "active" } } });
    assert.ok(badge(cards()["Fixture Fold"]));
    state.setOperator(PEER);
    window.dispatchEvent(new CustomEvent("bbx:operator-changed", { detail: { operator: PEER } }));
    await flush();
    const c = cards();
    assert.equal(badge(c["Fixture Fold"]), null, "the previous operator's badge is gone");
    assert.equal(useBtn(c["Fixture Fold"]), null);
    assert.ok(useBtn(c["Peer Tablet"]), "the new operator's own phone");
    assert.ok(meshCalls().includes(`/devices/mesh?operator=${PEER}`));
});

test("a click after a silent operator switch re-checks instead of posting for the old operator", async () => {
    const c = await load();
    state.setOperator(PEER);   // no event: the card still shows the old operator's button
    useBtn(c["Fixture Fold"]).click();
    await flush();
    assert.deepEqual(posts(), [], "nothing is posted for an operator who is no longer selected");
    assert.equal(useBtn(cards()["Fixture Fold"]), null, "the list re-renders for the current operator");
});

test("unassign asks in the page, never with window.confirm, and Cancel restores the owner", async () => {
    await load();
    const card = cards()["Fixture Fold"];
    const sel = card.querySelector(".device-owner-select");
    sel.value = "__unassign__";
    sel.dispatchEvent(new Event("change"));
    await flush();
    const row = card.querySelector(".inline-confirm-row");
    assert.ok(row, "an inline question, not a dialog");
    assert.match(row.textContent, /Unassign Fixture Fold from wellness-fixture\?/);
    row.querySelector(".inline-confirm-cancel").click();
    await flush();
    assert.equal(sel.value, ME, "Cancel puts the owner back");
    assert.equal(card.querySelector(".inline-confirm-row"), null);
    assert.deepEqual(posts(), []);
    assert.deepEqual(dialogs, []);
});

test("unassign: Continue posts /unassign once", async () => {
    await load();
    const card = cards()["Fixture Fold"];
    const sel = card.querySelector(".device-owner-select");
    sel.value = "__unassign__";
    sel.dispatchEvent(new Event("change"));
    await flush();
    card.querySelector(".inline-confirm-row .inline-confirm-ok").click();
    await flush();
    assert.deepEqual(posts(), [{ url: "/devices/fold/unassign", body: { operator: ME } }]);
    assert.deepEqual(dialogs, []);
});

test("re-home asks in the page; a newer pick replaces the open question", async () => {
    await load();
    const card = cards()["Fixture Fold"];
    const sel = card.querySelector(".device-owner-select");
    sel.value = "__unassign__";
    sel.dispatchEvent(new Event("change"));
    await flush();
    sel.value = PEER;   // changed their mind before answering
    sel.dispatchEvent(new Event("change"));
    await flush();
    const rows = card.querySelectorAll(".inline-confirm-row");
    assert.equal(rows.length, 1, "one question at a time");
    assert.match(rows[0].textContent, /Re-home Fixture Fold from wellness-fixture to wellness-fixture-peer\?/);
    assert.equal(sel.value, PEER, "the superseded question does not reset the newer pick");
    rows[0].querySelector(".inline-confirm-ok").click();
    await flush();
    assert.deepEqual(posts(), [
        { url: "/devices/fold/unassign", body: { operator: PEER } },
        { url: "/devices/fold/operator", body: { operator: PEER } },
    ]);
    assert.deepEqual(dialogs, []);
});

test("a refresh asked for while one is loading runs after it, with the newest filter", async () => {
    await load();
    box.hold = [];
    $("btnRefreshDevices").click();          // load 1: "All operators", held at the box
    await flush();
    const sel = $("devicesOperatorFilter");
    sel.value = ME;                          // changed while load 1 is out
    sel.dispatchEvent(new Event("change"));
    await flush();
    const held = box.hold;
    box.hold = null;
    held.forEach((release) => release());
    await flush();
    await flush();
    const names = Object.keys(cards());
    assert.ok(!names.includes("Peer Tablet"), `the list shows the newest filter (got ${names.join(", ")})`);
    assert.ok(names.includes("Fixture Fold"));
    assert.equal(meshCalls().slice(-1)[0], `/devices/mesh?operator=${ME}`);
    sel.value = "";
    sel.dispatchEvent(new Event("change"));
    await flush();
});
