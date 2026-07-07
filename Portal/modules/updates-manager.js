// Portal Updates panel + SSE log modal (T8).
//
// Consumed by Portal/index.html's #menuModal → .updates-section block.
// On menu open, initUpdatesPanel() fetches /update/status and renders
// the panel. User actions (Check / Install / View Log / Rollback) wire
// to /update/* endpoints. The Install flow opens a shared modal that
// streams /update/log/stream as SSE events.
//
// The shared SSE modal (openUpdateLogModal) is exported so other modules
// (Portal/onboarding/steps/done.js — T9) can reuse it for the wizard's
// done-step "Install Now" affordance. Single source of truth for the
// "Installing... → restart in 3s → polling /health → done" UX.
//
// State machine for the panel:
//   loading                — initial fetch
//   git-not-initialized    — show [First-time setup] button (audit I9)
//   up-to-date             — neutral status, all action buttons disabled
//   updates-available      — show commits + categories + [Install] enabled
//   in-progress            — runner already going, poll until done
//   failed                 — show last error + [Rollback] enabled
//   interrupted            — recovery banner (audit C3) + manual rollback
//   error                  — fetch error itself (network down, etc.)
//
// Embeddings notification card (pluggable embeddings, Task 14): on every
// menu open this module ALSO fetches /embeddings/status and renders a card
// into #embeddingsCard when noteworthy (health superseded/broken, or a
// migration job running). [Update] POSTs /embeddings/migrate directly;
// [Manage] deep-links to /onboarding/?step=embeddings (wizard owns the full
// management surface). Any failure of that fetch hides the card and never
// breaks the updates panel.
//
// Reranker status line (read-only): the module also fetches /rerank/status and
// renders a single NON-interactive line (Reranking: ON/OFF, plus provider/model
// when it's actually in use) with its own [Manage] deep-link. It renders
// UNCONDITIONALLY whenever /rerank/status is reachable — even on a fully healthy
// box — so a silent reranker failure (enabled but preflight-down) is always
// visible. Reranker SELECTION now lives only in the onboarding wizard.
//
// Embedding compute card (WI-9/M10): below the notification card the SAME
// status payload renders a hardware line (status.hardware — GPU name/VRAM or
// "CPU only") plus a per-local-model Auto/GPU/CPU placement toggle POSTing
// /embeddings/placement {slug, placement: "gpu"|"cpu"|null}. placement (the
// persisted pin, null = auto) and recommended_placement come from each
// models[] entry. A toggle applies on the model's next embed — no restart.

import { toastError } from "./core-utils.js";

let _state = "loading";
let _lastStatus = null;  // last /update/status response
let _isInitialized = false;

/**
 * Public entry point — called by Portal/app-modular.js on menu open.
 * Idempotent: re-running on each menu-open is fine.
 */
export async function initUpdatesPanel() {
    if (!_isInitialized) {
        _wireButtons();
        _isInitialized = true;
    }
    // Fire-and-forget (it catches everything internally): the embeddings
    // card must never delay or break the updates panel itself.
    _refreshEmbeddingsCard();
    await refreshPanel();
}

async function refreshPanel(forceFresh = false) {
    _setState("loading");
    try {
        const url = forceFresh ? "/update/preflight" : "/update/status";
        const opts = forceFresh ? { method: "POST" } : {};
        const r = await fetch(url, opts);
        if (!r.ok) {
            _setState("error", { error: `HTTP ${r.status}` });
            return;
        }
        _lastStatus = await r.json();
        _renderFromStatus(_lastStatus);
    } catch (e) {
        _setState("error", { error: e.message });
    }
}

function _renderFromStatus(status) {
    if (!status.git_initialized) {
        _setState("git-not-initialized");
        return;
    }
    if (status.fetch_error) {
        _setState("error", { error: status.fetch_error });
        return;
    }
    if (status.in_progress) {
        _setState("in-progress");
        // Keep polling — runner will eventually emit complete + restart.
        setTimeout(() => refreshPanel(false), 3000);
        return;
    }
    const lastState = status.last_state;
    if (lastState) {
        if (lastState.phase === "failed") {
            _setState("failed", { lastState });
            return;
        }
        // is_interrupted is server-side computed
        if (status.last_state && status.last_state.phase
            && status.last_state.phase !== "complete"
            && status.last_state.phase !== "failed"
            && status.last_state.phase !== "rolled_back"
            && !status.in_progress) {
            _setState("interrupted", { lastState });
            return;
        }
    }
    if (status.commits_behind > 0) {
        _setState("updates-available", { status });
    } else if (status.commits_ahead > 0) {
        _setState("local-ahead", { status });
    } else {
        _setState("up-to-date", { status });
    }
}

