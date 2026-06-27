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

// Shared section catalog — the 10 hub/rail sections (welcome+done are NOT
// sections). Order matches onboarding.js STEPS minus welcome/done; the rail
// re-groups via .filter(group) for display. Guarded against STEPS drift by
// Orchestrator/tests/test_onboarding_steps_parity.py. Keep this a plain
// greppable literal (no computed construction) or update that regex.
export const SECTIONS = [
    { key: "tailscale",              group: "network",      label: "Tailnet",     required: false },
    { key: "api_keys",               group: "keys",         label: "API Keys",    required: true  },
    { key: "embeddings",             group: "keys",         label: "Memory",      required: true  },
    { key: "optional_integrations",  group: "capabilities", label: "Extras",      required: false },
    { key: "transcription",          group: "capabilities", label: "Speech",      required: false },
    { key: "web_search",             group: "capabilities", label: "Web Search",  required: false },
    { key: "image",                  group: "capabilities", label: "Image",       required: false },
    { key: "pair_phone",             group: "network",      label: "Pair Phone",  required: false },
    { key: "cli_agents",             group: "capabilities", label: "Agents",      required: false },
    { key: "mcp",                    group: "network",      label: "MCP Server",  required: false },
    { key: "operator",               group: "identity",     label: "Operators",   required: true  },
];

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

// Presentational left-rail navigator (M4). `sections` is the array from GET
// /onboarding/status (already fetched by the hub); we DERIVE NOTHING — state
// comes straight off each section object. Returns the <nav>; the hub mounts it.
export function renderRail(sections) {
    const byKey = new Map((sections || []).map((s) => [s.key, s]));
    const nav = document.createElement("nav");
    nav.className = "ob-rail";
    nav.setAttribute("aria-label", "Section navigator");
    for (const group of GROUP_ORDER) {
        const items = SECTIONS.filter((s) => s.group === group);
        if (!items.length) continue;
        const groupEl = document.createElement("div");
        groupEl.className = "ob-rail-group";
        groupEl.insertAdjacentHTML(
            "beforeend",
            `<div class="ob-rail-group-label">${escapeHtml(GROUP_LABELS[group] || group)}</div>`,
        );
        for (const sec of items) {
            const live = byKey.get(sec.key);
            const state = (live && live.state) || "checking";
            const a = document.createElement("a");
            a.className = "ob-rail-item";
            a.href = `/onboarding/?step=${encodeURIComponent(sec.key)}`;
            a.dataset.key = sec.key;
            a.dataset.state = state;
            a.innerHTML = `
                <span class="ob-rail-pip" aria-hidden="true"></span>
                <span class="ob-rail-label">${escapeHtml(sec.label)}</span>
                ${sec.required ? `<span class="ob-rail-required" title="Required" aria-label="required">&lowast;</span>` : ""}
            `;
            groupEl.appendChild(a);
        }
        nav.appendChild(groupEl);
    }
    return nav;
}

// Live-update one rail item's pip (called from the hub's SSE section handler,
// in lockstep with the tile update).
export function updateRailItem(railEl, key, state) {
    if (!railEl || !key) return;
    const item = railEl.querySelector(`.ob-rail-item[data-key="${cssEscape(key)}"]`);
    if (item && state) item.dataset.state = state;
}

// Surface the Portal HTTPS URL (Android pairing / desktop access) in the hub
// header. Hostname comes from /onboarding/current-config (same source as the
// done summary). Fails open: LAN-only boxes (no BLACKBOX_TAILNET_HOSTNAME)
// render nothing. Returns the element (or null), caller mounts it.
export async function renderPortalUrlCard() {
    let hostname = null;
    try {
        const r = await fetch("/onboarding/current-config");
        if (r.ok) {
            const cfg = await r.json();
            hostname = (cfg && cfg.tailscale && cfg.tailscale.detail
                        && cfg.tailscale.detail.hostname) || null;
        }
    } catch (_) { /* fail open */ }
    if (!hostname) return null;

    const url = `https://${hostname}`;
    const el = document.createElement("div");
    el.className = "ob-hub-url-card";
    el.innerHTML = `
        <span class="ob-hub-url-label">Portal URL</span>
        <code class="ob-hub-url-code">${escapeHtml(url)}</code>
        <button type="button" class="ob-hub-url-copy" aria-label="Copy Portal URL">Copy</button>
    `;
    el.querySelector(".ob-hub-url-copy").addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        try { await navigator.clipboard.writeText(url); btn.textContent = "Copied ✓"; }
        catch (_) { btn.textContent = "Copy failed"; }
        setTimeout(() => { btn.textContent = "Copy"; }, 1500);
    });
    return el;
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
