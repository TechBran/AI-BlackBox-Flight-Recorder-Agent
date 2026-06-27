// MCP Tool Server step — one guided flow to expose the BlackBox tool catalog as a
// remote MCP server (Claude Code / Codex / Antigravity via bearer, claude.ai web /
// Claude Desktop via OAuth). Wraps the /mcp/* backend: live status, per-operator
// token mint/reveal, one-click service start + public Funnel exposure (auto-derived
// URL, no typing), and ready-to-paste client configs.
//
// Box-infra (service install) is bootstrapped by the installer; this card detects
// it and one-clicks the runtime pieces. Funnel-up is PUBLIC-internet exposure, so
// it's gated behind an explicit inline confirmation.

import { cssEscape } from "../util.js";

let lastMintedToken = null;   // shown ONCE after a mint, for the config snippets

export async function render(container, { next, back, skip, sigil }) {
    container.innerHTML = `
        <section class="ob-step ob-mcp">
            <aside class="ob-step-sigil" aria-hidden="true">
                <div class="ob-step-sigil-num"><em>${sigil ? sigil.num : "11"}</em></div>
                <div class="ob-step-sigil-rule"></div>
                <div class="ob-step-sigil-label">MCP SERVER</div>
            </aside>
            <div class="ob-step-body">
                <div class="ob-step-eyebrow">
                    <span class="ob-step-eyebrow-dot" aria-hidden="true"></span>
                    Remote tool server
                </div>
                <h1 class="ob-step-title">
                    Use the BlackBox as an <em>MCP tool server</em> from any AI app.
                </h1>
                <p class="ob-step-lede">
                    Expose the full tool catalog to Claude Code, Codex, Antigravity
                    (bearer token) and claude.ai / Claude&nbsp;Desktop (OAuth) over your
                    private Tailscale Funnel. Pick an operator, mint a token, and copy
                    the config &mdash; the public URL is detected for you.
                </p>

                <div id="ob-mcp-status" class="ob-cli-agent-meta-row" style="gap:0.5rem; flex-wrap:wrap; margin-bottom:var(--ob-space-5,1.25rem);">
                    <div class="ob-loading">Checking the MCP server&hellip;</div>
                </div>

                <div id="ob-mcp-service"></div>
                <div id="ob-mcp-token" style="margin-top:var(--ob-space-5,1.25rem);"></div>
                <div id="ob-mcp-funnel" style="margin-top:var(--ob-space-5,1.25rem);"></div>
                <div id="ob-mcp-connect" style="margin-top:var(--ob-space-5,1.25rem);"></div>

                <nav class="ob-step-nav" aria-label="Step navigation">
                    <button type="button" class="ob-back" id="ob-mcp-back">
                        <span aria-hidden="true">&larr;</span> Back
                    </button>
                    <button type="button" class="ob-cta" id="ob-mcp-continue">
                        Continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                    </button>
                    <button type="button" class="ob-skip" id="ob-mcp-skip">
                        Skip &mdash; set up later <span aria-hidden="true">&rarr;</span>
                    </button>
                </nav>
            </div>
        </section>
    `;
    container.querySelector("#ob-mcp-back").addEventListener("click", back);
    container.querySelector("#ob-mcp-skip").addEventListener("click", skip);
    container.querySelector("#ob-mcp-continue").addEventListener("click", () => next());

    await refresh(container);
}

async function refresh(container) {
    const [status, ops] = await Promise.all([
        fetchJson("/mcp/status"),
        fetchJson("/operators"),
    ]);
    const s = status || {};
    const operators = (ops && ops.operators) || [];
    renderStatusPips(container, s);
    renderService(container, s);
    await renderTokens(container, operators);
    renderFunnel(container, s);
    await renderConnect(container);
}

// ---- status pips ----
function pip(label, ok, optional) {
    const state = ok ? "ready" : (optional ? "optional" : "attention");
    const mark = ok ? "✓" : (optional ? "–" : "⚠");
    return `<span class="ob-status-pip ob-status-pip-${state}" data-state="${state}"
        style="display:inline-flex;align-items:center;gap:0.35rem;padding:0.2rem 0.6rem;border:1px solid var(--ob-border,#333);border-radius:999px;font-size:0.8rem;">
        <strong aria-hidden="true">${mark}</strong> ${escapeHtml(label)}</span>`;
}
function renderStatusPips(container, s) {
    container.querySelector("#ob-mcp-status").innerHTML =
        pip("Server running", !!s.mcp_up, false) +
        pip("Token", !!s.tokens_present, false) +
        pip("Public (Funnel)", !!s.funnel_up, true) +
        pip("OAuth ready", !!s.oauth_ready, true);
}

