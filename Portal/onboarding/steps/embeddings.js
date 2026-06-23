// Embeddings (memory & search) step — fourth screen of the onboarding wizard
// and the SINGLE management surface for the pluggable-embeddings module
// (design doc §8: the updates cards only notify and deep-link back here via
// /onboarding/?step=embeddings).
//
// Backed by the BINDING /embeddings/* contract (Orchestrator/routes/
// embeddings_routes.py):
//   GET  /embeddings/status         — models[], stores[], health, job, ollama
//   POST /embeddings/validate       — {slug} → {ok, dims | error} (15s cap)
//   POST /embeddings/migrate        — {target} → job dict | 409 when running
//   POST /embeddings/migrate/cancel — cooperative cancel
//   POST /embeddings/ollama/pull    — {model: <registry slug>} → pull state
//
// This step:
//   1. Renders a model card per status.models[] entry: label, quality note,
//      dims, RAM (local) or $/1M tokens (cloud), privacy badge, store
//      freshness ("N snapshots behind" / "Up to date" / unreadable), and
//      blockers as remediation lines. The active model gets an ACTIVE badge.
//   2. Selecting a READY non-active model → Validate (probe-embed) → on ok,
//      the migrate action: "Backfill N snapshots & switch" when its store
//      already exists, else "Build index & switch".
//   3. An Ollama model whose weights aren't pulled gets a "Pull model (≈X GB)"
//      button; pull progress is polled via status.ollama.pull every 2s, then
//      blockers refresh and the validate→migrate flow continues.
//   4. While a migration runs (status.job.state === "running" — including one
//      somebody else started; 409 on migrate lands here too) the card grid is
//      replaced by a progress panel: done/total, percent, rate-based ETA
//      (total may GROW mid-job; recomputed every poll, never >100%), skipped
//      count, and a Cancel button. done → success summary; stalled → error
//      panel with "Re-run to resume — progress is kept" + retry.
//   5. Completing requires nothing — the current active model stays. "Keep
//      current (skip)" is always available; Continue completes the step via
//      the standard wizard next() like every other step.
//
// Sigil note: numbered 04 (after KEYS 03). Later steps keep their original
// hardcoded numbers — same precedent as the transcription step's insertion
// (pair_phone and transcription both show 05 today).
//
// Reuses the .ob-cli-agent-* card/badge CSS plus thin ob-emb-* additions
// (grid sizing, freshness lines, progress bar, banners) in onboarding.css.

// Selected-card highlight is the shared .ob-card-selected class (onboarding.css).

const POLL_MS = 2000;

let status = null;        // last GET /embeddings/status payload
let selectedSlug = null;  // card the user clicked (never the active model)
let validation = null;    // /embeddings/validate result for selectedSlug
let validating = false;   // probe in flight
let migrating = false;    // migrate POST in flight (button debounce)
let pulling = false;      // pull POST in flight (button debounce)
let keepAliveBusy = null; // slug whose keep_alive toggle POST is in flight
let cancelClicked = false; // local echo until job.cancel_requested arrives
let pollTimer = null;
let etaSamples = [];      // [{t(ms), done}] for the rate-based ETA
let etaJobKey = null;     // job.started_at — reset samples when it changes
let watchingJob = false;  // we showed the running panel; done/stalled → panels
let root = null;          // our <section>; disconnected ⇒ step unmounted
let ctx = null;           // { container, next, back, skip }

