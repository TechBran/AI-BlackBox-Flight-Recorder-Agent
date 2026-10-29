// One copy of each small helper (audit rank 75: P02-voice-web-12,
// P08-onboarding-installer-10). The Portal modules take escapeHtml, fetchJson
// and postJson from core-utils.js; the wizard (served from its own /onboarding/
// mount, so it cannot import /ui/ modules) takes escapeHtml, fetchJson and
// safeDetail from onboarding/util.js and showHint from onboarding/hint.js.
// The copies had drifted: escapeHtml(0) was '' in some, fetchJson dropped the
// fetch options local_models needs, the error text differed per module.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { installDom, PORTAL } from "./helpers/portal-dom.mjs";
import { stripComments } from "./helpers/sink-scan.mjs";

installDom();

const core = await import("../../Portal/modules/core-utils.js");

const reply = (status, body) => ({
    ok: status >= 200 && status < 300, status,
    json: () => (body === undefined ? Promise.reject(new SyntaxError("no body")) : Promise.resolve(body)),
});

test("fetchJson returns the body, passes the fetch options, and reports detail or status", async () => {
    const seen = [];
    globalThis.fetch = window.fetch = async (url, init) => { seen.push([url, init]); return reply(200, { a: 1 }); };
    assert.deepEqual(await core.fetchJson("/x", { cache: "no-store" }), { a: 1 });
    assert.deepEqual(seen[0], ["/x", { cache: "no-store" }]);

    globalThis.fetch = window.fetch = async () => reply(409, { detail: "already running" });
    await assert.rejects(core.fetchJson("/x"), (e) => e.message === "already running" && e.status === 409);
    globalThis.fetch = window.fetch = async () => reply(502);
    await assert.rejects(core.fetchJson("/x"), (e) => e.message === "HTTP 502" && e.status === 502);
});

test("postJson sends JSON", async () => {
    let sent = null;
    globalThis.fetch = window.fetch = async (url, init) => { sent = { url, ...init }; return reply(200, { ok: true }); };
    assert.deepEqual(await core.postJson("/devices/d1/primary", { operator: "Ana" }), { ok: true });
    assert.equal(sent.method, "POST");
    assert.equal(sent.headers["Content-Type"], "application/json");
    assert.equal(sent.body, JSON.stringify({ operator: "Ana" }));
});

function definesOwn(src, names) {
    const code = stripComments(src);
    return names.filter((n) => new RegExp(`\\bfunction\\s+${n}\\s*\\(|\\b(const|let)\\s+${n}\\s*=`).test(code));
}

test("no Portal module keeps its own copy of the core-utils helpers", () => {
    const dir = resolve(PORTAL, "modules");
    const offenders = [];
    for (const f of readdirSync(dir).filter((f) => f.endsWith(".js") && f !== "core-utils.js")) {
        const own = definesOwn(readFileSync(resolve(dir, f), "utf8"), ["escapeHtml", "escapeAttr", "fetchJson", "postJson"]);
        if (own.length) offenders.push(`${f}: ${own.join(", ")}`);
    }
    assert.deepEqual(offenders, []);
});

test("no wizard step keeps its own copy of the onboarding helpers", () => {
    const dir = resolve(PORTAL, "onboarding", "steps");
    const offenders = [];
    for (const f of readdirSync(dir).filter((f) => f.endsWith(".js"))) {
        const own = definesOwn(readFileSync(resolve(dir, f), "utf8"), ["escapeHtml", "fetchJson", "showHint", "safeDetail"]);
        if (own.length) offenders.push(`${f}: ${own.join(", ")}`);
    }
    assert.deepEqual(offenders, []);
});

test("the wizard's showHint: after an anchor, inside a slot, or an existing line", async () => {
    const { showHint } = await import("../../Portal/onboarding/hint.js");
    const c = document.createElement("div");
    c.innerHTML = `<div id="grid"></div><div id="slot"><i>old</i></div><p id="lm" class="ob-lm-hint" hidden></p>`;

    showHint(c, "<b>saved</b>", false, { id: "h1", after: "#grid" });
    const h1 = c.querySelector("#h1");
    assert.equal(h1.previousElementSibling.id, "grid");
    assert.equal(h1.textContent, "<b>saved</b>", "text, never HTML");
    showHint(c, "failed", true, { id: "h1", after: "#grid" });
    assert.equal(c.querySelectorAll("#h1").length, 1, "one line, reused");
    assert.ok(h1.classList.contains("ob-cli-agent-hint-error"));

    showHint(c, "in the slot", false, { id: "h2", inside: "#slot" });
    assert.equal(c.querySelector("#slot").innerHTML, '<div id="h2" class="ob-cli-agent-hint">in the slot</div>');

    showHint(c, "busy", true, { id: "lm", className: "ob-lm-hint" });
    const lm = c.querySelector("#lm");
    assert.equal(lm.hidden, false);
    assert.equal(lm.className, "ob-lm-hint ob-lm-hint-error");
    showHint(c, "done", false, { id: "lm", className: "ob-lm-hint" });
    assert.equal(lm.className, "ob-lm-hint");

    showHint(c, "nowhere", false, { id: "h3", after: "#missing" });
    assert.equal(c.querySelector("#h3"), null);
    showHint(null, "no step", false, { id: "h4", after: "#grid" });   // a step not on screen
});

test("the wizard's fetchJson is soft and keeps the fetch options; safeDetail reads a string detail", async () => {
    const util = await import("../../Portal/onboarding/util.js");
    let seen = null;
    globalThis.fetch = window.fetch = async (url, init) => { seen = init; return reply(200, { x: 1 }); };
    assert.deepEqual(await util.fetchJson("/local-models/status", { cache: "no-store" }), { x: 1 });
    assert.deepEqual(seen, { cache: "no-store" });
    globalThis.fetch = window.fetch = async () => reply(500, { detail: "boom" });
    assert.equal(await util.fetchJson("/x"), null);
    globalThis.fetch = window.fetch = async () => { throw new TypeError("offline"); };
    assert.equal(await util.fetchJson("/x"), null);

    assert.equal(await util.safeDetail(reply(409, { detail: "busy" })), "busy");
    assert.equal(await util.safeDetail(reply(422, { detail: [{ msg: "bad" }] })), "HTTP 422");
    assert.equal(await util.safeDetail(reply(502)), "HTTP 502");
});
