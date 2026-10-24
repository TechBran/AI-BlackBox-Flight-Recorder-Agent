// The Portal's main-path controls (audit rank 66: P01-chat-core-web-06,
// X05-architecture-09, P07-html-css-03, P05-ops-panels-web-01,
// P04-media-tasks-fx-web-02, W23).
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { installDom, PORTAL } from "./helpers/portal-dom.mjs";
import { stripComments } from "./helpers/sink-scan.mjs";

installDom();

const html = readFileSync(resolve(PORTAL, "index.html"), "utf8");
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);

/** Every Portal .js source (comments stripped), the inline scripts of index.html included. */
function portalSources() {
    const out = [];
    const walk = (dir) => {
        for (const ent of readdirSync(dir, { withFileTypes: true })) {
            if (["vendor", "uploads", "node_modules"].includes(ent.name)) continue;
            const p = resolve(dir, ent.name);
            if (ent.isDirectory()) walk(p);
            else if (ent.name.endsWith(".js") && !ent.name.endsWith(".test.mjs")) {
                out.push(stripComments(readFileSync(p, "utf8")));
            }
        }
    };
    walk(PORTAL);
    for (const m of html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)) out.push(m[1]);
    return out.join("\n");
}

test("index.html declares every id once", () => {
    const seen = new Map();
    for (const id of ids) seen.set(id, (seen.get(id) || 0) + 1);
    const dupes = [...seen].filter(([, n]) => n > 1).map(([id]) => id);
    assert.deepEqual(dupes, [], "a duplicate id binds two handlers to one element and none to the other");
});

test("every btn*/ctl*/mic* control in index.html is wired by some module", () => {
    const src = portalSources();
    const dead = [];
    for (const id of ids) {
        if (!/^(btn|ctl|mic)/.test(id)) continue;
        const tag = html.slice(html.lastIndexOf("<", html.indexOf(`id="${id}"`)), html.indexOf(">", html.indexOf(`id="${id}"`)) + 1);
        if (/\bclass="[^"]*\bmodal-close\b/.test(tag)) continue; // wired by class (voice agents, gmail)
        if (!new RegExp(`['"\`#]${id}['"\`\\s,)\\]]`).test(src)) dead.push(id);
    }
    assert.deepEqual(dead, [], "a control with no handler renders as clickable and silently does nothing");
});

test("the composer and Tools-menu Computer Use buttons each get their own handler", async () => {
    const src = readFileSync(resolve(PORTAL, "modules", "app-init.js"), "utf8");
    assert.match(src, /\$\('btnToolsComputerUse'\)/);
    assert.match(src, /\$\("btnComputerUse"\)/);
    assert.match(html, /id="btnToolsComputerUse"/);
    assert.equal((html.match(/id="btnComputerUse"/g) || []).length, 1);
});

// ── CU typing bar ────────────────────────────────────────────────────────
test("one Enter in the CU typing bar sends exactly one /browser/key", async () => {
    const calls = [];
    globalThis.fetch = window.fetch = async (url, opts = {}) => {
        calls.push({ url: String(url), body: opts.body ? JSON.parse(opts.body) : null });
        return new Response(JSON.stringify({}), { status: 200, headers: { "Content-Type": "application/json" } });
    };
    const cu = await import("../../Portal/modules/cu-interact.js");
    cu.open("/ui/uploads/shot.png", "blackbox");
    const input = document.querySelector(".cu-interact-typing-input");
    assert.ok(input, "the typing bar exists");
    calls.length = 0;
    for (const key of ["Enter", "Backspace", "Tab"]) {
        input.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }));
    }
    const keys = calls.filter((c) => c.url === "/browser/key").map((c) => c.body.key);
    assert.deepEqual(keys, ["Return", "BackSpace", "Tab"], "each key once, never twice");

    // A soft-keyboard 'Unidentified' keydown is left alone (its text arrives via input).
    const ev = new KeyboardEvent("keydown", { key: "Unidentified", bubbles: true, cancelable: true });
    input.dispatchEvent(ev);
    assert.equal(ev.defaultPrevented, false);

    // Modifier combos from the bar still reach the desktop, once.
    calls.length = 0;
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "c", ctrlKey: true, bubbles: true, cancelable: true }));
    assert.deepEqual(calls.filter((c) => c.url === "/browser/key").map((c) => c.body.key), ["ctrl+c"]);
    cu.close();
});

// ── TaskManager: a finished task is shown once ───────────────────────────
test("a task resumed after reload completes exactly once (resume + poll)", async () => {
    const { TaskManager } = await import("../../Portal/modules/task-manager.js");
    const tm = new TaskManager();
    const done = { status: "completed", task_type: "image_generation", result_url: "/ui/uploads/x.png" };
    tm.checkTaskStatus = async () => done;
    let shown = 0;
    tm.handleTaskComplete = () => { shown += 1; };
    await tm.resumeTask("t-1", "tester");
    await tm.pollActiveTasks();
    await tm.pollActiveTasks();
    assert.equal(shown, 1, "the result bubble, toast and notification are not repeated");
    assert.equal(tm.activeTasks.size, 0);

    // A cancelled task resumed after reload is terminal too.
    tm.checkTaskStatus = async () => ({ status: "cancelled", task_type: "video_generation" });
    await tm.resumeTask("t-2", "tester");
    await tm.pollActiveTasks();
    assert.equal(shown, 2);
    assert.equal(tm.activeTasks.size, 0);
});

test("two overlapping polls of one finished task still complete it once", async () => {
    const { TaskManager } = await import("../../Portal/modules/task-manager.js");
    const tm = new TaskManager();
    tm.activeTasks.set("t-3", { type: "lyria_music", operator: "tester", startTime: Date.now() });
    let release;
    const gate = new Promise((r) => { release = r; });
    tm.checkTaskStatus = async () => { await gate; return { status: "completed", task_type: "lyria_music" }; };
    let shown = 0;
    tm.handleTaskComplete = () => { shown += 1; };
    const a = tm.pollActiveTasks();
    const b = tm.pollActiveTasks();
    release();
    await Promise.all([a, b]);
    assert.equal(shown, 1);
});

// ── Apps tab: status is probed, not the registry's "running" ─────────────
test("the Apps tab shows a dead app as not running", async () => {
    const list = document.createElement("div");
    list.id = "menuRunningApps";
    document.body.appendChild(list);
    globalThis.fetch = window.fetch = async (url) => {
        if (String(url) === "/agent/apps") {
            return new Response(JSON.stringify({ apps: [
                { name: "Alive", port: 8061, operator: "system", status: "running" },
                { name: "Dead", port: 8062, operator: "system", status: "running" },
            ] }), { status: 200 });
        }
        if (String(url).startsWith("/app-proxy/8061/")) return new Response("<html></html>", { status: 200 });
        if (String(url).startsWith("/app-proxy/8062/")) {
            return new Response(JSON.stringify({ detail: "Cannot connect to app on port 8062. Is it running?" }), { status: 503 });
        }
        return new Response("{}", { status: 404 });
    };
    const { loadRegisteredApps } = await import("../../Portal/modules/agent-handler.js");
    await loadRegisteredApps();
    await new Promise((r) => setTimeout(r, 30));
    const status = (port) => list.querySelector(`[data-port="${port}"] .app-status`).textContent;
    assert.equal(status(8061), "running");
    assert.equal(status(8062), "not running");
    list.remove();
});