export async function render(container, { next, back, skip, sigil }) {
    stopPolling();
    status = null;
    selectedSlug = null;
    validation = null;
    validating = false;
    migrating = false;
    pulling = false;
    cancelClicked = false;
    etaSamples = [];
    etaJobKey = null;
    watchingJob = false;
    ctx = { container, next, back, skip };

    container.innerHTML = `
        <section class="ob-step ob-embeddings" id="ob-emb-root">
            <aside class="ob-step-sigil" aria-hidden="true">
                <div class="ob-step-sigil-num"><em>${sigil ? sigil.num : "04"}</em></div>
                <div class="ob-step-sigil-rule"></div>
                <div class="ob-step-sigil-label">MEMORY</div>
            </aside>
            <div class="ob-step-body">
                <div class="ob-step-eyebrow">
                    <span class="ob-step-eyebrow-dot" aria-hidden="true"></span>
                    Memory &amp; search
                </div>
                <h1 class="ob-step-title">
                    Choose how the BlackBox <em>remembers</em>.
                </h1>
                <p class="ob-step-lede">
                    Every conversation snapshot is embedded into a vector index
                    so semantic search can find it later. Cloud models
                    (<strong>Gemini</strong>, <strong>OpenAI</strong>) bill per
                    token on your own keys; local <strong>Qwen3</strong> models
                    run fully offline through Ollama. Switching models
                    re-embeds your snapshots in the background &mdash; search
                    keeps working the whole time, and the switch survives
                    restarts.
                </p>
                <div id="ob-emb-content">
                    <div class="ob-loading">Checking embedding status&hellip;</div>
                </div>
                <nav class="ob-step-nav" aria-label="Step navigation">
                    <button type="button" class="ob-back" id="ob-emb-back">
                        <span aria-hidden="true">&larr;</span> Back to ${sigil && sigil.backLabel ? sigil.backLabel.toLowerCase() : "keys"}
                    </button>
                    <button type="button" class="ob-cta" id="ob-emb-continue">
                        Continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                    </button>
                    <button type="button" class="ob-skip" id="ob-emb-skip">
                        Keep current (skip) <span aria-hidden="true">&rarr;</span>
                    </button>
                </nav>
            </div>
        </section>
    `;
    root = container.querySelector("#ob-emb-root");
    container.querySelector("#ob-emb-back").addEventListener("click", back);
    container.querySelector("#ob-emb-skip").addEventListener("click", skip);
    // Completing requires nothing — the current active model simply stays.
    container.querySelector("#ob-emb-continue").addEventListener("click", next);

    await refreshStatus();
    routeRender();
}

// ── Status + polling ─────────────────────────────────────────────

async function refreshStatus() {
    const fresh = await fetchJson("/embeddings/status");
    if (fresh) status = fresh;
    return fresh;
}

function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(tick, POLL_MS);
}

function stopPolling() {
    if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
}

async function tick() {
    // The wizard replaces the step container's innerHTML on navigation; our
    // section going stale is the unmount signal — stop polling, drop timers.
    if (!root || !root.isConnected) {
        stopPolling();
        return;
    }
    const fresh = await fetchJson("/embeddings/status");
    // The await yields — the wizard may have navigated away mid-fetch.
    if (!root || !root.isConnected) {
        stopPolling();
        return;
    }
    if (!fresh) return; // transient fetch hiccup — keep last known state
    status = fresh;

    const job = status.job;
    if (job && job.state === "running") {
        watchingJob = true;
        renderJobPanel(); // updates in place when the panel is already up
        return;
    }
    if (watchingJob && job) {
        // We watched it run; it just finished. cancelled → back to the picker.
        watchingJob = false;
        stopPolling();
        if (job.state === "done") {
            renderJobDone(job);
        } else if (job.state === "stalled") {
            renderJobStalled(job);
        } else {
            renderPicker();
            showHint("Migration cancelled — progress so far is kept.", false);
        }
        return;
    }

    // Picker-mode polling: an Ollama pull is streaming.
    const pull = status.ollama && status.ollama.pull;
    if (pull && pull.state === "running") {
        updatePullProgress(pull);
        return;
    }
    stopPolling();
    if (pull && pull.state === "error") {
        renderPicker();
        showHint(`Model download failed: ${pull.error || "unknown error"}. Try the pull again.`, true);
    } else {
        // Pull finished — blockers refresh; validate→migrate flow continues.
        renderPicker();
    }
}

// ── Routing ──────────────────────────────────────────────────────

function routeRender() {
    if (!status) {
        renderUnavailable();
        return;
    }
    const job = status.job;
    if (job && job.state === "running") {
        watchingJob = true;
        renderJobPanel();
        startPolling();
        return;
    }
    if (job && job.state === "stalled") {
        // A previous run failed mid-way; surface it instead of hiding the
        // half-built store behind the picker.
        renderJobStalled(job);
        return;
    }
    renderPicker();
    const pull = status.ollama && status.ollama.pull;
    if (pull && pull.state === "running") startPolling();
}

function contentEl() {
    return ctx.container.querySelector("#ob-emb-content");
}

