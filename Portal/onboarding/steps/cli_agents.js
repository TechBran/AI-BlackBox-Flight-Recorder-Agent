// CLI Agents step — sixth screen of the onboarding wizard.
// Three CLI providers ship with the BlackBox: Anthropic Claude Code,
// Google Gemini CLI, and OpenAI Codex. Install.sh's Step 1c npm-installs
// the binaries at first boot, but each provider requires a one-time
// interactive auth (Anthropic Console / Google OAuth / OpenAI auth)
// that can't be automated. This step:
//
//   1. GET /onboarding/cli-agent/status — per-provider {installed, auth}
//   2. Render 3 provider cards. Each card shows status badges + actions:
//      - If !installed: "Install" button → spawns terminal running npm install
//      - If installed but !auth: "Sign in" button → spawns terminal running
//        the provider's interactive login command
//      - If installed AND auth: green checkmark, no action
//   3. "Re-check status" button re-fetches /status (user runs it after
//      finishing auth in the spawned terminal)
//   4. Continue button enabled when all 3 providers are ready
//   5. Skip option always available (advanced users who want a subset)
//
// Wizard does NOT block the terminal — once spawned, the user does
// their auth flow in gnome-terminal independently and comes back to
// click Re-check. Same passive UX as the api_keys validate buttons.

const PROVIDERS = [
    {
        key: "claude",
        name: "Claude Code",
        vendor: "Anthropic",
        package: "@anthropic-ai/claude-code",
        authBlurb: "Sign in via your Anthropic Console account.",
    },
    {
        key: "gemini",
        name: "Gemini CLI",
        vendor: "Google",
        package: "@google/gemini-cli",
        authBlurb: "Sign in with your Google account (OAuth).",
    },
    {
        key: "codex",
        name: "Codex",
        vendor: "OpenAI",
        package: "@openai/codex",
        authBlurb: "Sign in via your OpenAI account.",
    },
];

let lastStatus = null;  // cached most recent /status response
let busyProvider = null;  // provider key whose terminal-spawn is in flight

export async function render(container, { next, back, skip }) {
    container.innerHTML = `
        <section class="ob-step ob-cli-agents">
            <aside class="ob-step-sigil" aria-hidden="true">
                <div class="ob-step-sigil-num"><em>06</em></div>
                <div class="ob-step-sigil-rule"></div>
                <div class="ob-step-sigil-label">AGENTS</div>
            </aside>
            <div class="ob-step-body">
                <div class="ob-step-eyebrow">
                    <span class="ob-step-eyebrow-dot" aria-hidden="true"></span>
                    Terminal model providers
                </div>
                <h1 class="ob-step-title">
                    Sign into your <em>CLI agents</em>.
                </h1>
                <p class="ob-step-lede">
                    The BlackBox ships with three command-line AI agents from
                    Anthropic, Google, and OpenAI. They're already installed
                    &mdash; each one just needs a one-time sign-in to your
                    account. Click <strong>Sign in</strong>, finish the prompt
                    in the terminal that opens, then come back here and click
                    <strong>Re-check</strong>.
                </p>
                <div id="ob-cli-agent-grid" class="ob-cli-agent-grid">
                    <div class="ob-loading">Probing CLI agents&hellip;</div>
                </div>
                <div class="ob-cli-agent-toolbar">
                    <button type="button" class="ob-cta ob-cta-secondary" id="ob-cli-agent-recheck">
                        Re-check status <span class="ob-cta-arrow" aria-hidden="true">&#8635;</span>
                    </button>
                </div>
                <nav class="ob-step-nav" aria-label="Step navigation">
                    <button type="button" class="ob-back" id="ob-cli-back">
                        <span aria-hidden="true">&larr;</span> Back to pairing
                    </button>
                    <button type="button" class="ob-cta" id="ob-cli-continue" disabled>
                        Continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                    </button>
                    <button type="button" class="ob-skip" id="ob-cli-skip">
                        Skip &mdash; set up later <span aria-hidden="true">&rarr;</span>
                    </button>
                </nav>
            </div>
        </section>
    `;

    document.getElementById("ob-cli-back").addEventListener("click", back);
    document.getElementById("ob-cli-skip").addEventListener("click", skip);
    document.getElementById("ob-cli-continue").addEventListener("click", next);
    document.getElementById("ob-cli-agent-recheck").addEventListener("click", () => {
        refreshStatus(container);
    });

    await refreshStatus(container);
}

async function refreshStatus(container) {
    const grid = container.querySelector("#ob-cli-agent-grid");
    const recheckBtn = container.querySelector("#ob-cli-agent-recheck");
    if (recheckBtn) {
        recheckBtn.disabled = true;
        const arrow = recheckBtn.querySelector(".ob-cta-arrow");
        if (arrow) arrow.style.animation = "ob-spin 0.8s linear infinite";
    }
    try {
        const r = await fetch("/onboarding/cli-agent/status");
        if (!r.ok) throw new Error(`status returned ${r.status}`);
        lastStatus = await r.json();
        renderGrid(container, lastStatus);
    } catch (e) {
        grid.innerHTML = `
            <div class="ob-cli-agent-error">
                <p>Couldn't probe CLI agent status: ${escapeHtml(e.message)}</p>
                <p class="ob-step-helper">The wizard service may be reloading. Try Re-check in a few seconds.</p>
            </div>
        `;
    } finally {
        if (recheckBtn) {
            recheckBtn.disabled = false;
            const arrow = recheckBtn.querySelector(".ob-cta-arrow");
            if (arrow) arrow.style.animation = "";
        }
    }
}

