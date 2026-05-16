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

let recheckBusy = false;
let _currentAuthAbort = null;
let _authInFlight = false;

export async function render(container, { next, back, skip }) {
    // C1: abort any in-flight auth poll loop from a prior render of this step
    if (_currentAuthAbort) _currentAuthAbort.aborted = true;
    _currentAuthAbort = null;

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

    // Re-entrancy guard — prevents double-click on Re-check from firing
    // two concurrent validator probes. The orchestrator's `busy` flag is
    // held only for the initial render call, not subsequent re-checks
    // fired from inside the step.
    const guardedRecheck = async () => {
        if (recheckBusy) return;
        recheckBusy = true;
        try {
            await render(container, { next, back, skip });
        } finally {
            recheckBusy = false;
        }
    };

    if (result.ok) {
        renderBranchA(statusEl, result, { next, back, skip });
    } else if (typeof result.error === "string" && result.error.includes("binary not found")) {
        renderBranchB(statusEl, result, { back, skip, recheck: guardedRecheck });
    } else {
        // BackendState mismatch OR any other error → "installed, needs auth".
        // Re-checking is the recovery path.
        renderBranchC(statusEl, result, { back, skip, recheck: guardedRecheck });
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

    const portalUrl = `https://${hostname}`;
    statusEl.innerHTML = `
        <div class="ob-status-badge ob-status-badge-success" role="status">
            <span class="ob-status-badge-pip" aria-hidden="true">&check;</span>
            <span class="ob-status-badge-label">
                Tailscale online &mdash; <strong>${escapeHtml(hostname)}</strong>
            </span>
            <span class="ob-status-badge-version">${escapeHtml(ip)}</span>
        </div>
        <div class="ob-tailnet-url-card">
            <div class="ob-tailnet-url-label">Your Portal URL</div>
            <div class="ob-tailnet-url-row">
                <code class="ob-tailnet-url-code">${escapeHtml(portalUrl)}</code>
                <button type="button" class="ob-copy-btn" data-copy="${escapeHtml(portalUrl)}">
                    <span aria-hidden="true">&#9112;</span> Copy
                </button>
            </div>
            <p class="ob-tailnet-url-hint">
                Open this in any browser on a phone, laptop, or other device on
                your tailnet &mdash; pair the Android app, or just use desktop.
            </p>
        </div>
        <div class="ob-cta-row">
            <button type="button" class="ob-cta" id="ob-tailscale-continue">
                Continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
            </button>
        </div>
        ${renderDisclosure(false)}
        ${renderStepNav({ showSkip: false })}
    `;

    wireCopyBtn(statusEl);
    document.getElementById("ob-tailscale-continue").addEventListener("click", next);
    wireStepNav(statusEl, { back, skip });
}

// ── Branch B: binary not found ──
// Per E1, Tailscale is pre-installed by install.sh Step 1b, so this branch
// is effectively unreachable on a fresh BlackBox. If it DOES fire, customer
// must have manually uninstalled — surface clear recovery instructions
// rather than a full install button flow (T6 was skipped per E1).
function renderBranchB(statusEl, result, { back, skip, recheck }) {
    statusEl.innerHTML = `
        <div class="ob-status-badge ob-status-badge-error" role="status">
            <span class="ob-status-badge-pip" aria-hidden="true">!</span>
            <span class="ob-status-badge-label">Tailscale binary not found on device</span>
        </div>
        <div class="ob-action-card">
            <p class="ob-action-card-prose">
                Tailscale should have been installed during BlackBox setup, but
                it appears to be missing. This is unusual &mdash; it may have been
                manually removed.
            </p>
            <p class="ob-action-card-prose">
                To recover, open a terminal on this device, change into the
                BlackBox install directory, and re-run the installer:
            </p>
            <div class="ob-code-block">
                <span class="ob-code-prompt" aria-hidden="true">$</span>
                <code class="ob-code-cmd">sudo ./Scripts/install.sh</code>
            </div>
            <p class="ob-action-card-prose">
                (If you're not sure where you extracted the BlackBox installer,
                check your Downloads or Desktop folder.) Then return to this
                wizard and click <strong>Re-check</strong> below.
            </p>
            <div class="ob-cta-row">
                <button type="button" class="ob-cta" id="ob-recheck-btn">
                    Re-check status <span class="ob-cta-arrow" aria-hidden="true">&#x21bb;</span>
                </button>
            </div>
        </div>
        ${renderDisclosure(true)}
        ${renderStepNav({ showSkip: true })}
    `;
    document.getElementById("ob-recheck-btn").addEventListener("click", recheck);
    wireStepNav(statusEl, { back, skip });
}

// ── Branch C: installed but not authenticated (also re-auth after 180-day expiry) ──
function renderBranchC(statusEl, result, { back, skip, recheck }) {
    const errMsg = (result.error || "needs authentication").replace(/^RuntimeError:\s*/, "");
    statusEl.innerHTML = `
        <div class="ob-status-badge" role="status">
            <span class="ob-status-badge-pip" aria-hidden="true">!</span>
            <span class="ob-status-badge-label">
                Tailscale needs authentication
            </span>
            <span class="ob-status-badge-version">${escapeHtml(errMsg)}</span>
        </div>
        <div class="ob-action-card">
            <p class="ob-action-card-prose">
                Click below &mdash; we will open your browser to the Tailscale login page.
                After you sign in, this screen will auto-detect within a few seconds.
            </p>
            <div class="ob-cta-row">
                <button type="button" class="ob-cta" id="ob-auth-btn">
                    Authenticate Now <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                </button>
            </div>
            <div id="ob-auth-status" hidden></div>
        </div>
        ${renderDisclosure(true)}
        ${renderStepNav({ showSkip: true })}
    `;
    document.getElementById("ob-auth-btn").addEventListener("click", () => {
        if (_authInFlight) return;  // C2: prevent double-click parallel pollers
        _authInFlight = true;
        startAuth(statusEl, { back, skip, recheck })
            .finally(() => { _authInFlight = false; });
    });
    wireStepNav(statusEl, { back, skip });
}

async function startAuth(statusEl, { back, skip, recheck }) {
    // C1: install a fresh abort token for this auth attempt
    if (_currentAuthAbort) _currentAuthAbort.aborted = true;
    const myAbort = { aborted: false };
    _currentAuthAbort = myAbort;

    const btn = document.getElementById("ob-auth-btn");
    const statusBox = document.getElementById("ob-auth-status");
    btn.disabled = true;
    btn.textContent = "Starting...";
    statusBox.hidden = false;
    statusBox.innerHTML = `<p class="ob-auth-waiting">Contacting Tailscale...</p>`;

    let loginUrl;
    try {
        const resp = await fetch("/onboarding/tailscale/up", { method: "POST" });
        const j = await resp.json();
        if (!resp.ok) {
            statusBox.innerHTML = `<p class="ob-auth-err">${escapeHtml(j.detail || "failed")}</p>`;
            btn.disabled = false;
            btn.textContent = "Try again";
            return;
        }
        loginUrl = j.login_url;
    } catch (e) {
        statusBox.innerHTML = `<p class="ob-auth-err">Network error: ${escapeHtml(e.message)}</p>`;
        btn.disabled = false;
        btn.textContent = "Try again";
        return;
    }

    // I1: always render clickable fallback in case xdg-open didn't work
    // (e.g., user accessing wizard from a remote browser, not on device)
    statusBox.innerHTML = `
        <p class="ob-auth-prompt">
            Your browser should open automatically. If not:
        </p>
        <p class="ob-auth-link-row">
            <a href="${escapeHtml(loginUrl)}" target="_blank" rel="noopener" class="ob-auth-link">
                Open Tailscale login &rarr;
            </a>
        </p>
        <p class="ob-auth-waiting">Waiting for authentication...</p>
    `;
    btn.textContent = "Waiting...";

    // Poll every 2s. Audit I5: 5 min total, "still waiting?" hint at 3 min.
    const startedAt = Date.now();
    const TIMEOUT_MS = 5 * 60 * 1000;
    const HINT_MS = 3 * 60 * 1000;
    let hintShown = false;
    let consecutivePollFailures = 0;
    let backendDownShown = false;
    const pollOnce = async () => {
        if (myAbort.aborted) return;  // C1: superseded — stop polling
        const elapsed = Date.now() - startedAt;
        if (elapsed > TIMEOUT_MS) {
            statusBox.insertAdjacentHTML("beforeend", `
                <p class="ob-auth-err">Timed out waiting for authentication.</p>
            `);
            await fetch("/onboarding/tailscale/cancel", { method: "POST" });
            btn.disabled = false;
            btn.textContent = "Try again";
            return;
        }
        if (elapsed > HINT_MS && !hintShown) {
            hintShown = true;
            statusBox.insertAdjacentHTML("beforeend", `
                <p class="ob-auth-hint">
                    Still waiting? Make sure you completed the login in your browser.
                    <a href="${escapeHtml(loginUrl)}" target="_blank" rel="noopener">
                        Re-open login URL
                    </a>
                </p>
            `);
        }
        try {
            const r = await fetch("/onboarding/tailscale/poll");
            const j = await r.json();
            if (j.state === "running") {
                statusBox.innerHTML = `<p class="ob-auth-ok">Authenticated. Loading...</p>`;
                setTimeout(recheck, 800);
                return;
            }
            consecutivePollFailures = 0;
        } catch (_) {
            consecutivePollFailures++;
            if (consecutivePollFailures >= 5 && !backendDownShown) {
                backendDownShown = true;
                statusBox.insertAdjacentHTML("beforeend", `
                    <p class="ob-auth-err">
                        Cannot reach BlackBox backend. Will keep trying...
                    </p>
                `);
            }
        }
        setTimeout(() => { if (!myAbort.aborted) pollOnce(); }, 2000);
    };
    setTimeout(() => { if (!myAbort.aborted) pollOnce(); }, 2000);
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
