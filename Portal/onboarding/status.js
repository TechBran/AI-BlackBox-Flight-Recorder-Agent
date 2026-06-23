// PRESENTATIONAL ONLY. Consumes GET /onboarding/status (M1 backend rollup).
// Contains NO derivation logic — state, summary, and attention rows are all
// pre-computed server-side. This file just turns the payload into HTML.

import { cssEscape } from "./util.js";

const GROUP_ORDER = ["network", "keys", "capabilities", "identity"];
const GROUP_LABELS = {
    network: "Network & Access",
    keys: "Keys & Models",
    capabilities: "Capabilities",
    identity: "Identity",
};

const PIP_TEXT = {
    ready: "✓ Ready",
    attention: "⚠ Attention",
    optional: "– Optional",
    checking: "Checking…",
};

export function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}

// One tile. `state`/`summary` come straight from the payload.
export function sectionCardHtml(section) {
    const state = section.state || "checking";
    const summary = section.summary || (state === "checking" ? "Checking…" : "");
    const href = `/onboarding/?step=${encodeURIComponent(section.key)}`;
    return `
        <a class="ob-section-card" href="${href}"
           data-section="${escapeHtml(section.key)}" data-state="${escapeHtml(state)}">
            <div class="ob-section-card-head">
                <span class="ob-section-card-label">${escapeHtml(section.label)}</span>
                <span class="ob-section-card-pip">${escapeHtml(PIP_TEXT[state] || PIP_TEXT.checking)}</span>
            </div>
            <div class="ob-section-card-summary">${escapeHtml(summary)}</div>
        </a>
    `;
}

// Grouped section grids, in canonical group order.
export function groupsHtml(sections) {
    return GROUP_ORDER.map((group) => {
        const inGroup = sections.filter((s) => s.group === group);
        if (inGroup.length === 0) return "";
        return `
            <div class="ob-hub-group" data-group="${group}">
                <div class="ob-hub-group-label">${escapeHtml(GROUP_LABELS[group] || group)}</div>
                <div class="ob-section-grid">
                    ${inGroup.map(sectionCardHtml).join("")}
                </div>
            </div>
        `;
    }).join("");
}

// Readiness meter line.
export function readinessHtml(readyCount, total) {
    return `<em>${escapeHtml(String(readyCount))}</em> of ${escapeHtml(String(total))} ready`;
}

// Attention header rows (warn/error), each with a "Set up" CTA.
export function attentionHtml(attention) {
    if (!attention || attention.length === 0) return "";
    return attention.map((a) => {
        const sev = a.severity === "error" ? "error" : "warn";
        const cta = a.cta_step
            ? `<a class="ob-attention-cta" href="/onboarding/?step=${encodeURIComponent(a.cta_step)}">Set up &rarr;</a>`
            : "";
        return `
            <div class="ob-attention-row" data-severity="${sev}" data-section="${escapeHtml(a.section || "")}">
                <span class="ob-attention-msg">${escapeHtml(a.message)}</span>
                ${cta}
            </div>
        `;
    }).join("");
}

// Live SSE fill — update one tile in place from an `event: section` payload.
export function applySectionEvent(root, ev) {
    if (!ev || !ev.key) return;
    const card = root.querySelector(`.ob-section-card[data-section="${cssEscape(ev.key)}"]`);
    if (!card) return;
    if (ev.state) card.setAttribute("data-state", ev.state);
    const pip = card.querySelector(".ob-section-card-pip");
    if (pip && ev.state) pip.textContent = PIP_TEXT[ev.state] || PIP_TEXT.checking;
    const sum = card.querySelector(".ob-section-card-summary");
    if (sum && typeof ev.summary === "string") sum.textContent = ev.summary;
}
