// The chat's health surfaces (M7 health design, docs/plans/
// 2026-10-25-samsung-health-integration.md §3.7, §6.1-§6.3): the
// "Used your health data" footer under a reply a wellness tool fed, and
// "Show numbers" on a masked reply ("[·]" where the ledger copy holds a
// health value) that carries its wellness_ref.
//
//  * The footer names the source, "as of" and who the data was shared with,
//    never a value, and stays text whatever the strings carry.
//  * Show numbers exists only inside the BlackBox Android app (its message
//    channel, window.BlackBoxWellness, signs GET /wellness/ref/{ref}?operator=X);
//    the numbers show in a private in-page panel that is never saved and
//    closes on navigation; 403/404 are "Not available".
//  * A POST /chat turn (streaming off) carries its health use in result_data
//    (wellnessFromReply), and a snapshot reloaded from the ledger names its
//    refs in its WELLNESS gauge (wellnessGaugeRefs, snapshotNumbersFooter).
//  * A health reply is never read aloud: its bubble (and its streaming
//    reasoning panel) is marked, and its speak buttons only say why.
//
// The fixture numbers are the synthetic wellness-fixture operator's
// (Orchestrator/tests/wellness_support.py): sleeping HR 47.3, steps 12345.
//
// Run: bash scripts/run_web_tests.sh
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { installDom, loadPurify, loadMarked, scriptVectors, PORTAL, REPO_ROOT } from "./helpers/portal-dom.mjs";
import { stripComments, innerHtmlAssignments, isSafeRhs, rawHtmlApiUses } from "./helpers/sink-scan.mjs";
import { fakeChannel, installChannel } from "./helpers/wellness-channel.mjs";

installDom();
loadPurify();
loadMarked();
localStorage.setItem("bbx_operator", "wellness-fixture");

const bubbles = await import("../../Portal/modules/chat-bubbles.js");
const state = await import("../../Portal/modules/state-management.js");
const {
    formatWellnessFooter, formatAsOf, normalizeWellnessUsed, mergeWellnessUsed, isMaskedHealthTurn,
    wellnessRefsOf, refPanel, decorateWellnessBubble, closeWellnessNumbers, appendBubble, WELLNESS_MASK,
    WELLNESS_SPEECH_OFF, isWellnessReply, wellnessGaugeRefs, wellnessFromReply, snapshotNumbersFooter,
    closeWellnessNumbersIn,
} = bubbles;
const { ORIGIN_HEADER, providerLabel } = await import("../../Portal/modules/wellness-section.js");

const XSS = `<img src=x onerror="window.__pwned=1">`;
const REF = "w-20261029-7f3a";
const EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
const ZONE = { locale: "en-GB", timeZone: "America/New_York" };
const NOW = Date.parse("2026-10-29T15:00:00Z");   // 11:00 in New York

/**
 * Resolve once `pred()` holds. A real SubtleCrypto digest finishes on Node's
 * threadpool, not within a fixed number of ticks, so a signed fetch is waited
 * for, never timed.
 */
async function waitFor(pred, what, timeoutMs = 5000) {
    const until = Date.now() + timeoutMs;
    while (!pred()) {
        if (Date.now() > until) throw new Error(`timed out waiting for ${what}`);
        await new Promise((r) => setTimeout(r, 5));
    }
}

/** The bubble's numbers panel, once it is past "Loading…". */
const loaded = (bubble) => waitFor(() => {
    const panel = bubble.querySelector(".wellness-numbers");
    return panel && !panel.textContent.includes("Loading");
}, "the numbers panel");

const USED = {
    ref: REF, categories: ["sleep", "heart"], as_of: "2026-10-29T07:10-04:00",
    provider: "anthropic", provider_label: "Anthropic",
};

const SUMMARY = {
    ref: REF, ts: 1761736200000, tool: "wellness_summary", categories: ["heart", "sleep"],
    payload: {
        ok: true, tool: "wellness_summary", ref: REF, as_of: "2026-10-29T07:10-04:00", stale: false,
        period: { label: "last night", start: "2026-10-28T18:00-04:00", end: "2026-10-29T14:00-04:00", tz: "UTC-04:00" },
        metrics: [
            { metric: "sleeping_hr_median", category: "heart", label: "Sleeping heart rate", value: 47.3, unit: "bpm",
              display: "47 bpm", spoken: "about forty-seven beats per minute",
              vs_usual: { label: "below your usual range", delta_display: "a little less" },
              coverage: { n_used: 1, n_expected: 1, pct: 100, label: "worn all night" }, flags: [] },
            { metric: "steps", category: "activity", label: "Steps", value: 12345, unit: "steps",
              display: "12,345 steps", coverage: { label: "worn 14 h" } },
        ],
        gaps: [{ metric: "sleep_duration", day: "2026-10-28", reason: "watch probably not worn" }],
        troubles: [], notes: [],
    },
};

