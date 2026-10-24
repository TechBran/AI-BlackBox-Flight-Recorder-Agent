// The Timeline pages the snapshot index instead of downloading the ledger.
// Audit P04-media-tasks-fx-web-06 / rank 45: loadTimelineData fetched /assert
// (the whole 58 MB volume inside a JSON string), regexed it in the browser on
// every open, and every SNAP-id click in chat did it again; filterTimeline
// lowercased every snapshot body on every keystroke. Now the list comes from
// GET /api/snapshots/recent a page at a time (?before= pages back), a body is
// fetched from GET /fossil/snapshot/{id} only when opened, typing filters the
// loaded previews, and Enter runs the server-side hybrid search.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { installDom, PORTAL } from "./helpers/portal-dom.mjs";

installDom();
document.body.insertAdjacentHTML("beforeend", `
    <button id="openTimelineBtn"></button>
    <div id="timelineModal" class="modal hide">
      <button id="btnCloseTimeline"></button>
      <input type="text" id="timelineSearch" />
      <select id="timelineOperatorFilter"><option value="">All Operators</option></select>
      <div id="timelineList"></div>
    </div>
    <div id="snapshotPopup" class="hide"><button id="btnCloseSnapshotPopup"></button><div id="snapshotPopupBody"></div></div>`);

const settle = (ms = 30) => new Promise((r) => setTimeout(r, ms));

/** Fake box: N snapshots for two operators, newest = highest number. */
function fakeBox(total = 230) {
    const snaps = [];
    for (let i = total; i >= 1; i--) {
        snaps.push({
            snap_id: `SNAP-20261001-${i}`, operator: i % 2 ? "Ana" : "Ben",
            timestamp: "2026-10-01T00:00:00Z", type: "normal",
            snippet: i === 7 ? "the reranker froze the loop" : `turn ${i}`,
        });
    }
    const calls = [];
    const fetchImpl = async (url) => {
        const u = new URL(String(url), "http://box");
        calls.push(u.pathname + u.search);
        if (u.pathname === "/api/snapshots/recent") {
            const limit = Number(u.searchParams.get("limit") || 30);
            const op = u.searchParams.get("operator") || "";
            const before = u.searchParams.get("before") || "";
            const num = (id) => Number(id.split("-")[2]);
            let rows = snaps.filter((s) => !op || s.operator === op);
            if (before) rows = rows.filter((s) => num(s.snap_id) < num(before));
            const page = rows.slice(0, limit);
            return json({ results: page, count: page.length, has_more: rows.length > limit,
                          operators: ["Ana", "Ben"] });
        }
        if (u.pathname.startsWith("/fossil/snapshot/")) {
            const id = decodeURIComponent(u.pathname.split("/").pop());
            const s = snaps.find((x) => x.snap_id === id);
            if (!s) return json({ detail: "not found" }, 404);
            return json({ snap_id: id, content: `=== START ${id} ===\nbody of ${id} <b>x</b>`,
                          metadata: { operator: s.operator, timestamp: s.timestamp, type: "normal" } });
        }
        if (u.pathname === "/fossil/hybrid") {
            return json({ results: [{ ...snaps[snaps.length - 7], similarity: 0.5 }], count: 1 });
        }
        throw new Error(`unexpected fetch ${u.pathname}`);
    };
    return { calls, fetchImpl };
}

function json(body, status = 200) {
    return Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(body) });
}

const items = () => [...document.querySelectorAll("#timelineList .timeline-item")];

test("opening the Timeline lists one index page and never fetches the ledger", async () => {
    const box = fakeBox();
    globalThis.fetch = box.fetchImpl;
    const tl = await import("../../Portal/modules/timeline-browser.js");
    tl.setupTimelineBrowser();
    document.getElementById("openTimelineBtn").click();
    await settle();

    assert.deepEqual(box.calls, ["/api/snapshots/recent?limit=100"]);
    assert.equal(items().length, 100);
    assert.equal(items()[0].dataset.snapId, "SNAP-20261001-230");
    assert.equal(items()[0].querySelector(".timeline-item-preview").textContent, "turn 230");
    const ops = [...document.querySelectorAll("#timelineOperatorFilter option")].map((o) => o.value);
    assert.deepEqual(ops, ["", "Ana", "Ben"]);

    // Older pages load on demand, appended in order.
    document.querySelector("#timelineList .timeline-more").click();
    await settle();
    assert.equal(box.calls.at(-1), "/api/snapshots/recent?limit=100&before=SNAP-20261001-131");
    assert.equal(items().length, 200);
    document.querySelector("#timelineList .timeline-more").click();
    await settle();
    assert.equal(items().length, 230);
    assert.equal(document.querySelector("#timelineList .timeline-more"), null);
    assert.ok(!box.calls.some((c) => c.startsWith("/assert")));
});

