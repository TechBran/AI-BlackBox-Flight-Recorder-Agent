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
