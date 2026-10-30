// System Menu > Health card (M7.2 of the M7 health design, docs/plans
// 2026-10-25-samsung-health-integration.md §6.2): status only.
//
// A browser the phone has not approved gets the UNcredentialed status reply,
// {state, source_label, last_sync, enrolled}. The card shows those four fields
// and nothing else (rule W17: categories, providers and trouble codes never
// reach an unapproved surface, even if a reply carries them), asks only about
// the operator this Portal is on, keeps server text as text, explains what is
// needed when the operator has no Android phone, and polls only while the
// System Menu is open and the page is visible.
//
// Run: bash scripts/run_web_tests.sh
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { installDom, PORTAL, scriptVectors } from "./helpers/portal-dom.mjs";
import { stripComments, innerHtmlAssignments, isSafeRhs, rawHtmlApiUses } from "./helpers/sink-scan.mjs";

installDom();

const ME = "wellness-fixture";
const PEER = "wellness-fixture-peer";
const XSS = `<img src=x onerror="window.__pwned=1">`;
const ATTR_BREAK = `x" autofocus onfocus="window.__pwned=1`;
const NOW = Date.parse("2026-10-29T12:00:00Z");

// The card's real markup, taken from the page the Portal serves, inside a
// stand-in System Menu.
{
    const page = readFileSync(resolve(PORTAL, "index.html"), "utf8");
    const section = new DOMParser().parseFromString(page, "text/html").getElementById("wellnessSection");
    assert.ok(section, "Portal/index.html has the Health card (#wellnessSection)");
    document.body.insertAdjacentHTML("beforeend", `<div id="menuModal" class="modal hide"><div class="menu-body"></div></div>`);
    document.querySelector("#menuModal .menu-body").appendChild(document.importNode(section, true));
}

const state = await import("../../Portal/modules/state-management.js");
const wellness = await import("../../Portal/modules/wellness-section.js");

const $ = (id) => document.getElementById(id);
const flush = async () => {
    for (let i = 0; i < 4; i++) await new Promise((r) => setImmediate(r));
};

/** Deterministic timers: nothing runs until the test advances the clock. */
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

function setHidden(hidden) {
    Object.defineProperty(document, "hidden", { value: hidden, configurable: true });
    Object.defineProperty(document, "visibilityState", { value: hidden ? "hidden" : "visible", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
}

const reply = (body, status = 200) => Promise.resolve({
    ok: status >= 200 && status < 300, status,
    json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)),
});

// A scripted box: the test sets what /wellness/status and /devices/mesh answer.
const box = { status: {}, statusCode: 200, mesh: [], meshCode: 200 };
const calls = [];
globalThis.fetch = window.fetch = (url, opts = {}) => {
    const u = String(url);
    calls.push({ url: u, method: opts.method || "GET" });
    if (box.statusCode === 0) return Promise.reject(new TypeError("Failed to fetch"));
    if (u.startsWith("/wellness/status")) return reply(box.status, box.statusCode);
    if (u.startsWith("/devices/mesh")) return reply({ devices: box.mesh }, box.meshCode);
    return Promise.reject(new TypeError("offline"));
};
const statusCalls = () => calls.filter((c) => c.url.startsWith("/wellness/status")).map((c) => c.url);
const meshCalls = () => calls.filter((c) => c.url.startsWith("/devices/mesh")).map((c) => c.url);

const menu = () => $("menuModal");
async function openMenu() { menu().classList.remove("hide"); await flush(); }
async function closeMenu() { menu().classList.add("hide"); await flush(); }

/** Text of everything in [root] that is not inside a [hidden] element. */
function shownText(root) {
    const copy = root.cloneNode(true);
    copy.querySelectorAll("[hidden]").forEach((el) => el.remove());
    return copy.textContent.replace(/\s+/g, " ").trim();
}

const ACTIVE = { state: "active", source_label: "Fixture Phone (SM-TEST1)", last_sync: "2026-10-29T11:48:00Z", enrolled: true };