// ---- step: service ----
function renderService(container, s) {
    const el = container.querySelector("#ob-mcp-service");
    const svc = s.service || {};
    if (!svc.installed) {
        el.innerHTML = card("1 · Service",
            `<p class="ob-step-helper">The <code>blackbox-mcp.service</code> unit isn't installed on this box.
             It's installed once by the installer (it writes <code>/etc</code>, which the app can't do at runtime).
             Re-run the installer, then re-check.</p>
             <button type="button" class="ob-cta-secondary" data-act="recheck">Re-check</button>`, "attention");
    } else if (s.mcp_up) {
        el.innerHTML = card("1 · Service", `<p class="ob-step-helper">MCP server is <strong>running</strong>.</p>`, "ready");
    } else {
        el.innerHTML = card("1 · Service",
            `<p class="ob-step-helper">The service is installed but not running.</p>
             <button type="button" class="ob-cta-secondary" data-act="start">Start the MCP server</button>`, "attention");
    }
    el.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", async () => {
        b.disabled = true; b.textContent = "Working…";
        if (b.dataset.act === "start") await postJson("/mcp/service/start", {});
        await refresh(container);
    }));
}

// ---- step: token ----
async function renderTokens(container, operators) {
    const el = container.querySelector("#ob-mcp-token");
    const opts = operators.map((o) => `<option value="${escapeHtml(o)}">${escapeHtml(o)}</option>`).join("");
    el.innerHTML = card("2 · Your token",
        `<p class="ob-step-helper">Each token is bound to one operator; the server routes everything that token does through it.</p>
         <div class="ob-cli-agent-meta-row" style="gap:0.6rem;align-items:center;flex-wrap:wrap;">
            <label class="ob-cli-agent-meta-label" for="ob-mcp-op">Operator</label>
            <select id="ob-mcp-op" class="ob-cli-agent-bin" style="padding:0.35rem 0.6rem;background:var(--ob-surface-elevated,#0a0a0a);color:var(--ob-text-primary,#fff);border:1px solid var(--ob-border,#333);">${opts}</select>
            <button type="button" class="ob-cta-secondary" id="ob-mcp-mint">Generate token</button>
         </div>
         <div id="ob-mcp-token-reveal"></div>
         <div id="ob-mcp-token-list" style="margin-top:0.75rem;"></div>`, "");
    el.querySelector("#ob-mcp-mint").addEventListener("click", () => mint(container));
    await renderTokenList(container);
}

async function mint(container) {
    const op = container.querySelector("#ob-mcp-op").value;
    const btn = container.querySelector("#ob-mcp-mint");
    btn.disabled = true; btn.textContent = "Generating…";
    const res = await postJson("/mcp/tokens", { operator: op });
    btn.disabled = false; btn.textContent = "Generate token";
    const reveal = container.querySelector("#ob-mcp-token-reveal");
    if (!res || !res.token) {
        reveal.innerHTML = `<p class="ob-cli-agent-hint ob-cli-agent-hint-error">Couldn't mint a token${res && res.detail ? ": " + escapeHtml(res.detail) : ""}.</p>`;
        return;
    }
    lastMintedToken = res.token;
    reveal.innerHTML =
        `<div class="ob-cli-agent-card" data-state="ready" style="margin-top:0.75rem;">
            <p class="ob-step-helper"><strong>Copy this token now &mdash; it's shown only once.</strong> Bound to ${escapeHtml(res.operator)}.</p>
            <div style="display:flex;gap:0.5rem;align-items:center;">
               <code style="flex:1;word-break:break-all;background:var(--ob-surface-elevated,#0a0a0a);padding:0.5rem;border:1px solid var(--ob-border,#333);">${escapeHtml(res.token)}</code>
               <button type="button" class="ob-cta-secondary" data-copy="${escapeHtml(res.token)}">Copy</button>
            </div>
            ${res.live === false ? `<p class="ob-step-helper">${escapeHtml(res.note || "")}</p>` : ""}
        </div>`;
    wireCopy(reveal);
    await renderTokenList(container);
    await renderConnect(container);   // snippets now show the real token
}

async function renderTokenList(container) {
    const list = container.querySelector("#ob-mcp-token-list");
    if (!list) return;
    const data = await fetchJson("/mcp/tokens");
    const toks = (data && data.tokens) || [];
    if (!toks.length) { list.innerHTML = `<p class="ob-step-helper">No tokens yet.</p>`; return; }
    list.innerHTML = `<p class="ob-step-helper">Existing tokens (the secret is never shown again):</p>` +
        toks.map((t) =>
            `<div class="ob-cli-agent-meta-row" style="justify-content:space-between;gap:0.5rem;padding:0.3rem 0;">
                <code style="font-size:0.8rem;">${escapeHtml(t.token_id)}</code>
                <span>${escapeHtml(t.operator)}</span>
                <button type="button" class="ob-skip" data-revoke="${escapeHtml(t.token_id)}">Revoke</button>
            </div>`).join("");
    list.querySelectorAll("[data-revoke]").forEach((b) => b.addEventListener("click", async () => {
        b.disabled = true;
        await fetch(`/mcp/tokens?token_id=${encodeURIComponent(b.dataset.revoke)}`, { method: "DELETE" });
        await renderTokenList(container);
    }));
}

