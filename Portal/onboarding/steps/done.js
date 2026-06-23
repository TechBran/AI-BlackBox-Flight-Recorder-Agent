// Done step — final screen of the onboarding wizard.
// Renders a summary of what was configured/skipped, then a single CTA
// that POSTs /onboarding/complete (writes the sentinel that lets
// FirstRunMiddleware stop redirecting /ui) and navigates to Portal.
//
// Visual: editorial gravitas. Sigil "07" + "DONE" label. Big Fraunces
// title "All set." with italic-red on a key word. Summary list with
// status pips. Big "Open Portal →" CTA. The customer has crossed a
// threshold; the system is now theirs.

let busy = false;

export async function render(container, { next, back, skip, sigil }) {
    container.innerHTML = `
        <section class="ob-step ob-done">
            <aside class="ob-step-sigil" aria-hidden="true">
                <div class="ob-step-sigil-num"><em>${sigil ? sigil.num : "08"}</em></div>
                <div class="ob-step-sigil-rule"></div>
                <div class="ob-step-sigil-label">DONE</div>
            </aside>
            <div class="ob-step-body">
                <div class="ob-step-eyebrow">
                    <span class="ob-step-eyebrow-dot" aria-hidden="true"></span>
                    Setup complete
                </div>
                <h1 class="ob-step-title">
                    Your <em>BlackBox</em> is ready.
                </h1>
                <p class="ob-step-lede">
                    Here's what you configured. You can change any of this from
                    the System Menu later.
                </p>
                <div id="ob-done-summary" class="ob-done-summary">
                    <div class="ob-loading">Building summary&hellip;</div>
                </div>
                <!-- T9: update-available badge — conditionally rendered after status fetch -->
                <div id="ob-done-updates" class="ob-done-updates" hidden></div>
                <div class="ob-cta-row">
                    <button type="button" class="ob-cta ob-cta-large" id="ob-done-open" disabled>
                        Open Portal <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                    </button>
                    <button type="button" class="ob-cta ob-cta-restart" id="ob-done-restart" hidden>
                        <span class="ob-cta-restart-label">Restart Service</span>
                        <span class="ob-cta-arrow" aria-hidden="true">&#x21bb;</span>
                    </button>
                    <button type="button" class="ob-cta-secondary" id="ob-view-logs-btn">
                        View Logs
                    </button>
                    <span class="ob-restart-status" id="ob-done-restart-status" hidden></span>
                </div>
                <nav class="ob-step-nav" aria-label="Step navigation">
                    <button type="button" class="ob-back" id="ob-done-back">
                        <span aria-hidden="true">&larr;</span> Back to ${sigil && sigil.backLabel ? sigil.backLabel.toLowerCase() : "operator setup"}
                    </button>
                </nav>
            </div>
        </section>
    `;

    document.getElementById("ob-done-back").addEventListener("click", back);

    // Fetch summary data + render
    const summary = await loadSummary();
    renderSummary(container, summary);

    // Wire the Open Portal CTA
    const openBtn = document.getElementById("ob-done-open");
    openBtn.disabled = false;  // enable once summary loaded
    openBtn.addEventListener("click", () => completeAndOpen(openBtn));

    // E9: status-aware Restart Service button. Probe drift detection — if
    // any .env value differs from the running process's in-memory constant,
    // the customer's wizard changes haven't taken effect yet for chat
    // handlers. Show the actionable restart button in that case; otherwise
    // show the passive "up to date" indicator.
    initRestartButton();

    // E10: View Logs button — opens a console-style modal streaming live
    // blackbox.service logs via SSE. Advanced users + customer-support
    // diagnostic affordance.
    const logsBtn = document.getElementById("ob-view-logs-btn");
    if (logsBtn) logsBtn.addEventListener("click", openLogsModal);

    // T9: update-available badge. Best-effort — failures stay silent so a
    // git-not-initialized or rate-limited /update/status never blocks the
    // happy "you're done!" path.
    initUpdatesBadge();
}

