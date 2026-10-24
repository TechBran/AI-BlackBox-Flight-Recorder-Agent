// Onboarding wizard lifecycle (audit rank 62 Portal half:
// P08-onboarding-installer-05, -03, -01, -06).
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { installDom, PORTAL } from "./helpers/portal-dom.mjs";
import { stripComments } from "./helpers/sink-scan.mjs";

installDom();

const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
    status, headers: { "Content-Type": "application/json" },
});
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── open-url: {ok:false} is a failure ───────────────────────────────────
test("an external link the box cannot open is opened on this device", async () => {
    const { openExternalUrl } = await import("../../Portal/onboarding/open-url.js");
    const opened = [];
    window.open = (url, target) => { opened.push([url, target]); return { opener: window }; };

    globalThis.fetch = window.fetch = async () => json({ ok: false, error: "no browser available" });
    assert.equal(await openExternalUrl("https://login.tailscale.com/admin"), false);
    globalThis.fetch = window.fetch = async () => json({ detail: "only http(s) URLs" }, 400);
    await openExternalUrl("https://example.com/");
    globalThis.fetch = window.fetch = async () => { throw new TypeError("Failed to fetch"); };
    await openExternalUrl("https://example.org/");
    assert.deepEqual(opened.map((o) => o[0]),
        ["https://login.tailscale.com/admin", "https://example.com/", "https://example.org/"]);

    opened.length = 0;
    globalThis.fetch = window.fetch = async () => json({ ok: true });
    assert.equal(await openExternalUrl("https://example.net/"), true);
    assert.deepEqual(opened, [], "opened on the box: nothing more here");
});

// ── two-tap inline confirm instead of confirm() ──────────────────────────
test("confirmInline arms on the first tap and confirms on the second", async () => {
    const { confirmInline } = await import("../../Portal/onboarding/inline-confirm.js");
    const btn = document.createElement("button");
    btn.textContent = "×";
    btn.title = "Remove";
    assert.equal(confirmInline(btn, { prompt: "Remove?", detail: "it goes away", timeoutMs: 50 }), false);
    assert.equal(btn.textContent, "Remove?");
    assert.equal(btn.title, "it goes away");
    assert.equal(confirmInline(btn), true);
    assert.equal(btn.textContent, "×", "label restored on confirm");
    assert.equal(btn.title, "Remove");
    assert.equal(confirmInline(btn, { timeoutMs: 20 }), false);
    await sleep(40);
    assert.equal(btn.textContent, "×", "the arm lapses");
    assert.equal(confirmInline(btn, { timeoutMs: 20 }), false, "a lapsed arm needs two taps again");
});

test("no wizard flow depends on confirm() or alert() (dead in the Android WebView)", () => {
    const offenders = [];
    const walk = (dir) => {
        for (const ent of readdirSync(dir, { withFileTypes: true })) {
            const p = resolve(dir, ent.name);
            if (ent.isDirectory()) walk(p);
            else if (ent.name.endsWith(".js")) {
                const src = stripComments(readFileSync(p, "utf8"));
                if (/(?:^|[^.\w])(?:window\.)?(?:confirm|alert)\s*\(/m.test(src)) offenders.push(ent.name);
            }
        }
    };
    walk(resolve(PORTAL, "onboarding"));
    assert.deepEqual(offenders, []);
});

test("'I've already paired' completes the step only through next()", () => {
    const src = stripComments(readFileSync(resolve(PORTAL, "onboarding", "steps", "pair_phone.js"), "utf8"));
    assert.ok(!src.includes("/onboarding/step/complete"),
        "an explicit completion POST double-completes and rewinds current_step on a hub revisit");
});

// ── Tailscale sign-in poll stops with its step ───────────────────────────
async function startSignIn() {
    const ts = await import("../../Portal/onboarding/steps/tailscale.js");
    const counts = { poll: 0, cancel: 0 };
    globalThis.fetch = window.fetch = async (url) => {
        const u = String(url);
        if (u === "/onboarding/validate") return json({ ok: false, error: "BackendState NeedsLogin: not authenticated" });
        if (u === "/onboarding/tailscale/up") return json({ login_url: "https://login.tailscale.com/a/abc" });
        if (u === "/onboarding/tailscale/poll") { counts.poll += 1; return json({ state: "NeedsLogin" }); }
        if (u === "/onboarding/tailscale/cancel") { counts.cancel += 1; return json({}); }
        return json({});
    };
    window.open = () => null;
    const container = document.createElement("div");
    document.body.appendChild(container);
    let skipped = 0;
    await ts.render(container, { next() {}, back() {}, skip() { skipped += 1; container.innerHTML = "<p>next step</p>"; } });
    container.querySelector("#ob-auth-btn").click();
    await sleep(30);
    assert.ok(container.querySelector(".ob-auth-url-code"), "waiting for sign-in");
    return { container, counts, skippedRef: () => skipped };
}

test("Skip ends the sign-in poll: no polling into a detached step, no late cancel", async () => {
    const { container, counts, skippedRef } = await startSignIn();
    container.querySelector("#ob-tailscale-skip").click();
    assert.equal(skippedRef(), 1);
    await sleep(2300);   // the first poll was due 2 s after sign-in started
    assert.equal(counts.poll, 0);
    assert.equal(counts.cancel, 0);
    container.remove();
});

test("a step removed from the page stops polling on its own", async () => {
    const { container, counts } = await startSignIn();
    container.remove();
    await sleep(2300);
    assert.equal(counts.poll, 0);
    assert.equal(counts.cancel, 0);
});