const PROOF = `v1;enr=en_0000abcd;ts=1800000000;nonce=${"0".repeat(32)};sig=${"b".repeat(64)}`;
/** The app's signer channel (helpers/wellness-channel.mjs), answering PROOF unless told otherwise. */
const fakeBridge = ({ sign } = {}) => fakeChannel({ sign: sign || (() => PROOF) });
const REF_PATH = `/wellness/ref/${REF}?operator=wellness-fixture`;

const reply = (body, status = 200) => Promise.resolve({
    ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body),
});

function stubFetch(handler) {
    const calls = [];
    globalThis.fetch = window.fetch = (url, opts = {}) => {
        calls.push({ url: String(url), opts });
        return handler(String(url), opts);
    };
    return calls;
}

function maskedBubble(entry = {}) {
    const e = { role: "assistant", content: "Your sleeping heart rate was [·] bpm.", wellness_ref: REF, wellness_used: USED, ...entry };
    const bubble = appendBubble("assistant", e.content);
    decorateWellnessBubble(bubble, e, { sourceLabel: "Fixture Phone (SM-TEST1)" });
    return { bubble, entry: e };
}

function reset(t) {
    document.getElementById("history").innerHTML = "";
    delete window.AndroidBridge;
    installChannel(null);
    t.after(() => {
        closeWellnessNumbers();
        delete window.AndroidBridge;
        installChannel(null);
        document.getElementById("history").innerHTML = "";
    });
}

// ── The footer ─────────────────────────────────────────────────────────────

test("the footer: source, as of (a time today), who it was shared with", () => {
    assert.equal(formatWellnessFooter(USED, { sourceLabel: "Fixture Phone (SM-TEST1)", now: NOW, ...ZONE }),
        "Used your health data · Fixture Phone (SM-TEST1) · as of 07:10 · Anthropic");
});

test("the footer: 'your phone' when no source is known; a date for an earlier day", () => {
    const old = { ...USED, as_of: "2026-10-27T22:40-04:00" };
    assert.equal(formatWellnessFooter(old, { now: NOW, ...ZONE }),
        "Used your health data · your phone · as of 27 Oct, 22:40 · Anthropic");
});

test("the footer: the label the turn recorded wins over the live one; a provider id in words", () => {
    const u = { ...USED, source_label: "Recorded Phone", provider_label: null, provider: "custom:lan-box" };
    assert.equal(formatWellnessFooter(u, { sourceLabel: "Live Phone", now: NOW, ...ZONE }),
        "Used your health data · Recorded Phone · as of 07:10 · lan-box");
});

test("the footer: an unreadable as_of is left out, never 'as of Jan 1'", () => {
    for (const as_of of [null, "", XSS, "yesterday", 1761736200000]) {
        assert.equal(formatWellnessFooter({ ...USED, as_of }, { now: NOW, ...ZONE }),
            "Used your health data · your phone · Anthropic", String(as_of));
    }
    assert.equal(formatAsOf("2026-10-29T07:10-04:00", { now: NOW, ...ZONE }), "07:10");
});

test("the footer is empty for a frame with no usable ref", () => {
    // A ref goes into a signed URL path: nothing that would be percent-encoded.
    for (const bad of [null, {}, { ref: "" }, { ref: XSS }, { ref: 7 }, "w-1", { ref: "w-20261029:7f3a" },
        { ref: "w-../x" }, { ref: "w 1" }]) {
        assert.equal(formatWellnessFooter(bad), "", JSON.stringify(bad));
        assert.equal(normalizeWellnessUsed(bad), null);
    }
});