function renderUnavailable() {
    contentEl().innerHTML = `
        <div class="ob-cli-agent-error">
            <p>Couldn't reach <code>/embeddings/status</code>. The service may
            still be starting (index rebuild takes 60&ndash;90s).</p>
            <p><button type="button" class="ob-cta" id="ob-emb-retry-status">Retry</button></p>
        </div>
    `;
    const btn = contentEl().querySelector("#ob-emb-retry-status");
    btn.addEventListener("click", async () => {
        btn.disabled = true;
        await refreshStatus();
        routeRender();
    });
}

// ── Picker (health banner + model card grid) ─────────────────────

function renderPicker() {
    const health = status.health || { state: "ok" };
    contentEl().innerHTML = `
        ${healthBannerHtml(health)}
        <div id="ob-emb-grid" class="ob-emb-grid" role="radiogroup"
             aria-label="Embedding models"></div>
        <div id="ob-emb-hint-slot"></div>
    `;
    const switchBtn = contentEl().querySelector("#ob-emb-health-switch");
    if (switchBtn) {
        switchBtn.addEventListener("click", () => {
            startMigrate(status.health.successor_slug, switchBtn);
        });
    }
    renderGrid();
}

function healthBannerHtml(health) {
    if (!health || health.state === "ok") return "";
    if (health.state === "superseded") {
        const successor = health.successor
            ? ` Successor: <strong>${escapeHtml(health.successor)}</strong>.`
            : "";
        // "Switch now" only when the watcher resolved a registry slug —
        // display-only otherwise.
        const btn = health.successor_slug
            ? `<button type="button" class="ob-cta ob-emb-banner-btn" id="ob-emb-health-switch">Switch now</button>`
            : "";
        return `
            <div class="ob-emb-banner ob-emb-banner-info" role="status">
                <p>${escapeHtml(health.detail || "The active embedding model has been superseded.")}${successor}</p>
                ${btn}
            </div>
        `;
    }
    // broken (or any unknown non-ok state): urgent styling, detail only.
    return `
        <div class="ob-emb-banner ob-emb-banner-broken" role="alert">
            <p><strong>Embedding model problem:</strong>
            ${escapeHtml(health.detail || "The active embedding model is no longer usable.")}</p>
        </div>
    `;
}

function renderGrid() {
    const grid = contentEl().querySelector("#ob-emb-grid");
    if (!grid) return;
    grid.innerHTML = (status.models || []).map(renderCard).join("");
    (status.models || []).forEach((m) => {
        const card = grid.querySelector(`.ob-cli-agent-card[data-slug="${m.slug}"]`);
        if (card) card.addEventListener("click", () => choose(m.slug));
    });
    wireCardActions(grid);
}

function renderCard(m) {
    const isActive = m.slug === status.active;
    const isSelected = m.slug === selectedSlug;
    const local = m.privacy === "local";

    const badges = [
        isActive
            ? `<span class="ob-cli-agent-badge ob-cli-agent-badge-ok">ACTIVE</span>`
            : "",
        `<span class="ob-cli-agent-badge ${local ? "ob-cli-agent-badge-info" : "ob-emb-badge-cloud"}">${local ? "LOCAL" : "CLOUD"}</span>`,
    ].join("");

    const costRow = local
        ? `<div class="ob-cli-agent-meta-row">
               <span class="ob-cli-agent-meta-label">RAM</span>
               <span>${escapeHtml(fmtNum(m.ram_gb))} GB</span>
           </div>`
        : `<div class="ob-cli-agent-meta-row">
               <span class="ob-cli-agent-meta-label">Cost</span>
               <span>$${escapeHtml(fmtNum(m.cost_per_1m_tokens))} / 1M tokens</span>
           </div>`;

    const blockersHtml = (m.blockers || []).length
        ? `<ul class="ob-emb-blockers">${m.blockers
              .map((b) => `<li>${escapeHtml(b)}</li>`)
              .join("")}</ul>`
        : "";

    const dataState = m.ready || isActive ? "ready" : "needs-auth";
    const selectedClass = isSelected ? " ob-card-selected" : "";

    return `
        <div class="ob-cli-agent-card${selectedClass}" data-slug="${escapeHtml(m.slug)}"
             data-state="${dataState}" role="radio"
             aria-checked="${isSelected ? "true" : "false"}" tabindex="0">
            <div class="ob-cli-agent-head">
                <div class="ob-cli-agent-title">
                    <span class="ob-cli-agent-name">${escapeHtml(m.label)}${isSelected ? " &check;" : ""}</span>
                    <span class="ob-cli-agent-vendor">${escapeHtml(m.slug)}</span>
                </div>
                <span class="ob-emb-badges">${badges}</span>
            </div>
            <p class="ob-cli-agent-auth-blurb">${escapeHtml(m.quality_note || "")}</p>
            <div class="ob-cli-agent-meta-row">
                <span class="ob-cli-agent-meta-label">Dims</span>
                <span>${escapeHtml(String(m.dims))}</span>
            </div>
            ${costRow}
            ${freshnessHtml(m)}
            ${blockersHtml}
            ${warmToggleHtml(m)}
            <div class="ob-cli-agent-actions">${cardActionsHtml(m, isActive, isSelected)}</div>
        </div>
    `;
}

