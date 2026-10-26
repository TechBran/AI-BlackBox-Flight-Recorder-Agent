// The wizard/hub Restart Service button waits long enough for a restart that
// drains first (audit rank 70 review).
//
// The service used to hard-exit on SIGTERM. uvicorn now owns the signal, so a
// restart first drains open connections (--timeout-graceful-shutdown 30: a
// chat stream in another tab, the Android app, the logs modal), then systemd
// waits RestartSec=10, then the snapshot index rebuild takes 60-90 s. The
// button polled /health for 120 s after a 5 s pause and reported a restart
// that worked as "did not come back". The Updates panel (and Android) wait 180 s.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom } from "./helpers/portal-dom.mjs";

installDom();

const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
    status, headers: { "Content-Type": "application/json" },
});
const flush = () => new Promise((resolve) => setImmediate(resolve));

async function restartThatTakes(t, backAfterMs) {
    t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 1_000_000 });
    const { mountRestartControl } = await import("../../Portal/onboarding/hub-controls.js");
    let clickedAt = null;
    globalThis.fetch = window.fetch = async (url) => {
        const path = String(url);
        if (path.startsWith("/onboarding/restart-status")) return json({ needs_restart: false });
        if (path === "/onboarding/restart") throw new TypeError("connection closed");
        if (path === "/health") {
            if (Date.now() - clickedAt >= backAfterMs) return json({ status: "ok" });
            throw new TypeError("Failed to fetch");
        }
        throw new Error(`unexpected fetch ${path}`);
    };
    const btn = document.createElement("button");
    btn.innerHTML = '<span class="ob-cta-restart-label">Restart Service</span>';
    const statusEl = document.createElement("div");
    document.body.append(btn, statusEl);
    await mountRestartControl(btn, statusEl);
    clickedAt = Date.now();
    btn.click();
    // Walk the clock a second at a time until the flow ends (the button is
    // enabled again), noting what it said when the restart finished.
    let outcome = null;
    for (let elapsed = 0; elapsed < 400_000; elapsed += 1000) {
        await flush();
        if (!outcome && (statusEl.classList.contains("ob-restart-status-done")
                         || statusEl.classList.contains("ob-restart-status-warn"))) {
            outcome = statusEl.textContent;
        }
        if (!btn.disabled) break;
        t.mock.timers.tick(1000);
    }
    assert.equal(btn.disabled, false, "the restart flow never finished");
    return outcome;
}

test("a restart that drains first and is back after 150 s is not reported as failed", async (t) => {
    const text = await restartThatTakes(t, 150_000);
    assert.doesNotMatch(text, /didn't complete/);
    assert.match(text, /Service (restarted|is back online)/);
});

test("a restart that never comes back is still reported", async (t) => {
    const text = await restartThatTakes(t, Infinity);
    assert.match(text, /Restart didn't complete: Service did not come back within 180 seconds/);
});