// ── State-driven render ────────────────────────────────────────────────

function _setState(newState, ctx = {}) {
    _state = newState;
    const row = document.getElementById("updatesStatusRow");
    const list = document.getElementById("updatesCommitsList");
    const btnInstall = document.getElementById("btnInstallUpdate");
    const btnLog = document.getElementById("btnViewUpdateLog");
    const btnRollback = document.getElementById("btnRollbackUpdate");
    const btnCheck = document.getElementById("btnCheckUpdates");
    if (!row) return;  // panel not in DOM (menu modal not built yet)

    btnInstall.disabled = true;
    btnLog.disabled = true;
    btnRollback.disabled = true;

    switch (newState) {
        case "loading":
            row.textContent = "Checking for updates…";
            list.innerHTML = "";
            break;

        case "error":
            row.innerHTML = `<span class="updates-status-err">Error: ${_esc(ctx.error || "unknown")}</span>`;
            list.innerHTML = "";
            break;

        case "git-not-initialized":
            row.innerHTML = `
                <span class="updates-status-warn">Updates not yet initialized.</span>
                <button id="btnInitUpdates" class="btn btn-warn">First-time setup</button>
            `;
            list.innerHTML = "<p class='updates-hint-inline'>Run this once to enable in-place updates. Clones the BlackBox repo into .git/ without touching your data.</p>";
            const btnInit = document.getElementById("btnInitUpdates");
            if (btnInit) {
                btnInit.addEventListener("click", _onInitClick);
            }
            break;

        case "up-to-date": {
            const { status } = ctx;
            row.innerHTML = `<span class="updates-status-ok">✓ Up to date</span> <code class="updates-sha">${_esc(status.current_short)}</code>`;
            list.innerHTML = "";
            btnRollback.disabled = !status.last_state;
            break;
        }

        case "local-ahead": {
            const { status } = ctx;
            row.innerHTML = `<span class="updates-status-info">Local ahead of origin by ${status.commits_ahead} commit(s)</span> <code class="updates-sha">${_esc(status.current_short)}</code>`;
            list.innerHTML = "<p class='updates-hint-inline'>Unpushed local work. Nothing to pull from GitHub right now.</p>";
            btnRollback.disabled = !status.last_state;
            break;
        }

        case "updates-available": {
            const { status } = ctx;
            row.innerHTML = `
                <span class="updates-status-update">⬆ ${status.commits_behind} update${status.commits_behind === 1 ? "" : "s"} available</span>
                <code class="updates-sha">${_esc(status.current_short)} → ${_esc(status.latest_short)}</code>
            `;
            list.innerHTML = _renderCommits(status);
            btnInstall.disabled = false;
            btnRollback.disabled = !status.last_state;
            break;
        }

        case "in-progress":
            row.innerHTML = `<span class="updates-status-progress">⟳ Update in progress…</span>`;
            list.innerHTML = "<p class='updates-hint-inline'>The runner is updating now. Re-checking every few seconds.</p>";
            break;

        case "failed": {
            const { lastState } = ctx;
            row.innerHTML = `<span class="updates-status-err">✕ Last update failed during <code>${_esc(lastState.failed_phase || lastState.phase)}</code></span>`;
            list.innerHTML = `<pre class="updates-error">${_esc(lastState.error || "(no error message recorded)")}</pre>`;
            btnRollback.disabled = false;
            break;
        }

        case "interrupted": {
            const { lastState } = ctx;
            row.innerHTML = `<span class="updates-status-warn">⚠ Update interrupted</span>`;
            list.innerHTML = `
                <pre class="updates-error">Last phase: ${_esc(lastState.phase)}
Target SHA: ${_esc(lastState.target_sha || "unknown")}
Started: ${_esc(lastState.updated_iso || "")}</pre>
                <p class='updates-hint-inline'>The system was killed mid-update (crash, power, or kill -9). Rollback to the pre-update tag is safe — your code reverts to the last known-good SHA.</p>
            `;
            btnRollback.disabled = false;
            break;
        }
    }
}