async function setup(t, { status = ACTIVE, statusCode = 200, mesh = [], operator = ME } = {}) {
    calls.length = 0;
    Object.assign(box, { status, statusCode, mesh, meshCode: 200 });
    setHidden(false);
    await closeMenu();
    state.setOperator(operator);
    delete window.AndroidBridge;
    const timers = manualTimers();
    const ctl = wellness.initWellnessSection({ timers, now: () => NOW });
    t.after(async () => {
        ctl.destroy();
        await closeMenu();
        delete window.AndroidBridge;
    });
    return { timers, ctl };
}

test("asks nothing until the System Menu opens, then only about the current operator", async (t) => {
    await setup(t);
    await flush();
    assert.deepEqual(statusCalls(), [], "a closed menu polls nothing");
    await openMenu();
    assert.deepEqual(statusCalls(), [`/wellness/status?operator=${ME}`]);
});

test("the operator name is URL-encoded in the status query", async (t) => {
    await setup(t, { operator: "fixture & co" });
    await openMenu();
    assert.deepEqual(statusCalls(), ["/wellness/status?operator=fixture%20%26%20co"]);
});

test("an active status shows the state, the source, the last sync and points to the phone", async (t) => {
    await setup(t);
    await openMenu();
    assert.equal($("wellnessStateChip").textContent, "Active");
    assert.equal($("wellnessStateChip").dataset.tone, "ok");
    assert.equal($("wellnessSource").textContent, "Fixture Phone (SM-TEST1)");
    assert.equal($("wellnessSourceRow").hidden, false);
    assert.equal($("wellnessLastSync").textContent, "12 min ago");
    assert.match(shownText($("wellnessSection")), /Manage on your phone: BlackBox, Settings, Health/);
    assert.equal($("wellnessSteps").hidden, true, "no setup steps once it is set up");
});

test("awaiting_phone says to finish setting up on the phone", async (t) => {
    await setup(t, { status: { state: "awaiting_phone", source_label: "Fixture Phone (SM-TEST1)", last_sync: null, enrolled: false } });
    await openMenu();
    assert.equal($("wellnessStateChip").textContent, "Finishing setup");
    assert.match(shownText($("wellnessSection")), /Finish setting up Health on your phone/);
    assert.equal($("wellnessLastSync").textContent, "Not synced yet");
    assert.deepEqual(meshCalls(), [], "a started setup needs no device lookup");
});

test("every known state has a chip label; paused and the trouble states point to the phone", async (t) => {
    await setup(t);
    await openMenu();
    const views = {
        paused: ["Paused", /Resume or manage it on your phone: BlackBox, Settings, Health/],
        stale: ["Needs attention", /BlackBox, Settings, Health/],
        needs_attention: ["Needs attention", /BlackBox, Settings, Health/],
        ownership_changed: ["Needs attention", /no longer assigned to you in Devices/],
        revoked: ["Disconnected", /set it up on your phone/],
    };
    for (const [st, [label, note]] of Object.entries(views)) {
        box.status = { ...ACTIVE, state: st };
        window.dispatchEvent(new CustomEvent("bbx:wellness-changed"));
        await flush();
        assert.equal($("wellnessStateChip").textContent, label, st);
        assert.match(shownText($("wellnessSection")), note, st);
        assert.equal(wellness.healthStateLabel(st), label, `healthStateLabel(${st})`);
    }
    assert.equal(wellness.healthStateLabel("constructor"), "", "only the states this Portal knows");
    assert.equal(wellness.healthStateLabel(XSS), "");
    assert.equal(wellness.healthStateLabel(null), "");
});

