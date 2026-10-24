// The Updates panel's destructive actions (audit rank 43: O16-01, A13-ui-ops-01).
//
//  * Install, Rollback and Restart Service ask first; declining sends nothing.
//  * Local changes to tracked files are named in the confirm, and agreeing
//    sends discard_local_changes (the backend stashes them); a 409 that lists
//    dirty_files asks again and retries with consent.
//  * After the action the modal waits for /health's boot_id to CHANGE: the old
//    process answers for ~2 s after the complete event, and its 200 used to be
//    reported as "back online after 0s".
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom } from "./helpers/portal-dom.mjs";

installDom();

const settle = (ms = 30) => new Promise((r) => setTimeout(r, ms));
const reply = (status, body) => Promise.resolve({
    ok: status >= 200 && status < 300, status,
    json: () => Promise.resolve(body), text: () => Promise.resolve(JSON.stringify(body)),
});

// ── fakes: confirm/alert, EventSource, fetch ─────────────────────────────

let answers = [];
const asked = [];
window.confirm = (text) => { asked.push(text); return answers.length ? answers.shift() : false; };
const alerts = [];
window.alert = (text) => { alerts.push(text); };

const streams = [];
globalThis.EventSource = window.EventSource = class {
    constructor(url) { this.url = url; this.closed = false; streams.push(this); }
    close() { this.closed = true; }
    emit(event) { this.onmessage({ data: JSON.stringify(event) }); }
};

const STATUS = {
    git_initialized: true, commits_behind: 1, commits_ahead: 0,
    current_sha: "a".repeat(40), current_short: "aaaaaaa",
    latest_sha: "b".repeat(40), latest_short: "bbbbbbb",
    commits: [{ short: "bbbbbbb", subject: "fix: a thing" }], categories: {},
    in_progress: false, last_state: null, active_cli_sessions: 0, dirty_files: [],
};
let status = { ...STATUS };
let bootId = "boot-A";            // what /health answers; null = down
let updateState = { has_state: false, state: null, in_progress: false };  // GET /update/state
const gets = [];                   // GET urls, in order
const posts = [];                  // [url, parsed body]
let postReplies = {};              // url -> [reply factories]

globalThis.fetch = window.fetch = (url, opts = {}) => {
    const u = String(url);
    if (opts.method === "POST") {
        let body = null;
        try { body = opts.body ? JSON.parse(opts.body) : null; } catch (_) { body = opts.body; }
        posts.push([u, body]);
        const queue = postReplies[u] || [];
        if (queue.length) return queue.shift()();
        if (u === "/update/preflight") return reply(200, status);
        return reply(404, { detail: "not found" });
    }
    gets.push(u);
    if (u === "/update/status") return reply(200, status);
    if (u === "/update/state") return reply(200, updateState);
    if (u === "/health") {
        return bootId === null
            ? Promise.reject(new TypeError("Failed to fetch"))
            : reply(200, { status: "ok", boot_id: bootId });
    }
    return reply(404, {});
};

document.body.insertAdjacentHTML("beforeend", `
    <div id="updatesStatusRow"></div><div id="updatesCommitsList"></div>
    <button id="btnCheckUpdates"></button><button id="btnInstallUpdate"></button>
    <button id="btnViewUpdateLog"></button><button id="btnRollbackUpdate"></button>
    <button id="btnRestart"></button>`);

const { initUpdatesPanel } = await import("../../Portal/modules/updates-manager.js");

async function panel(withStatus) {
    status = { ...STATUS, ...withStatus };
    await initUpdatesPanel();
}

function reset() {
    answers = []; asked.length = 0; alerts.length = 0; posts.length = 0; gets.length = 0;
    streams.length = 0; postReplies = {}; bootId = "boot-A";
    updateState = { has_state: false, state: null, in_progress: false };
    const modal = document.getElementById("updatesLogModal");
    if (modal) modal.classList.add("hide");
}

const logText = () => document.querySelector("#updatesLogModal .updates-log-pre").textContent;

// ── Install ──────────────────────────────────────────────────────────────

test("Install asks first, and declining sends nothing", async () => {
    reset();
    await panel({});
    answers = [false];
    document.getElementById("btnInstallUpdate").click();
    await settle();
    assert.equal(asked.length, 1);
    assert.match(asked[0], /Install 1 update/);
    assert.deepEqual(posts.filter(([u]) => u === "/update/start"), []);
});