test("normalizeWellnessUsed keeps only well-typed fields; mergeWellnessUsed keeps the first ref", () => {
    const n = normalizeWellnessUsed({ ...USED, categories: ["sleep", XSS, 3, "sleep"], extra: "SENTINEL", provider_label: "x".repeat(500) });
    assert.deepEqual(Object.keys(n).sort(), ["as_of", "categories", "provider", "provider_label", "ref", "refs", "source_label"]);
    assert.deepEqual(n.categories, ["sleep"]);
    assert.equal(n.provider_label.length, 80);
    const merged = mergeWellnessUsed(USED, { ref: "w-20261029-a1b2", categories: ["activity"], as_of: "2026-10-29T07:20-04:00",
        provider: "anthropic", provider_label: "Anthropic" });
    assert.equal(merged.ref, REF, "the first ref of the turn stays the message's wellness_ref");
    assert.deepEqual(merged.refs, [REF, "w-20261029-a1b2"]);
    assert.deepEqual(merged.categories, ["sleep", "heart", "activity"]);
    assert.equal(merged.as_of, "2026-10-29T07:20-04:00");
    assert.deepEqual(mergeWellnessUsed(null, { ref: "" }), null);
    assert.deepEqual(mergeWellnessUsed(USED, { ref: "" }).refs, [REF], "a bad frame changes nothing");
});

test("the footer renders under the reply, as text, with no numbers", (t) => {
    reset(t);
    const hostile = { ...USED, provider_label: XSS, as_of: XSS };
    const bubble = appendBubble("assistant", "Your sleeping heart rate was 47 bpm.");
    decorateWellnessBubble(bubble, { content: "Your sleeping heart rate was 47 bpm.", wellness_ref: REF, wellness_used: hostile },
        { sourceLabel: XSS });
    const footer = bubble.querySelector(".wellness-footer");
    assert.ok(footer);
    assert.equal(footer.previousElementSibling, bubble.querySelector(".bubble-text"), "right under the text");
    assert.equal(footer.textContent, `Used your health data · ${XSS} · ${XSS}`);
    assert.deepEqual(scriptVectors(bubble), [], bubble.innerHTML);
    assert.equal(bubble.querySelector(".wellness-footer img"), null);
    assert.ok(!/47/.test(footer.textContent), "the footer never carries a value");
    assert.equal(bubble.querySelector(".wellness-show-numbers"), null, "an unmasked reply needs no Show numbers");
});

test("decorateWellnessBubble is idempotent and leaves an old entry alone", (t) => {
    reset(t);
    const bubble = appendBubble("assistant", "plain");
    assert.equal(decorateWellnessBubble(bubble, { role: "assistant", content: "plain" }), null);
    assert.equal(bubble.querySelector(".wellness-footer"), null);
    decorateWellnessBubble(bubble, { content: "plain", wellness_ref: REF, wellness_used: USED });
    decorateWellnessBubble(bubble, { content: "plain", wellness_ref: REF, wellness_used: USED });
    assert.equal(bubble.querySelectorAll(".wellness-footer").length, 1);
});

// ── Never read aloud ───────────────────────────────────────────────────────

test("a health reply's bubble is marked (and its floating reasoning panel); an old reply is not", (t) => {
    reset(t);
    const old = appendBubble("assistant", "An answer from before M7.");
    decorateWellnessBubble(old, { role: "assistant", content: "An answer from before M7." });
    assert.equal(old.dataset.wellness, undefined);
    assert.equal(isWellnessReply(old.querySelector(".speak-btn")), false);

    const live = appendBubble("assistant", "Your sleeping heart rate was 47 bpm.");
    live._floatingPanel = document.createElement("div");
    decorateWellnessBubble(live, { content: "", wellness_used: USED });   // as the stream's frame does
    assert.equal(live.dataset.wellness, "1");
    assert.equal(live._floatingPanel.dataset.wellness, "1", "the reasoning panel that streams outside the bubble");
    assert.equal(isWellnessReply(live.querySelector(".speak-btn")), true);

    const refOnly = appendBubble("assistant", "Your sleeping heart rate was [·] bpm.");
    decorateWellnessBubble(refOnly, { content: "Your sleeping heart rate was [·] bpm.", wellness_ref: REF });
    assert.equal(refOnly.dataset.wellness, "1", "a ref alone is a health reply too");
    decorateWellnessBubble(live, { content: "x" });
    assert.equal(live.dataset.wellness, "1", "once a health reply, always one");
});