function _renderCommits(status) {
    if (!status.commits || !status.commits.length) {
        return "";
    }
    const catsOn = Object.entries(status.categories || {})
        .filter(([k, v]) => v && k !== "code_only")
        .map(([k]) => k);
    const catsHtml = catsOn.length
        ? `<div class="updates-categories">${catsOn.map((c) => `<span class="updates-cat updates-cat-${_esc(c)}">${_esc(c)}</span>`).join(" ")}</div>`
        : "";
    const items = status.commits.slice(0, 8).map((c) => `
        <li>
            <code class="updates-commit-sha">${_esc(c.short)}</code>
            <span class="updates-commit-subject">${_esc(c.subject)}</span>
        </li>
    `).join("");
    const more = status.commits.length > 8
        ? `<li class="updates-commit-more">… and ${status.commits.length - 8} more</li>`
        : "";
    return `<ul class="updates-commits-list">${items}${more}</ul>${catsHtml}`;
}

// ── Action handlers ────────────────────────────────────────────────────

function _wireButtons() {
    const btnCheck = document.getElementById("btnCheckUpdates");
    const btnInstall = document.getElementById("btnInstallUpdate");
    const btnLog = document.getElementById("btnViewUpdateLog");
    const btnRollback = document.getElementById("btnRollbackUpdate");
    if (btnCheck) btnCheck.addEventListener("click", () => refreshPanel(true));
    if (btnInstall) btnInstall.addEventListener("click", _onInstallClick);
    if (btnLog) btnLog.addEventListener("click", _onViewLogClick);
    if (btnRollback) btnRollback.addEventListener("click", _onRollbackClick);
}

async function _onInitClick() {
    // Lazy-init by re-running install.sh's Step 0a equivalent — for now,
    // POST /update/preflight does the fetch which forces lazy-init's
    // happy path. Backend's preflight returns git_initialized=false with
    // a helpful message if it's still missing; user can SSH in for full setup.
    await refreshPanel(true);
}

async function _onInstallClick() {
    if (!_lastStatus || !_lastStatus.commits_behind) return;
    const activeSessions = _lastStatus.active_cli_sessions || 0;
    let confirm = `Install ${_lastStatus.commits_behind} update(s)?\n\nThis will restart the BlackBox service. Expect 60–90 seconds of downtime.`;
    if (activeSessions > 0) {
        confirm = `WARNING: ${activeSessions} active CLI agent session(s) will be disconnected.\n\n` + confirm;
    }
    if (!window.confirm(confirm)) return;
    try {
        const r = await fetch("/update/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ confirm_sha: _lastStatus.latest_sha }),
        });
        if (r.status === 409) {
            const body = await r.json();
            window.alert(`Update conflict: ${body.detail || "another update may be in progress."}`);
            await refreshPanel(true);
            return;
        }
        if (!r.ok) {
            window.alert(`Failed to start update: HTTP ${r.status}`);
            return;
        }
        const { task_id } = await r.json();
        openUpdateLogModal(task_id);
    } catch (e) {
        window.alert(`Network error starting update: ${e.message}`);
    }
}

function _onViewLogClick() {
    // No streaming task_id available post-restart — open a static log view
    // of the journal. Re-using /onboarding/logs/stream (existing SSE endpoint).
    // For v1 this is a passthrough; can grow later.
    window.open("/onboarding/logs/stream?lines=200", "_blank");
}

async function _onRollbackClick() {
    if (!window.confirm("Roll back to the pre-update tag and restart the service?\n\nUser data is unaffected — only application code reverts.")) return;
    try {
        const r = await fetch("/update/rollback", { method: "POST" });
        if (!r.ok) {
            const txt = await r.text();
            window.alert(`Rollback failed: ${txt.substring(0, 300)}`);
            return;
        }
        const body = await r.json();
        const tag = body.reverted_to || "(unknown)";
        // Show the same restart-detection UI as after a successful update.
        openHealthPollModal(`Rolled back to ${tag}. Restarting service…`);
    } catch (e) {
        window.alert(`Rollback error: ${e.message}`);
    }
}

