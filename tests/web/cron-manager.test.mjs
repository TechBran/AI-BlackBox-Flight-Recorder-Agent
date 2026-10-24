// The Portal cron editor must not change a job by opening and saving it
// (audit rank 53: P05-ops-panels-web-03, -04).
//
//  * A job whose operator is not on the /health roster (the reserved Flight
//    Recorder, a deleted operator) was silently re-homed to the first operator
//    in the list, because the browser fell back to the first <option> and save
//    always sent operator.
//  * "Every N minutes/hours" emitted */N for any N, which cron restarts every
//    hour/day: */45 fires at :00 and :45, not every 45 minutes. Only divisors
//    of the period can be picked now, and a stored */45 opens in Advanced.
//  * A schedule the Simple editor cannot rebuild exactly opens in Advanced, so
//    saving never rewrites it.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { installDom, loadPurify, loadMarked, PORTAL } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

const settle = () => new Promise((r) => setTimeout(r, 30));
const json = (b, status = 200) => Promise.resolve({
    ok: status >= 200 && status < 300, status,
    json: () => Promise.resolve(b), text: () => Promise.resolve(JSON.stringify(b)),
});

// The real cron modals from index.html, so the test drives the shipped markup.
const page = new DOMParser().parseFromString(readFileSync(resolve(PORTAL, "index.html"), "utf8"), "text/html");
for (const id of ["cronManagerModal", "cronEditModal", "cronHistoryModal"]) {
    const el = page.getElementById(id);
    assert.ok(el, `index.html has #${id}`);
    document.body.appendChild(document.importNode(el, true));
}
document.body.insertAdjacentHTML("beforeend", `<button id="btnCronManager"></button>`);

const state = await import("../../Portal/modules/state-management.js");
const cron = await import("../../Portal/modules/cron-manager.js");
cron.initCronManager();

const ROSTER = ["Ana", "Ben"];

/** Serve the panel with [jobs]; returns the recorded writes. */
function serve(jobs) {
    const writes = [];
    globalThis.fetch = (url, opts = {}) => {
        const u = String(url);
        if (opts.method === "PUT" || opts.method === "POST") {
            writes.push({ url: u, method: opts.method, body: JSON.parse(opts.body || "{}") });
            if (u.startsWith("/api/cron/preview")) return json({ next_runs: [] });
            return json({ job: jobs[0] });
        }
        if (u.startsWith("/api/cron/jobs")) return json({ jobs, count: jobs.length });
        if (u.startsWith("/health")) return json({ users: { list: ROSTER } });
        if (u.startsWith("/models/")) return json({ models: [], default_id: "" });
        if (u.startsWith("/api/cron/contacts")) return json({ contacts: [] });
        return json({});
    };
    return writes;
}

function job(over = {}) {
    return {
        id: "cron_1", name: "Report", prompt: "write the report", schedule: "0 9 * * *",
        frequency_hint: "Daily at 09:00 (local)", provider: "google", model: "",
        delivery: "snapshot", delivery_target: null, operator: "Ana", status: "active",
        one_shot: false, run_count: 0, ...over,
    };
}

async function openEditor(j) {
    const writes = serve([j]);
    state.setOperator("Ana");
    document.getElementById("btnCronManager").click();
    await settle();
    document.querySelector(`[data-action="edit"][data-job-id="${j.id}"]`).click();
    await settle();
    return writes;
}

async function save() {
    document.getElementById("btnSaveCronJob").click();
    await settle();
}

function closeAll() {
    document.getElementById("btnCloseCronManager").click();  // stops the 5 s poll
}

const advancedActive = () => !document.getElementById("cronAdvancedSchedule").classList.contains("hide");

test("an off-roster operator stays selected and a save does not re-home the job", async () => {
    const writes = await openEditor(job({ operator: "Flight Recorder" }));
    try {
        const sel = document.getElementById("cronOperator");
        assert.equal(sel.value, "Flight Recorder");
        assert.match(sel.selectedOptions[0].textContent, /not on roster/i);
        await save();
        const put = writes.find((w) => w.method === "PUT");
        assert.ok(put, "the edit is saved");
        assert.notEqual(put.body.operator, "Ana", "the job must not move to the first roster entry");
        assert.ok(!("operator" in put.body) || put.body.operator === "Flight Recorder");
    } finally {
        closeAll();
    }
});