// ── T9: update-available badge ─────────────────────────────────────────
// Fetches /update/status (cached 60s server-side). Renders nothing if:
//   - git not initialized (fresh install case — no update mechanism yet)
//   - commits_behind === 0 (already current)
//   - status fetch fails (no point alarming the user during onboarding)
// Renders the "N updates available" banner with Install/Skip buttons if
// 1-10 commits behind, or "Major update available — install later from
// System Menu" if >10 (audit I3 — avoid alarming first-run customers).
async function initUpdatesBadge() {
    let status;
    try {
        const r = await fetch("/update/status");
        if (!r.ok) return;
        status = await r.json();
    } catch (_) {
        return;
    }
    if (!status.git_initialized) return;
    if (!status.commits_behind) return;

    const host = document.getElementById("ob-done-updates");
    if (!host) return;
    host.hidden = false;

    const n = status.commits_behind;
    const isMajor = n > 10;
    host.innerHTML = isMajor
        ? `
            <div class="ob-update-banner ob-update-banner-major">
                <strong>Major update available</strong>
                <p>${n} commits behind. Recommended to install from System Menu after onboarding completes — gives you time to test it.</p>
            </div>
        `
        : `
            <div class="ob-update-banner">
                <strong>⬆ ${n} update${n === 1 ? "" : "s"} available since this install</strong>
                <p>Apply now (restarts service, ~60–90s) or skip and use System Menu later.</p>
                <div class="ob-update-banner-actions">
                    <button type="button" class="ob-cta-secondary" id="ob-done-update-install">Install Now</button>
                    <button type="button" class="ob-link-button" id="ob-done-update-skip">Skip — install later</button>
                </div>
            </div>
        `;

    if (!isMajor) {
        document.getElementById("ob-done-update-install").addEventListener("click", async () => {
            try {
                const r = await fetch("/update/start", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ confirm_sha: status.latest_sha }),
                });
                if (!r.ok) {
                    const t = await r.text();
                    window.alert("Couldn't start update: " + t.substring(0, 200));
                    return;
                }
                const { task_id } = await r.json();
                const mod = await import("/ui/modules/updates-manager.js");
                mod.openUpdateLogModal(task_id);
            } catch (e) {
                window.alert("Network error: " + e.message);
            }
        });
        document.getElementById("ob-done-update-skip").addEventListener("click", () => {
            host.hidden = true;
        });
    }
}

async function loadSummary() {
    const data = { config: null, state: null, error: null };
    try {
        const [configR, stateR] = await Promise.all([
            fetch("/onboarding/current-config"),
            fetch("/onboarding/state"),
        ]);
        if (configR.ok) data.config = await configR.json();
        if (stateR.ok) data.state = await stateR.json();
    } catch (e) {
        data.error = e.message;
    }
    return data;
}

function renderSummary(container, { config, state, error }) {
    const summaryEl = container.querySelector("#ob-done-summary");
    if (error || !config || !state) {
        summaryEl.innerHTML = `
            <p class="ob-step-helper">
                Couldn't load the summary (${escapeHtml(error || "unknown error")}).
                Setup is still complete &mdash; clicking Open Portal works.
            </p>
        `;
        return;
    }

    const skipped = new Set(state.skipped_steps || []);
    const completed = new Set(state.completed_steps || []);
    const rows = [];

    // Tailscale row
    if (skipped.has("tailscale")) {
        rows.push(summaryRow("Tailscale", "skip", "LAN-only mode"));
    } else if (config.tailscale && config.tailscale.configured) {
        const host = (config.tailscale.detail && config.tailscale.detail.hostname) || "configured";
        rows.push(summaryRow("Tailscale", "ok", host));
    } else {
        rows.push(summaryRow("Tailscale", "warn", "not configured"));
    }

    // API keys row — count present LLM providers
    const llmKeys = ["openai", "anthropic", "google"];
    const presentLLM = llmKeys.filter(k => config.providers?.[k]?.present);
    if (skipped.has("api_keys") && presentLLM.length === 0) {
        rows.push(summaryRow("AI providers", "skip", "no keys yet"));
    } else if (presentLLM.length > 0) {
        const labels = presentLLM.map(k => k.charAt(0).toUpperCase() + k.slice(1)).join(", ");
        rows.push(summaryRow("AI providers", "ok", `${presentLLM.length} configured · ${labels}`));
    } else {
        rows.push(summaryRow("AI providers", "warn", "no keys configured"));
    }

    // Optional integrations (Google Workspace) row
    if (skipped.has("optional_integrations")) {
        rows.push(summaryRow("Optional integrations", "skip", "configure later"));
    } else if (config.providers?.gmail?.present) {
        rows.push(summaryRow("Google Workspace", "ok", "OAuth client configured"));
    } else {
        rows.push(summaryRow("Optional integrations", "skip", "none configured"));
    }

    // Phone pairing row — E15b (Brandon 2026-05-17): also honor the "I've
    // already paired this phone" manual completion (E14). Customer who clicked
    // that button doesn't add an entry to paired_devices registry (the
    // registry only logs OAuth-style /pair/claim handshakes), but they DO
    // mark the step complete via /onboarding/step/complete. So the summary
    // should reflect their self-reported intent in addition to the registry.
    const pairedCount = (config.paired_devices || []).length;
    if (skipped.has("pair_phone")) {
        rows.push(summaryRow("Phone pairing", "skip", "pair later from System Menu"));
    } else if (pairedCount > 0) {
        const names = config.paired_devices.map(d => d.hostname || d.device_kind || "device").join(", ");
        rows.push(summaryRow("Phone pairing", "ok", `${pairedCount} paired · ${names}`));
    } else if (completed.has("pair_phone")) {
        // E14 manual mark-complete path — no registry entry but step IS done
        rows.push(summaryRow("Phone pairing", "ok", "marked as paired"));
    } else {
        rows.push(summaryRow("Phone pairing", "skip", "no devices paired"));
    }

    // Operators row
    const operators = config.operators || [];
    if (operators.length > 0) {
        rows.push(summaryRow("Operators", "ok", `${operators.length} · ${operators.join(", ")}`));
    } else {
        rows.push(summaryRow("Operators", "warn", "none registered"));
    }

    summaryEl.innerHTML = `
        <ul class="ob-summary-list">
            ${rows.join("")}
        </ul>
    `;
}

