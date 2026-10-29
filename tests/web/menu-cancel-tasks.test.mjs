// Cancel Tasks (Portal menu) asks in the page, never with window.confirm: the
// Android WebView has no dialog host, so confirm() returned false there
// without showing anything and the button silently did nothing on the phone.
// The first tap arms the button (the same two-tap confirmInline as Restart
// Service); a second tap within the window POSTs /tasks/cancel-all once.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { installDom, PORTAL } from "./helpers/portal-dom.mjs";
import { stripComments } from "./helpers/sink-scan.mjs";

installDom();

const settle = (ms = 30) => new Promise((r) => setTimeout(r, ms));

const dialogs = [];
for (const kind of ["confirm", "alert", "prompt"]) {
    window[kind] = (text) => { dialogs.push(`${kind}: ${text}`); throw new Error(`window.${kind} called`); };
}

const posts = [];
globalThis.fetch = window.fetch = (url, opts = {}) => {
    if (opts.method === "POST") {
        posts.push(String(url));
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ cancelled: 3 }) });
    }
    return Promise.reject(new TypeError("offline"));
};

document.body.insertAdjacentHTML("beforeend", `
    <div class="menu-grid"><button id="btnCancelTasks" class="btn" title="Cancel all stuck tasks">Cancel Tasks</button></div>`);

{
    const ui = await import("../../Portal/modules/ui-setup.js");
    const warn = console.warn;
    console.warn = () => {};   // it warns about every control this page lacks
    ui.initModalHandlers();
    await settle(100);
    console.warn = warn;
}

const btn = () => document.getElementById("btnCancelTasks");
const cancels = () => posts.filter((u) => u === "/tasks/cancel-all");

test("Cancel Tasks: the first tap arms the button and sends nothing", async () => {
    btn().click();
    await settle();
    assert.deepEqual(cancels(), []);
    assert.equal(btn().textContent, "Tap again to cancel all tasks");
    assert.match(btn().getAttribute("title"), /every operator/, "the armed title says what happens");
    assert.deepEqual(dialogs, []);
});

test("Cancel Tasks: the second tap POSTs /tasks/cancel-all once and reports", async () => {
    btn().click();
    await settle();
    assert.equal(cancels().length, 1, "exactly one cancel-all");
    assert.equal(btn().textContent, "Cancel Tasks", "confirming restores the label");
    assert.equal(btn().getAttribute("title"), "Cancel all stuck tasks");
    assert.match(document.getElementById("toast").textContent, /Cancelled 3/);
    assert.deepEqual(dialogs, []);
});

test("Cancel Tasks: a single tap after that only arms again", async () => {
    btn().click();
    await settle();
    assert.equal(cancels().length, 1);
    assert.equal(btn().textContent, "Tap again to cancel all tasks");
    assert.deepEqual(dialogs, []);
    btn().click();               // disarm, so no timer outlives the file
    await settle();
    assert.equal(cancels().length, 2);
});

test("the Cancel Tasks handler never reaches window.confirm", () => {
    const src = stripComments(readFileSync(resolve(PORTAL, "modules", "ui-setup.js"), "utf8"));
    const handler = src.slice(src.indexOf('"btnCancelTasks"'), src.indexOf("/tasks/cancel-all"));
    assert.ok(handler.length > 0, "the handler is in ui-setup.js");
    assert.doesNotMatch(handler, /(?:^|[^.\w])(?:window\.)?confirm\s*\(/m);
});
