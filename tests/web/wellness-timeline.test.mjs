// Show numbers on a turn reloaded from the ledger (M7 wave 3b, contract (a);
// spec §6.1 "Chat").
//
// Inside the BlackBox Android app the Timeline popup (and a SNAP-id link)
// reads GET /fossil/snapshot/{id}?operator=X signed by the phone (the app
// signs a GET only when it names the Portal's operator). The box answers the
// owner's enrolled phone with the turn as minted: masked ("[·]") and its
// WELLNESS gauge intact ("... · ref w-YYYYMMDD-xxxx · values not recorded"),
// so the popup offers "Show numbers" (the chat's private panel, closed with
// the popup, never kept). Another operator's turn, a turn the gate hides and
// an unsigned read stay "[wellness conversation · not shared]": no offer. A
// proof the box refuses (401, 409, 410) is read again once, unsigned, so the
// popup never shows less than a desktop browser; a desktop's read is exactly
// today's request. A signed read is never kept on the list item.
//
// The fixture numbers are the synthetic wellness-fixture operator's.
//
// Run: bash scripts/run_web_tests.sh
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { installDom, loadPurify, loadMarked, scriptVectors, PORTAL } from "./helpers/portal-dom.mjs";
import { fakeChannel, installChannel } from "./helpers/wellness-channel.mjs";
import { stripComments, rawHtmlApiUses } from "./helpers/sink-scan.mjs";

installDom();
loadPurify();
loadMarked();
localStorage.setItem("bbx_operator", "wellness-fixture");
document.body.insertAdjacentHTML("beforeend", `
    <div id="timelineModal" class="modal hide"><div id="timelineList"></div></div>
    <div id="snapshotPopup" class="snapshot-popup hide"><div class="snapshot-popup-content">
      <button id="btnCloseSnapshotPopup"></button><div id="snapshotPopupBody"></div></div></div>`);

const tl = await import("../../Portal/modules/timeline-browser.js");
const state = await import("../../Portal/modules/state-management.js");
const { closeWellnessNumbers } = await import("../../Portal/modules/chat-bubbles.js");
tl.setupTimelineBrowser();

const ME = "wellness-fixture";
const SNAP = "SNAP-20261029-0042";
const REF = "w-20261029-7f3a";
const HEADER = "X-BlackBox-Origin";
const EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
const XSS = `<img src=x onerror="window.__pwned=1">`;
const PLACEHOLDER = "[wellness conversation · not shared]";

/** A minted snapshot (Orchestrator/monitoring.render_snapshot_body_v71) with `gauge` and `log`. */
function snapshot({ gauge, log }) {
    return `=== START SNAPSHOT — UTC 2026-10-29T11:20:00Z — ${SNAP} (7.1.0) ===\n`
        + "GAUGES\nCONTINUITY: auto\nDRIFT: green\n"
        + (gauge ? `WELLNESS: ${gauge}\n` : "")
        + `OPERATOR: ${ME}\nMODE: Normal\n\nSNAPSHOT BODY\n\nRaw Session Log\n${log}\n`
        + "Release Notes\n- Hybrid context window in effect; gauges record drift and token usage.\n\n"
        + `=== END SNAPSHOT — ${SNAP} — UTC 2026-10-29T11:20:00Z ===\n`;
}
// What the box gives the owner's enrolled phone (masked, the gauge intact)...
const MASKED = snapshot({
    gauge: `sleep,heart · night of 2026-10-28 · ref ${REF} · values not recorded`,
    log: `- [1] 2026-10-29T11:19:00Z operator=${ME} user: how did I sleep? ${XSS}\n`
        + `- [2] 2026-10-29T11:19:30Z operator=${ME} assistant: Your sleeping heart rate was [·] bpm.`,
});
// ...and every other reader (redact.hide).
const HIDDEN = snapshot({
    gauge: PLACEHOLDER,
    log: `- [1] 2026-10-29T11:19:00Z operator=${ME} user: ${PLACEHOLDER}\n`
        + `- [2] 2026-10-29T11:19:30Z operator=${ME} assistant: ${PLACEHOLDER}`,
});

const SUMMARY = {
    ref: REF, ts: 1761736200000, tool: "wellness_summary", categories: ["heart", "sleep"],
    payload: {
        ok: true, tool: "wellness_summary", ref: REF, as_of: "2026-10-29T07:10-04:00", stale: false,
        period: { label: "last night" },
        metrics: [{ metric: "sleeping_hr_median", label: "Sleeping heart rate", value: 47.3, unit: "bpm", display: "47 bpm" }],
        gaps: [],
    },
};

const json = (body, status = 200) => Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });

/**
 * The scripted box: a signed read gets `signed` ({code, content}), an unsigned
 * one `unsigned`; /wellness/ref/{ref} answers SUMMARY. Records every call as
 * {url, args} (args: what fetch got after the URL).
 */