function summaryRow(label, status, detail) {
    const glyph = status === "ok" ? "&check;" : (status === "skip" ? "&#8856;" : "!");
    return `
        <li class="ob-summary-row ob-summary-${status}">
            <span class="ob-summary-glyph" aria-hidden="true">${glyph}</span>
            <span class="ob-summary-label">${escapeHtml(label)}</span>
            <span class="ob-summary-detail">${escapeHtml(detail)}</span>
        </li>
    `;
}

async function completeAndOpen(btn) {
    if (busy) return;
    busy = true;
    btn.disabled = true;
    const orig = btn.innerHTML;
    btn.innerHTML = "Finalizing&hellip;";

    try {
        // POST /complete writes the sentinel — FirstRunMiddleware respects it
        const r = await fetch("/onboarding/complete", { method: "POST" });
        if (!r.ok) {
            throw new Error(`/onboarding/complete returned ${r.status}`);
        }
        // Brief moment for the sentinel write to settle
        await new Promise(resolve => setTimeout(resolve, 250));
        // Navigate to Portal — FirstRunMiddleware now sees the sentinel and lets us through
        location.href = "/ui";
    } catch (e) {
        btn.innerHTML = orig;
        btn.disabled = false;
        const summaryEl = document.getElementById("ob-done-summary");
        if (summaryEl) {
            const err = document.createElement("p");
            err.className = "ob-step-helper ob-summary-error";
            err.textContent = `Couldn't finalize setup: ${e.message}. Try again.`;
            summaryEl.appendChild(err);
        }
    } finally {
        busy = false;
    }
}

// ── E9: status-aware Restart Service button ─────────────────────────────
// Three states:
//   A — up to date: passive "Service up to date ✓" text, no button
//   B — needs restart: visible amber button + helper text
//   C — restarting: disabled spinner button, polling /health
//
// On click: POST /onboarding/restart (fire-and-forget — the restart will
// SIGTERM the service mid-response). Wait 5s, then poll /health every 2s
// for up to 120s. When it returns 200, poll /restart-status until drift
// clears, show "Restarted ✓" briefly, then fade back to State A.

let restartBusy = false;

async function initRestartButton() {
    const btn = document.getElementById("ob-done-restart");
    const statusEl = document.getElementById("ob-done-restart-status");
    if (!btn || !statusEl) return;

    try {
        const r = await fetch("/onboarding/restart-status");
        if (!r.ok) {
            // Endpoint missing or errored — silently hide the button.
            // Don't block the customer from clicking Open Portal.
            return;
        }
        const data = await r.json();
        renderRestartState(btn, statusEl, data);
    } catch (e) {
        // Network error — silently skip. Wizard finalize still works.
        console.warn("restart-status probe failed:", e);
    }
}