// Warm/cold keep_alive toggle — local (Ollama) models only, once pulled.
// WARM pins the model in RAM for instant embeddings; COLD frees the RAM and
// reloads on demand (the first snapshot after idle is slower). Backed by
// POST /embeddings/keep_alive; status carries m.warm (bool) / m.keep_alive.
function warmToggleHtml(m) {
    if (m.privacy !== "local" || m.warm === null || m.warm === undefined) return "";
    // Only offer warm/cold when the model can actually be loaded: pulled,
    // daemon up, RAM sufficient (m.ready), or it's the live active model.
    if (!m.ready && m.slug !== status.active) return "";
    const on = m.warm === true;
    const busy = keepAliveBusy === m.slug;
    return `
        <div class="ob-emb-warmrow">
            <button type="button" class="ob-emb-warm-toggle" data-slug="${escapeHtml(m.slug)}"
                    role="switch" aria-checked="${on ? "true" : "false"}"
                    aria-label="Keep ${escapeHtml(m.label)} loaded in RAM"
                    ${busy ? "disabled" : ""}>
                <span class="ob-emb-warm-track ${on ? "ob-emb-warm-on" : ""}">
                    <span class="ob-emb-warm-knob"></span>
                </span>
                <span class="ob-emb-warm-name">Keep loaded (warm)${busy ? "&hellip;" : ""}</span>
            </button>
            <p class="ob-emb-warm-help">${on
                ? `Resident in RAM (&approx;${escapeHtml(fmtNum(m.ram_gb))} GB) &mdash; every snapshot embeds instantly.`
                : `Unloaded when idle &mdash; frees &approx;${escapeHtml(fmtNum(m.ram_gb))} GB, but the first snapshot after a quiet spell reloads the model and takes longer. Turn on if you have the RAM to spare.`}</p>
        </div>
    `;
}

function freshnessHtml(m) {
    if (!m.store_exists) return "";
    if (m.missing === null || m.missing === undefined) {
        return `<p class="ob-emb-fresh ob-emb-fresh-subdued">Store exists &mdash; unreadable (will be rebuilt on switch)</p>`;
    }
    if (m.missing === 0) {
        return `<p class="ob-emb-fresh ob-emb-fresh-ok">Up to date</p>`;
    }
    return `<p class="ob-emb-fresh">Store exists &mdash; ${fmtInt(m.missing)} snapshot${m.missing === 1 ? "" : "s"} behind</p>`;
}

// Backend contract: embeddings_routes._model_preflight emits the
// "Pull the model from the setup wizard (≈X GB download)" blocker ONLY when
// the Ollama daemon is up and the weights are absent — exactly the state the
// Pull button resolves. Keyed on the blocker copy; both live in this repo.
function pullBlocker(m) {
    return (m.blockers || []).find((b) => /pull the model/i.test(b)) || null;
}

