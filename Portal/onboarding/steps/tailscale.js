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

// Per-render state — module globals leaked across non-linear hub mounts.
// _active points at the latest render so a new render can abort the old
// render's auth poll loop.
let _active = null;

export async function render(container, { next, back, skip, sigil }) {
    // C1: abort any in-flight auth poll loop from a prior render, install fresh
    // per-render state, and remember the real container for re-check re-renders.
    if (_active && _active.authAbort) _active.authAbort.aborted = true;
    const rs = { container, recheckBusy: false, authAbort: null,
                 authInFlight: false, branchAGen: 0, serveDone: false };
    _active = rs;

    // Initial loading state
    container.innerHTML = `
        <section class="ob-step ob-tailscale">
            <aside class="ob-step-sigil" aria-hidden="true">
                <div class="ob-step-sigil-num"><em>${sigil ? sigil.num : "02"}</em></div>
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
        if (rs.recheckBusy) return;
        rs.recheckBusy = true;
        try {
            await render(rs.container, { next, back, skip });
        } finally {
            rs.recheckBusy = false;
        }
    };

    const err = typeof result.error === "string" ? result.error : "";
    if (result.ok) {
        renderBranchA(statusEl, result, { next, back, skip }, rs);
    } else if (err.includes("binary not found")) {
        renderBranchB(statusEl, result, { back, skip, recheck: guardedRecheck }, rs);
    } else if (err.includes("BackendState") || err.includes("NeedsLogin") || err.includes("not authenticated")) {
        // Genuinely installed-but-unauthenticated → the auth action card.
        renderBranchC(statusEl, result, { back, skip, recheck: guardedRecheck }, rs);
    } else {
        // Anything else (network error, backend down, unexpected) is NOT
        // "needs auth" — show it honestly so Re-check is the recovery path
        // without the misleading "installed" badge.
        renderBranchC(statusEl, { ...result, _unexpected: true },
                      { back, skip, recheck: guardedRecheck }, rs);
    }
}

// ── Branch A: already configured ────────────────────────────────
async function renderBranchA(statusEl, result, { next, back, skip }, rs) {
    // I1: generation token — captured per renderBranchA invocation.
    // Used to bail post-await DOM mutations if user navigated away
    // or a Re-check triggered a fresh render. Mirrors T7's
    // _currentAuthAbort pattern (without needing per-call object refs).
    rs.branchAGen++;
    const myGen = rs.branchAGen;

    const hostname = (result.detail && result.detail.hostname) || "unknown";
    const ip = (result.detail && result.detail.ip) || "unknown";
    const magicdnsEnabled = !!(result.detail && result.detail.magicdns_enabled);

    // Persist hostname to .env (unchanged)
    try {
        await fetch("/onboarding/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ secrets: { BLACKBOX_TAILNET_HOSTNAME: hostname } }),
        });
    } catch (e) {
        console.warn("Couldn't persist BLACKBOX_TAILNET_HOSTNAME:", e);
    }

    // Fire-and-forget: set --accept-dns=true (device-side, idempotent).
    // No re-render guard needed — endpoint is idempotent + cheap; re-firing
    // on re-check allows recovery if the first attempt raced tailscaled
    // startup or hit a transient backend issue.
    fetch("/onboarding/tailscale/accept-dns", { method: "POST" }).catch(() => {});

    // HTTPS Portal setup — replaces v1.1-deferred uvicorn HTTPS plan.
    // Calls `tailscale serve` to expose Portal via HTTPS:443 (Android app
    // pairing requires this). Tailscale handles cert + TLS termination +
    // renewal automatically.
    // Promise + 60s client-side timeout (first-run ACME can be slow).
    let servePromise = null;
    if (!rs.serveDone) {
        const serveTimeout = new Promise(r => setTimeout(() => r({
            ok: false, error: "timeout", timed_out: true,
        }), 60_000));
        const serveFetch = fetch("/onboarding/tailscale/serve", { method: "POST" })
            .then(r => r.json())
            .catch(() => ({ ok: false, error: "network" }));
        servePromise = Promise.race([serveFetch, serveTimeout]).then(result => {
            rs.serveDone = true;
            return result;
        });
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
        <div id="ob-tailscale-banners"></div>
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

    // Render banners as serve + magicdns results land
    const banners = document.getElementById("ob-tailscale-banners");
    if (!magicdnsEnabled) {
        banners.insertAdjacentHTML("beforeend", magicdnsBanner());
        wireRecheckBtn(banners, { next, back, skip }, rs);
        wireBannerOpenBtn(banners);  // E4: synchronous window.open for admin link
    }

    // HTTPS Portal pending banner — render spinner while serve setup runs
    if (servePromise) {
        const pendingId = "ob-serve-pending";
        banners.insertAdjacentHTML("beforeend", servePendingBanner(pendingId));
        const result = await servePromise;
        if (myGen !== rs.branchAGen) return;  // I1: superseded by re-render
        const pendingEl = document.getElementById(pendingId);
        if (pendingEl) pendingEl.remove();
        if (result.ok) {
            banners.insertAdjacentHTML("beforeend", serveSuccessBanner(hostname));
            wireBannerOpenBtn(banners);
        } else if (result.https_disabled) {
            banners.insertAdjacentHTML("beforeend",
                httpsDisabledBanner(result.admin_url));
            wireRecheckBtn(banners, { next, back, skip }, rs);
            wireBannerOpenBtn(banners);
        } else if (result.timed_out) {
            banners.insertAdjacentHTML("beforeend",
                serveTimeoutBanner());
            wireRecheckBtn(banners, { next, back, skip }, rs);
        }
    }
}

// I4: MagicDNS banner — Android-app-gate framing + step-by-step.
// Also calls out HTTPS as the peer toggle (Brandon's MSO2 testing
// confirmed both are required for Android pairing).
function magicdnsBanner() {
    return `
        <div class="ob-banner ob-banner-warn">
            <strong>Enable MagicDNS to use the Android app</strong>
            <p>
                Your Android app reaches the BlackBox at a friendly hostname like
                <code>blackbox.<em>your-tailnet</em>.ts.net</code> &mdash; that name
                only resolves when MagicDNS is on for your tailnet. Without it,
                pairing won't work.
            </p>
            <p><strong>Takes about 10 seconds:</strong></p>
            <ol class="ob-banner-steps">
                <li>Click the <strong>Open admin console</strong> button below
                    (opens in your browser).</li>
                <li>Find the <strong>MagicDNS</strong> section near the top of
                    the page and toggle it <strong>On</strong>.</li>
                <li>Scroll down a bit and also enable
                    <strong>HTTPS Certificates</strong> &mdash; same page, the
                    Android app needs HTTPS to pair.</li>
                <li>Come back here and click <strong>Re-check</strong>.</li>
            </ol>
            <div class="ob-banner-actions">
                <a href="https://login.tailscale.com/admin/dns" target="_blank" rel="noopener"
                   class="ob-banner-link ob-banner-link-primary">Open admin console &rarr;</a>
                <button type="button" class="ob-banner-link ob-banner-recheck"
                        data-recheck="magicdns">Re-check &#x21bb;</button>
            </div>
        </div>
    `;
}

// HTTPS banner — required for Android app pairing (cleartext blocked
// since Android API 28). Brandon's MSO2 testing confirmed.
function httpsDisabledBanner(adminUrl) {
    return `
        <div class="ob-banner ob-banner-warn">
            <strong>Enable HTTPS for your tailnet</strong>
            <p>
                The Android app requires HTTPS to pair with your BlackBox. The
                toggle is on the same admin page as MagicDNS (just below it).
            </p>
            <p><strong>Takes about 10 seconds:</strong></p>
            <ol class="ob-banner-steps">
                <li>Click the <strong>Open admin console</strong> button.</li>
                <li>Scroll to the <strong>HTTPS Certificates</strong> section.</li>
                <li>Click <strong>Enable HTTPS</strong> and confirm.</li>
                <li>Come back here and click <strong>Re-check</strong>.</li>
            </ol>
            <div class="ob-banner-actions">
                <a href="${escapeHtml(adminUrl)}" target="_blank" rel="noopener"
                   class="ob-banner-link ob-banner-link-primary">Open admin console &rarr;</a>
                <button type="button" class="ob-banner-link ob-banner-recheck"
                        data-recheck="serve">Re-check &#x21bb;</button>
            </div>
        </div>
    `;
}

function servePendingBanner(id) {
    return `
        <div class="ob-banner ob-banner-info" id="${id}">
            <span class="ob-banner-spinner" aria-hidden="true">&#9696;</span>
            Setting up HTTPS Portal&hellip;
            <span class="ob-banner-spinner-hint">(usually 5-30 seconds — Tailscale fetches Let's Encrypt cert + sets up reverse proxy)</span>
        </div>
    `;
}

function serveSuccessBanner(hostname) {
    const url = `https://${hostname}`;
    return `
        <div class="ob-banner ob-banner-success">
            <strong>HTTPS Portal ready</strong>
            <p>
                Your BlackBox is now reachable at the URL below via HTTPS.
                This is what the Android app needs for pairing.
            </p>
            <div class="ob-banner-actions">
                <a href="${url}" target="_blank" rel="noopener"
                   class="ob-banner-link ob-banner-link-primary">Open Portal &rarr;</a>
            </div>
        </div>
    `;
}

function serveTimeoutBanner() {
    return `
        <div class="ob-banner ob-banner-warn">
            <strong>HTTPS Portal setup took too long.</strong>
            <p>This usually clears up on its own. Click Re-check to try again.</p>
            <div class="ob-banner-actions">
                <button type="button" class="ob-banner-link ob-banner-recheck"
                        data-recheck="serve">Re-check &#x21bb;</button>
            </div>
        </div>
    `;
}

// E4 (admin console buttons): Tauri WebKitGTK doesn't auto-open
// <a target=_blank> to system browser. Synchronous window.open from
// user-initiated click is popup-blocker-friendly + bypasses the issue.
function wireBannerOpenBtn(scope) {
    scope.querySelectorAll(".ob-banner-link-primary[href]").forEach(a => {
        // Avoid double-binding if banner re-renders
        if (a.dataset.openWired === "1") return;
        a.dataset.openWired = "1";
        a.addEventListener("click", (e) => {
            try {
                window.open(a.href, "_blank", "noopener,noreferrer");
            } catch (_) { /* fall through — anchor's default may still work */ }
        });
    });
}

// Re-check wiring: each "Re-check" button re-runs the appropriate probe and
// re-renders Branch A so banners refresh based on new state.
function wireRecheckBtn(scope, navCallbacks, rs) {
    scope.querySelectorAll(".ob-banner-recheck").forEach(btn => {
        btn.addEventListener("click", async (e) => {
            e.preventDefault();
            const which = btn.dataset.recheck;
            btn.disabled = true;
            btn.textContent = "Checking...";
            // Reset serve guard so it can re-fire on re-check
            if (which === "cert" || which === "serve") {
                rs.serveDone = false;
            }
            // Force full step re-render via top-level render into the real
            // container the wizard/hub passed in — never document.body.
            await render(rs.container, navCallbacks);
        });
    });
}

// ── Branch B: binary not found — fallback install button (E1 reversal) ──
// Per E1, Tailscale is pre-installed by install.sh Step 1b, so this branch
// is normally unreachable. But if a customer manually removed tailscale OR
// install.sh Step 1b failed silently, Branch B gives them a recovery path
// WITHOUT needing a terminal. Uses apt-get install (apt repo + signing key
// were already added during Step 1b's curl|sh root-context run, so this
// works without sudoers metacharacter issues).
function renderBranchB(statusEl, result, { back, skip, recheck }) {
    statusEl.innerHTML = `
        <div class="ob-status-badge ob-status-badge-error" role="status">
            <span class="ob-status-badge-pip" aria-hidden="true">!</span>
            <span class="ob-status-badge-label">Tailscale not installed</span>
        </div>
        <div class="ob-action-card">
            <p class="ob-action-card-prose">
                Click below to install Tailscale on this device. Installation
                takes about 30 to 60 seconds.
            </p>
            <div class="ob-cta-row">
                <button type="button" class="ob-cta" id="ob-install-btn">
                    Install Tailscale Now <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                </button>
            </div>
            <div id="ob-install-stream" class="ob-install-stream" hidden>
                <div class="ob-install-stream-header">Installing Tailscale&hellip;</div>
                <pre id="ob-install-stream-log" class="ob-install-stream-log"></pre>
            </div>
        </div>
        ${renderDisclosure(true)}
        ${renderStepNav({ showSkip: true })}
    `;
    document.getElementById("ob-install-btn").addEventListener("click", () => {
        startInstall(statusEl, { back, skip, recheck });
    });
    wireStepNav(statusEl, { back, skip });
}

async function startInstall(statusEl, { back, skip, recheck }) {
    const btn = document.getElementById("ob-install-btn");
    const streamBox = document.getElementById("ob-install-stream");
    const log = document.getElementById("ob-install-stream-log");
    btn.disabled = true;
    btn.textContent = "Installing...";
    streamBox.hidden = false;

    try {
        const resp = await fetch("/onboarding/tailscale/install/stream", { method: "POST" });
        if (resp.status === 409) {
            log.textContent = "Install already in progress — please wait...";
            return;
        }
        if (!resp.ok) {
            log.textContent = `Install failed to start (HTTP ${resp.status})`;
            btn.disabled = false;
            btn.textContent = "Try again";
            return;
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        let done = false;
        while (!done) {
            const { value, done: isDone } = await reader.read();
            done = isDone;
            if (value) {
                buffer += decoder.decode(value, { stream: true });
                const events = buffer.split("\n\n");
                buffer = events.pop() || "";
                for (const evt of events) {
                    const lines = evt.split("\n");
                    let event = "message", data = "";
                    for (const ln of lines) {
                        if (ln.startsWith("event:")) event = ln.slice(6).trim();
                        else if (ln.startsWith("data:")) data += ln.slice(5).trim() + "\n";
                    }
                    if (event === "done") {
                        log.textContent += "\nInstall complete.\n";
                        setTimeout(recheck, 1000);
                        return;
                    }
                    if (event === "error") {
                        log.textContent += "\nError: " + data;
                        btn.disabled = false;
                        btn.textContent = "Try again";
                        return;
                    }
                    log.textContent += data;
                    log.scrollTop = log.scrollHeight;
                }
            }
        }
    } catch (e) {
        log.textContent += "\nNetwork error: " + e.message;
        btn.disabled = false;
        btn.textContent = "Try again";
    }
}

// ── Branch C: installed but not authenticated (also re-auth after 180-day expiry) ──
function renderBranchC(statusEl, result, { back, skip, recheck }, rs) {
    const errMsg = (result.error || "needs authentication").replace(/^RuntimeError:\s*/, "");
    const unexpected = !!result._unexpected;
    statusEl.innerHTML = `
        <div class="ob-status-badge ${unexpected ? "ob-status-badge-error" : "ob-status-badge-installed"}" role="status">
            <span class="ob-status-badge-pip" aria-hidden="true">${unexpected ? "!" : "&check;"}</span>
            <span class="ob-status-badge-label">${unexpected ? "Couldn't determine Tailscale state" : "Tailscale installed"}</span>
        </div>
        <div class="ob-status-badge" role="status">
            <span class="ob-status-badge-pip" aria-hidden="true">!</span>
            <span class="ob-status-badge-label">
                Authentication needed
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
        if (rs.authInFlight) return;  // C2: prevent double-click parallel pollers
        rs.authInFlight = true;
        startAuth(statusEl, { back, skip, recheck }, rs)
            .finally(() => { rs.authInFlight = false; });
    });
    wireStepNav(statusEl, { back, skip });
}

async function startAuth(statusEl, { back, skip, recheck }, rs) {
    // C1: install a fresh abort token for this auth attempt
    if (rs.authAbort) rs.authAbort.aborted = true;
    const myAbort = { aborted: false };
    rs.authAbort = myAbort;

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

    // E4 (empirical): Tauri WebKitGTK doesn't auto-open target="_blank"
    // to system browser, AND backend xdg-open fails because systemd-
    // launched service has stripped env (no DISPLAY/DBUS). So we present
    // the URL prominently with a copy button — same UX as Tailscale's
    // own headless CLI flow. User copies, opens browser, signs in.
    // Best-effort window.open is tried first (works in real browsers /
    // remote-wizard access where Tauri's not in the way).
    try {
        window.open(loginUrl, "_blank", "noopener,noreferrer");
    } catch (_) { /* fine — manual copy works */ }

    statusBox.innerHTML = `
        <p class="ob-auth-prompt">
            <strong>Sign in to Tailscale</strong> &mdash; copy the URL below and
            open it in any browser. We'll auto-detect when you're done.
        </p>
        <div class="ob-auth-url-card">
            <code class="ob-auth-url-code">${escapeHtml(loginUrl)}</code>
            <div class="ob-auth-url-actions">
                <button type="button" class="ob-copy-btn ob-copy-btn-primary"
                        data-copy="${escapeHtml(loginUrl)}">
                    <span aria-hidden="true">&#9112;</span> Copy URL
                </button>
                <a href="${escapeHtml(loginUrl)}" target="_blank" rel="noopener"
                   class="ob-auth-link-secondary">
                    Try to open here &rarr;
                </a>
            </div>
        </div>
        <p class="ob-auth-waiting">Waiting for sign-in&hellip;</p>
    `;
    wireCopyBtn(statusBox);
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