function renderRestartState(btn, statusEl, data) {
    // E11 followup (Brandon 2026-05-17): button is ALWAYS visible + clickable.
    // Customer should be able to restart anytime, not only when drift is detected
    // (covers cases where they edit .env directly, or just want a fresh boot for
    // sanity). Status text differentiates the two cases — actionable warn vs
    // passive 'up to date' — but the button itself is always pressable.
    btn.hidden = false;
    btn.disabled = false;
    btn.classList.remove("ob-cta-restart-done");
    const label = btn.querySelector(".ob-cta-restart-label");
    if (label) label.textContent = "Restart Service";
    statusEl.hidden = false;

    if (data && data.needs_restart) {
        // State B: actionable — amber warn styling on both button + status
        btn.classList.remove("ob-cta-restart-passive");
        btn.classList.add("ob-cta-restart-warn");
        statusEl.classList.remove("ob-restart-status-passive", "ob-restart-status-done");
        statusEl.classList.add("ob-restart-status-warn");
        // Compose human-readable reason from the drift list when available
        const drifted = (data.drifted_keys || []).length;
        statusEl.textContent = drifted > 0
            ? `${drifted} setting${drifted === 1 ? "" : "s"} changed — restart so they take effect`
            : "Settings changed — restart so they take effect";
    } else {
        // State A: passive — neutral styling, but button still clickable
        btn.classList.remove("ob-cta-restart-warn");
        btn.classList.add("ob-cta-restart-passive");
        statusEl.classList.remove("ob-restart-status-warn", "ob-restart-status-done");
        statusEl.classList.add("ob-restart-status-passive");
        statusEl.innerHTML = "Service up to date &check;";
    }

    // Wire (idempotent — replace any prior handler). Done in both states
    // because the button is now always clickable.
    btn.onclick = () => doRestart(btn, statusEl);
}

async function doRestart(btn, statusEl) {
    if (restartBusy) return;
    restartBusy = true;

    // E9 followup (Brandon's MSO2 Ultra report 2026-05-17): customer clicked
    // button, nothing happened. Journal confirmed /onboarding/restart was
    // never hit. Suspected root cause: if any State-C UI mutation threw
    // BEFORE the fetch (e.g., btn.querySelector returns null in some render
    // state, .textContent on null throws TypeError), the function would
    // exit early WITHOUT entering try/finally, leaving restartBusy=true
    // permanently. All subsequent clicks then no-op silently. Defensive fix:
    // wrap ALL state mutations inside the try block so finally always runs.

    try {
        // State C: restarting (mutations inside try so finally always runs)
        btn.disabled = true;
        const label = btn.querySelector(".ob-cta-restart-label");
        if (label) label.textContent = "Restarting service…";
        statusEl.classList.remove("ob-restart-status-warn", "ob-restart-status-done");
        statusEl.classList.add("ob-restart-status-passive");
        statusEl.textContent = "This takes about 60 to 90 seconds. The page will reconnect automatically.";

        // Fire-and-forget — the response may not arrive (server SIGTERMs mid-flight)
        try {
            await fetch("/onboarding/restart", { method: "POST" });
        } catch (e) {
            // Expected: server disconnects before responding. Continue with health poll.
            console.log("restart POST disconnected (expected):", e.message);
        }

        // Wait 5s for service to start shutting down
        await sleep(5000);

        // Poll /health every 2s for up to 120s
        const healthy = await pollHealth(120_000, 2_000);
        if (!healthy) {
            throw new Error("Service did not come back within 120 seconds");
        }

        // Confirm drift cleared via /restart-status
        const clearedDrift = await pollRestartCleared(15_000, 1_500);

        // State "done": show "Restarted ✓" briefly, then fade to State A
        const doneLabel = btn.querySelector(".ob-cta-restart-label");
        if (doneLabel) doneLabel.textContent = "Restarted";
        btn.classList.add("ob-cta-restart-done");
        statusEl.classList.remove("ob-restart-status-warn", "ob-restart-status-passive");
        statusEl.classList.add("ob-restart-status-done");
        statusEl.innerHTML = clearedDrift
            ? "Service restarted &check;"
            : "Service is back online &check;";

        await sleep(3000);
        // Re-probe and render whatever state we're in now (typically State A)
        const r = await fetch("/onboarding/restart-status");
        if (r.ok) {
            const data = await r.json();
            renderRestartState(btn, statusEl, data);
        } else {
            // Fall back to passive
            renderRestartState(btn, statusEl, { needs_restart: false });
        }
    } catch (e) {
        // Surface error inline. Customer can still click Open Portal — chat just won't pick up new keys.
        btn.disabled = false;
        const retryLabel = btn.querySelector(".ob-cta-restart-label");
        if (retryLabel) retryLabel.textContent = "Retry Restart";
        statusEl.classList.remove("ob-restart-status-passive", "ob-restart-status-done");
        statusEl.classList.add("ob-restart-status-warn");
        statusEl.textContent = `Restart didn't complete: ${e.message}. Try again or open Portal anyway.`;
    } finally {
        restartBusy = false;
    }
}