test("a health reply's speak buttons never reach their handler; an ordinary reply's do", (t) => {
    reset(t);
    const heard = [];
    const make = (entry) => {
        const bubble = appendBubble("assistant", entry.content);
        const speak = bubble.querySelector(".speak-btn");
        speak.onclick = () => heard.push(entry.content);
        const reasoning = document.createElement("button");
        reasoning.dataset.bubbleAction = "thinking-speak";
        reasoning.addEventListener("click", () => heard.push(`reasoning: ${entry.content}`));
        bubble.appendChild(reasoning);
        decorateWellnessBubble(bubble, entry);
        return { speak, reasoning };
    };
    const health = make({ content: "Your sleeping heart rate was 47 bpm.", wellness_ref: REF, wellness_used: USED });
    const plain = make({ content: "Hello." });
    document.getElementById("toast").textContent = "";
    for (const el of [health.speak, health.speak.querySelector("path") || health.speak, health.reasoning]) {
        el.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true }));
    }
    assert.deepEqual(heard, []);
    assert.equal(document.getElementById("toast").textContent, WELLNESS_SPEECH_OFF);
    plain.speak.click();
    plain.reasoning.click();
    assert.deepEqual(heard, ["Hello.", "reasoning: Hello."]);
});

// ── Show numbers ───────────────────────────────────────────────────────────

test("isMaskedHealthTurn: the ledger mask and a ref", () => {
    assert.equal(isMaskedHealthTurn("was [·] bpm", REF), true);
    assert.equal(isMaskedHealthTurn("was 47 bpm", REF), false);
    assert.equal(isMaskedHealthTurn("was [·] bpm", null), false);
    assert.equal(isMaskedHealthTurn("was [·] bpm", XSS), false);
    assert.equal(isMaskedHealthTurn(null, REF), false);
    assert.deepEqual(wellnessRefsOf({ wellness_ref: REF, wellness_used: { ...USED, refs: [REF, "w-20261029-a1b2", XSS] } }),
        [REF, "w-20261029-a1b2"]);
    assert.deepEqual(wellnessRefsOf({ content: "x" }), []);
});

test("without the app's channel (a desktop browser) there is no Show numbers", (t) => {
    reset(t);
    const { bubble } = maskedBubble();
    assert.ok(bubble.querySelector(".wellness-footer"), "the footer still shows");
    assert.equal(bubble.querySelector(".wellness-show-numbers"), null);
    // An older app: AndroidBridge only (its wellnessSign is no signer any more).
    window.AndroidBridge = { wellnessOpenSettings() {}, wellnessSign: () => PROOF };
    const again = maskedBubble().bubble;
    assert.equal(again.querySelector(".wellness-show-numbers"), null);
});

test("inside the app a masked reply offers Show numbers; an unmasked one does not", (t) => {
    reset(t);
    installChannel(fakeBridge());
    assert.ok(maskedBubble().bubble.querySelector("button.wellness-show-numbers"));
    assert.equal(maskedBubble({ content: "was 47 bpm" }).bubble.querySelector(".wellness-show-numbers"), null);
    const noRef = maskedBubble({ wellness_ref: undefined, wellness_used: undefined }).bubble;
    assert.equal(noRef.querySelector(".wellness-footer"), null, "no ref, nothing to show");
    // A masked turn with a ref but no frame (a save that came back masked) still offers it.
    const refOnly = maskedBubble({ wellness_used: undefined }).bubble;
    assert.ok(refOnly.querySelector(".wellness-show-numbers"));
    assert.equal(refOnly.querySelector(".wellness-footer-text"), null);
});

test("Show numbers: a signed GET /wellness/ref/{ref}?operator=, then a private panel of display strings", async (t) => {
    reset(t);
    const bridge = fakeBridge();
    installChannel(bridge);
    const calls = stubFetch((url) => url === REF_PATH ? reply(SUMMARY) : reply({}, 404));
    const before = JSON.stringify({ ...localStorage });
    const { bubble, entry } = maskedBubble();
    const copyText = bubble._bbxCopyText;
    const button = bubble.querySelector(".wellness-show-numbers");
    button.click();
    await loaded(bubble);
    // The app signs a GET only when it names the Portal's operator (WellnessChatRules.bridgeOperator).
    assert.deepEqual(bridge.signed, [{ method: "GET", path: REF_PATH, hex: EMPTY_SHA256 }]);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, REF_PATH, "the request is the one that was signed");
    assert.equal(calls[0].opts.headers["X-BlackBox-Origin"], PROOF);
    const panel = bubble.querySelector(".wellness-numbers");
    assert.ok(panel, "the panel opens");
    const text = panel.textContent;
    assert.match(text, /last night/);
    assert.match(text, /As of /);
    assert.match(text, /Sleeping heart rate: 47 bpm · below your usual range, a little less · worn all night/);
    assert.match(text, /Steps: 12,345 steps · worn 14 h/);
    assert.match(text, /Not recorded: sleep duration on 2026-10-28 \(watch probably not worn\)/);
    assert.ok(!text.includes("47.3"), "display strings, not raw values");
    assert.equal(button.textContent, "Hide numbers");
    assert.equal(button.getAttribute("aria-expanded"), "true");
    // Never persisted: not in local storage, the history entry or the copy text.
    assert.equal(JSON.stringify({ ...localStorage }), before);
    assert.ok(!JSON.stringify(entry).includes("47 bpm"));
    assert.equal(bubble._bbxCopyText, copyText);
    button.click();
    assert.equal(bubble.querySelector(".wellness-numbers"), null, "a second click hides them");
    assert.equal(button.textContent, "Show numbers");
});