// ── Embeddings notification card (pluggable embeddings, Task 14) ───────
//
// Contract (GET /embeddings/status, Orchestrator/routes/embeddings_routes.py):
//   health: { state: "ok"|"superseded"|"broken", detail, successor,
//             successor_slug }
//   job:    { state, done, total, target, error, cancel_requested } | null
// Card precedence: broken (urgent, [Manage] only, progress shown when the
// watcher's auto-migration is underway) > job running (progress + [Manage])
// > superseded ([Update] when successor_slug known + [Manage]) > hidden.

let _embedPollTimer = null;   // 5s job-progress poll; self-clears (see below)
let _embedWarnedOnce = false; // one console.warn per page load, never spam
let _placementBusy = null;    // slug whose placement POST is in flight
let _rerankStatus = null;     // last GET /rerank/status payload (read-only line; null = hide)

async function _refreshEmbeddingsCard() {
    const container = document.getElementById("embeddingsCard");
    if (!container) return;  // panel not in DOM (menu modal not built yet)
    // /rerank/status rides along (M11): additive + fail-soft. Fetched BEFORE
    // the embeddings status so a reranker hiccup can never mask an embeddings
    // error; null (older backend / unreachable) hides only the reranker block.
    _rerankStatus = await _fetchJsonSoft("/rerank/status");
    try {
        const r = await fetch("/embeddings/status", { cache: "no-store" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const status = await r.json();
        _renderEmbeddingsCard(container, status);
    } catch (e) {
        // Failure can NEVER break the updates panel: hide the card, warn once.
        _hideEmbeddingsCard(container);
        if (!_embedWarnedOnce) {
            _embedWarnedOnce = true;
            console.warn("[updates] embeddings status unavailable:", e.message);
        }
    }
}

function _hideEmbeddingsCard(container) {
    container.classList.add("hide");
    container.innerHTML = "";
    _stopEmbedPoll();
}

function _renderEmbeddingsCard(container, status) {
    const health = status.health || {};
    const job = status.job;
    const jobRunning = !!(job && job.state === "running");

    let html = "";
    if (health.state === "broken") {
        // Urgent: the watcher has already kicked off auto-migration — show
        // the what/why detail (and live progress if the job is visible).
        html = `
            <div class="embeddings-card embeddings-card-broken">
                <div class="embeddings-card-title">⚠ Search memory needs attention</div>
                <p class="embeddings-card-copy">${_esc(health.detail || "The active embedding model stopped working. Automatic recovery is migrating your search memory to a working model.")}</p>
                ${jobRunning ? `<p class="embeddings-card-progress">${_embedProgressLine(job)}</p>` : ""}
                <div class="embeddings-card-actions">
                    <button id="btnEmbeddingsManage" class="btn embeddings-manage-btn">Manage</button>
                </div>
            </div>`;
    } else if (jobRunning) {
        html = `
            <div class="embeddings-card embeddings-card-info">
                <div class="embeddings-card-title">⟳ Search memory update in progress</div>
                <p class="embeddings-card-progress">${_embedProgressLine(job)}</p>
                <div class="embeddings-card-actions">
                    <button id="btnEmbeddingsManage" class="btn embeddings-manage-btn">Manage</button>
                </div>
            </div>`;
    } else if (health.state === "superseded") {
        const successorLabel = health.successor || health.successor_slug || "a newer model";
        html = `
            <div class="embeddings-card embeddings-card-info">
                <div class="embeddings-card-title">⬆ Search memory update available</div>
                <p class="embeddings-card-copy">Your system will transfer embeddings to ${_esc(successorLabel)} in the background. Search keeps working the whole time; the switch happens automatically when it finishes and survives restarts.</p>
                <div class="embeddings-card-actions">
                    ${health.successor_slug ? `<button id="btnEmbeddingsUpdate" class="btn btn-primary">Update</button>` : ""}
                    <button id="btnEmbeddingsManage" class="btn embeddings-manage-btn">Manage</button>
                </div>
            </div>`;
    }

    // Compute card (WI-9): hardware line + per-local-model placement toggle.
    // Renders whenever the status carries the probe — independent of whether
    // anything above is noteworthy.
    html += _computeCardHtml(status);
    // Read-only reranker status line (selection moved to the wizard). Rendered
    // UNCONDITIONALLY whenever /rerank/status is reachable (rr non-null) — even
    // on a healthy box (health ok, no job) — so a silent reranker failure is
    // always visible. Returns "" when null (older backend / unreachable) → hidden.
    html += _rerankStatusLineHtml(_rerankStatus);

    if (!html) {
        _hideEmbeddingsCard(container);  // nothing noteworthy, no reranker status
        return;
    }
    container.innerHTML = html;
    container.classList.remove("hide");

    // [Manage] deep-links to the wizard. Wired via a shared class so it works
    // whether the button came from a notification card OR the always-present
    // reranker status line — a healthy box with no notification card still
    // gets a working [Manage].
    container.querySelectorAll(".embeddings-manage-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            window.location.href = "/onboarding/?step=embeddings";
        });
    });
    const btnUpdate = document.getElementById("btnEmbeddingsUpdate");
    if (btnUpdate) {
        btnUpdate.addEventListener("click",
            () => _onEmbeddingsUpdateClick(btnUpdate, health.successor_slug));
    }
    container.querySelectorAll(".embeddings-hw-segbtn").forEach((btn) => {
        btn.addEventListener("click", () => _onPlacementClick(btn));
    });

    if (jobRunning) _startEmbedPoll();
    else _stopEmbedPoll();
}

