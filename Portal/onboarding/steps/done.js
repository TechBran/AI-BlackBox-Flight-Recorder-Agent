// Done step — final screen of the onboarding wizard.
// Renders a summary of what was configured/skipped, then a single CTA
// that POSTs /onboarding/complete (writes the sentinel that lets
// FirstRunMiddleware stop redirecting /ui) and navigates to Portal.
//
// Visual: editorial gravitas. Sigil "07" + "DONE" label. Big Fraunces
// title "All set." with italic-red on a key word. Summary list with
// status pips. Big "Open Portal →" CTA. The customer has crossed a
// threshold; the system is now theirs.

import { openLogsModal, mountRestartControl, mountUpdatesBadge } from "../hub-controls.js";

// Per-render busy state — module globals would survive across remounts and
// silently dead-lock the buttons. Reset each render. (The restart busy guard
// now lives module-level inside hub-controls.js — one restart at a time.)
let rs = { busy: false };

export async function render(container, { next, back, skip, sigil }) {
    rs = { busy: false };
    container.innerHTML =`
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
    // Source is the backend /onboarding/status rollup (M1) — the single source
    // of the section list, so done shows every section (incl. embeddings/STT/
    // web_search/image/agents) and each row deep-links to its step.
    // Presentational only: no derivation here.
    const status = await loadStatus();
    renderSummary(container, status);

    // Wire the Open Portal CTA
    const openBtn = document.getElementById("ob-done-open");
    openBtn.disabled = false;  // enable once summary loaded
    openBtn.addEventListener("click", () => completeAndOpen(openBtn));

    // E9: status-aware Restart Service button. Probe drift detection — if
    // any .env value differs from the running process's in-memory constant,
    // the customer's wizard changes haven't taken effect yet for chat
    // handlers. Show the actionable restart button in that case; otherwise
    // show the passive "up to date" indicator. (Shared impl in hub-controls.js.)
    mountRestartControl(
        document.getElementById("ob-done-restart"),
        document.getElementById("ob-done-restart-status"),
    );

    // E10: View Logs button — opens a console-style modal streaming live
    // blackbox.service logs via SSE. Advanced users + customer-support
    // diagnostic affordance. (Shared impl in hub-controls.js.)
    const logsBtn = document.getElementById("ob-view-logs-btn");
    if (logsBtn) logsBtn.addEventListener("click", openLogsModal);

    // T9: update-available badge. Best-effort — failures stay silent so a
    // git-not-initialized or rate-limited /update/status never blocks the
    // happy "you're done!" path. (Shared impl in hub-controls.js.)
    mountUpdatesBadge(document.getElementById("ob-done-updates"));
}

async function loadStatus() {
    try {
        const r = await fetch("/onboarding/status");
        if (!r.ok) return { error: `/onboarding/status returned ${r.status}` };
        return await r.json();
    } catch (e) {
        return { error: e.message };
    }
}

function renderSummary(container, status) {
    const summaryEl = container.querySelector("#ob-done-summary");
    if (!status || status.error || !Array.isArray(status.sections)) {
        summaryEl.innerHTML = `
            <p class="ob-step-helper">
                Couldn't load the summary (${escapeHtml((status && status.error) || "unknown error")}).
                Setup is still complete &mdash; clicking Open Portal works.
            </p>
        `;
        return;
    }

    const rows = status.sections.map(summaryRow).join("");
    summaryEl.innerHTML = `
        <ul class="ob-summary-list">
            ${rows}
        </ul>
    `;
}

// Glyph per status state — presentational mapping only.
const STATE_GLYPH = {
    ready: "&check;",
    attention: "!",
    optional: "&#8856;",   // ⊘
    checking: "&hellip;",
};

// Maps one /onboarding/status section to a clickable summary row. The <li>
// keeps .ob-summary-list semantics; the <a> is the row and deep-links to
// ?step=<key>. Glyph + accent derive ONLY from section.state; the one-line
// summary string is rendered verbatim from the backend (no derivation here).
function summaryRow(section) {
    const state = section.state || "optional";
    const glyph = STATE_GLYPH[state] || "&#8856;";
    const label = section.label || section.key || "";
    const detail = section.summary || "";
    const href = `/onboarding/?step=${encodeURIComponent(section.step || section.key)}`;
    const showCta = state === "attention";
    const ctaClass = showCta ? " ob-summary-has-cta" : "";
    const cta = showCta
        ? `<span class="ob-summary-cta" aria-hidden="true">Set up &rarr;</span>`
        : "";
    return `
        <li>
            <a class="ob-summary-row${ctaClass}" data-state="${escapeHtml(state)}"
               href="${escapeHtml(href)}">
                <span class="ob-summary-glyph" aria-hidden="true">${glyph}</span>
                <span class="ob-summary-label">${escapeHtml(label)}</span>
                <span class="ob-summary-detail">${escapeHtml(detail)}</span>
                ${cta}
            </a>
        </li>
    `;
}

async function completeAndOpen(btn) {
    if (rs.busy) return;
    rs.busy = true;
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
        rs.busy = false;
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