test("Show numbers keeps the envelope's strings as text", async (t) => {
    reset(t);
    installChannel(fakeBridge());
    const hostile = structuredClone(SUMMARY);
    hostile.payload.period.label = XSS;
    hostile.payload.metrics[0].display = XSS;
    hostile.payload.metrics[0].label = XSS;
    hostile.payload.gaps[0].reason = XSS;
    stubFetch(() => reply(hostile));
    const { bubble } = maskedBubble();
    bubble.querySelector(".wellness-show-numbers").click();
    await loaded(bubble);
    const panel = bubble.querySelector(".wellness-numbers");
    assert.ok(panel.textContent.includes(XSS));
    assert.deepEqual(scriptVectors(bubble), [], bubble.innerHTML);
    assert.equal(panel.querySelector("img"), null);
});

for (const status of [403, 404, 401, 503]) {
    test(`Show numbers: ${status} says Not available`, async (t) => {
        reset(t);
        installChannel(fakeBridge());
        stubFetch(() => reply({ error: "SENTINEL-CODE" }, status));
        const { bubble } = maskedBubble();
        bubble.querySelector(".wellness-show-numbers").click();
        await loaded(bubble);
        const panel = bubble.querySelector(".wellness-numbers");
        assert.match(panel.textContent, /Not available/);
        assert.ok(!panel.textContent.includes("SENTINEL"));
    });
}

test("Show numbers: a refused signature (null) never sends the request", async (t) => {
    reset(t);
    installChannel(fakeBridge({ sign: () => null }));
    const calls = stubFetch(() => reply(SUMMARY));
    const { bubble } = maskedBubble();
    bubble.querySelector(".wellness-show-numbers").click();
    await loaded(bubble);
    assert.equal(calls.length, 0);
    assert.match(bubble.querySelector(".wellness-numbers").textContent, /Not available/);
});