function box({ signed = { code: 200, content: MASKED }, unsigned = { code: 200, content: HIDDEN } } = {}) {
    const calls = [];
    globalThis.fetch = window.fetch = (url, ...args) => {
        const u = String(url);
        calls.push({ url: u, args });
        const opts = args[0] || {};
        if (u.startsWith("/fossil/snapshot/")) {
            const answer = opts.headers && opts.headers[HEADER] ? signed : unsigned;
            if (answer.code === 404) return json({ detail: "not found" }, 404);
            if (answer.code !== 200) return json({ error: "bad_signature" }, answer.code);
            return json({ snap_id: SNAP, content: answer.content, metadata: { operator: ME, timestamp: "2026-10-29T11:20:00Z", type: "normal" } });
        }
        if (u.startsWith("/wellness/ref/")) return json(SUMMARY);
        return Promise.reject(new TypeError("offline"));
    };
    return calls;
}

const popup = () => document.getElementById("snapshotPopup");
const body = () => document.getElementById("snapshotPopupBody");
const snapCalls = (calls) => calls.filter((c) => c.url.startsWith("/fossil/snapshot/"));
const settle = (ms = 20) => new Promise((r) => setTimeout(r, ms));

async function waitFor(pred, what, timeoutMs = 3000) {
    const until = Date.now() + timeoutMs;
    while (!pred()) {
        if (Date.now() > until) throw new Error(`timed out waiting for ${what}`);
        await settle(5);
    }
}

function reset(t) {
    installChannel(null);
    popup().classList.add("hide");
    body().replaceChildren();
    state.setOperator(ME);
    t.after(() => {
        closeWellnessNumbers();
        installChannel(null);
        state.setOperator(ME);
    });
}

const open = () => tl.openSnapshotById(SNAP);

// ── The read ────────────────────────────────────────────────────────────────

test("inside the app the snapshot is read signed: GET with ?operator=, the proof, no body", async (t) => {
    reset(t);
    const channel = fakeChannel();
    installChannel(channel);
    const calls = box();
    assert.equal(await open(), true);
    const [read] = snapCalls(calls);
    assert.equal(snapCalls(calls).length, 1);
    assert.equal(read.url, `/fossil/snapshot/${SNAP}?operator=${ME}`);
    assert.match(read.args[0].headers[HEADER], /^v1;enr=en_0000abcd;/);
    assert.equal(read.args[0].cache, "no-store");
    assert.equal(read.args[0].body, undefined);
    assert.deepEqual(channel.signed, [{ method: "GET", path: `/fossil/snapshot/${SNAP}?operator=${ME}`, hex: EMPTY_SHA256 }]);
    assert.equal(popup().classList.contains("hide"), false);
});

test("a desktop browser reads exactly as before: the bare path, no options", async (t) => {
    reset(t);
    const calls = box();
    await open();
    assert.deepEqual(snapCalls(calls).map((c) => [c.url, c.args.length]), [[`/fossil/snapshot/${SNAP}`, 0]]);
    assert.ok(body().textContent.includes(PLACEHOLDER));
    assert.equal(body().querySelector(".wellness-show-numbers"), null);
});

test("a null from the app reads unsigned at once", async (t) => {
    reset(t);
    installChannel(fakeChannel({ sign: () => null }));
    const calls = box();
    await open();
    assert.deepEqual(snapCalls(calls).map((c) => [c.url, c.args.length]), [[`/fossil/snapshot/${SNAP}`, 0]]);
    assert.equal(body().querySelector(".wellness-show-numbers"), null);
});

for (const code of [401, 409, 410]) {
    test(`a signed read the box refuses (${code}) is read again once, unsigned: never less than a desktop`, async (t) => {
        reset(t);
        installChannel(fakeChannel());
        const calls = box({ signed: { code } });
        assert.equal(await open(), true);
        const reads = snapCalls(calls);
        assert.equal(reads.length, 2);
        assert.ok(reads[0].args[0].headers[HEADER]);
        assert.deepEqual([reads[1].url, reads[1].args.length], [`/fossil/snapshot/${SNAP}`, 0], "today's request");
        assert.ok(body().textContent.includes(PLACEHOLDER));
        assert.equal(body().querySelector(".wellness-show-numbers"), null);
    });
}

test("a 404 on the signed read is not found, not retried", async (t) => {
    reset(t);
    installChannel(fakeChannel());
    const calls = box({ signed: { code: 404 } });
    assert.equal(await open(), false);
    assert.equal(snapCalls(calls).length, 1);
});

// ── Show numbers in the popup ───────────────────────────────────────────────

test("the owner's masked snapshot offers Show numbers; the numbers come from a signed GET /wellness/ref", async (t) => {
    reset(t);
    const channel = fakeChannel();
    installChannel(channel);
    const calls = box();
    await open();
    const button = body().querySelector("button.wellness-show-numbers");
    assert.ok(button, "Show numbers");
    assert.ok(body().textContent.includes("[·]"), "the masked text");
    assert.ok(body().textContent.includes(`ref ${REF}`), "its gauge intact");
    button.click();
    await waitFor(() => body().querySelector(".wellness-numbers") && !body().querySelector(".wellness-numbers").textContent.includes("Loading"),
        "the numbers");
    const ref = calls.find((c) => c.url.startsWith("/wellness/ref/"));
    assert.equal(ref.url, `/wellness/ref/${REF}?operator=${ME}`);
    assert.ok(ref.args[0].headers[HEADER]);
    assert.match(body().querySelector(".wellness-numbers").textContent, /Sleeping heart rate: 47 bpm/);
    // Closing the popup closes the numbers; nothing was kept.
    document.getElementById("btnCloseSnapshotPopup").click();
    assert.equal(popup().classList.contains("hide"), true);
    assert.equal(body().querySelector(".wellness-numbers"), null);
    assert.ok(!JSON.stringify({ ...localStorage }).includes("47 bpm"));
});