function cardActionsHtml(m, isActive, isSelected) {
    if (isActive) {
        return `<p class="ob-cli-agent-ready-blurb">Currently active &mdash; all snapshot searches use this model.</p>`;
    }
    if (!isSelected) {
        return `<p class="ob-cli-agent-auth-blurb">Click to select.</p>`;
    }

    const pull = status.ollama && status.ollama.pull;
    const pullRunning = pull && pull.state === "running";
    if (pullBlocker(m)) {
        if (pullRunning) return pullProgressHtml(pull);
        return `
            <button type="button" class="ob-cli-agent-action ob-cli-agent-action-auth"
                    id="ob-emb-pull" ${pulling ? "disabled" : ""}>
                Pull model (&approx;${escapeHtml(fmtNum(m.ram_gb))} GB)
            </button>
        `;
    }
    if (!m.ready) {
        // Non-pull blockers (key missing, daemon down, RAM short) — the
        // remediation lines above are the action.
        return `<p class="ob-cli-agent-auth-blurb">Resolve the items above, then re-select this model.</p>`;
    }

    // Ready, selected, not active → validate → migrate.
    const parts = [];
    if (validating) {
        parts.push(`<span class="ob-status-pill ob-status-pill-validating">Validating&hellip;</span>`);
    } else if (validation && validation.ok) {
        parts.push(`
            <span class="ob-status-pill ob-status-pill-ok">
                <span class="ob-status-pill-glyph" aria-hidden="true">&check;</span>
                ${escapeHtml(String(validation.dims))} dims
            </span>
        `);
        parts.push(`
            <button type="button" class="ob-cli-agent-action ob-cli-agent-action-auth"
                    id="ob-emb-migrate" ${migrating ? "disabled" : ""}>
                ${escapeHtml(migrateLabel(m))}
            </button>
        `);
    } else {
        if (validation && !validation.ok) {
            parts.push(`
                <span class="ob-status-pill ob-status-pill-error">
                    <span class="ob-status-pill-glyph" aria-hidden="true">!</span>
                    ${escapeHtml(validation.error || "validation failed")}
                </span>
            `);
        }
        parts.push(`
            <button type="button" class="ob-cli-agent-action ob-cli-agent-action-auth"
                    id="ob-emb-validate">Validate</button>
        `);
    }
    return parts.join("");
}

function migrateLabel(m) {
    if (!m.store_exists) return "Build index & switch";
    if (m.missing === null || m.missing === undefined) return "Rebuild index & switch";
    if (m.missing === 0) return "Switch now (store up to date)";
    return `Backfill ${fmtInt(m.missing)} snapshot${m.missing === 1 ? "" : "s"} & switch`;
}

function wireCardActions(grid) {
    const validateBtn = grid.querySelector("#ob-emb-validate");
    if (validateBtn) {
        validateBtn.addEventListener("click", (e) => {
            e.stopPropagation(); // card click would re-select
            runValidate();
        });
    }
    const migrateBtn = grid.querySelector("#ob-emb-migrate");
    if (migrateBtn) {
        migrateBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            startMigrate(selectedSlug, migrateBtn);
        });
    }
    const pullBtn = grid.querySelector("#ob-emb-pull");
    if (pullBtn) {
        pullBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            startPull(selectedSlug);
        });
    }
    // keep_alive toggles can appear on multiple cards → wire each by data-slug.
    grid.querySelectorAll(".ob-emb-warm-toggle").forEach((btn) => {
        btn.addEventListener("click", (e) => {
            e.stopPropagation(); // card click would re-select
            setKeepAlive(btn.dataset.slug, btn.getAttribute("aria-checked") !== "true");
        });
    });
}

// ── keep_alive (warm/cold) toggle ────────────────────────────────

async function setKeepAlive(slug, warm) {
    if (keepAliveBusy || !slug) return;
    keepAliveBusy = slug;
    renderGrid();
    try {
        const r = await fetch("/embeddings/keep_alive", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ slug, warm }),
        });
        if (!r.ok) {
            showHint(`Couldn't change the warm/cold setting: ${await safeDetail(r)}`, true);
        }
    } catch (e) {
        showHint(`Network error changing warm/cold: ${e.message}`, true);
    }
    keepAliveBusy = null;
    await refreshStatus(); // reflect the new warm state from the server
    renderGrid();
}

function choose(slug) {
    if (slug === status.active) return;   // active card is informational
    if (slug === selectedSlug) return;    // already chosen — no-op
    selectedSlug = slug;
    validation = null;
    validating = false;
    renderGrid();
}

// ── Validate ─────────────────────────────────────────────────────

async function runValidate() {
    if (validating || !selectedSlug) return;
    validating = true;
    validation = null;
    renderGrid();
    let result;
    try {
        const r = await fetch("/embeddings/validate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ slug: selectedSlug }),
        });
        // Provider failure is an expected ok=false body; non-2xx = route error.
        result = r.ok ? await r.json() : { ok: false, error: `validate returned ${r.status}` };
    } catch (e) {
        result = { ok: false, error: `network error: ${e.message}` };
    }
    validating = false;
    validation = result;
    renderGrid();
}