// ---- step: funnel (public exposure, inline-confirmed) ----
function renderFunnel(container, s) {
    const el = container.querySelector("#ob-mcp-funnel");
    const url = s.public_url || s.derived_public_url || "";
    if (s.funnel_up && url) {
        el.innerHTML = card("3 · Public access",
            `<p class="ob-step-helper">Reachable on the public internet at <code>${escapeHtml(url)}/mcp</code>. OAuth ${s.oauth_ready ? "is ready" : "will be ready once discovery refreshes"}.</p>`, "ready");
        return;
    }
    el.innerHTML = card("3 · Public access",
        `<p class="ob-step-helper">Expose the server over your Tailscale Funnel so remote apps can reach it.
         Detected URL: <code>${escapeHtml(url || "(run `tailscale up` first)")}</code></p>
         <p class="ob-step-helper" style="color:var(--ob-amber,#e0a800);"><strong>This makes the MCP server reachable from the public internet</strong> (auth still required).</p>
         <button type="button" class="ob-cta-secondary" data-act="expose">Expose via Funnel…</button>
         <span id="ob-mcp-expose-confirm"></span>`, "");
    const btn = el.querySelector('[data-act="expose"]');
    btn.addEventListener("click", () => {
        const slot = el.querySelector("#ob-mcp-expose-confirm");
        slot.innerHTML = ` <button type="button" class="ob-cta" data-act="expose-yes">Yes, expose publicly</button>`;
        slot.querySelector('[data-act="expose-yes"]').addEventListener("click", async (e) => {
            e.target.disabled = true; e.target.textContent = "Exposing…";
            await postJson("/mcp/funnel/up", { confirm: true });
            await refresh(container);
        });
    });
}

// ---- step: connect ----
async function renderConnect(container) {
    const el = container.querySelector("#ob-mcp-connect");
    if (!el) return;
    const conn = await fetchJson("/mcp/connection");
    const cfgs = (conn && conn.per_app_configs) || {};
    // Substitute the just-minted token into the snippets (it's only in memory).
    const sub = (txt) => lastMintedToken ? String(txt).replaceAll("<your-token>", lastMintedToken) : txt;
    const blocks = [
        ["Claude Code", cfgs.claude_code],
        ["Codex", cfgs.codex],
        ["Antigravity", cfgs.antigravity],
        ["claude.ai / Claude Desktop (OAuth)", cfgs.claude_desktop_oauth],
    ].filter(([, v]) => v);
    el.innerHTML = card("4 · Connect your app",
        `<p class="ob-step-helper">Paste into your client. ${lastMintedToken ? "Your new token is filled in below." : "Mint a token above to fill in the token."}</p>` +
        blocks.map(([name, body]) =>
            `<div style="margin-bottom:0.75rem;">
                <div class="ob-cli-agent-meta-row" style="justify-content:space-between;">
                    <strong>${escapeHtml(name)}</strong>
                    <button type="button" class="ob-cta-secondary" data-copy="${escapeHtml(sub(body))}">Copy</button>
                </div>
                <pre style="white-space:pre-wrap;word-break:break-word;background:var(--ob-surface-elevated,#0a0a0a);padding:0.5rem;border:1px solid var(--ob-border,#333);font-size:0.8rem;"><code>${escapeHtml(sub(body))}</code></pre>
            </div>`).join(""), "");
    wireCopy(el);
}

// ---- helpers ----
function card(title, inner, state) {
    return `<div class="ob-cli-agent-card"${state ? ` data-state="${state}"` : ""}>
        <div class="ob-cli-agent-head"><div class="ob-cli-agent-title"><span class="ob-cli-agent-name">${escapeHtml(title)}</span></div></div>
        ${inner}</div>`;
}
function wireCopy(scope) {
    scope.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(b.dataset.copy); const t = b.textContent; b.textContent = "Copied ✓"; setTimeout(() => (b.textContent = t), 1200); }
        catch (_) { b.textContent = "Copy failed"; }
    }));
}
async function fetchJson(url) {
    try { const r = await fetch(url); if (!r.ok) return null; return await r.json(); } catch (_) { return null; }
}
async function postJson(url, body) {
    try {
        const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) return { ...j, ok: false, detail: j.detail };
        return j;
    } catch (_) { return null; }
}
function escapeHtml(s) {
    if (s == null) return "";
    return String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}