function renderGrid(container, status) {
    const grid = container.querySelector("#ob-cli-agent-grid");
    grid.innerHTML = PROVIDERS.map((p) => renderCard(p, status.providers[p.key] || {})).join("");

    // Wire per-card buttons. Buttons are dynamically rendered so we
    // attach AFTER innerHTML write.
    PROVIDERS.forEach((p) => {
        const installBtn = grid.querySelector(`#ob-cli-install-${p.key}`);
        const authBtn = grid.querySelector(`#ob-cli-auth-${p.key}`);
        if (installBtn) {
            installBtn.addEventListener("click", () => spawnTerminal(container, p, "install"));
        }
        if (authBtn) {
            authBtn.addEventListener("click", () => spawnTerminal(container, p, "auth"));
        }
    });

    // Enable Continue when all three providers are ready.
    const cont = container.querySelector("#ob-cli-continue");
    if (cont) cont.disabled = !status.ready;
}

function renderCard(provider, state) {
    const installed = !!state.installed;
    const authed = !!state.authenticated;
    const ready = installed && authed;
    const busy = busyProvider === provider.key;

    let statusBadge;
    if (ready) {
        statusBadge = `<span class="ob-cli-agent-badge ob-cli-agent-badge-ok">&check; Ready</span>`;
    } else if (installed && !authed) {
        statusBadge = `<span class="ob-cli-agent-badge ob-cli-agent-badge-needs-auth">! Needs sign-in</span>`;
    } else {
        statusBadge = `<span class="ob-cli-agent-badge ob-cli-agent-badge-missing">&times; Not installed</span>`;
    }

    const installRow = installed
        ? `<div class="ob-cli-agent-meta-row">
              <span class="ob-cli-agent-meta-label">Installed</span>
              <code class="ob-cli-agent-bin">${escapeHtml(state.bin_path || "")}</code>
           </div>`
        : `<div class="ob-cli-agent-meta-row">
              <span class="ob-cli-agent-meta-label">Package</span>
              <code class="ob-cli-agent-bin">${escapeHtml(provider.package)}</code>
           </div>`;

    let actions = "";
    if (!installed) {
        actions = `
            <button type="button" class="ob-cta ob-cli-agent-action ob-cli-agent-action-install"
                    id="ob-cli-install-${provider.key}" ${busy ? "disabled" : ""}>
                ${busy ? "Opening terminal&hellip;" : "Install"}
                <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
            </button>
        `;
    } else if (!authed) {
        actions = `
            <p class="ob-cli-agent-auth-blurb">${escapeHtml(provider.authBlurb)}</p>
            <button type="button" class="ob-cta ob-cli-agent-action ob-cli-agent-action-auth"
                    id="ob-cli-auth-${provider.key}" ${busy ? "disabled" : ""}>
                ${busy ? "Opening terminal&hellip;" : "Sign in"}
                <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
            </button>
        `;
    } else {
        actions = `
            <p class="ob-cli-agent-ready-blurb">All set. Available in the BlackBox CLI Agent panel.</p>
        `;
    }

    return `
        <div class="ob-cli-agent-card" data-state="${ready ? "ready" : installed ? "needs-auth" : "missing"}">
            <div class="ob-cli-agent-head">
                <div class="ob-cli-agent-title">
                    <span class="ob-cli-agent-name">${escapeHtml(provider.name)}</span>
                    <span class="ob-cli-agent-vendor">${escapeHtml(provider.vendor)}</span>
                </div>
                ${statusBadge}
            </div>
            ${installRow}
            <div class="ob-cli-agent-actions">${actions}</div>
        </div>
    `;
}

async function spawnTerminal(container, provider, mode) {
    if (busyProvider) return;
    busyProvider = provider.key;
    // Re-render with the busy state on the relevant button
    if (lastStatus) renderGrid(container, lastStatus);

    let result;
    try {
        const r = await fetch("/onboarding/cli-agent/spawn-terminal", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ provider: provider.key, mode }),
        });
        result = await r.json();
    } catch (e) {
        result = { ok: false, error: e.message };
    } finally {
        busyProvider = null;
    }

    if (result && result.ok) {
        showHint(container,
            mode === "install"
                ? `Installing ${provider.name} in a new terminal. Re-check when it finishes.`
                : `Sign-in terminal opened for ${provider.name}. Finish the prompts there, then click Re-check.`);
    } else {
        const err = (result && (result.stderr || result.error)) || "unknown error";
        showHint(container, `Couldn't open terminal for ${provider.name}: ${err}`, true);
    }
    // Always re-render to clear the per-button "Opening terminal…" label.
    if (lastStatus) renderGrid(container, lastStatus);
}

function showHint(container, msg, isError) {
    let hint = container.querySelector("#ob-cli-agent-hint");
    if (!hint) {
        hint = document.createElement("div");
        hint.id = "ob-cli-agent-hint";
        hint.className = "ob-cli-agent-hint";
        const toolbar = container.querySelector(".ob-cli-agent-toolbar");
        if (toolbar) toolbar.insertAdjacentElement("afterend", hint);
    }
    hint.classList.toggle("ob-cli-agent-hint-error", !!isError);
    hint.textContent = msg;
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