// ── Ollama pull ──────────────────────────────────────────────────

async function startPull(slug) {
    if (pulling || !slug) return;
    pulling = true;
    renderGrid();
    try {
        const r = await fetch("/embeddings/ollama/pull", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ model: slug }), // body field is `model`, value is the REGISTRY SLUG
        });
        if (!r.ok && r.status !== 409) {
            // 409 = a pull is already streaming — just watch it.
            const detail = await safeDetail(r);
            showHint(`Couldn't start the download: ${detail}`, true);
            pulling = false;
            renderGrid();
            return;
        }
    } catch (e) {
        showHint(`Network error starting the download: ${e.message}`, true);
        pulling = false;
        renderGrid();
        return;
    }
    pulling = false;
    await refreshStatus();
    renderGrid();
    startPolling(); // tick() re-renders the picker when the pull finishes
}

function pullProgressHtml(pull) {
    const pct = pullPct(pull);
    return `
        <div class="ob-emb-pullwrap" id="ob-emb-pullwrap">
            <div class="ob-emb-progress-track">
                <div class="ob-emb-progress-fill" id="ob-emb-pull-fill" style="width: ${pct}%"></div>
            </div>
            <p class="ob-emb-fresh" id="ob-emb-pull-text">${escapeHtml(pullText(pull))}</p>
        </div>
    `;
}

function pullPct(pull) {
    if (!pull.total) return 0;
    return Math.min(100, Math.floor((pull.completed / pull.total) * 100));
}

function pullText(pull) {
    const pct = pullPct(pull);
    const size = pull.total ? ` of ${fmtBytes(pull.total)}` : "";
    return `Downloading ${pull.model || ""}… ${pct}%${size} (${pull.status || "starting"})`;
}

function updatePullProgress(pull) {
    const fill = contentEl() && contentEl().querySelector("#ob-emb-pull-fill");
    const text = contentEl() && contentEl().querySelector("#ob-emb-pull-text");
    if (!fill || !text) {
        // Progress markup only lives on a selected pull-blocked card. Rebuild
        // the grid only when that's the current selection — if the user is
        // busy with another card (e.g. validating a cloud model mid-pull),
        // don't clobber their UI every poll.
        const sel = (status.models || []).find((m) => m.slug === selectedSlug);
        if (sel && pullBlocker(sel)) renderGrid();
        return;
    }
    fill.style.width = pullPct(pull) + "%";
    text.textContent = pullText(pull);
}

// ── Migrate ──────────────────────────────────────────────────────

async function startMigrate(target, btn) {
    if (migrating || !target) return;
    migrating = true;
    if (btn) btn.disabled = true;
    try {
        const r = await fetch("/embeddings/migrate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ target }),
        });
        if (r.status === 409) {
            // Someone else started one — show the running panel instead.
            migrating = false;
            await refreshStatus();
            routeRender();
            return;
        }
        if (!r.ok) {
            const detail = await safeDetail(r);
            migrating = false;
            showHint(`Couldn't start the migration: ${detail}`, true);
            if (btn) btn.disabled = false;
            return;
        }
        // The 200 body IS the freshly-claimed job dict — seed it so the
        // progress panel renders even if the follow-up refresh hiccups.
        try {
            const seeded = await r.json();
            if (seeded && seeded.state && status) status.job = seeded;
        } catch (_) {
            // body optional — the refresh below covers it
        }
    } catch (e) {
        migrating = false;
        showHint(`Network error starting the migration: ${e.message}`, true);
        if (btn) btn.disabled = false;
        return;
    }
    migrating = false;
    cancelClicked = false;
    etaSamples = [];
    etaJobKey = null;
    await refreshStatus();
    // Tiny jobs (store already up to date → cutover only) can finish before
    // this first refresh — show their terminal panel directly.
    const job = status && status.job;
    if (job && job.state === "done") {
        renderJobDone(job);
        return;
    }
    if (job && job.state === "stalled") {
        renderJobStalled(job);
        return;
    }
    routeRender(); // job is running → progress panel + polling
}

// ── Running-job panel ────────────────────────────────────────────