test("a snapshot body is fetched once, when it is opened, and rendered as text", async () => {
    const box = fakeBox();
    globalThis.fetch = box.fetchImpl;
    const tl = await import("../../Portal/modules/timeline-browser.js");
    await tl.loadTimelineData({ operator: "" });
    tl.renderTimelineList();
    assert.equal(box.calls.filter((c) => c.startsWith("/fossil/snapshot/")).length, 0);

    items()[2].click();
    await settle();
    const body = document.getElementById("snapshotPopupBody");
    assert.equal(box.calls.at(-1), "/fossil/snapshot/SNAP-20261001-228");
    assert.ok(body.textContent.includes("body of SNAP-20261001-228 <b>x</b>"));
    assert.equal(body.querySelector("b"), null);
    assert.ok(!document.getElementById("snapshotPopup").classList.contains("hide"));

    items()[2].click();
    await settle();
    assert.equal(box.calls.filter((c) => c === "/fossil/snapshot/SNAP-20261001-228").length, 1);
});

test("typing filters the loaded previews without a request; Enter searches the box", async () => {
    const box = fakeBox();
    globalThis.fetch = box.fetchImpl;
    const tl = await import("../../Portal/modules/timeline-browser.js");
    await tl.loadTimelineData({ operator: "" });
    tl.renderTimelineList();
    const before = box.calls.length;
    const input = document.getElementById("timelineSearch");

    input.value = "turn 22";
    input.dispatchEvent(new window.Event("input"));
    input.dispatchEvent(new window.Event("input"));
    assert.equal(items().length, 100, "filtering waits for the typing pause");
    await settle(260);
    assert.deepEqual(items().map((i) => i.dataset.snapId).slice(0, 2),
                     ["SNAP-20261001-229", "SNAP-20261001-228"]);
    assert.equal(items().length, 10);  // turn 220..229; turn 22 is on a page not loaded yet
    assert.equal(box.calls.length, before, "no request per keystroke");

    input.value = "reranker";
    input.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter" }));
    await settle();
    assert.equal(box.calls.at(-1), "/fossil/hybrid?q=reranker&limit=50");
    assert.deepEqual(items().map((i) => i.dataset.snapId), ["SNAP-20261001-7"]);

    input.value = "";
    input.dispatchEvent(new window.Event("input"));
    await settle(260);
    assert.equal(box.calls.at(-1), "/api/snapshots/recent?limit=100");
    assert.equal(items().length, 100);
});

test("choosing an operator reloads that operator's pages from the server", async () => {
    const box = fakeBox();
    globalThis.fetch = box.fetchImpl;
    const tl = await import("../../Portal/modules/timeline-browser.js");
    document.getElementById("openTimelineBtn").click();
    await settle();
    const sel = document.getElementById("timelineOperatorFilter");
    sel.value = "Ben";
    sel.dispatchEvent(new window.Event("change"));
    await settle();
    assert.equal(box.calls.at(-1), "/api/snapshots/recent?limit=100&operator=Ben");
    assert.ok(items().every((i) => i.querySelector(".timeline-item-operator").textContent === "Ben"));
    sel.value = "";
    sel.dispatchEvent(new window.Event("change"));
    await settle();
});

test("a SNAP-id link opens that snapshot from the per-id route", async () => {
    const box = fakeBox();
    globalThis.fetch = box.fetchImpl;
    const tl = await import("../../Portal/modules/timeline-browser.js");
    box.calls.length = 0;

    assert.equal(await tl.openSnapshotById("snap-20261001-3"), true);
    assert.deepEqual(box.calls, ["/fossil/snapshot/SNAP-20261001-3"]);
    assert.ok(document.getElementById("snapshotPopupBody").textContent.includes("body of SNAP-20261001-3"));

    assert.equal(await tl.openSnapshotById("SNAP-19990101-1"), false);
    assert.equal(document.getElementById("toast").textContent, "Snapshot SNAP-19990101-1 not found in volume");
});

test("no Portal module downloads the whole ledger from /assert", () => {
    const dir = resolve(PORTAL, "modules");
    for (const name of readdirSync(dir).filter((f) => f.endsWith(".js"))) {
        const src = readFileSync(resolve(dir, name), "utf8");
        assert.ok(!/fetch\(\s*['"`]\/assert/.test(src), `${name} fetches /assert`);
    }
});
