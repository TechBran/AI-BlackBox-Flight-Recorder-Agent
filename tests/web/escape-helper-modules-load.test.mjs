// The modules that dropped their private escapeHtml copies for the shared helper
// (audit P08-onboarding-installer-10, M3 U20 item 5) must still LINK and load:
// a missing named export or a leftover duplicate declaration is a module link
// error that blanks the wizard or the Portal panel.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { installDom, loadPurify, loadMarked } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

// onboarding.js boots the wizard on import: give it the real page skeleton and
// park every request, so the boot runs as far as it can without a backend.
const page = readFileSync(new URL("../../Portal/onboarding/index.html", import.meta.url), "utf8");
document.body.innerHTML = page.slice(page.indexOf("<body"), page.lastIndexOf("</body>"))
    .replace(/^<body[^>]*>/, "").replace(/<script[\s\S]*?<\/script>/g, "");
globalThis.fetch = () => new Promise(() => {});

const MODULES = [
    "onboarding/util.js", "onboarding/status.js", "onboarding/hub.js", "onboarding/onboarding.js",
    ...["api_keys", "cli_agents", "done", "embeddings", "image", "local_models", "mcp", "operator",
        "optional_integrations", "pair_phone", "tailscale", "transcription", "web_search", "welcome"]
        .map((s) => `onboarding/steps/${s}.js`),
    "voice-lab.js", "voice-library.js", "modules/devices-section.js",
];

for (const rel of MODULES) {
    test(`Portal/${rel} imports cleanly`, async () => {
        const mod = await import(`../../Portal/${rel}`);
        assert.ok(mod && typeof mod === "object");
    });
}

test("a wizard step renders hostile server text as text", async () => {
    const { escapeHtml } = await import("../../Portal/onboarding/util.js");
    const probe = document.createElement("div");
    const hostile = `x" onfocus="window.__pwned=1"><img src=x onerror="window.__pwned=1">`;
    probe.innerHTML = `<input value="${escapeHtml(hostile)}"><span>${escapeHtml(hostile)}</span>`;
    assert.equal(probe.querySelector("input").getAttribute("value"), hostile);
    assert.equal(probe.querySelector("span").textContent, hostile);
    assert.equal(probe.querySelector("img"), null);
    assert.equal(probe.querySelector("input").hasAttribute("onfocus"), false);
});