test("Show numbers: a network failure says so, and a reply for another ref is not shown", async (t) => {
    reset(t);
    installChannel(fakeBridge());
    stubFetch(() => Promise.reject(new TypeError("Failed to fetch")));
    const { bubble } = maskedBubble();
    bubble.querySelector(".wellness-show-numbers").click();
    await loaded(bubble);
    assert.match(bubble.querySelector(".wellness-numbers").textContent, /Couldn't load the numbers/);
    closeWellnessNumbers();
    stubFetch(() => reply({ ...SUMMARY, ref: "w-20261029-ffff" }));
    bubble.querySelector(".wellness-show-numbers").click();
    await loaded(bubble);
    assert.match(bubble.querySelector(".wellness-numbers").textContent, /Not available/);
    assert.ok(!bubble.querySelector(".wellness-numbers").textContent.includes("47 bpm"));
});

for (const [name, fire] of Object.entries({
    pagehide: () => window.dispatchEvent(new Event("pagehide")),
    "an operator switch": () => window.dispatchEvent(new CustomEvent("bbx:operator-changed")),
    "the page going to the background": () => {
        Object.defineProperty(document, "hidden", { value: true, configurable: true });
        document.dispatchEvent(new Event("visibilitychange"));
        Object.defineProperty(document, "hidden", { value: false, configurable: true });
    },
    popstate: () => window.dispatchEvent(new Event("popstate")),
})) {
    test(`the numbers panel closes on ${name}`, async (t) => {
        reset(t);
        installChannel(fakeBridge());
        stubFetch(() => reply(SUMMARY));
        const { bubble } = maskedBubble();
        bubble.querySelector(".wellness-show-numbers").click();
        await loaded(bubble);
        assert.ok(bubble.querySelector(".wellness-numbers"));
        fire();
        assert.equal(bubble.querySelector(".wellness-numbers"), null);
        assert.equal(bubble.querySelector(".wellness-show-numbers").textContent, "Show numbers");
    });
}

test("a reply that lands after an operator switch is dropped", async (t) => {
    reset(t);
    installChannel(fakeBridge());
    let release;
    stubFetch(() => new Promise((r) => { release = () => r({ ok: true, status: 200, json: () => Promise.resolve(SUMMARY) }); }));
    const { bubble } = maskedBubble();
    bubble.querySelector(".wellness-show-numbers").click();
    await waitFor(() => typeof release === "function", "the request");
    state.setOperator("wellness-fixture-peer");
    t.after(() => state.setOperator("wellness-fixture"));
    release();
    await waitFor(() => !bubble.querySelector(".wellness-numbers"), "the panel to close");
    assert.ok(!bubble.textContent.includes("47 bpm"));
});

// ── The envelope shapes (refPanel, pure) ───────────────────────────────────

test("refPanel: a series lists each point's display string and coverage", () => {
    const p = refPanel({ ref: REF, payload: {
        ok: true, tool: "wellness_series", metric: "steps", window: "the last 7 days", as_of: null, stale: true,
        granularity_used: "day",
        points: [{ t: "2026-10-26", value: 12345, display: "12,345 steps", coverage_pct: 100 },
                 { t: "2026-10-27", value: null, display: null, coverage_pct: 0 }],
    } }, { now: NOW, ...ZONE });
    assert.equal(p.title, "steps · the last 7 days");
    assert.deepEqual(p.lines, ["As of: not synced yet", "2026-10-26: 12,345 steps (100% covered)", "2026-10-27: not recorded (0% covered)"]);
});

test("refPanel: sessions and the history table", () => {
    const s = refPanel({ ref: REF, payload: {
        ok: true, tool: "wellness_sessions", kind: "exercise", window: "the last 30 days", as_of: "2026-10-29T07:10-04:00",
        stale: true,
        // The fixture's run (generate_fixtures.py: 18:00-18:45, 7210 m).
        sessions: [{ kind: "exercise", type: "run", start: "2026-10-27T18:00-04:00", end: "2026-10-27T18:45-04:00",
                     duration: "45 min", distance: "7.2 km", avg_hr_display: "47 bpm", hr_coverage_pct: 100 }],
    } }, { now: NOW, ...ZONE });
    assert.equal(s.title, "exercise sessions · the last 30 days");
    assert.deepEqual(s.lines, ["As of 07:10 (not synced recently)",
        "run, 27 Oct, 18:00 to 27 Oct, 18:45 · 45 min · 7.2 km · average heart rate 47 bpm · heart rate coverage 100%"]);
    const h = refPanel({ ref: REF, payload: {
        ok: true, tool: "wellness_history", start: "2026-10-26", end: "2026-10-27", as_of: "2026-10-29T07:10-04:00",
        columns: ["day", "steps", "sleeping_hr_median", "night_worn", "day_wear_h"],
        units: { steps: "steps", sleeping_hr_median: "bpm" },
        rows: [["2026-10-26", 12345, 47.3, 1, 14], ["2026-10-27", null, null, 0, null]],
    } }, { now: NOW, ...ZONE });
    assert.equal(h.title, "2026-10-26 to 2026-10-27");
    assert.deepEqual(h.lines.slice(1), [
        "2026-10-26: steps 12345 steps, sleeping hr median 47.3 bpm, worn at night, worn 14 h by day",
        "2026-10-27: steps not recorded, sleeping hr median not recorded, not worn at night",
    ]);
});

test("refPanel: an error envelope or junk has nothing to show", () => {
    for (const bad of [null, {}, { payload: null }, { payload: { ok: false, error: "no_data" } }, { payload: "x" }]) {
        assert.deepEqual(refPanel(bad), { title: "", lines: [] }, JSON.stringify(bad));
    }
});

test("the Portal's health constants match the box's", () => {
    const py = (name) => readFileSync(resolve(REPO_ROOT, "Orchestrator", "wellness", name), "utf8");
    assert.ok(py("mask.py").includes(`\nMASK = "${WELLNESS_MASK}"\n`), "the ledger's mask");
    assert.match(py("origin_proof.py"), /^HEADER = "X-BlackBox-Origin"$/m);
    assert.equal(ORIGIN_HEADER, "X-BlackBox-Origin");
    // Refs: "w-<day>-" + token_hex(4) (8 hex), or token_hex(8) after 20 collisions;
    // older deliveries carry 4 hex, and the Portal accepts 4 to 16 like the app.
    const api = py("api.py");
    assert.ok(api.includes('ref = f"w-{day}-{secrets.token_hex(4)}"') && api.includes('return f"w-{day}-{secrets.token_hex(8)}"'));
    for (const ref of ["w-20261029-7f3a", "w-20261029-7f3a9b0c", "w-20261029-7f3a9b0c7f3a9b0c"]) assert.equal(isMaskedHealthTurn(WELLNESS_MASK, ref), true);
    // The provider names the box gives its destinations.
    const block = py("destinations.py").match(/^PROVIDER_LABELS = \{([^}]*)\}/m);
    assert.ok(block, "destinations.PROVIDER_LABELS");
    const labels = Object.fromEntries([...block[1].matchAll(/"([^"]+)": "([^"]+)"/g)].map((m) => [m[1], m[2]]));
    assert.deepEqual(Object.keys(labels).sort(), ["anthropic", "google", "local", "openai", "xai"]);
    for (const [id, label] of Object.entries(labels)) assert.equal(providerLabel(id), label, id);
});

