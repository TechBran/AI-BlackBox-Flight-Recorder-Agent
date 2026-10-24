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
// (Embedding compute/placement selection — GPU/CPU pins — also lives in the
// onboarding wizard now; the updates panel no longer renders that card.)
//
// Destructive actions (audit rank 43): Install, Rollback and Restart Service
// ask first. Local changes to tracked files (status.dirty_files) are named in
// the Install/Rollback confirm, and confirming sends discard_local_changes so
// the backend stashes them; a 409 that lists dirty_files asks again. After any
// action that restarts the service the modal waits for /health's boot_id to
// CHANGE (A13-ui-ops-01) instead of trusting the first 200, which the old
// process answers for the ~2 s before the restart fires. When the log stream
// drops mid-update, /update/state says whether that task is still running,
// finished with a restart coming, or ended without one (A13-ui-ops-06).

import { escapeHtml, toastError } from "./core-utils.js";

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
    // On-box local model stack status (M8): fire-and-forget, fail-soft — it
    // must never delay or break the updates panel itself (same contract as
    // the embeddings card).
    _refreshLocalModelsCard();
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
            row.innerHTML = `<span class="updates-status-err">Error: ${escapeHtml(ctx.error || "unknown")}</span>`;
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
            row.innerHTML = `<span class="updates-status-ok">✓ Up to date</span> <code class="updates-sha">${escapeHtml(status.current_short)}</code>`;
            list.innerHTML = _renderDirtyNote(status);
            btnRollback.disabled = !status.last_state;
            break;
        }

        case "local-ahead": {
            const { status } = ctx;
            row.innerHTML = `<span class="updates-status-info">Local ahead of origin by ${status.commits_ahead} commit(s)</span> <code class="updates-sha">${escapeHtml(status.current_short)}</code>`;
            list.innerHTML = "<p class='updates-hint-inline'>Unpushed local work. Nothing to pull from GitHub right now.</p>"
                + _renderDirtyNote(status);
            btnRollback.disabled = !status.last_state;
            break;
        }

        case "updates-available": {
            const { status } = ctx;
            row.innerHTML = `
                <span class="updates-status-update">⬆ ${status.commits_behind} update${status.commits_behind === 1 ? "" : "s"} available</span>
                <code class="updates-sha">${escapeHtml(status.current_short)} → ${escapeHtml(status.latest_short)}</code>
            `;
            list.innerHTML = _renderCommits(status) + _renderDirtyNote(status);
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
            row.innerHTML = `<span class="updates-status-err">✕ Last update failed during <code>${escapeHtml(lastState.failed_phase || lastState.phase)}</code></span>`;
            list.innerHTML = `<pre class="updates-error">${escapeHtml(lastState.error || "(no error message recorded)")}</pre>`;
            btnRollback.disabled = false;
            break;
        }

        case "interrupted": {
            const { lastState } = ctx;
            row.innerHTML = `<span class="updates-status-warn">⚠ Update interrupted</span>`;
            list.innerHTML = `
                <pre class="updates-error">Last phase: ${escapeHtml(lastState.phase)}
Target SHA: ${escapeHtml(lastState.target_sha || "unknown")}
Started: ${escapeHtml(lastState.updated_iso || "")}</pre>
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
        ? `<div class="updates-categories">${catsOn.map((c) => `<span class="updates-cat updates-cat-${escapeHtml(c)}">${escapeHtml(c)}</span>`).join(" ")}</div>`
        : "";
    const items = status.commits.slice(0, 8).map((c) => `
        <li>
            <code class="updates-commit-sha">${escapeHtml(c.short)}</code>
            <span class="updates-commit-subject">${escapeHtml(c.subject)}</span>
        </li>
    `).join("");
    const more = status.commits.length > 8
        ? `<li class="updates-commit-more">… and ${status.commits.length - 8} more</li>`
        : "";
    return `<ul class="updates-commits-list">${items}${more}</ul>${catsHtml}`;
}

// Local changes an Install or Rollback would overwrite (status.dirty_files).
// File names come from the box's checkout: escaped like every other field.
function _renderDirtyNote(status) {
    const dirty = (status && status.dirty_files) || [];
    if (!dirty.length) return "";
    return `<p class="updates-hint-inline updates-status-warn">⚠ ${dirty.length} tracked file${dirty.length === 1 ? " has" : "s have"} local changes: `
        + `${dirty.slice(0, 5).map((f) => `<code>${escapeHtml(f)}</code>`).join(", ")}${dirty.length > 5 ? ` and ${dirty.length - 5} more` : ""}. `
        + `Install and Rollback stash them first (<code>git stash list</code> shows them later).</p>`;
}

// ── Confirm wording (plain text for window.confirm) ────────────────────

function _dirtyLine(dirty, action) {
    const shown = dirty.slice(0, 5).join(", ") + (dirty.length > 5 ? ` and ${dirty.length - 5} more` : "");
    return `${dirty.length} tracked file(s) on this box have local changes (${shown}). `
        + `${action} stashes them first, so they can be restored later with git stash apply.`;
}

export function installConfirmText(status) {
    const n = status.commits_behind;
    let text = `Install ${n} update(s)?\n\nThis will restart the BlackBox service. Expect 60–90 seconds of downtime.`;
    const activeSessions = status.active_cli_sessions || 0;
    if (activeSessions > 0) {
        text = `WARNING: ${activeSessions} active CLI agent session(s) will be disconnected.\n\n` + text;
    }
    const dirty = status.dirty_files || [];
    if (dirty.length) text += `\n\n${_dirtyLine(dirty, "Installing")}`;
    return text;
}

export function rollbackConfirmText(status) {
    let text = "Roll back to the pre-update tag and restart the service?\n\n"
        + "User data is unaffected — only application code (and the Python packages that update changed) reverts.";
    const dirty = (status && status.dirty_files) || [];
    if (dirty.length) text += `\n\n${_dirtyLine(dirty, "Rolling back")}`;
    return text;
}

export const RESTART_CONFIRM_TEXT = "Restart the BlackBox service now?\n\n"
    + "Every connected client is cut off for 60–90 seconds while it comes back.";

function _stashRetryText(dirty, action) {
    return `${action} was refused: ${_dirtyLine(dirty, action)}\n\nStash them and continue?`;
}

// ── Restart detection (A13-ui-ops-01) ──────────────────────────────────

// Fallback for a backend without boot_id: accept a 200 only after the
// service was seen down, or this long after the wait began (the restart fires
// 2 s after the complete event, and the old process stops answering then).
export const RESTART_GRACE_MS = 10_000;

/**
 * The running process's /health boot_id: the string, "" from a backend that
 * predates it, or null when /health did not answer 200.
 */
export async function readBootId(fetchImpl = (...a) => fetch(...a)) {
    try {
        const r = await fetchImpl("/health", { cache: "no-store" });
        if (!r.ok) return null;
        const body = await r.json();
        return typeof body.boot_id === "string" ? body.boot_id : "";
    } catch (_) {
        return null;
    }
}

/**
 * Has the service restarted since `priorBootId` was read? `bootId` is what a
 * 200 /health just returned ("" = no boot_id field). priorBootId is the id
 * read before the action, "" when that backend had none, or null when it
 * could not be read.
 */
export function restartObserved({ priorBootId, bootId, seenDown, elapsedMs, graceMs = RESTART_GRACE_MS }) {
    if (priorBootId) return bootId !== priorBootId;  // "" too: an older build answered
    if (priorBootId === "" && bootId) return true;    // a newer build answered
    return !!seenDown || elapsedMs >= graceMs;        // nothing to compare
}

/** Does a complete event promise a restart? Older backends omit the flag;
 * their only no-restart success is "Already up to date" (equal SHAs). */
export function restartExpected(event) {
    if (typeof event.restart_scheduled === "boolean") return event.restart_scheduled;
    return !!event.succeeded && event.sha_before !== event.sha_after;
}

/**
 * What /update/state says about update task `taskId` once its log stream has
 * dropped: "running" while the update lock is held; "restarting" once that
 * task finished and scheduled the restart (the lock is free ~2 s before the
 * restart fires, while the old process still answers); "ended" when it
 * failed, was refused or had nothing to do, so no restart is coming.
 */
export function taskVerdict(state, taskId) {
    if (state.in_progress) return "running";
    const last = state.state;
    if (last && last.task_id === taskId
        && ["restart_pending", "complete", "rolled_back"].includes(last.phase)) {
        return "restarting";
    }
    return "ended";
}

/** taskVerdict from GET /update/state (no git call, unlike /update/status);
 * null when it did not answer. */
export async function readTaskState(taskId, fetchImpl = (...a) => fetch(...a)) {
    try {
        const r = await fetchImpl("/update/state", { cache: "no-store" });
        if (!r.ok) return null;
        return taskVerdict(await r.json(), taskId);
    } catch (_) {
        return null;
    }
}

/**
 * Poll /health until the service has restarted, or `timeoutMs` passes.
 * Resolves { restarted, elapsedMs, lastUp } — lastUp says the final probe was
 * answered (by the old process, if not restarted).
 *
 * `taskState()`, when given (the log stream dropped, see taskVerdict), is
 * asked each time the OLD process answers. "ended" ends the wait early with
 * { restarted: false, gaveUp: true }. "running" keeps it going until
 * `timeoutMs` after the task was last seen running (a pip phase alone can
 * take 10 minutes), at most `maxMs`; a wait that reaches maxMs while the
 * update still runs resolves { restarted: false, stillRunning: true }.
 * `onTick(elapsedMs, verdict)` hears the last verdict (null without a probe).
 */
export async function waitForRestart({
    priorBootId, seenDown = false, readBoot = readBootId,
    now = () => Date.now(), sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
    timeoutMs = 180_000, maxMs = 3_600_000, intervalMs = 2000, graceMs = RESTART_GRACE_MS,
    onTick = null, taskState = null,
} = {}) {
    const start = now();
    let lastUp = false;
    let verdict = null;
    let runningAt = null;  // elapsedMs when the task was last seen running
    for (;;) {
        const elapsedMs = now() - start;
        const deadline = runningAt === null ? timeoutMs
            : Math.max(timeoutMs, Math.min(maxMs, runningAt + timeoutMs));
        if (elapsedMs > deadline) {
            return lastUp && verdict === "running"
                ? { restarted: false, stillRunning: true, elapsedMs, lastUp }
                : { restarted: false, elapsedMs, lastUp };
        }
        if (onTick) onTick(elapsedMs, verdict);
        const bootId = await readBoot();
        lastUp = bootId !== null;
        if (bootId === null) {
            seenDown = true;
        } else if (restartObserved({ priorBootId, bootId, seenDown, elapsedMs, graceMs })) {
            return { restarted: true, elapsedMs, lastUp };
        } else if (taskState) {
            verdict = await taskState();
            if (verdict === "ended") return { restarted: false, gaveUp: true, elapsedMs, lastUp };
            if (verdict === "running") runningAt = elapsedMs;
        }
        await sleep(intervalMs);
    }
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
    // System Controls' Restart Service sits in the same menu and needs the
    // same restart detection, so it is wired here.
    const btnRestart = document.getElementById("btnRestart");
    if (btnRestart) btnRestart.addEventListener("click", _onRestartClick);
}

/**
 * POST an update action; on a 409 that lists dirty_files, ask once to stash
 * them and retry with discard_local_changes. Resolves the JSON body, or null
 * when the operator declined or the request failed (already reported).
 * `freshOnConflict`: refresh the panel from a new fetch (preflight) after any
 * other 409. A rollback passes false: preflight deletes a failed state, and
 * with it the tag the rollback needs.
 */
async function _postUpdateAction(url, body, action, { freshOnConflict = true } = {}) {
    const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
    if (r.status === 409) {
        const detail = await r.json().catch(() => ({}));
        const dirty = Array.isArray(detail.dirty_files) ? detail.dirty_files : [];
        if (dirty.length && !body.discard_local_changes) {
            if (!window.confirm(_stashRetryText(dirty, action))) return null;
            return _postUpdateAction(url, { ...body, discard_local_changes: true }, action,
                                     { freshOnConflict });
        }
        window.alert(`${action} conflict: ${detail.detail || "another update may be in progress."}`);
        await refreshPanel(freshOnConflict);
        return null;
    }
    if (!r.ok) {
        const txt = await r.text().catch(() => "");
        window.alert(`${action} failed: HTTP ${r.status} ${txt.substring(0, 300)}`);
        return null;
    }
    return r.json();
}

async function _onInitClick() {
    // Lazy-init by re-running install.sh's Step 0a equivalent — for now,
    // POST /update/preflight does the fetch which forces lazy-init's
    // happy path. Backend's preflight returns git_initialized=false with
    // a helpful message if it's still missing; user can SSH in for full setup.
    await refreshPanel(true);
}

function _onInstallClick() {
    return startInstall(_lastStatus);
}

/**
 * The Install flow, shared with the onboarding hub's Install Now badge
 * (Portal/onboarding/hub-controls.js): confirm (naming CLI sessions and local
 * changes), POST /update/start with consent for the files the confirm named,
 * ask again on a dirty-tree 409, then stream the task and wait for the
 * restart. `status` is a /update/status reply. Resolves true when an update
 * was started.
 */
export async function startInstall(status) {
    if (!status || !status.commits_behind) return false;
    if (!window.confirm(installConfirmText(status))) return false;
    try {
        // Read before the action: the old process's id is what must change.
        const priorBootId = await readBootId();
        const body = await _postUpdateAction("/update/start", {
            confirm_sha: status.latest_sha,
            // The confirm just named these files; agreeing to it is consent.
            discard_local_changes: (status.dirty_files || []).length > 0,
        }, "Update");
        if (!body) return false;
        openUpdateLogModal(body.task_id, { priorBootId });
        return true;
    } catch (e) {
        window.alert(`Network error starting update: ${e.message}`);
        return false;
    }
}

function _onViewLogClick() {
    // No streaming task_id available post-restart — open a static log view
    // of the journal. Re-using /onboarding/logs/stream (existing SSE endpoint).
    // For v1 this is a passthrough; can grow later.
    window.open("/onboarding/logs/stream?lines=200", "_blank");
}

async function _onRollbackClick() {
    if (!window.confirm(rollbackConfirmText(_lastStatus))) return;
    try {
        const priorBootId = await readBootId();
        const body = await _postUpdateAction("/update/rollback", {
            discard_local_changes: ((_lastStatus && _lastStatus.dirty_files) || []).length > 0,
        }, "Rollback", { freshOnConflict: false });
        if (!body) return;
        const tag = body.reverted_to || "(unknown)";
        if (body.task_id) {
            // The rollback runs as a task (venv restore + reset under the
            // update lock): stream it, then wait for the restart.
            openUpdateLogModal(body.task_id, { priorBootId, title: `Rolling back to ${tag}` });
        } else {
            openHealthPollModal(`Rolled back to ${tag}. Restarting service…`, { priorBootId });
        }
    } catch (e) {
        window.alert(`Rollback error: ${e.message}`);
    }
}

async function _onRestartClick() {
    if (!window.confirm(RESTART_CONFIRM_TEXT)) return;
    const priorBootId = await readBootId();
    try {
        const r = await fetch("/restart", { method: "POST" });
        const body = await r.json().catch(() => ({}));
        // /restart answers 200 with status "error" when sudo refuses. Any
        // other answer (a proxy's 502 included) may be the restart itself:
        // the wait below tells the two apart by boot_id.
        if (body.status === "error") {
            window.alert(`Restart failed: ${body.message || body.error || "unknown error"}`);
            return;
        }
    } catch (_) {
        // Expected: the restart can cut this very request off.
    }
    openHealthPollModal("Restart requested. Waiting for the service…", { priorBootId });
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
                <p class="embeddings-card-copy">${escapeHtml(health.detail || "The active embedding model stopped working. Automatic recovery is migrating your search memory to a working model.")}</p>
                ${jobRunning ? `<div class="embeddings-card-progress">${_embedProgressLine(job)}</div>` : ""}
                <div class="embeddings-card-actions">
                    <button class="btn embeddings-manage-btn">Manage</button>
                </div>
            </div>`;
    } else if (jobRunning) {
        html = `
            <div class="embeddings-card embeddings-card-info">
                <div class="embeddings-card-title">⟳ Search memory update in progress</div>
                <div class="embeddings-card-progress">${_embedProgressLine(job)}</div>
                <div class="embeddings-card-actions">
                    <button class="btn embeddings-manage-btn">Manage</button>
                </div>
            </div>`;
    } else if (health.state === "superseded") {
        const successorLabel = health.successor || health.successor_slug || "a newer model";
        html = `
            <div class="embeddings-card embeddings-card-info">
                <div class="embeddings-card-title">⬆ Search memory update available</div>
                <p class="embeddings-card-copy">Your system will transfer embeddings to ${escapeHtml(successorLabel)} in the background. Search keeps working the whole time; the switch happens automatically when it finishes and survives restarts.</p>
                <div class="embeddings-card-actions">
                    ${health.successor_slug ? `<button id="btnEmbeddingsUpdate" class="btn btn-primary">Update</button>` : ""}
                    <button class="btn embeddings-manage-btn">Manage</button>
                </div>
            </div>`;
    }

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

    if (jobRunning) _startEmbedPoll();
    else _stopEmbedPoll();
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
        line = `Reranking: ON — ${escapeHtml(rr.provider || "?")}/${escapeHtml(rr.model || "?")}`;
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
                <button class="btn embeddings-manage-btn">Manage</button>
            </div>
        </div>`;
}

// ── On-box local model stack card (M8, read-only) ─────────────────────
// Reflects GET /local-models/status. Status-only: no activation here — the
// [Manage] button deep-links to the wizard step. Fail-soft: any error hides
// the card (never breaks the panel), mirroring the embeddings card contract.
let _lmWarnedOnce = false;

async function _refreshLocalModelsCard() {
    const container = document.getElementById("localModelsCard");
    if (!container) return;  // panel not in DOM (menu modal not built yet)
    const status = await _fetchJsonSoft("/local-models/status");
    if (!status) {  // unreachable / older backend / not installed → hide
        container.classList.add("hide");
        container.innerHTML = "";
        if (!_lmWarnedOnce) { _lmWarnedOnce = true; console.warn("[updates] local-models status unavailable"); }
        return;
    }
    _renderLocalModelsCard(container, status);
}

function _renderLocalModelsCard(container, status) {
    const routing = status.routing || {};
    // GET /local-models/status emits routing[cap] as an OBJECT
    // {enabled, healthy, decision}; decision "on-box" means the capability is
    // seeded AND the stack is reachable (see _routing_decision in
    // local_models_routes.py). A capability counts as active only when it is
    // actually routing on-box.
    const _isOnbox = (cap) => (routing[cap] || {}).decision === "on-box";
    const onbox = [];
    if (_isOnbox("stt")) onbox.push("Speech");
    if (_isOnbox("tts")) onbox.push("Voice");
    if (_isOnbox("embeddings")) onbox.push("Memory");
    if (_isOnbox("rerank")) onbox.push("Reranking");

    let line;
    if (!status.installed) line = "On-box models: not installed";
    else if (!status.healthy) line = "On-box models: installed — stack not running";
    else if (onbox.length) line = `On-box models: ${onbox.join(", ")}`;
    else line = "On-box models: ready — none active";

    container.innerHTML = `
        <div class="embeddings-card embeddings-rerank-status">
            <div class="embeddings-card-title">${escapeHtml(line)}</div>
            <div class="embeddings-card-actions">
                <button class="btn local-models-manage-btn">Manage</button>
            </div>
        </div>`;
    container.classList.remove("hide");
    container.querySelectorAll(".local-models-manage-btn").forEach((btn) => {
        btn.addEventListener("click", () => { window.location.href = "/onboarding/?step=local_models"; });
    });
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
    // Determinate bar; total==0 (unknown yet) → 0% rather than NaN.
    const pct = total > 0 ? Math.min(100, Math.floor(done / total * 100)) : 0;
    // NOTE: contains a block-level <div> — the call sites MUST wrap this in a
    // <div>, not a <p> (a <p> auto-closes at the first block child, orphaning
    // the bar). See .embeddings-card-progress in _updates.css.
    return `Re-embedding ${done}/${total}…${cancelling}`
        + `<div class="embeddings-progress-track">`
        + `<div class="embeddings-progress-fill" style="width:${pct}%"></div>`
        + `</div>`;
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
export function openUpdateLogModal(taskId, { priorBootId, title } = {}) {
    const modal = _ensureLogModal();
    const log = modal.querySelector(".updates-log-pre");
    const header = modal.querySelector(".updates-log-header");
    log.textContent = "";
    header.textContent = title || `Update in progress (task ${taskId.substring(0, 20)}…)`;
    modal.classList.remove("hide");

    // The id the restart must change. A caller that read it before starting
    // passes it; otherwise read it now, while the runner is still working
    // (the restart comes only after the complete event).
    const prior = priorBootId !== undefined ? Promise.resolve(priorBootId) : readBootId();

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
            const succeeded = !!data.succeeded;
            evt.close();
            _appendLog(log,
                succeeded
                    ? `\n✓ COMPLETE: ${data.sha_before} → ${data.sha_after}`
                    : `\n✕ FAILED: ${data.error || "(no error msg)"}`);
            if (succeeded && restartExpected(data)) {
                // Service will restart in ~2s. Switch to health-poll modal.
                setTimeout(async () => {
                    modal.classList.add("hide");
                    openHealthPollModal(data.phase === "rolled_back"
                        ? "Rollback applied. Restarting service…"
                        : "Update applied. Restarting service…", { priorBootId: await prior });
                }, 1500);
            } else {
                // Nothing restarts (already up to date, refused, failed):
                // show the panel's new state behind the log.
                if (succeeded) _appendLog(log, "No restart needed.");
                refreshPanel(false).catch(() => {});
            }
        }
    };
    evt.onerror = () => {
        if (completeReceived) return;  // expected end-of-stream
        _appendLog(log, "\n(stream disconnected — service may be restarting)");
        evt.close();
        // The runner keeps going without the stream. Wait for the restart,
        // asking /update/state about THIS task: keep waiting while it runs or
        // once it scheduled its restart, stop once it ended without one.
        setTimeout(async () => {
            modal.classList.add("hide");
            openHealthPollModal("Service appears to be restarting…", {
                priorBootId: await prior,
                taskState: () => readTaskState(taskId),
            });
        }, 2000);
    };
}

/**
 * After a restart was requested, poll /health every 2s for up to 180s with
 * progressive copy (audit C5) until boot_id differs from `priorBootId`
 * (A13-ui-ops-01), then refresh the panel. `taskState`: see waitForRestart.
 */
export function openHealthPollModal(initialMsg, { priorBootId, taskState = null } = {}) {
    const modal = _ensureLogModal();
    const log = modal.querySelector(".updates-log-pre");
    const header = modal.querySelector(".updates-log-header");
    log.textContent = initialMsg + "\n";
    header.textContent = "Waiting for service…";
    modal.classList.remove("hide");

    const timeoutMs = 180_000;
    (async () => {
        let prior = priorBootId;
        let seenDown = false;
        if (prior === undefined) {
            // No id from before the action: read it now. A failed read means
            // the service is already down, which is what the wait needs.
            prior = await readBootId();
            seenDown = prior === null;
        }
        const result = await waitForRestart({
            priorBootId: prior, seenDown, timeoutMs, taskState,
            onTick: (elapsed, verdict) => {
                // Progressive 3-stage copy (audit C5), unless the update
                // behind a dropped stream is still running.
                header.textContent = verdict === "running"
                    ? "Update still running on the box…"
                    : elapsed < 30000
                        ? "Restarting service…"
                        : elapsed < 90000
                            ? "Rebuilding snapshot index (60–90s typical)…"
                            : "Still warming. If this hangs, run `journalctl -u blackbox.service`.";
            },
        });
        if (result.restarted) {
            _appendLog(log, `\n✓ Service back online after ${Math.round(result.elapsedMs / 1000)}s.`);
            setTimeout(() => {
                modal.classList.add("hide");
                refreshPanel(true).catch(() => {});
            }, 1500);
        } else if (result.gaveUp) {
            header.textContent = "No restart";
            _appendLog(log, "\nThe update ended and the service did not restart. See the Updates panel for what happened.");
            refreshPanel(false).catch(() => {});
        } else if (result.stillRunning) {
            header.textContent = "Still updating";
            _appendLog(log, `\nThe update is still running on the box after ${Math.round(result.elapsedMs / 60000)} min. The Updates panel shows its progress; the service restarts when it finishes.`);
            refreshPanel(false).catch(() => {});
        } else if (result.lastUp) {
            header.textContent = "Not restarted";
            _appendLog(log, `\n⚠ After ${Math.round(result.elapsedMs / 1000)}s the service is still the process that was running before: the restart did not happen. Restart it from System Controls, or run \`sudo systemctl restart blackbox.service\` on the box.`);
        } else {
            _appendLog(log, `\n⚠ Still down after ${Math.round(result.elapsedMs / 1000)}s. Check the logs manually.`);
        }
    })();
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
