// The console hub (M3). renderHub() paints grouped sections + a readiness
// header from the fast persisted GET /onboarding/status, then opens the SSE
// /status/stream to live-fill each tile as its server-side probe resolves.
// All HTML comes from the presentational status.js — this file is
// orchestration + the SSE lifecycle.

import { groupsHtml, readinessHtml, attentionHtml, applySectionEvent, renderRail, updateRailItem, renderPortalUrlCard, escapeHtml } from "./status.js";
import { cssEscape } from "./util.js";
import { openLogsModal, mountRestartControl, mountUpdatesBadge } from "./hub-controls.js";

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
                <div class="ob-hub-head-main">
                    <h1 class="ob-hub-title">Your <em>BlackBox</em> console.</h1>
                    <div class="ob-hub-readiness" id="ob-hub-readiness">${readinessHtml(readyCount, total)}</div>
                </div>
                <div class="ob-hub-toolbar" id="ob-hub-toolbar"></div>
            </div>
            <div class="ob-hub-attention" id="ob-hub-attention">${attentionHtml(data.attention)}</div>
            <div class="ob-hub-body">
                <div id="ob-hub-rail"></div>
                <div class="ob-hub-groups" id="ob-hub-groups">${groupsHtml(sections)}</div>
            </div>
        </section>
    `;

    // Mount the left-rail navigator (M4) into the two-column body.
    const railHost = container.querySelector("#ob-hub-rail");
    if (railHost) railHost.appendChild(renderRail(sections));

    // Console-grade header affordance (M4): the Portal HTTPS URL (fail-open —
    // LAN-only boxes render nothing).
    const toolbar = container.querySelector("#ob-hub-toolbar");
    if (toolbar) {
        const urlCard = await renderPortalUrlCard();
        if (urlCard) toolbar.appendChild(urlCard);

        // Operational controls — the same four affordances as the done step,
        // reusing the shared (battle-tested) hub-controls.js implementation.
        // The hub is post-completion (the sentinel is already written), so
        // Open Portal is a plain /ui link — do NOT re-POST /onboarding/complete.
        const controls = document.createElement("div");
        controls.className = "ob-hub-controls";
        controls.innerHTML = `
            <a class="ob-cta-secondary ob-hub-open-portal" href="/ui">Open Portal &rarr;</a>
            <button type="button" class="ob-cta-secondary" id="ob-hub-logs">View Logs</button>
            <button type="button" class="ob-cta ob-cta-restart" id="ob-hub-restart" hidden>
                <span class="ob-cta-restart-label">Restart Service</span>
                <span class="ob-cta-arrow" aria-hidden="true">&#x21bb;</span>
            </button>
            <span class="ob-restart-status" id="ob-hub-restart-status" hidden></span>
        `;
        toolbar.appendChild(controls);

        // Updates badge — its own full-width host below the toolbar row (the
        // banner reads as a notice, not an inline control). Reuses the
        // .ob-done-updates / .ob-update-banner styles already in the sheet.
        const updatesHost = document.createElement("div");
        updatesHost.id = "ob-hub-updates";
        updatesHost.className = "ob-done-updates";
        updatesHost.hidden = true;
        toolbar.appendChild(updatesHost);

        // Wire the shared controls (parameterized by element refs).
        const logsBtn = controls.querySelector("#ob-hub-logs");
        if (logsBtn) logsBtn.addEventListener("click", openLogsModal);
        mountRestartControl(
            controls.querySelector("#ob-hub-restart"),
            controls.querySelector("#ob-hub-restart-status"),
        );
        mountUpdatesBadge(updatesHost);
    }

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
        updateRailItem(container.querySelector(".ob-rail"), payload.key, payload.state);
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
