// Tailscale step — second screen of the onboarding wizard.
//
// Probes POST /onboarding/validate {provider:"tailscale"} on mount and
// dispatches to one of three primary branches based on the result:
//
//   A. ok=true → Tailscale is configured and authenticated.
//      Show success badge with hostname + IP, persist hostname to .env
//      via /onboarding/save (BLACKBOX_TAILNET_HOSTNAME), primary CTA = Continue.
//
//   B. ok=false + error includes "binary not found" → Tailscale not installed.
//      Action card with copyable install command + Re-check button (re-probes).
//
//   C. ok=false + error includes "BackendState" (or any other error fallback) →
//      Tailscale installed but not authenticated. Action card with copyable
//      `sudo tailscale up` + Re-check button.
//
// Branch D (the "Don't have a Tailscale account yet?" disclosure) is rendered
// in ALL branches as an always-visible <details> block. Default-open in B+C
// (user likely needs an account); default-closed in A (user is already on tailnet).
//
// Visual reference: Portal/onboarding/_mocks/tailscale.html (shows Branch C
// with the disclosure expanded).

export async function render(container, { next, back, skip }) {
    // Initial loading state
    container.innerHTML = `
        <section class="ob-step ob-tailscale">
            <aside class="ob-step-sigil" aria-hidden="true">
                <div class="ob-step-sigil-num"><em>02</em></div>
                <div class="ob-step-sigil-rule"></div>
                <div class="ob-step-sigil-label">Tailnet</div>
            </aside>
            <div class="ob-step-body">
                <div class="ob-step-eyebrow">Private mesh networking</div>
                <h1 class="ob-step-title">
                    Connect your <em>tailnet</em>.
                </h1>
                <p class="ob-step-lede">
                    Your BlackBox needs to join your private Tailscale network so
                    your phone, laptop, and any other devices can reach it
                    securely &mdash; without exposing anything to the public
                    internet.
                </p>
                <div id="ob-tailscale-status">
                    <div class="ob-loading">Checking Tailscale status&hellip;</div>
                </div>
            </div>
        </section>
    `;

    // Probe the validator. Network errors are not fatal — fall through to
    // Branch C with the raw error in the badge so the customer can re-check.
    let result;
    try {
        const r = await fetch("/onboarding/validate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ provider: "tailscale", credentials: {} }),
        });
        result = await r.json();
    } catch (e) {
        result = { ok: false, error: `Network error: ${e.message}` };
    }

    const statusEl = document.getElementById("ob-tailscale-status");
    const recheck = () => render(container, { next, back, skip });

    if (result.ok) {
        renderBranchA(statusEl, result, { next, back, skip });
    } else if (typeof result.error === "string" && result.error.includes("binary not found")) {
        renderBranchB(statusEl, result, { back, skip, recheck });
    } else {
        // BackendState mismatch OR any other error → "installed, needs auth".
        // Re-checking is the recovery path.
        renderBranchC(statusEl, result, { back, skip, recheck });
    }
}

// ── Branch A: already configured ────────────────────────────────
async function renderBranchA(statusEl, result, { next, back, skip }) {
    const hostname = (result.detail && result.detail.hostname) || "unknown";
    const ip = (result.detail && result.detail.ip) || "unknown";

    // Persist hostname so other code can find the tailnet name. Non-fatal:
    // if /save fails (disk full, permissions, etc.), the customer can still
    // continue; the hostname just won't be in .env.
    try {
        await fetch("/onboarding/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ secrets: { BLACKBOX_TAILNET_HOSTNAME: hostname } }),
        });
    } catch (e) {
        console.warn("Couldn't persist BLACKBOX_TAILNET_HOSTNAME:", e);
    }

    statusEl.innerHTML = `
        <div class="ob-status-badge ob-status-badge-success" role="status">
            <span class="ob-status-badge-pip" aria-hidden="true">&check;</span>
            <span class="ob-status-badge-label">
                Tailscale online &mdash; <strong>${escapeHtml(hostname)}</strong>
            </span>
            <span class="ob-status-badge-version">${escapeHtml(ip)}</span>
        </div>
        <p class="ob-step-helper">
            Your BlackBox is reachable on your tailnet at
            <code>${escapeHtml(hostname)}</code>.
            We've saved this so other parts of the system can find it.
        </p>
        <div class="ob-cta-row">
            <button type="button" class="ob-cta" id="ob-tailscale-continue">
                Continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
            </button>
        </div>
        ${renderDisclosure(false)}
        ${renderStepNav({ showSkip: false })}
    `;

    document.getElementById("ob-tailscale-continue").addEventListener("click", next);
    wireStepNav(statusEl, { back, skip });
}

// ── Branch B: not installed ──────────────────────────────────────
function renderBranchB(statusEl, result, { back, skip, recheck }) {
    const installCmd = "curl -fsSL https://tailscale.com/install.sh | sudo sh";
    statusEl.innerHTML = `
        <div class="ob-status-badge ob-status-badge-error" role="status">
            <span class="ob-status-badge-pip" aria-hidden="true">!</span>
            <span class="ob-status-badge-label">Tailscale not installed</span>
        </div>
        <div class="ob-action-card">
            <div class="ob-action-card-header">
                <span class="ob-action-card-label">Run on this device</span>
                <span class="ob-action-card-os">install tailscale</span>
            </div>
            <div class="ob-code-block">
                <span class="ob-code-prompt" aria-hidden="true">$</span>
                <code class="ob-code-cmd">${escapeHtml(installCmd)}</code>
                <button type="button" class="ob-copy-btn" data-copy="${escapeHtml(installCmd)}">
                    <span aria-hidden="true">&#9112;</span> Copy
                </button>
            </div>
        </div>
        <div class="ob-cta-row">
            <button type="button" class="ob-cta" id="ob-recheck-btn">
                Re-check status <span class="ob-cta-arrow" aria-hidden="true">&#x21bb;</span>
            </button>
        </div>
        ${renderDisclosure(true)}
        ${renderStepNav({ showSkip: true })}
    `;
    wireCopyBtn(statusEl);
    document.getElementById("ob-recheck-btn").addEventListener("click", recheck);
    wireStepNav(statusEl, { back, skip });
}