test("a reply carrying more than the four fields renders only those four", async (t) => {
    const planted = {
        ...ACTIVE,
        categories: { sleep: "SENTINEL-CATEGORY-proactive" },
        local_only: { blood_pressure: "SENTINEL-LOCAL-ONLY" },
        providers_allowed: ["SENTINEL-PROVIDER"],
        google_paid_key_confirmed: "SENTINEL-PAID-KEY",
        troubles: [{ code: "SENTINEL-TROUBLE", title: "SENTINEL-TROUBLE-TITLE" }],
        wear: { hr_mode: "SENTINEL-WEAR", nights_worn_7: 987654 },
        sync: { last_push_at: "SENTINEL-PUSH", outbox_hint: "SENTINEL-OUTBOX" },
        instinct_prompt: "SENTINEL-INSTINCT-PROMPT",
        policy_version: "SENTINEL-POLICY",
        disclosures_count: 876543,
        operator: PEER,
    };
    await setup(t, { status: planted });
    await openMenu();
    const section = $("wellnessSection");
    const everything = section.innerHTML + " " + [...section.querySelectorAll("*")]
        .flatMap((el) => [...el.attributes].map((a) => a.value)).join(" ");
    for (const sentinel of ["SENTINEL", "987654", "876543", PEER, "proactive"]) {
        assert.ok(!everything.includes(sentinel), `${sentinel} must not reach the card`);
    }
    assert.equal($("wellnessStateChip").textContent, "Active", "the four fields still render");
    assert.deepEqual(Object.keys(wellness.pickStatus(planted)).sort(), ["enrolled", "last_sync", "source_label", "state"]);
    assert.deepEqual(wellness.pickStatus(planted), {
        state: "active", source_label: "Fixture Phone (SM-TEST1)", last_sync: "2026-10-29T11:48:00Z", enrolled: true,
    });
});

test("hostile server strings stay text", async (t) => {
    await setup(t, { status: { state: XSS, source_label: `${XSS} ${ATTR_BREAK}`, last_sync: XSS, enrolled: true } });
    await openMenu();
    const section = $("wellnessSection");
    assert.deepEqual(scriptVectors(section), [], section.innerHTML);
    assert.equal(section.querySelector("img"), null);
    assert.equal(section.querySelector("[onfocus]"), null);
    assert.equal($("wellnessSource").textContent, `${XSS} ${ATTR_BREAK}`, "the label is shown as typed, as text");
    assert.ok(!$("wellnessStateChip").textContent.includes("<img"), "an unknown state never reaches the chip");
    assert.equal($("wellnessStateChip").textContent, "Connected", "an enrolled source in a state this Portal does not know");
    assert.equal($("wellnessLastSync").textContent, "Not synced yet", "an unreadable time is not shown");
});

test("pickStatus keeps only well-typed values", () => {
    assert.deepEqual(wellness.pickStatus(null), { state: null, source_label: null, last_sync: null, enrolled: false });
    assert.deepEqual(wellness.pickStatus({ state: 7, source_label: { x: 1 }, last_sync: 5, enrolled: "yes" }),
        { state: null, source_label: null, last_sync: null, enrolled: false });
    assert.equal(wellness.pickStatus({ source_label: "x".repeat(500) }).source_label.length, 200, "a runaway label is capped");
});

test("not set up, no Android phone of their own: explains what Health needs", async (t) => {
    await setup(t, {
        status: { state: null, source_label: null, last_sync: null, enrolled: false },
        mesh: [
            { id: "peer-phone", type: "android", owner: PEER },
            { id: "my-laptop", type: "windows", owner: ME },
            { id: "spare", type: "android", owner: null },
        ],
    });
    await openMenu();
    assert.equal($("wellnessStateChip").textContent, "Not set up");
    const text = shownText($("wellnessSection"));
    assert.match(text, /BlackBox Android app/);
    assert.match(text, /Health Connect/);
    assert.match(text, /iPhone/);
    assert.equal($("wellnessSourceRow").hidden, true);
    assert.equal($("wellnessSyncRow").hidden, true);
    assert.deepEqual(meshCalls(), [`/devices/mesh?operator=${ME}`], "the device lookup is for this operator only");
    assert.ok($("wellnessSteps").querySelectorAll("li").length >= 2, "numbered setup steps");
});