async function pollHealth(timeoutMs, intervalMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        try {
            const r = await fetch("/health", { cache: "no-store" });
            if (r.ok) return true;
        } catch (e) {
            // Service still down — keep polling
        }
        await sleep(intervalMs);
    }
    return false;
}

async function pollRestartCleared(timeoutMs, intervalMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        try {
            const r = await fetch("/onboarding/restart-status", { cache: "no-store" });
            if (r.ok) {
                const data = await r.json();
                if (!data.needs_restart) return true;
            }
        } catch (e) {
            // Try again
        }
        await sleep(intervalMs);
    }
    return false;
}

// ── E10: View Logs modal — live SSE journalctl stream ───────────────────
// Opens a full-screen modal with a dark console-style log area that
// consumes /onboarding/logs/stream via EventSource. Auto-scrolls to
// bottom on each new line UNLESS the user has scrolled up (they're
// inspecting history — don't yank them back). Close via X / Esc /
// click-outside closes the EventSource, which triggers the backend's
// CancelledError handler killing the journalctl process — no orphan
// tail subprocesses.

function openLogsModal() {
    // Create modal if not present
    let modal = document.getElementById("ob-logs-modal");
    if (modal) {
        modal.hidden = false;
        return;  // already open; just reveal
    }
    modal = document.createElement("div");
    modal.id = "ob-logs-modal";
    modal.className = "ob-logs-modal";
    modal.innerHTML = `
        <div class="ob-logs-modal-backdrop"></div>
        <div class="ob-logs-modal-panel" role="dialog" aria-label="BlackBox service logs">
            <div class="ob-logs-modal-header">
                <span class="ob-logs-modal-title">BlackBox Service Logs</span>
                <button type="button" class="ob-logs-modal-close" aria-label="Close">&times;</button>
            </div>
            <pre id="ob-logs-modal-body" class="ob-logs-modal-body"></pre>
            <div class="ob-logs-modal-footer">
                <span id="ob-logs-modal-status" class="ob-logs-status ob-logs-status-connecting">Connecting&hellip;</span>
                <span id="ob-logs-modal-count" class="ob-logs-count">0 lines</span>
                <button type="button" id="ob-logs-modal-copy" class="ob-logs-copy">Copy all</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    const body = modal.querySelector("#ob-logs-modal-body");
    const status = modal.querySelector("#ob-logs-modal-status");
    const count = modal.querySelector("#ob-logs-modal-count");
    const copy = modal.querySelector("#ob-logs-modal-copy");
    const closeBtn = modal.querySelector(".ob-logs-modal-close");
    const backdrop = modal.querySelector(".ob-logs-modal-backdrop");

    let lineCount = 0;
    let autoScroll = true;

    // Detect user-scrolled-up (don't auto-yank if they're reading history)
    body.addEventListener("scroll", () => {
        const distFromBottom = body.scrollHeight - body.scrollTop - body.clientHeight;
        autoScroll = distFromBottom < 50;  // within 50px of bottom = stay pinned
    });

    // SSE connection
    const eventSource = new EventSource("/onboarding/logs/stream?lines=200");
    eventSource.addEventListener("start", () => {
        status.textContent = "Connected";
        status.className = "ob-logs-status ob-logs-status-connected";
    });
    eventSource.onmessage = (e) => {
        body.textContent += e.data + "\n";
        lineCount++;
        count.textContent = lineCount + " lines";
        if (autoScroll) {
            body.scrollTop = body.scrollHeight;
        }
    };
    eventSource.onerror = () => {
        status.textContent = "Disconnected";
        status.className = "ob-logs-status ob-logs-status-disconnected";
    };

    function closeModal() {
        eventSource.close();
        modal.remove();
        document.removeEventListener("keydown", escHandler);
    }

    closeBtn.addEventListener("click", closeModal);
    backdrop.addEventListener("click", closeModal);
    const escHandler = (e) => { if (e.key === "Escape") closeModal(); };
    document.addEventListener("keydown", escHandler);

    copy.addEventListener("click", async () => {
        try {
            await navigator.clipboard.writeText(body.textContent);
            copy.textContent = "Copied ✓";
            setTimeout(() => { copy.textContent = "Copy all"; }, 1500);
        } catch {
            copy.textContent = "Copy failed";
        }
    });
}

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}
