// The Portal runs under its Content-Security-Policy (audit rank 75,
// Orchestrator/utils/portal_csp.py): script-src <host>/ui/, so no inline
// <script>, no inline on*= handler (static or built into an HTML string), no
// javascript: URL, no eval anywhere under /ui, and no script from outside the
// Portal tree (/app-proxy/, /cu/, ... are not script sources). The bubbles' panel controls
// and the agent banners' collapse used inline onclick strings; they are now
// one delegated listener and init-time bindings.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { installDom, loadPurify, loadMarked, PORTAL } from "./helpers/portal-dom.mjs";
import { stripComments } from "./helpers/sink-scan.mjs";

/** Every file under Portal/ with one of `exts`, minus vendored and runtime trees. */
function walk(dir, exts, out = []) {
    for (const name of readdirSync(dir)) {
        if (["vendor", "uploads", "artifacts", "node_modules"].includes(name) || name.startsWith(".")) continue;
        const p = join(dir, name);
        if (statSync(p).isDirectory()) walk(p, exts, out);
        else if (exts.some((e) => name.endsWith(e)) && !name.endsWith(".test.mjs")) out.push(p);
    }
    return out;
}

// Portal/cu-view/index.html is served at /cu/view/{session} (browser_routes.py),
// outside /ui and its policy; its classic diagnostics script must run even when
// the module chain fails, so it stays inline there.
const NOT_UNDER_THE_POLICY = new Set(["cu-view/index.html"]);