// ── Embedding compute card (WI-9/M10: hardware + placement) ────────────

function _computeCardHtml(status) {
    const hw = status.hardware;
    if (!hw) return "";  // older backend — additive contract, degrade silently
    const locals = (status.models || []).filter((m) => m.privacy === "local");

    const rows = locals.map((m) => {
        const current = m.placement || "";  // "" = auto (null persisted)
        const rec = m.recommended_placement
            ? `<span class="embeddings-hw-rec">recommended: ${_esc(m.recommended_placement.toUpperCase())}</span>`
            : "";
        const seg = ["", "gpu", "cpu"].map((value) => {
            const label = value === "" ? "Auto" : value.toUpperCase();
            const active = value === current ? " embeddings-hw-segbtn-active" : "";
            const busy = _placementBusy === m.slug ? " disabled" : "";
            return `<button type="button"
                        class="embeddings-hw-segbtn${active}"
                        data-slug="${_esc(m.slug)}" data-placement="${_esc(value)}"
                        aria-pressed="${value === current}"${busy}>${label}</button>`;
        }).join("");
        return `
            <div class="embeddings-hw-row">
                <span class="embeddings-hw-model">${_esc(m.label)}</span>
                ${rec}
                <span class="embeddings-hw-seg" role="group"
                      aria-label="Device placement for ${_esc(m.label)}">${seg}</span>
            </div>`;
    }).join("");

    return `
        <div class="embeddings-card embeddings-card-hw">
            <div class="embeddings-card-title">Embedding compute</div>
            <p class="embeddings-card-copy">${_hardwareLine(hw)}</p>
            ${rows}
        </div>`;
}

function _hardwareLine(hw) {
    let line;
    if (hw.gpu) {
        const vram = hw.vram_mb
            ? `${(hw.vram_mb / 1024).toFixed(1)} GB VRAM`
            : "VRAM unknown";
        line = `GPU: ${_esc(hw.gpu_name || "detected")} &middot; ${_esc(vram)}`;
    } else {
        line = "CPU only &mdash; no GPU detected";
    }
    if (hw.ram_mb) {
        line += ` &middot; ${(hw.ram_mb / 1024).toFixed(1)} GB RAM`;
    }
    return line;
}