function renderJobPanel() {
    const job = status.job;
    if (!job) return;

    if (etaJobKey !== job.started_at) {
        etaJobKey = job.started_at;
        etaSamples = [];
        // New job identity — a cancel clicked on a PREVIOUS job must not
        // pre-disable this one's Cancel button.
        cancelClicked = false;
    }
    etaSamples.push({ t: Date.now(), done: job.done || 0 });
    if (etaSamples.length > 30) etaSamples.shift();

    const existing = contentEl() && contentEl().querySelector("#ob-emb-jobpanel");
    if (existing) {
        updateJobPanel(job);
        return;
    }

    const label = modelLabel(job.target);
    contentEl().innerHTML = `
        <div class="ob-emb-jobpanel" id="ob-emb-jobpanel">
            <h2 class="ob-emb-jobtitle">Re-embedding to <em>${escapeHtml(label)}</em></h2>
            <div class="ob-emb-progress-track">
                <div class="ob-emb-progress-fill" id="ob-emb-job-fill" style="width: 0%"></div>
            </div>
            <div class="ob-emb-jobmeta">
                <span id="ob-emb-job-count"></span>
                <span id="ob-emb-job-pct"></span>
                <span id="ob-emb-job-eta"></span>
                <span id="ob-emb-job-skipped" hidden></span>
            </div>
            <p class="ob-cli-agent-auth-blurb">
                Search keeps working during the transfer. The switch happens
                automatically when it finishes and survives restarts &mdash;
                you can leave this page.
            </p>
            <button type="button" class="ob-cli-agent-action ob-cli-agent-action-install"
                    id="ob-emb-cancel">Cancel</button>
        </div>
    `;
    contentEl().querySelector("#ob-emb-cancel").addEventListener("click", cancelMigration);
    updateJobPanel(job);
}

function updateJobPanel(job) {
    const c = contentEl();
    if (!c) return;
    const fill = c.querySelector("#ob-emb-job-fill");
    const count = c.querySelector("#ob-emb-job-count");
    const pctEl = c.querySelector("#ob-emb-job-pct");
    const eta = c.querySelector("#ob-emb-job-eta");
    const skipped = c.querySelector("#ob-emb-job-skipped");
    const cancelBtn = c.querySelector("#ob-emb-cancel");
    if (!fill || !count) return;

    // total can GROW mid-job (live mints land in the snapshot index while the
    // backfill walks it) — recompute from scratch every poll, clamp ≤100%.
    const done = job.done || 0;
    const total = job.total || 0;
    const pct = total > 0 ? Math.min(100, Math.floor((done / total) * 100)) : 0;
    fill.style.width = pct + "%";
    count.textContent = `${fmtInt(done)} / ${total > 0 ? fmtInt(total) : "?"} snapshots`;
    if (pctEl) pctEl.textContent = pct + "%";
    if (eta) eta.textContent = etaText(done, total);
    if (skipped) {
        const n = (job.skipped || []).length;
        skipped.hidden = n === 0;
        skipped.textContent = n ? `${fmtInt(n)} skipped` : "";
    }
    if (cancelBtn) {
        if (job.cancel_requested || cancelClicked) {
            cancelBtn.disabled = true;
            cancelBtn.textContent = "Cancelling — finishing current batch…";
        } else {
            cancelBtn.disabled = false;
            cancelBtn.textContent = "Cancel";
        }
    }
}

function etaText(done, total) {
    if (total <= 0 || done >= total) return "";
    if (etaSamples.length < 2) return "ETA: estimating…";
    const first = etaSamples[0];
    const last = etaSamples[etaSamples.length - 1];
    const dt = (last.t - first.t) / 1000;
    const dd = last.done - first.done;
    if (dt <= 0 || dd <= 0) return "ETA: estimating…";
    const secs = Math.ceil((total - done) / (dd / dt));
    if (secs < 90) return `ETA: ~${secs}s`;
    return `ETA: ~${Math.ceil(secs / 60)} min`;
}

async function cancelMigration() {
    cancelClicked = true;
    if (status && status.job) updateJobPanel(status.job);
    try {
        await fetch("/embeddings/migrate/cancel", { method: "POST" });
    } catch (_) {
        // tick() keeps polling; the disabled state self-corrects from
        // job.cancel_requested either way.
    }
}