test("not set up, with an Android phone of their own: points to Use for health in Devices", async (t) => {
    const { timers } = await setup(t, {
        status: { state: "not_set_up", source_label: null, last_sync: null, enrolled: false },
        mesh: [{ id: "my-phone", type: "Android", owner: ME }],
    });
    await openMenu();
    const text = shownText($("wellnessSection"));
    assert.equal($("wellnessStateChip").textContent, "Not set up");
    assert.match(text, /Use for health/);
    assert.match(text, /BlackBox, Settings, Health/);
    assert.doesNotMatch(text, /iPhone/);
    await timers.advance(wellness.WELLNESS_POLL_MS);
    assert.equal(statusCalls().length, 2);
    assert.equal(meshCalls().length, 1, "the device lookup is not repeated on every tick while the menu stays open");
});

test("pauses while the page is hidden and catches up as soon as it is visible", async (t) => {
    const { timers } = await setup(t);
    await openMenu();
    assert.equal(statusCalls().length, 1);
    await timers.advance(wellness.WELLNESS_POLL_MS);
    assert.equal(statusCalls().length, 2, "polls while visible");
    setHidden(true);
    await timers.advance(wellness.WELLNESS_POLL_MS * 6);
    assert.equal(statusCalls().length, 2, "nothing while hidden");
    setHidden(false);
    await flush();
    assert.equal(statusCalls().length, 3, "one at once on return");
});

test("stops when the System Menu closes and asks again when it reopens", async (t) => {
    const { timers } = await setup(t);
    await openMenu();
    await closeMenu();
    await timers.advance(wellness.WELLNESS_POLL_MS * 4);
    assert.equal(statusCalls().length, 1, "a closed menu polls nothing");
    await openMenu();
    assert.equal(statusCalls().length, 2);
});

test("an operator switch asks about the new operator only and drops the old one's status", async (t) => {
    await setup(t);
    await openMenu();
    assert.equal($("wellnessSource").textContent, "Fixture Phone (SM-TEST1)");
    box.status = { state: null, source_label: null, last_sync: null, enrolled: false };
    state.setOperator(PEER);
    window.dispatchEvent(new CustomEvent("bbx:operator-changed", { detail: { operator: PEER } }));
    await flush();
    assert.deepEqual(statusCalls(), [`/wellness/status?operator=${ME}`, `/wellness/status?operator=${PEER}`]);
    assert.equal($("wellnessStateChip").textContent, "Not set up");
    assert.equal($("wellnessSourceRow").hidden, true);
    assert.ok(!$("wellnessSection").textContent.includes("Fixture Phone"), "the previous operator's source is gone");
});

test("a reply for an operator who is no longer selected is not shown", async (t) => {
    let release;
    const parked = globalThis.fetch;
    globalThis.fetch = window.fetch = (url, opts) => {
        if (String(url) === `/wellness/status?operator=${ME}`) {
            calls.push({ url: String(url), method: "GET" });
            return new Promise((r) => { release = () => r({ ok: true, status: 200, json: () => Promise.resolve(ACTIVE) }); });
        }
        return parked(url, opts);
    };
    t.after(() => { globalThis.fetch = window.fetch = parked; });
    await setup(t, { status: { state: "paused", source_label: "Peer Phone", last_sync: null, enrolled: true } });
    await openMenu();
    state.setOperator(PEER);   // switched while the first request is still out
    release();
    await flush();
    assert.deepEqual(statusCalls(), [`/wellness/status?operator=${ME}`, `/wellness/status?operator=${PEER}`],
        "the stale answer is dropped and the current operator is asked at once");
    assert.equal($("wellnessStateChip").textContent, "Paused");
    assert.equal($("wellnessSource").textContent, "Peer Phone");
});

test("no operator selected: asks nothing and says to select one", async (t) => {
    await setup(t, { operator: "" });
    await openMenu();
    assert.deepEqual(statusCalls(), []);
    assert.match(shownText($("wellnessSection")), new RegExp(state.OPERATOR_REQUIRED_MESSAGE));
});

test("a box without the Health routes (404) says Health is not available here", async (t) => {
    await setup(t, { status: { detail: "Not Found" }, statusCode: 404 });
    await openMenu();
    assert.equal($("wellnessStateChip").textContent, "Unavailable");
    assert.match(shownText($("wellnessSection")), /not available on this BlackBox/);
});