// ── Branch C: installed but not authenticated ───────────────────
function renderBranchC(statusEl, result, { back, skip, recheck }) {
    const authCmd = "sudo tailscale up";
    const errMsg = (result.error || "needs authentication").replace(/^RuntimeError:\s*/, "");
    statusEl.innerHTML = `
        <div class="ob-status-badge" role="status">
            <span class="ob-status-badge-pip" aria-hidden="true">!</span>
            <span class="ob-status-badge-label">
                Tailscale installed &mdash; not authenticated
            </span>
            <span class="ob-status-badge-version">${escapeHtml(errMsg)}</span>
        </div>
        <div class="ob-action-card">
            <div class="ob-action-card-header">
                <span class="ob-action-card-label">Run on this device</span>
                <span class="ob-action-card-os">authenticate</span>
            </div>
            <div class="ob-code-block">
                <span class="ob-code-prompt" aria-hidden="true">$</span>
                <code class="ob-code-cmd">${escapeHtml(authCmd)}</code>
                <button type="button" class="ob-copy-btn" data-copy="${escapeHtml(authCmd)}">
                    <span aria-hidden="true">&#9112;</span> Copy
                </button>
            </div>
        </div>
        <div class="ob-cta-row">
            <button type="button" class="ob-cta" id="ob-recheck-btn">
                Re-check status <span class="ob-cta-arrow" aria-hidden="true">&#x21bb;</span>
            </button>
        </div>
        ${renderDisclosure(true)}
        ${renderStepNav({ showSkip: true })}
    `;
    wireCopyBtn(statusEl);
    document.getElementById("ob-recheck-btn").addEventListener("click", recheck);
    wireStepNav(statusEl, { back, skip });
}

// ── Disclosure (Branch D — always rendered, default-open or closed) ──
function renderDisclosure(openByDefault) {
    return `
        <details class="ob-disclosure" ${openByDefault ? "open" : ""}>
            <summary class="ob-disclosure-summary">
                <span class="ob-disclosure-q">
                    Don't have a <em>Tailscale</em> account yet?
                </span>
                <span class="ob-disclosure-toggle" aria-hidden="true">Show / Hide</span>
            </summary>
            <div class="ob-disclosure-body">
                <div class="ob-disclosure-prose">
                    <p>
                        Tailscale is a peer-to-peer mesh VPN that gives every
                        device you own a stable private IP. It's the easiest
                        way to reach your BlackBox from anywhere &mdash; coffee
                        shop, hotel, your phone on cellular &mdash; without
                        opening firewall ports or exposing it to the public
                        internet.
                    </p>
                    <p>
                        The free Personal plan covers up to 100 devices and 3
                        users, which is more than enough for a household. Sign
                        in with Google, GitHub, Apple, or Microsoft &mdash; no
                        separate password.
                    </p>
                    <p>
                        <a class="ob-disclosure-link"
                           href="https://login.tailscale.com/start"
                           target="_blank" rel="noopener">
                            Create a free Tailscale account
                        </a>
                    </p>
                </div>
                <aside class="ob-disclosure-aside" aria-label="Free plan summary">
                    <div class="ob-disclosure-aside-label">Free plan includes</div>
                    <ul class="ob-disclosure-aside-list">
                        <li>Up to 100 devices</li>
                        <li>Up to 3 users</li>
                        <li>End-to-end WireGuard encryption</li>
                        <li>MagicDNS &amp; HTTPS certs</li>
                        <li>SSO via Google, GitHub, Apple</li>
                    </ul>
                </aside>
            </div>
        </details>
    `;
}

// ── Step nav (bottom row: Back ← / Skip → ) ──────────────────────
function renderStepNav({ showSkip }) {
    const skipBtn = showSkip
        ? `<button type="button" class="ob-skip" id="ob-tailscale-skip">
               Skip &mdash; I'll use LAN-only mode
               <span class="ob-skip-arrow" aria-hidden="true">&rarr;</span>
           </button>`
        : "";
    return `
        <nav class="ob-step-nav" aria-label="Step navigation">
            <button type="button" class="ob-back" id="ob-tailscale-back">Back to welcome</button>
            ${skipBtn}
        </nav>
    `;
}

function wireStepNav(scope, { back, skip }) {
    const backBtn = scope.querySelector("#ob-tailscale-back");
    if (backBtn && back) backBtn.addEventListener("click", back);
    const skipBtn = scope.querySelector("#ob-tailscale-skip");
    if (skipBtn && skip) skipBtn.addEventListener("click", skip);
}

// ── Copy button: clipboard + transient "Copied" feedback ─────────
function wireCopyBtn(scope) {
    scope.querySelectorAll(".ob-copy-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
            const text = btn.dataset.copy || "";
            const orig = btn.innerHTML;
            try {
                await navigator.clipboard.writeText(text);
                btn.textContent = "Copied ✓";
            } catch (e) {
                btn.textContent = "Copy failed";
            }
            setTimeout(() => {
                btn.innerHTML = orig;
            }, 1500);
        });
    });
}

// ── HTML escape (step modules are isolated; shell has its own copy) ──
function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
}
