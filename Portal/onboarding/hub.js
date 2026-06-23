// The console hub (M3). renderHub() paints grouped sections + a readiness
// header from the fast persisted GET /onboarding/status, then opens the SSE
// /status/stream to live-fill each tile as its server-side probe resolves.
// All HTML comes from the presentational status.js — this file is
// orchestration + the SSE lifecycle.

import { groupsHtml, readinessHtml, attentionHtml, applySectionEvent, escapeHtml } from "./status.js";
import { cssEscape } from "./util.js";

let sse = null;  // active EventSource — closed on re-render / unmount

export async function renderHub(container) {
    closeStream();  // idempotent — never leak a prior stream
    container.innerHTML = `<div class="ob-loading">Loading console&hellip;</div>`;

    let data;
    try {
        const r = await fetch("/onboarding/status", { cache: "no-store" });
        if (!r.ok) throw new Error(`/onboarding/status returned ${r.status}`);
        data = await r.json();
    } catch (e) {
        container.innerHTML = `
            <div class="ob-step-error">
                <h2 class="ob-step-title">Console unavailable</h2>
                <p class="ob-step-lede">Couldn't load setup status. Check the service is running, then refresh.</p>
                <p class="ob-step-helper">Error: ${escapeHtml(e.message)}</p>
            </div>`;
        return;
    }

    const sections = data.sections || [];
    const readyCount = data.ready_count ?? 0;
    const total = data.total ?? sections.length;

    container.innerHTML = `
        <section class="ob-hub">
            <div class="ob-hub-head">
                <h1 class="ob-hub-title">Your <em>BlackBox</em> console.</h1>
                <div class="ob-hub-readiness" id="ob-hub-readiness">${readinessHtml(readyCount, total)}</div>
            </div>
            <div class="ob-hub-attention" id="ob-hub-attention">${attentionHtml(data.attention)}</div>
            <div class="ob-hub-groups" id="ob-hub-groups">${groupsHtml(sections)}</div>
        </section>
    `;

    openStream(container);
}

// Live re-validate: open the SSE stream and fill each tile as its probe
// resolves. Reuses the EventSource pattern from done.js's logs modal.
// Fired on view only (no timer). Closed by closeStream() on re-render.
function openStream(container) {
    let es;
    try {
        es = new EventSource("/onboarding/status/stream");
    } catch (_) {
        return;  // SSE unsupported — skeleton stays at persisted state, still usable
    }
    sse = es;

    es.addEventListener("section", (e) => {
        let payload;
        try { payload = JSON.parse(e.data); } catch (_) { return; }
        applySectionEvent(container, payload);
        // A section probe may carry its own attention rows; merge them in.
        if (Array.isArray(payload.attention)) {
            mergeAttention(container, payload.section || payload.key, payload.attention);
        }
    });

    es.addEventListener("done", (e) => {
        let payload;
        try { payload = JSON.parse(e.data); } catch (_) { payload = {}; }
        const meter = container.querySelector("#ob-hub-readiness");
        if (meter && payload.ready_count != null && payload.total != null) {
            meter.innerHTML = readinessHtml(payload.ready_count, payload.total);
        }
        closeStream();  // re-validation complete — no need to hold the connection
    });

    es.onerror = () => {
        // Stream dropped (or completed without a clean close). The persisted
        // skeleton remains valid; just stop. Don't surface an error — this is
        // a progressive enhancement over the fast read.
        closeStream();
    };
}

// Replace any prior attention rows for a section with the freshly-probed set,
// re-rendering the attention header. Keeps the header in sync with live state.
function mergeAttention(container, sectionKey, rows) {
    const host = container.querySelector("#ob-hub-attention");
    if (!host) return;
    // Drop existing rows for this section, then append the new ones.
    host.querySelectorAll(`.ob-attention-row[data-section="${cssEscape(sectionKey)}"]`)
        .forEach((el) => el.remove());
    host.insertAdjacentHTML("beforeend", attentionHtml(rows));
}

export function closeStream() {
    if (sse) {
        try { sse.close(); } catch (_) {}
        sse = null;
    }
}

// Expose for tests/inspection.
export { applySectionEvent };