const INLINE_HANDLER = /\son[a-z]{3,}\s*=\s*\\?["'`]/i;

test("no page under /ui has an inline script, handler or javascript: URL", () => {
    const offenders = [];
    for (const file of walk(PORTAL, [".html"])) {
        const rel = relative(PORTAL, file);
        if (NOT_UNDER_THE_POLICY.has(rel)) continue;
        const html = readFileSync(file, "utf8").replace(/<!--[\s\S]*?-->/g, "");
        for (const tag of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
            if (!/\ssrc\s*=/.test(tag[1]) && tag[2].trim()) offenders.push(`${rel}: inline <script>`);
        }
        if (INLINE_HANDLER.test(html)) offenders.push(`${rel}: ${html.match(INLINE_HANDLER)[0].trim()}`);
        if (/\b(href|src|action)\s*=\s*["']\s*javascript:/i.test(html)) offenders.push(`${rel}: javascript: URL`);
    }
    assert.deepEqual(offenders, []);
});

test("no script under /ui builds an inline handler, a javascript: URL or evals", () => {
    const offenders = [];
    for (const file of walk(PORTAL, [".js", ".mjs"])) {
        const rel = relative(PORTAL, file);
        const code = stripComments(readFileSync(file, "utf8"));
        if (INLINE_HANDLER.test(code)) offenders.push(`${rel}: ${code.match(INLINE_HANDLER)[0].trim()}`);
        if (/setAttribute\(\s*["']on[a-z]+["']/i.test(code)) offenders.push(`${rel}: setAttribute('on…')`);
        if (/["'`]\s*javascript:/i.test(code)) offenders.push(`${rel}: javascript: URL`);
        if (/(^|[^.\w])eval\s*\(|\bnew\s+Function\s*\(/.test(code)) offenders.push(`${rel}: eval / new Function`);
    }
    assert.deepEqual(offenders, []);
});

// Portal/cu-view/ is the /cu/view/{session} page's own code (it imports
// /cu/novnc/), framed by the Portal, never loaded as a Portal script.
const LOADED_OUTSIDE_THE_POLICY = /^cu-view\//;

test("every script a page under /ui loads comes from the /ui/ tree", () => {
    const offenders = [];
    for (const file of walk(PORTAL, [".html"])) {
        const rel = relative(PORTAL, file);
        if (NOT_UNDER_THE_POLICY.has(rel)) continue;
        const html = readFileSync(file, "utf8").replace(/<!--[\s\S]*?-->/g, "");
        for (const tag of html.matchAll(/<script\b[^>]*\ssrc\s*=\s*["']([^"']+)["']/gi)) {
            const src = tag[1];
            // index.html is served at "/" too, where a relative src would leave /ui/.
            const relativeOk = rel !== "index.html" && !/^([a-z][a-z0-9+.-]*:|\/)/i.test(src);
            if (!src.startsWith("/ui/") && !relativeOk) offenders.push(`${rel}: ${src}`);
        }
    }
    for (const file of walk(PORTAL, [".js", ".mjs"])) {
        const rel = relative(PORTAL, file);
        if (LOADED_OUTSIDE_THE_POLICY.test(rel)) continue;
        const code = stripComments(readFileSync(file, "utf8"));
        const loads = /(?:\bfrom|\bimport\s*\(|addModule\s*\(|\bWorker\s*\(|importScripts\s*\()\s*["'`]([^"'`]+)["'`]/g;
        for (const m of code.matchAll(loads)) {
            const spec = m[1];
            if (/^([a-z][a-z0-9+.-]*:|\/)/i.test(spec) && !spec.startsWith("/ui/")) {
                offenders.push(`${rel}: ${spec}`);
            }
        }
    }
    assert.deepEqual(offenders, []);
});

test("the vendored libraries need no eval either", () => {
    const offenders = [];
    const files = [];
    (function all(dir) {
        for (const name of readdirSync(dir)) {
            const p = join(dir, name);
            if (statSync(p).isDirectory()) all(p);
            else if (name.endsWith(".js")) files.push(p);
        }
    })(join(PORTAL, "vendor"));
    assert.ok(files.length > 0);
    for (const file of files) {
        const code = readFileSync(file, "utf8");
        if (/\bnew\s+Function\s*\(|(^|[^.\w])eval\s*\(/.test(code)) offenders.push(relative(PORTAL, file));
    }
    assert.deepEqual(offenders, []);
});

// ── The delegated bubble controls ───────────────────────────────────────
installDom();
loadPurify();
loadMarked();
localStorage.setItem("bbx_operator", "tester");
const chat = await import("../../Portal/modules/chat-send.js");

test("a reasoning header toggles its panel; its Listen button speaks without toggling", () => {
    const bubble = chat.createBubbleWithThinking("the answer", "", null);
    document.getElementById("history").appendChild(bubble);
    const panel = bubble.querySelector(".streaming-thinking-panel");
    const header = panel.querySelector(".reasoning-header");
    assert.ok(panel.classList.contains("collapsed"));
    header.querySelector(".reasoning-label").click();
    assert.ok(!panel.classList.contains("collapsed"), "a click on the header opens it");
    panel.querySelector(".toggle-btn .reasoning-toggle").click();
    assert.ok(panel.classList.contains("collapsed"), "the toggle button closes it");

    panel.querySelector(".tts-btn svg").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    assert.ok(panel.classList.contains("collapsed"), "Listen does not toggle the panel");
    assert.equal(document.getElementById("toast").textContent, "No thinking content to speak",
        "Listen reached speakThinkingContent");
});

test("the Computer Use panel and live view headers toggle", () => {
    const bubble = chat.createBubbleWithThinking("done", "", [{ type: "action", action: "click", params: "x", step: 1 }]);
    document.getElementById("history").appendChild(bubble);
    const cu = bubble.querySelector(".cu-activity-panel");
    assert.ok(cu.classList.contains("collapsed"));
    cu.querySelector(".cu-activity-label").click();
    assert.ok(!cu.classList.contains("collapsed"));
    assert.equal(cu.querySelector(".cu-activity-toggle").textContent, "▼");
});

test("the background Computer Use banner's buttons reach their actions", () => {
    const calls = [];
    const saved = [window.__openCUBrowserView, window.__reconnectCU];
    window.__openCUBrowserView = () => calls.push("view");
    window.__reconnectCU = () => calls.push("reconnect");
    try {
        const banner = document.createElement("div");
        banner.innerHTML = '<button class="cu-bg-view" data-bubble-action="cu-bg-view">Live Browser</button>'
            + '<button class="cu-bg-reconnect" data-bubble-action="cu-bg-reconnect">Reconnect</button>';
        document.body.appendChild(banner);
        banner.querySelector(".cu-bg-view").click();
        banner.querySelector(".cu-bg-reconnect").click();
        assert.deepEqual(calls, ["view", "reconnect"]);
        banner.remove();
    } finally {
        [window.__openCUBrowserView, window.__reconnectCU] = saved;
    }
});

test("the agent banners collapse and expand from init, before any handler exists", async () => {
    document.body.insertAdjacentHTML("beforeend", `
        <div id="claudeCodeBanner" class="collapsed">
          <div id="claudeBannerCollapsed"></div><span id="bannerCollapseBtn">x</span></div>
        <div id="geminiCodeBanner" class="collapsed">
          <div id="geminiBannerCollapsed"></div><span id="geminiBannerCollapseBtn">x</span></div>`);
    const agent = await import("../../Portal/modules/agent-handler.js");
    const gemini = await import("../../Portal/modules/gemini-agent-handler.js");
    globalThis.fetch = window.fetch = () => Promise.resolve(new Response("{}"));
    agent.initAgentHandler();
    gemini.initGeminiAgentHandler();
    assert.equal(agent.getAgentHandler(), null, "no handler yet");
    for (const [banner, strip, btn] of [["claudeCodeBanner", "claudeBannerCollapsed", "bannerCollapseBtn"],
        ["geminiCodeBanner", "geminiBannerCollapsed", "geminiBannerCollapseBtn"]]) {
        document.getElementById(strip).click();
        assert.ok(!document.getElementById(banner).classList.contains("collapsed"), `${strip} expands`);
        document.getElementById(btn).click();
        assert.ok(document.getElementById(banner).classList.contains("collapsed"), `${btn} collapses`);
        const ev = new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
        document.getElementById(strip).dispatchEvent(ev);
        assert.ok(!document.getElementById(banner).classList.contains("collapsed"), "and from the keyboard");
        assert.equal(document.getElementById(strip).getAttribute("role"), "button");
    }
});
