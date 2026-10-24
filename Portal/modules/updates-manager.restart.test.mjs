// Restart detection and confirm wording for the Updates panel (audit rank 43,
// A13-ui-ops-01 / O16-01).
//
// The restart fires 2 s after the update's complete event, so the first
// /health 200 used to come from the OLD process and the panel said "back
// online after 0s" before a 60-90 s outage. The panel now waits for
// /health's boot_id to change. These helpers are pure (fake fetch, fake
// clock): no DOM, no network.
// Run: node --test Portal/modules/updates-manager.restart.test.mjs
import test from "node:test";
import assert from "node:assert/strict";

import {
    restartObserved, restartExpected, waitForRestart, readBootId, taskVerdict, readTaskState,
    installConfirmText, rollbackConfirmText, RESTART_CONFIRM_TEXT, RESTART_GRACE_MS,
} from "./updates-manager.js";

// A scripted /health: each entry is a boot_id string, "" (field absent) or
// null (down). The clock advances by the poll interval on every sleep.
function scripted(probes) {
    let t = 0;
    const seen = [];
    return {
        seen,
        now: () => t,
        sleep: async (ms) => { t += ms; },
        readBoot: async () => {
            const v = probes.length ? probes.shift() : probes.last;
            seen.push(v);
            return v;
        },
    };
}

test("the old process answering is not a restart", () => {
    assert.equal(restartObserved({ priorBootId: "A", bootId: "A", seenDown: true, elapsedMs: 60_000 }), false);
    assert.equal(restartObserved({ priorBootId: "A", bootId: "B", seenDown: false, elapsedMs: 0 }), true);
});

test("a build without boot_id answering after one with it is a restart (rollback to old code)", () => {
    assert.equal(restartObserved({ priorBootId: "A", bootId: "", seenDown: false, elapsedMs: 0 }), true);
});

test("an old backend without boot_id falls back to seen-down or the grace period", () => {
    const base = { priorBootId: "", bootId: "", seenDown: false };
    assert.equal(restartObserved({ ...base, elapsedMs: 0 }), false);
    assert.equal(restartObserved({ ...base, elapsedMs: 0, seenDown: true }), true);
    assert.equal(restartObserved({ ...base, elapsedMs: RESTART_GRACE_MS }), true);
    // ...and the first answer from a build that has it is the new process.
    assert.equal(restartObserved({ ...base, bootId: "B", elapsedMs: 0 }), true);
});

test("an unreadable prior id never trusts the first answer", () => {
    assert.equal(restartObserved({ priorBootId: null, bootId: "B", seenDown: false, elapsedMs: 0 }), false);
    assert.equal(restartObserved({ priorBootId: null, bootId: "B", seenDown: true, elapsedMs: 0 }), true);
});

test("waitForRestart rides out the old process, the outage and the warm-up", async () => {
    const s = scripted(["A", "A", null, null, null, "B"]);
    const res = await waitForRestart({ priorBootId: "A", ...s, timeoutMs: 180_000, intervalMs: 2000 });
    assert.equal(res.restarted, true);
    assert.equal(s.seen.length, 6);
    assert.equal(res.elapsedMs, 10_000);
});

test("waitForRestart reports a restart that never happened, not 'still down'", async () => {
    const probes = [];
    probes.last = "A";
    const s = scripted(probes);
    const res = await waitForRestart({ priorBootId: "A", ...s, timeoutMs: 20_000, intervalMs: 2000 });
    assert.equal(res.restarted, false);
    assert.equal(res.lastUp, true, "the old process was still answering");
});

// ── a dropped log stream (A13-ui-ops-06) ────────────────────────────────

// A clock-driven box: /health answers `boot(t)` and /update/state `task(t)`.
function timeline({ boot, task }) {
    let t = 0;
    return {
        now: () => t,
        sleep: async (ms) => { t += ms; },
        readBoot: async () => boot(t),
        taskState: async () => task(t),
    };
}

test("taskVerdict: running under the lock, restarting once its task finished, else ended", () => {
    assert.equal(taskVerdict({ in_progress: true, state: null }, "update-1"), "running");
    for (const phase of ["restart_pending", "complete", "rolled_back"]) {
        assert.equal(taskVerdict({ in_progress: false, state: { task_id: "update-1", phase } }, "update-1"), "restarting");
    }
    assert.equal(taskVerdict({ in_progress: false, state: { task_id: "update-1", phase: "failed" } }, "update-1"), "ended");
    // An older update's "complete" says nothing about this one (refused, or up to date).
    assert.equal(taskVerdict({ in_progress: false, state: { task_id: "update-0", phase: "complete" } }, "update-1"), "ended");
    assert.equal(taskVerdict({ in_progress: false, state: null }, "update-1"), "ended");
});

test("readTaskState asks /update/state, and null when it cannot", async () => {
    const urls = [];
    const ok = async (url) => { urls.push(url); return { ok: true, json: async () => ({ in_progress: true }) }; };
    assert.equal(await readTaskState("update-1", ok), "running");
    assert.deepEqual(urls, ["/update/state"]);
    assert.equal(await readTaskState("update-1", async () => ({ ok: false })), null);
    assert.equal(await readTaskState("update-1", async () => { throw new TypeError("down"); }), null);
});