// ── A POST /chat turn's health use (result_data) ───────────────────────────

test("wellnessFromReply: the first ref is the message's, every ref kept, the frame for the footer", () => {
    const second = "w-20261029-a1b2";
    // tasks.process_chat_task: wellness_used = the newest delivery's frame,
    // wellness_refs = every ref of the turn (oldest first), wellness_ref = the first.
    const got = wellnessFromReply({ reply: "x", wellness_used: { ...USED, ref: second, categories: ["activity"] },
        wellness_refs: [REF, second], wellness_ref: REF });
    assert.equal(got.wellness_ref, REF);
    assert.deepEqual(got.wellness_used, { ref: REF, refs: [REF, second], categories: ["activity"],
        as_of: USED.as_of, provider: "anthropic", provider_label: "Anthropic", source_label: null });
    const one = wellnessFromReply({ wellness_used: USED, wellness_refs: [REF], wellness_ref: REF });
    assert.deepEqual([one.wellness_ref, one.wellness_used.refs], [REF, [REF]]);
    const refOnly = wellnessFromReply({ wellness_refs: [REF], wellness_ref: REF });
    assert.deepEqual(refOnly, { wellness_ref: REF, wellness_used: null }, "refs without a frame: Show numbers, no footer");
    for (const none of [null, "x", {}, { reply: "Hello." }, { wellness_ref: XSS, wellness_refs: [XSS, 7], wellness_used: { ref: XSS } }]) {
        assert.equal(wellnessFromReply(none), null, JSON.stringify(none));
    }
    const many = wellnessFromReply({ wellness_refs: Array.from({ length: 12 }, (_, i) => `w-20261029-${String(i).padStart(4, "0")}`) });
    assert.equal(many.wellness_ref, "w-20261029-0000");
});

// ── A snapshot reloaded from the ledger ────────────────────────────────────

/** A minted snapshot's text (Orchestrator/monitoring.render_snapshot_body_v71), its log as given. */
function snapshotText({ gauge = "sleep,heart · night of 2026-10-28 · ref w-20261029-7f3a · values not recorded", log } = {}) {
    return "=== START SNAPSHOT — UTC 2026-10-29T11:20:00Z — SNAP-20261029-0042 (7.1.0) ===\n"
        + "CROSS-FILE BEACON\n\nGAUGES\nCONTINUITY: auto\nDRIFT: green\nMODEL: m-1\n"
        + (gauge === null ? "" : `WELLNESS: ${gauge}\n`)
        + "OPERATOR: wellness-fixture\nMODE: Normal\n\nSNAPSHOT BODY\n\nRaw Session Log\n"
        + (log || "- [1] 2026-10-29T11:19:00Z operator=wellness-fixture user: how did I sleep?\n"
            + "- [2] 2026-10-29T11:19:30Z operator=wellness-fixture assistant: Your sleeping heart rate was [·] bpm.")
        + "\nRelease Notes\n- Hybrid context window in effect; gauges record drift and token usage.\n\n"
        + "=== END SNAPSHOT — SNAP-20261029-0042 — UTC 2026-10-29T11:20:00Z ===\n";
}

