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
    restartObserved, restartExpected, waitForRestart, readBootId,
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

test("waitForRestart can give up when the old process says nothing is running", async () => {
    const probes = [];
    probes.last = "A";
    const s = scripted(probes);
    let asked = 0;
    const res = await waitForRestart({
        priorBootId: "A", ...s, timeoutMs: 180_000, intervalMs: 2000, graceMs: 10_000,
        giveUp: async ({ elapsedMs }) => { asked += 1; return elapsedMs >= 12_000; },
    });
    assert.equal(res.gaveUp, true);
    assert.equal(res.restarted, false);
    assert.ok(asked >= 1 && res.elapsedMs >= 12_000 && res.elapsedMs < 20_000);
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