test("changing the operator on purpose is still sent", async () => {
    const writes = await openEditor(job({ operator: "Ana" }));
    try {
        const sel = document.getElementById("cronOperator");
        sel.value = "Ben";
        await save();
        assert.equal(writes.find((w) => w.method === "PUT").body.operator, "Ben");
    } finally {
        closeAll();
    }
});

for (const schedule of ["0 9 * * 1-5", "30 8 * 6 *", "*/45 * * * *", "0 */5 * * *", "0 9 * * 7", "00 09 * * *"]) {
    test(`'${schedule}' opens in Advanced and saves unchanged`, async () => {
        const writes = await openEditor(job({ schedule }));
        try {
            assert.ok(advancedActive(), "a schedule Simple cannot rebuild exactly opens in Advanced");
            assert.equal(document.getElementById("cronExpression").value, schedule);
            await save();
            assert.equal(writes.find((w) => w.method === "PUT").body.schedule, schedule);
        } finally {
            closeAll();
        }
    });
}

for (const [schedule, frequency] of [
    ["0 * * * *", "hourly"], ["0 9 * * *", "daily"], ["15 7 * * 1", "weekly"],
    ["*/15 * * * *", "custom_interval"], ["0 */6 * * *", "custom_interval"],
]) {
    test(`'${schedule}' opens in Simple (${frequency}) and saves unchanged`, async () => {
        const writes = await openEditor(job({ schedule }));
        try {
            assert.ok(!advancedActive());
            assert.equal(document.getElementById("cronFrequency").value, frequency);
            await save();
            assert.equal(writes.find((w) => w.method === "PUT").body.schedule, schedule);
        } finally {
            closeAll();
        }
    });
}

test("the interval picker offers only periods cron can repeat exactly", () => {
    assert.deepEqual(cron.INTERVAL_DIVISORS.minutes.filter((n) => 60 % n !== 0), []);
    assert.deepEqual(cron.INTERVAL_DIVISORS.hours.filter((n) => 24 % n !== 0), []);
    for (const unit of ["minutes", "hours"]) {
        for (const n of cron.INTERVAL_DIVISORS[unit]) {
            const built = cron.buildSimpleCron({ frequency: "custom_interval", intervalUnit: unit, intervalValue: n });
            assert.deepEqual(cron.parseSimpleSchedule(built),
                { frequency: "custom_interval", intervalUnit: unit, intervalValue: n });
        }
    }
    assert.equal(cron.buildSimpleCron({ frequency: "custom_interval", intervalUnit: "minutes", intervalValue: 45 }), null);
    assert.equal(cron.buildSimpleCron({ frequency: "custom_interval", intervalUnit: "hours", intervalValue: 13 }), null);
    assert.equal(cron.buildSimpleCron({ frequency: "custom_interval", intervalUnit: "minutes", intervalValue: NaN }), null);
});

test("the shipped interval control is a picker, not a free number", async () => {
    await openEditor(job({ schedule: "*/15 * * * *" }));
    try {
        const ctl = document.getElementById("cronIntervalValue");
        assert.equal(ctl.tagName, "SELECT");
        const values = [...ctl.options].map((o) => Number(o.value));
        assert.deepEqual(values, cron.INTERVAL_DIVISORS.minutes);
        const unit = document.getElementById("cronIntervalUnit");
        unit.value = "hours";
        unit.dispatchEvent(new Event("change"));
        assert.deepEqual([...ctl.options].map((o) => Number(o.value)), cron.INTERVAL_DIVISORS.hours);
    } finally {
        closeAll();
    }
});

test("Simple round-trips and refuses near misses", () => {
    const cases = {
        "0 * * * *": { frequency: "hourly" },
        "0 9 * * *": { frequency: "daily", hour: 9, minute: 0 },
        "0  9 * * *": { frequency: "daily", hour: 9, minute: 0 },  // whitespace is not meaning
        "30 6 * * 0": { frequency: "weekly", hour: 6, minute: 30, day: 0 },
        "0 9 * * 1-5": null,
        "0 * * * 1-5": null,
        "30 8 * 6 *": null,
        "0 25 * * *": null,
        "0 9 * * 7": null,
        "@daily": null,
        "0 0 9 * * *": null,
    };
    for (const [expr, expected] of Object.entries(cases)) {
        assert.deepEqual(cron.parseSimpleSchedule(expr), expected, expr);
    }
});