async function _onPlacementClick(btn) {
    const slug = btn.dataset.slug;
    if (!slug || _placementBusy) return;
    _placementBusy = slug;
    btn.disabled = true;
    try {
        const r = await fetch("/embeddings/placement", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            // data-placement "" means Auto → null clears the persisted pin
            body: JSON.stringify({
                slug,
                placement: btn.dataset.placement || null,
            }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
    } catch (e) {
        toastError(`Could not change device placement: ${e.message}`);
    }
    _placementBusy = null;
    await _refreshEmbeddingsCard();  // re-render with the server's truth
}

// Read-only reranker status line (selection moved to the onboarding wizard).
//
// A single NON-interactive line reflecting GET /rerank/status. Rendered
// UNCONDITIONALLY by _renderEmbeddingsCard whenever the status is reachable
// (rr non-null) — NOT nested in the health-conditional notification card,
// which is empty on a healthy box exactly when a silent reranker failure
// (enabled but preflight-down → available:false) must still show. Returns ""
// when rr is null (older backend / unreachable) → fail-soft hide. Carries its
// own [Manage] deep-link (the shared .embeddings-manage-btn class), so a
// healthy box with no notification card still gets a working [Manage].
function _rerankStatusLineHtml(rr) {
    if (!rr) return "";  // endpoint unreachable / older backend — hide
    let line;
    if (rr.enabled && rr.available) {
        line = `Reranking: ON — ${_esc(rr.provider || "?")}/${_esc(rr.model || "?")}`;
    } else if (rr.enabled && !rr.available) {
        // Configured ON but preflight failed / not configured — "not in use".
        line = "Reranking: ON — not in use";
    } else {
        line = "Reranking: OFF";
    }
    return `
        <div class="embeddings-card embeddings-rerank-status">
            <div class="embeddings-card-title">${line}</div>
            <div class="embeddings-card-actions">
                <button id="btnEmbeddingsManage" class="btn embeddings-manage-btn">Manage</button>
            </div>
        </div>`;
}

// Fail-soft JSON GET: null on any non-2xx or network error (never throws).
// Used for the additive /rerank/status probe so an older backend or a blip
// hides only the reranker line, never the whole embeddings card.
async function _fetchJsonSoft(url) {
    try {
        const r = await fetch(url, { cache: "no-store" });
        if (!r.ok) return null;
        return await r.json();
    } catch (_) {
        return null;
    }
}

function _embedProgressLine(job) {
    const done = Number(job.done) || 0;
    const total = Number(job.total) || 0;
    const cancelling = job.cancel_requested ? " (cancelling…)" : "";
    return `Re-embedding ${done}/${total}…${cancelling}`;
}

async function _onEmbeddingsUpdateClick(btn, targetSlug) {
    if (!targetSlug || btn.disabled) return;
    btn.disabled = true;  // double-click guard while the POST is in flight
    try {
        const r = await fetch("/embeddings/migrate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ target: targetSlug }),
        });
        if (r.ok || r.status === 409) {
            // 200: job claimed. 409: one is already running. Either way the
            // fresh status carries the running job → card shows migrating.
            await _refreshEmbeddingsCard();
            return;
        }
        throw new Error(`HTTP ${r.status}`);
    } catch (e) {
        toastError(`Could not start embedding update: ${e.message}`);
        btn.disabled = false;
    }
}

function _startEmbedPoll() {
    if (_embedPollTimer) return;
    _embedPollTimer = setInterval(() => {
        const container = document.getElementById("embeddingsCard");
        const menu = document.getElementById("menuModal");
        // Self-clear when the card left the DOM or the menu modal closed —
        // no progress polling in the background.
        if (!container || !container.isConnected
            || !menu || menu.classList.contains("hide")) {
            _stopEmbedPoll();
            return;
        }
        _refreshEmbeddingsCard();  // catches its own errors
    }, 5000);
}

function _stopEmbedPoll() {
    if (_embedPollTimer) {
        clearInterval(_embedPollTimer);
        _embedPollTimer = null;
    }
}

// ── Shared SSE log modal (exported for done.js / wizard reuse) ─────────

/**
 * Open the update log modal, stream SSE from /update/log/stream?task_id=X,
 * and on completion transition to the health-poll restart-detection UI.
 */
export function openUpdateLogModal(taskId) {
    const modal = _ensureLogModal();
    const log = modal.querySelector(".updates-log-pre");
    const header = modal.querySelector(".updates-log-header");
    log.textContent = "";
    header.textContent = `Update in progress (task ${taskId.substring(0, 20)}…)`;
    modal.classList.remove("hide");

    let succeeded = false;
    let completeReceived = false;
    const evt = new EventSource(`/update/log/stream?task_id=${encodeURIComponent(taskId)}`);
    evt.onmessage = (e) => {
        let data;
        try { data = JSON.parse(e.data); }
        catch (_) { return; }
        if (data.type === "heartbeat") {
            // No log; just keep the connection alive.
            return;
        }
        if (data.type === "phase") {
            _appendLog(log, `\n── ${data.phase.toUpperCase()} ──`);
            return;
        }
        if (data.type === "log") {
            _appendLog(log, `  ${data.text}`);
            return;
        }
        if (data.type === "complete") {
            completeReceived = true;
            succeeded = !!data.succeeded;
            evt.close();
            _appendLog(log,
                succeeded
                    ? `\n✓ COMPLETE: ${data.sha_before} → ${data.sha_after}`
                    : `\n✕ FAILED: ${data.error || "(no error msg)"}`);
            if (succeeded) {
                // Service will restart in ~2s. Switch to health-poll modal.
                setTimeout(() => {
                    modal.classList.add("hide");
                    openHealthPollModal("Update applied. Restarting service…");
                }, 1500);
            }
        }
    };
    evt.onerror = () => {
        if (completeReceived) return;  // expected end-of-stream
        _appendLog(log, "\n(stream disconnected — service may be restarting)");
        evt.close();
        if (succeeded || _state !== "in-progress") {
            // Best-effort transition to health-poll if we got a complete event
            setTimeout(() => {
                modal.classList.add("hide");
                openHealthPollModal("Service appears to be restarting…");
            }, 2000);
        }
    };
}

/**
 * After restart, poll /health every 2s for up to 180s with progressive
 * copy (audit C5). On HTTP 200 → success toast + refresh panel.
 */
export function openHealthPollModal(initialMsg) {
    const modal = _ensureLogModal();
    const log = modal.querySelector(".updates-log-pre");
    const header = modal.querySelector(".updates-log-header");
    log.textContent = initialMsg + "\n";
    header.textContent = "Waiting for service…";
    modal.classList.remove("hide");

    const start = Date.now();
    const timeoutMs = 180_000;
    const tick = async () => {
        const elapsed = Date.now() - start;
        if (elapsed > timeoutMs) {
            _appendLog(log, `\n⚠ Still down after ${Math.round(timeoutMs / 1000)}s. Check the logs manually.`);
            return;
        }
        // Progressive 3-stage copy (audit C5)
        const stage = elapsed < 30000
            ? "Restarting service…"
            : elapsed < 90000
                ? "Rebuilding snapshot index (60–90s typical)…"
                : "Still warming. If this hangs, run `journalctl -u blackbox.service`.";
        header.textContent = stage;
        try {
            const r = await fetch("/health", { cache: "no-store" });
            if (r.ok) {
                _appendLog(log, `\n✓ Service back online after ${Math.round(elapsed / 1000)}s.`);
                setTimeout(() => {
                    modal.classList.add("hide");
                    refreshPanel(true).catch(() => {});
                }, 1500);
                return;
            }
        } catch (_) { /* fetch fails while service is down — expected */ }
        setTimeout(tick, 2000);
    };
    tick();
}

function _ensureLogModal() {
    let modal = document.getElementById("updatesLogModal");
    if (modal) return modal;
    modal = document.createElement("div");
    modal.id = "updatesLogModal";
    modal.className = "modal updates-log-modal hide";
    modal.setAttribute("role", "dialog");
    modal.innerHTML = `
        <div class="modal-card updates-log-card">
            <div class="modal-head">
                <h3 class="updates-log-header">Update Log</h3>
                <button class="btn updates-log-close" aria-label="Close">✕</button>
            </div>
            <pre class="updates-log-pre"></pre>
        </div>
    `;
    document.body.appendChild(modal);
    modal.querySelector(".updates-log-close").addEventListener("click", () => {
        modal.classList.add("hide");
    });
    return modal;
}

function _appendLog(pre, text) {
    pre.textContent += text + "\n";
    pre.scrollTop = pre.scrollHeight;
}

function _esc(s) {
    if (s == null) return "";
    return String(s)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
}
