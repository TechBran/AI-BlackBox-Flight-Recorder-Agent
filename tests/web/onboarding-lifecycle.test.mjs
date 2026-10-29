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

// The box can take seconds to answer {ok:false} (each browser it tries gets a
// 4 s probe, then gio and the portal 10 s each). By then the click's user
// activation has expired, the popup is blocked, and navigating this tab to
// the link threw away whatever the operator had typed into the wizard.
test("a link that cannot open anywhere is offered inline; the wizard tab never navigates away", async () => {
    const { openExternalUrl } = await import("../../Portal/onboarding/open-url.js");
    const errors = [];
    const realError = console.error;
    console.error = (...args) => { errors.push(args.map(String).join(" ")); };
    const opened = [];
    let blocked = true;
    window.open = (url, target) => { opened.push([url, target]); return blocked ? null : { opener: window }; };
    globalThis.fetch = window.fetch = async () => json({ ok: false, error: "no browser available" });
    try {
        assert.equal(await openExternalUrl("https://console.cloud.google.com/apis/credentials"), false);
        await sleep(10);
    } finally {
        console.error = realError;
    }
    assert.ok(!errors.some((e) => /navigation/i.test(e)), `the wizard tab navigated away: ${errors.join(" | ")}`);
    const notice = document.querySelector(".ob-open-link-notice");
    assert.ok(notice, "the link is offered where the operator can tap it");
    const link = notice.querySelector("a");
    assert.equal(link.getAttribute("href"), "https://console.cloud.google.com/apis/credentials");
    assert.equal(link.textContent, "https://console.cloud.google.com/apis/credentials");
    assert.equal(link.dataset.openWired, "1", "the wizard's open-url interceptor leaves it alone");

    // The tap is a fresh user gesture: it opens the tab synchronously.
    blocked = false;
    opened.length = 0;
    link.click();
    assert.deepEqual(opened, [["https://console.cloud.google.com/apis/credentials", "_blank"]]);
    assert.equal(document.querySelector(".ob-open-link-notice"), null, "and the notice goes");
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

test("askInline asks in the page next to the button and answers once", async () => {
    const { askInline } = await import("../../Portal/onboarding/inline-confirm.js");
    const actions = document.createElement("div");
    const btn = document.createElement("button");
    btn.className = "btn btn-primary";
    actions.appendChild(btn);
    document.body.appendChild(actions);
    const rows = () => document.querySelectorAll(".inline-confirm-row");

    const yes = askInline(btn, "Install 1 update(s)?\n\n<b>app.py</b> is stashed", { okLabel: "Install" });
    assert.equal(rows().length, 1);
    const row = rows()[0];
    assert.equal(actions.nextElementSibling, row, "the question sits right after the button's row");
    const text = row.querySelector(".inline-confirm-text");
    assert.equal(text.textContent, "Install 1 update(s)?\n\n<b>app.py</b> is stashed");
    assert.equal(text.querySelector("b"), null, "the text is never parsed as markup");
    assert.equal(row.querySelector(".inline-confirm-ok").textContent, "Install");
    assert.equal(row.querySelector(".inline-confirm-ok").className, "btn inline-confirm-ok");
    row.querySelector(".inline-confirm-ok").click();
    assert.equal(await yes, true);
    assert.equal(rows().length, 0, "the row goes once answered");

    const no = askInline(btn, "Roll back?");
    rows()[0].querySelector(".inline-confirm-cancel").click();
    assert.equal(await no, false);

    const first = askInline(btn, "first");
    const second = askInline(btn, "second");
    assert.equal(await first, false, "asking again replaces the older question");
    assert.equal(rows().length, 1);
    assert.equal(rows()[0].querySelector(".inline-confirm-text").textContent, "second");
    rows()[0].querySelector(".inline-confirm-ok").click();
    assert.equal(await second, true);
    actions.remove();

    assert.equal(await askInline(document.createElement("button"), "detached"), false,
                 "a button outside the page cannot ask");
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