test("wellnessGaugeRefs: the refs of the header's WELLNESS gauge, as the box reads it", () => {
    assert.deepEqual(wellnessGaugeRefs(snapshotText()), [REF]);
    assert.deepEqual(wellnessGaugeRefs(snapshotText({ gauge: "activity,sleep · last night; today · ref w-20261029-7f3a, w-20261029-a1b2c3d4 · values not recorded" })),
        [REF, "w-20261029-a1b2c3d4"]);
    assert.deepEqual(wellnessGaugeRefs(snapshotText().replace(/\n/g, "\r\n")), [REF], "CRLF");
    // A hidden turn (redact.hide), an untagged one, a gauge with no ref.
    assert.deepEqual(wellnessGaugeRefs(snapshotText({ gauge: "[wellness conversation · not shared]" })), []);
    assert.deepEqual(wellnessGaugeRefs(snapshotText({ gauge: null })), []);
    assert.deepEqual(wellnessGaugeRefs(snapshotText({ gauge: "sleep · health data · ref none · values not recorded" })), []);
    assert.deepEqual(wellnessGaugeRefs(snapshotText({ gauge: `sleep · x · ref ${XSS}, w-1, w-20261029-ZZZZ, ../w · values not recorded` })), []);
    for (const junk of [null, 7, "", "WELLNESS: ", { text: "x" }]) assert.deepEqual(wellnessGaugeRefs(junk), []);
});

test("wellnessGaugeRefs: a gauge line typed into the session log is anyone's text, not a gauge", () => {
    const planted = snapshotText({ gauge: null,
        log: "- [1] 2026-10-29T11:19:00Z operator=wellness-fixture user: WELLNESS: sleep · x · ref w-20261029-dead · values not recorded\n"
            + "- [2] 2026-10-29T11:19:30Z operator=wellness-fixture assistant: [·]" });
    assert.deepEqual(wellnessGaugeRefs(planted), []);
});

test("snapshotNumbersFooter: inside the app, a masked snapshot whose gauge names refs; nothing otherwise", (t) => {
    reset(t);
    assert.equal(snapshotNumbersFooter(snapshotText()), null, "a desktop browser");
    installChannel(fakeBridge());
    const footer = snapshotNumbersFooter(snapshotText());
    assert.ok(footer, "the owner's masked snapshot");
    assert.ok(footer.querySelector("button.wellness-show-numbers"));
    assert.ok(!/\d{2} bpm|47/.test(footer.textContent), "no value");
    const hidden = snapshotText({ gauge: "[wellness conversation · not shared]",
        log: "- [1] 2026-10-29T11:19:00Z operator=wellness-fixture user: [wellness conversation · not shared]" });
    assert.equal(snapshotNumbersFooter(hidden), null, "a hidden turn");
    const unmasked = snapshotText({ log: "- [1] 2026-10-29T11:19:00Z operator=wellness-fixture user: how did I sleep?" });
    assert.equal(snapshotNumbersFooter(unmasked), null, "no masked value, nothing to show");
});

test("snapshotNumbersFooter: Show numbers loads every gauge ref, signed, and closes with its root", async (t) => {
    reset(t);
    const second = "w-20261029-a1b2";
    const bridge = fakeBridge();
    installChannel(bridge);
    const calls = stubFetch((url) => (url.startsWith("/wellness/ref/") ? reply({ ...SUMMARY, ref: decodeURIComponent(url.split("/")[3].split("?")[0]) })
        : reply({}, 404)));
    const root = document.createElement("div");
    document.body.appendChild(root);
    t.after(() => root.remove());
    const footer = snapshotNumbersFooter(snapshotText({ gauge: `sleep · last night · ref ${REF}, ${second} · values not recorded` }));
    root.append(footer);
    footer.querySelector(".wellness-show-numbers").click();
    await loaded(root);
    assert.deepEqual(calls.map((c) => c.url), [REF_PATH, `/wellness/ref/${second}?operator=wellness-fixture`]);
    assert.ok(calls.every((c) => c.opts.headers["X-BlackBox-Origin"] === PROOF));
    assert.match(root.querySelector(".wellness-numbers").textContent, /Sleeping heart rate: 47 bpm/);
    closeWellnessNumbersIn(root);
    assert.equal(root.querySelector(".wellness-numbers"), null);
    assert.equal(footer.querySelector(".wellness-show-numbers").textContent, "Show numbers");
    closeWellnessNumbersIn(null);                       // nothing to close: no throw
});

test("chat-bubbles.js still never hands server text to the HTML parser", () => {
    const code = stripComments(readFileSync(resolve(PORTAL, "modules", "chat-bubbles.js"), "utf8").replace(/\r\n/g, "\n"));
    assert.deepEqual(innerHtmlAssignments(code).filter((a) => !isSafeRhs(a.rhs)), []);
    assert.deepEqual(rawHtmlApiUses(code), []);
});