test("a refusal for this operator (4xx) is an answer, not an outage", async (t) => {
    const { timers } = await setup(t, { status: { error: "SENTINEL-REFUSAL-CODE" }, statusCode: 422 });
    await openMenu();
    assert.equal($("wellnessStateChip").textContent, "Unavailable");
    assert.match(shownText($("wellnessSection")), /not available for this operator/);
    assert.equal($("wellnessError").hidden, true, "not 'can't reach the BlackBox'");
    assert.ok(!$("wellnessSection").textContent.includes("SENTINEL"), "the box's own wording is not shown");
    await timers.advance(wellness.WELLNESS_POLL_MS);
    assert.equal(statusCalls().length, 2, "no backoff: the next ask is one interval away");
});

test("an unreachable box keeps the last status, says so, and clears the note on recovery", async (t) => {
    const { timers } = await setup(t);
    await openMenu();
    box.statusCode = 0;
    await timers.advance(wellness.WELLNESS_POLL_MS);
    assert.equal($("wellnessError").hidden, false);
    assert.equal($("wellnessStateChip").textContent, "Active", "the last answer stays up");
    box.statusCode = 200;
    await timers.advance(wellness.WELLNESS_POLL_MS * 2);   // the poller backs off after a failure
    assert.equal($("wellnessError").hidden, true);
});

test("a Devices pick (bbx:wellness-changed) refreshes the card at once", async (t) => {
    await setup(t, { status: { state: null, source_label: null, last_sync: null, enrolled: false }, mesh: [{ id: "p", type: "android", owner: ME }] });
    await openMenu();
    box.status = { state: "awaiting_phone", source_label: "Fixture Phone (SM-TEST1)", last_sync: null, enrolled: false };
    window.dispatchEvent(new CustomEvent("bbx:wellness-changed"));
    await flush();
    assert.equal(statusCalls().length, 2);
    assert.equal($("wellnessStateChip").textContent, "Finishing setup");
});

test("Open Health settings appears only inside the app, and opens the native screen", async (t) => {
    await setup(t);
    await openMenu();
    assert.equal($("btnWellnessOpenPhone").hidden, true, "a desktop browser has no native screen to open");
    let opened = 0;
    window.AndroidBridge = { wellnessOpenSettings() { opened += 1; } };
    window.dispatchEvent(new CustomEvent("bbx:wellness-changed"));
    await flush();
    assert.equal($("btnWellnessOpenPhone").hidden, false);
    $("btnWellnessOpenPhone").click();
    assert.equal(opened, 1);
});

test("formatLastSync: relative while recent, a date after two days, null when unreadable", () => {
    const at = (iso) => wellness.formatLastSync(iso, NOW);
    assert.equal(at(null), null);
    assert.equal(at(""), null);
    assert.equal(at("not a time"), null);
    assert.equal(at(XSS), null, "V8's lenient Date.parse reads a date out of this; the card must not");
    assert.equal(at("1"), null);
    assert.equal(at("2026-10-29T11:47:30.123456+00:00"), "12 min ago", "Python isoformat() with microseconds and offset");
    assert.equal(at("2026-10-29 11:48:00+00:00"), "12 min ago");
    assert.equal(at("2026-10-29T11:59:30Z"), "just now");
    assert.equal(at("2026-10-29T12:05:00Z"), "just now", "a phone clock ahead of the box is not 'in 5 min'");
    assert.equal(at("2026-10-29T11:48:00Z"), "12 min ago");
    assert.equal(at("2026-10-29T09:00:00Z"), "3 h ago");
    assert.equal(at("2026-10-27T13:00:00Z"), "47 h ago");
    assert.doesNotMatch(at("2026-10-20T07:10:00Z"), /ago/);
});

test("wellness-section.js never hands server text to the HTML parser", () => {
    const code = stripComments(readFileSync(resolve(PORTAL, "modules", "wellness-section.js"), "utf8"));
    const unsafe = innerHtmlAssignments(code).filter((a) => !isSafeRhs(a.rhs));
    assert.deepEqual(unsafe, []);
    assert.deepEqual(rawHtmlApiUses(code), []);
});