test("the panel and the confirm name dirty files, and agreeing sends consent", async () => {
    reset();
    await panel({ dirty_files: ["Orchestrator/config.py"] });
    assert.match(document.getElementById("updatesCommitsList").textContent, /Orchestrator\/config\.py/);
    answers = [true];
    postReplies["/update/start"] = [() => reply(200, { task_id: "update-1", status: "started" })];
    document.getElementById("btnInstallUpdate").click();
    await settle();
    assert.match(asked[0], /Orchestrator\/config\.py.*stashes them/s);
    const [, body] = posts.find(([u]) => u === "/update/start");
    assert.deepEqual(body, { confirm_sha: "b".repeat(40), discard_local_changes: true });
    assert.equal(streams.length, 1);
    assert.match(streams[0].url, /task_id=update-1/);
});

test("a 409 listing dirty files asks to stash and retries with consent", async () => {
    reset();
    await panel({});                     // the status did not know yet
    answers = [true, true];
    postReplies["/update/start"] = [
        () => reply(409, { detail: "This box has local changes...", refused: "dirty_tree", dirty_files: ["app.py"] }),
        () => reply(200, { task_id: "update-2", status: "started" }),
    ];
    document.getElementById("btnInstallUpdate").click();
    await settle();
    const starts = posts.filter(([u]) => u === "/update/start").map(([, b]) => b);
    assert.equal(starts.length, 2);
    assert.equal(starts[0].discard_local_changes, false);
    assert.equal(starts[1].discard_local_changes, true);
    assert.match(asked[1], /app\.py.*Stash them and continue/s);
    assert.match(streams[0].url, /task_id=update-2/);
});

test("after a successful update the modal waits for a NEW boot_id", async () => {
    reset();
    await panel({});
    answers = [true];
    postReplies["/update/start"] = [() => reply(200, { task_id: "update-3", status: "started" })];
    document.getElementById("btnInstallUpdate").click();
    await settle();
    streams[0].emit({ type: "complete", succeeded: true, sha_before: "aaaaaaa",
                      sha_after: "bbbbbbb", restart_scheduled: true });
    await settle(1700);                  // the modal switches 1.5 s after complete
    // The old process still answers /health: that is NOT the service coming back.
    assert.doesNotMatch(logText(), /back online/);
    bootId = null;                       // the restart takes it down...
    await settle(2100);
    bootId = "boot-B";                   // ...and a new process answers
    await settle(2100);
    assert.match(logText(), /back online/);
});

test("a refused update reports and never waits for a restart", async () => {
    reset();
    await panel({});
    answers = [true];
    postReplies["/update/start"] = [() => reply(200, { task_id: "update-4", status: "started" })];
    document.getElementById("btnInstallUpdate").click();
    await settle();
    streams[0].emit({ type: "complete", succeeded: false, refused: "dirty_tree",
                      dirty_files: ["app.py"], error: "This box has local changes to 1 tracked file (app.py).",
                      restart_scheduled: false });
    await settle(1700);
    assert.match(logText(), /FAILED: This box has local changes/);
    assert.doesNotMatch(logText(), /Restarting service/);
});

// ── Rollback ─────────────────────────────────────────────────────────────

test("Rollback asks first, then streams its task", async () => {
    reset();
    await panel({ commits_behind: 0, last_state: { phase: "complete", pre_update_tag: "pre-update-1" } });
    answers = [false];
    document.getElementById("btnRollbackUpdate").click();
    await settle();
    assert.match(asked[0], /Roll back to the pre-update tag/);
    assert.deepEqual(posts.filter(([u]) => u === "/update/rollback"), []);

    answers = [true];
    postReplies["/update/rollback"] = [() => reply(200, {
        task_id: "rollback-1", status: "started", reverted_to: "pre-update-1", restart_scheduled: true })];
    document.getElementById("btnRollbackUpdate").click();
    await settle();
    const [, body] = posts.find(([u]) => u === "/update/rollback");
    assert.deepEqual(body, { discard_local_changes: false });
    assert.match(streams[0].url, /task_id=rollback-1/);
    assert.match(document.querySelector("#updatesLogModal .updates-log-header").textContent, /Rolling back to pre-update-1/);
});