// ── Terminal job panels ──────────────────────────────────────────

function renderJobDone(job) {
    const label = modelLabel(job.target);
    const racedN = (job.raced || []).length;
    const skippedN = (job.skipped || []).length;
    const racedNote = racedN
        ? `<p class="ob-emb-fresh">Note: ${fmtInt(racedN)} snapshot${racedN === 1 ? " was" : "s were"}
           embedded concurrently by live activity during the migration &mdash; counted once.</p>`
        : "";
    const skippedNote = skippedN
        ? `<p class="ob-emb-fresh ob-emb-fresh-subdued">${fmtInt(skippedN)} snapshot${skippedN === 1 ? "" : "s"}
           could not be embedded and ${skippedN === 1 ? "was" : "were"} quarantined (re-run later to retry).</p>`
        : "";
    contentEl().innerHTML = `
        <div class="ob-emb-jobpanel">
            <h2 class="ob-emb-jobtitle">&check; Switched to <em>${escapeHtml(label)}</em></h2>
            <p class="ob-cli-agent-ready-blurb">
                ${fmtInt(job.done || 0)} snapshots embedded. All searches now use
                <strong>${escapeHtml(status.active || job.target)}</strong>.
            </p>
            ${racedNote}
            ${skippedNote}
            <div class="ob-emb-done-actions">
                <button type="button" class="ob-cta" id="ob-emb-done-continue">
                    Continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                </button>
                <button type="button" class="ob-skip" id="ob-emb-done-back">
                    Back to model picker <span aria-hidden="true">&rarr;</span>
                </button>
            </div>
        </div>
    `;
    contentEl().querySelector("#ob-emb-done-continue").addEventListener("click", ctx.next);
    contentEl().querySelector("#ob-emb-done-back").addEventListener("click", async () => {
        selectedSlug = null;
        validation = null;
        await refreshStatus();
        renderPicker();
    });
}

function renderJobStalled(job) {
    const label = modelLabel(job.target);
    contentEl().innerHTML = `
        <div class="ob-cli-agent-error">
            <p><strong>Migration to ${escapeHtml(label)} stalled:</strong>
            ${escapeHtml(job.error || "unknown error")}</p>
            <p>Re-run to resume &mdash; progress is kept (already-embedded
            snapshots are not re-embedded).</p>
            <p>
                <button type="button" class="ob-cta" id="ob-emb-stalled-retry">Re-run migration</button>
                <button type="button" class="ob-skip" id="ob-emb-stalled-back">
                    Back to model picker <span aria-hidden="true">&rarr;</span>
                </button>
            </p>
        </div>
        <div id="ob-emb-hint-slot"></div>
    `;
    const retry = contentEl().querySelector("#ob-emb-stalled-retry");
    retry.addEventListener("click", () => startMigrate(job.target, retry));
    contentEl().querySelector("#ob-emb-stalled-back").addEventListener("click", async () => {
        await refreshStatus();
        renderPicker();
    });
}

// ── Small helpers ────────────────────────────────────────────────

function modelLabel(slug) {
    const m = (status && status.models || []).find((x) => x.slug === slug);
    return (m && m.label) || slug || "";
}

function showHint(msg, isError) {
    const slot = contentEl() && contentEl().querySelector("#ob-emb-hint-slot");
    if (!slot) return;
    slot.innerHTML = `<div class="ob-cli-agent-hint${isError ? " ob-cli-agent-hint-error" : ""}"></div>`;
    slot.firstElementChild.textContent = msg;
}

async function safeDetail(r) {
    try {
        const body = await r.json();
        return body && body.detail ? String(body.detail) : `HTTP ${r.status}`;
    } catch (_) {
        return `HTTP ${r.status}`;
    }
}

function fmtInt(n) {
    return Number(n).toLocaleString("en-US");
}

function fmtNum(n) {
    // 1.0 → "1", 0.15 → "0.15"
    return String(Number(n));
}

function fmtBytes(n) {
    if (n >= 1e9) return (n / 1e9).toFixed(1) + " GB";
    if (n >= 1e6) return Math.round(n / 1e6) + " MB";
    return Math.round(n / 1e3) + " KB";
}

async function fetchJson(url) {
    try {
        const r = await fetch(url);
        if (!r.ok) return null;
        return await r.json();
    } catch (_) {
        return null;
    }
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