test("the review regression: the lock is free ~2 s before the restart, keep waiting", async () => {
    // The update completes at 20 s and releases the lock; the restart fires at
    // 22 s; the new process answers at 80 s. Giving up on !in_progress alone
    // ended the wait at 20 s with "the service did not restart".
    const s = timeline({
        boot: (t) => (t < 22_000 ? "OLD" : t < 80_000 ? null : "NEW"),
        task: (t) => (t < 20_000 ? "running" : "restarting"),
    });
    const res = await waitForRestart({ priorBootId: "OLD", ...s, intervalMs: 2000 });
    assert.equal(res.restarted, true, JSON.stringify(res));
    assert.equal(res.elapsedMs, 80_000);
});

test("waitForRestart gives up once the task ended without a restart", async () => {
    let asked = 0;
    const s = timeline({ boot: () => "A", task: (t) => { asked += 1; return t >= 12_000 ? "ended" : "running"; } });
    const res = await waitForRestart({ priorBootId: "A", ...s, timeoutMs: 180_000, intervalMs: 2000 });
    assert.equal(res.gaveUp, true);
    assert.equal(res.restarted, false);
    assert.ok(asked >= 1 && res.elapsedMs === 12_000);
});

test("a long pip phase keeps the wait going past the timeout", async () => {
    // pip alone may run 10 minutes; "restart it yourself" at 180 s would
    // interrupt a running update.
    const s = timeline({
        boot: (t) => (t < 602_000 ? "A" : t < 650_000 ? null : "B"),
        task: (t) => (t < 600_000 ? "running" : "restarting"),
    });
    const res = await waitForRestart({ priorBootId: "A", ...s, timeoutMs: 180_000, intervalMs: 2000 });
    assert.equal(res.restarted, true, JSON.stringify(res));
    assert.equal(res.elapsedMs, 650_000);
});

test("onTick hears the task's verdict, so the modal can say the update still runs", async () => {
    const s = timeline({
        boot: (t) => (t < 8_000 ? "A" : "B"),
        task: (t) => (t < 4_000 ? "running" : "restarting"),
    });
    const ticks = [];
    await waitForRestart({ priorBootId: "A", ...s, intervalMs: 2000,
                           onTick: (elapsed, verdict) => ticks.push([elapsed, verdict]) });
    assert.deepEqual(ticks, [[0, null], [2000, "running"], [4000, "running"], [6000, "restarting"], [8000, "restarting"]]);
});

test("an update that never finishes is reported as still running, at the cap", async () => {
    const s = timeline({ boot: () => "A", task: () => "running" });
    const res = await waitForRestart({ priorBootId: "A", ...s, timeoutMs: 20_000, maxMs: 60_000, intervalMs: 2000 });
    assert.equal(res.restarted, false);
    assert.equal(res.stillRunning, true);
    assert.ok(res.elapsedMs > 60_000 && res.elapsedMs <= 62_000, String(res.elapsedMs));
});

test("without a task probe the timeout is not extended", async () => {
    const s = timeline({ boot: () => "A", task: () => "running" });
    const res = await waitForRestart({ priorBootId: "A", now: s.now, sleep: s.sleep, readBoot: s.readBoot,
                                       timeoutMs: 20_000, intervalMs: 2000 });
    assert.equal(res.restarted, false);
    assert.equal(res.stillRunning, undefined);
    assert.equal(res.lastUp, true);
});

test("readBootId: the id, '' from an older backend, null when down", async () => {
    const ok = (body) => async () => ({ ok: true, json: async () => body });
    assert.equal(await readBootId(ok({ boot_id: "A" })), "A");
    assert.equal(await readBootId(ok({ status: "ok" })), "");
    assert.equal(await readBootId(async () => ({ ok: false, json: async () => ({}) })), null);
    assert.equal(await readBootId(async () => { throw new TypeError("Failed to fetch"); }), null);
});

test("restartExpected follows the flag, and old backends' equal-SHA no-op", () => {
    assert.equal(restartExpected({ succeeded: true, restart_scheduled: false }), false);
    assert.equal(restartExpected({ succeeded: true, restart_scheduled: true }), true);
    assert.equal(restartExpected({ succeeded: false, refused: "dirty_tree", restart_scheduled: false }), false);
    const sha = "a".repeat(40);
    assert.equal(restartExpected({ succeeded: true, sha_before: sha, sha_after: sha }), false);
    assert.equal(restartExpected({ succeeded: true, sha_before: "aaaaaaa", sha_after: "bbbbbbb" }), true);
});

test("the Install confirm names the files it will stash", () => {
    const clean = installConfirmText({ commits_behind: 3, active_cli_sessions: 0, dirty_files: [] });
    assert.match(clean, /Install 3 update/);
    assert.doesNotMatch(clean, /local changes/);
    const dirty = installConfirmText({ commits_behind: 1, active_cli_sessions: 2, dirty_files: ["app.py", "a b.txt"] });
    assert.match(dirty, /2 active CLI agent session/);
    assert.match(dirty, /2 tracked file\(s\).*app\.py, a b\.txt/s);
    assert.match(dirty, /stashes them/);
});

test("the Rollback and Restart confirms say what they do", () => {
    assert.match(rollbackConfirmText({ dirty_files: [] }), /Roll back to the pre-update tag/);
    assert.match(rollbackConfirmText({ dirty_files: ["x.py"] }), /x\.py.*stashes them/s);
    assert.match(rollbackConfirmText(null), /Roll back/);
    assert.match(RESTART_CONFIRM_TEXT, /Restart the BlackBox service/);
});