// ── Restart Service ──────────────────────────────────────────────────────

test("Restart Service asks first and then waits for the boot_id to change", async () => {
    reset();
    await panel({});
    answers = [false];
    document.getElementById("btnRestart").click();
    await settle();
    assert.match(asked[0], /Restart the BlackBox service/);
    assert.deepEqual(posts.filter(([u]) => u === "/restart"), []);

    answers = [true];
    postReplies["/restart"] = [() => Promise.reject(new TypeError("connection reset"))];
    document.getElementById("btnRestart").click();
    await settle(100);
    assert.equal(posts.filter(([u]) => u === "/restart").length, 1);
    assert.doesNotMatch(logText(), /back online/, "the old process answering is not a restart");
    bootId = "boot-C";
    await settle(2100);
    assert.match(logText(), /back online/);
});

test("Restart Service reports a refused restart instead of waiting", async () => {
    reset();
    await panel({});
    answers = [true];
    postReplies["/restart"] = [() => reply(200, { status: "error", message: "Restart failed: sudo: a password is required" })];
    document.getElementById("btnRestart").click();
    await settle();
    assert.match(alerts[0] || "", /password is required/);
});

// ── review follow-ups ────────────────────────────────────────────────────

async function installAndDropTheStream(taskId) {
    await panel({});
    answers = [true];
    postReplies["/update/start"] = [() => reply(200, { task_id: taskId, status: "started" })];
    document.getElementById("btnInstallUpdate").click();
    await settle();
    streams[0].onerror();                // Doze, a network blip: no complete event
}

test("a dropped stream keeps waiting through the restart its update scheduled", async () => {
    reset();
    // The update finished and released the lock; its restart is 2 s away and
    // the old process still answers. That is not "the service did not restart".
    updateState = { has_state: true, in_progress: false,
                    state: { task_id: "update-5", phase: "complete" } };
    await installAndDropTheStream("update-5");
    await settle(2300);                  // the poll modal opens 2 s after the drop
    assert.ok(gets.includes("/update/state"), gets.join(" "));
    assert.doesNotMatch(logText(), /did not restart|No restart/);
    bootId = "boot-B";
    await settle(2100);
    assert.match(logText(), /back online/);
});

test("a dropped stream stops waiting once its update ended without a restart", async () => {
    reset();
    updateState = { has_state: true, in_progress: false,
                    state: { task_id: "update-6", phase: "failed", error: "apt failed" } };
    await installAndDropTheStream("update-6");
    await settle(2300);
    assert.match(logText(), /did not restart/);
    assert.doesNotMatch(logText(), /back online/);
});

test("a rollback conflict refreshes from status, keeping the failed state's anchor", async () => {
    // POST /update/preflight deletes a failed state, and with it the tag a
    // rollback needs.
    reset();
    await panel({ commits_behind: 0, last_state: { phase: "failed", pre_update_tag: "pre-update-1" } });
    answers = [true];
    postReplies["/update/rollback"] = [() => reply(409, { detail: "An update or rollback is already in progress." })];
    document.getElementById("btnRollbackUpdate").click();
    await settle();
    assert.match(alerts[0] || "", /already in progress/);
    assert.deepEqual(posts.filter(([u]) => u === "/update/preflight"), []);
});

test("the onboarding hub's Install Now uses the panel's confirm and stash-and-retry", async () => {
    reset();
    status = { ...STATUS, commits_behind: 2, dirty_files: ["Orchestrator/config.py"] };
    const { mountUpdatesBadge } = await import("../../Portal/onboarding/hub-controls.js");
    const updates = await import("../../Portal/modules/updates-manager.js");
    const host = document.createElement("div");
    document.body.appendChild(host);
    await mountUpdatesBadge(host, { loadUpdates: async () => updates });
    answers = [true];
    postReplies["/update/start"] = [() => reply(200, { task_id: "update-7", status: "started" })];
    host.querySelector("[data-ob-update-install]").click();
    await settle();
    assert.match(asked[0], /Install 2 update.*Orchestrator\/config\.py.*stashes them/s);
    const [, body] = posts.find(([u]) => u === "/update/start");
    assert.deepEqual(body, { confirm_sha: "b".repeat(40), discard_local_changes: true });
    assert.match(streams[0].url, /task_id=update-7/);
});