test("a click outside the popup closes the numbers too", async (t) => {
    reset(t);
    installChannel(fakeChannel());
    box();
    await open();
    body().querySelector("button.wellness-show-numbers").click();
    await waitFor(() => body().querySelector(".wellness-numbers"), "the panel");
    popup().dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    assert.equal(popup().classList.contains("hide"), true);
    assert.equal(body().querySelector(".wellness-numbers"), null);
});

test("a hidden snapshot (another operator's, or the gate's) offers nothing", async (t) => {
    reset(t);
    installChannel(fakeChannel());
    box({ signed: { code: 200, content: HIDDEN } });
    await open();
    assert.ok(body().textContent.includes(PLACEHOLDER));
    assert.equal(body().querySelector(".wellness-show-numbers"), null);
    assert.equal(body().querySelector(".wellness-footer"), null);
});

test("masked text without the app's channel offers nothing (Show numbers needs the signer)", async (t) => {
    reset(t);
    box({ unsigned: { code: 200, content: MASKED } });
    await open();
    assert.ok(body().textContent.includes("[·]"));
    assert.equal(body().querySelector(".wellness-show-numbers"), null);
});

test("the popup stays text: the snapshot's strings never reach the HTML parser", async (t) => {
    reset(t);
    installChannel(fakeChannel());
    box();
    await open();
    assert.deepEqual(scriptVectors(body()), [], body().innerHTML);
    assert.equal(body().querySelector("img"), null);
    assert.ok(body().textContent.includes(XSS));
});

test("a signed read is never kept on the list item: opening it again reads again", async (t) => {
    reset(t);
    installChannel(fakeChannel());
    const calls = box();
    const item = { snapId: SNAP, operator: ME, timestamp: "2026-10-29T11:20:00Z", type: "normal", preview: "", content: null };
    await tl.showTimelineDetail(item);
    assert.equal(item.content, null, "the owner's masked text is not cached");
    await tl.showTimelineDetail(item);
    assert.equal(snapCalls(calls).length, 2);
    // An unsigned read is kept, as before.
    installChannel(null);
    const desk = { ...item, content: null };
    await tl.showTimelineDetail(desk);
    assert.equal(desk.content, HIDDEN);
});

test("an operator switch closes a popup that shows a signed read, and empties it", async (t) => {
    reset(t);
    installChannel(fakeChannel());
    box();
    await open();
    body().querySelector("button.wellness-show-numbers").click();
    await waitFor(() => body().querySelector(".wellness-numbers"), "the panel");
    state.setOperator("wellness-fixture-peer");
    window.dispatchEvent(new CustomEvent("bbx:operator-changed", { detail: { operator: "wellness-fixture-peer" } }));
    assert.equal(popup().classList.contains("hide"), true);
    assert.equal(body().textContent, "", "the owner's text is gone from the page");
});

test("an operator switch leaves a desktop's popup alone", async (t) => {
    reset(t);
    box();
    await open();
    state.setOperator("wellness-fixture-peer");
    window.dispatchEvent(new CustomEvent("bbx:operator-changed", { detail: { operator: "wellness-fixture-peer" } }));
    assert.equal(popup().classList.contains("hide"), false);
    assert.ok(body().textContent.includes(PLACEHOLDER));
});

test("opening another snapshot closes the numbers of the one before", async (t) => {
    reset(t);
    installChannel(fakeChannel());
    box();
    await open();
    body().querySelector("button.wellness-show-numbers").click();
    await waitFor(() => body().querySelector(".wellness-numbers"), "the panel");
    await open();
    assert.equal(body().querySelector(".wellness-numbers"), null);
    assert.equal(body().querySelector("button.wellness-show-numbers").textContent, "Show numbers");
});

test("timeline-browser.js writes the popup with escaped markup only", () => {
    const code = stripComments(readFileSync(resolve(PORTAL, "modules", "timeline-browser.js"), "utf8").replace(/\r\n/g, "\n"));
    assert.deepEqual(rawHtmlApiUses(code), []);
    // Every interpolation in its innerHTML templates is escapeHtml(...) or a number.
    const templates = [...code.matchAll(/innerHTML\s*=\s*`([\s\S]*?)`/g)].map((m) => m[1]);
    for (const tpl of templates) {
        for (const [, expr] of tpl.matchAll(/\$\{([^}]*)\}/g)) {
            assert.match(expr.trim(), /^(?:escapeHtml\(|\(snapshot\.content\.length)/, expr);
        }
    }
});
