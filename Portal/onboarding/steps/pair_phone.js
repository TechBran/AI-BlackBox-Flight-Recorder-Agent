// Phone pairing step — fifth screen of the onboarding wizard.
// On mount:
//   0. GET /onboarding/current-config — if paired_devices already exists,
//      skip directly to renderAlreadyPaired (E13). This is the fix for
//      "wizard shows QR forever after the phone is already paired" — the
//      phone won't re-claim a fresh token once it has stored credentials,
//      so polling alone never observes claimed=true on subsequent visits.
//   1. POST /pair/start → {token, exp}
//   2. Render QR pointing at /pair/qr/${token}
//   3. Poll /pair/status?token=${token} every 2s
//   4. On claimed: show "Paired with X" → Continue
//   5. On expired: re-mint token automatically + restart cycle
// Skip option always available.
//
// Cleanup: a module-level pollRef tracks the active interval id.
// On render() entry, the previous interval (if any) is cleared so re-renders
// (or back/skip navigation) don't leave zombie pollers.

let pollRef = null;
let activeToken = null;  // the token currently being polled — guards against stale tick races

function stopPolling() {
    if (pollRef !== null) {
        clearInterval(pollRef);
        pollRef = null;
    }
    activeToken = null;
}

export async function render(container, { next, back, skip }) {
    // First action: clear any zombie poller from a prior mount of this step
    stopPolling();

    container.innerHTML = `
        <section class="ob-step ob-pair-phone">
            <aside class="ob-step-sigil" aria-hidden="true">
                <div class="ob-step-sigil-num"><em>05</em></div>
                <div class="ob-step-sigil-rule"></div>
                <div class="ob-step-sigil-label">PAIR</div>
            </aside>
            <div class="ob-step-body">
                <div class="ob-step-eyebrow">
                    <span class="ob-step-eyebrow-dot" aria-hidden="true"></span>
                    Phone as remote
                </div>
                <h1 class="ob-step-title">
                    Pair your <em>phone</em>.
                </h1>
                <p class="ob-step-lede">
                    Open the AI BlackBox app on your phone and scan this QR code.
                    Your phone becomes a remote &mdash; voice, vision, and tool access
                    on the go, sharing the same memory as the desktop.
                </p>
                <div class="ob-pair-prereq">
                    <strong>Before you pair:</strong>
                    <p>
                        Make sure <strong>Tailscale</strong> is installed on your phone
                        and signed in with the <strong>same account</strong> you used
                        for this BlackBox (the Tailnet step earlier). The Android app
                        reaches the BlackBox via your tailnet &mdash; without Tailscale
                        signed in on your phone, the connection won't work.
                    </p>
                    <a href="https://play.google.com/store/apps/details?id=com.tailscale.ipn"
                       target="_blank" rel="noopener" class="ob-pair-prereq-link">
                        Get Tailscale for Android &rarr;
                    </a>
                </div>
                <div id="ob-pair-stage" class="ob-pair-stage">
                    <div class="ob-loading">Generating pairing code&hellip;</div>
                </div>
                <nav class="ob-step-nav" aria-label="Step navigation">
                    <button type="button" class="ob-back" id="ob-pair-back">
                        <span aria-hidden="true">&larr;</span> Back to extras
                    </button>
                    <button type="button" class="ob-pair-mark-done" id="ob-pair-mark-done">
                        I've already paired this phone &mdash; mark complete <span aria-hidden="true">&check;</span>
                    </button>
                    <button type="button" class="ob-skip" id="ob-pair-skip">
                        Skip &mdash; pair later from System Menu <span aria-hidden="true">&rarr;</span>
                    </button>
                </nav>
            </div>
        </section>
    `;

    document.getElementById("ob-pair-back").addEventListener("click", () => {
        stopPolling();
        back();
    });
    document.getElementById("ob-pair-skip").addEventListener("click", () => {
        stopPolling();
        skip();
    });

    // E14 (Brandon 2026-05-17): manual 'I've already paired' escape hatch.
    // Tailscale owns access control to the BlackBox — pairing is recorded
    // here for record-keeping but the wizard doesn't need magical detection.
    // If customer pairs via QR + polling fires → auto-advances (existing
    // flow). If customer pairs out-of-band OR has stored credentials from
    // a prior session → click this button to mark complete.
    document.getElementById("ob-pair-mark-done").addEventListener("click", async () => {
        stopPolling();
        // Best-effort: tell backend the step is complete so state reflects it
        try {
            await fetch("/onboarding/step/complete", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ step: "pair_phone" }),
            });
        } catch (_) { /* non-fatal */ }
        next();
    });

    // E13: if devices are already paired (persistent registry), show the
    // already-paired success view instead of a fresh QR — the phone won't
    // re-claim a new token once it holds stored credentials, so polling
    // alone would spin forever on revisit. Best-effort: any failure falls
    // through to the normal mint/poll flow.
    const stage = container.querySelector("#ob-pair-stage");
    let pairedDevices = [];
    try {
        const r = await fetch("/onboarding/current-config");
        if (r.ok) {
            const cfg = await r.json();
            pairedDevices = (cfg && cfg.paired_devices) || [];
        }
    } catch (_) { /* non-fatal — fall through to mint */ }

    if (pairedDevices.length > 0) {
        renderAlreadyPaired(stage, pairedDevices, {
            next,
            pairAnother: () => mintAndPoll(container, { next }),
        });
        return;
    }

    // No devices on record — mint a fresh pairing token + start QR/poll cycle.
    await mintAndPoll(container, { next });
}

async function mintAndPoll(container, { next }) {
    const stage = container.querySelector("#ob-pair-stage");

    // Mint
    let token, exp;
    try {
        const r = await fetch("/pair/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({}),
        });
        if (!r.ok) throw new Error(`/pair/start returned ${r.status}`);
        const data = await r.json();
        token = data.token;
        exp = data.exp;
    } catch (e) {
        renderError(stage, `Couldn't mint pairing token: ${escapeHtml(e.message)}`, () => mintAndPoll(container, { next }));
        return;
    }

    // Render the QR + waiting state
    activeToken = token;
    renderQR(stage, token, exp);

    // Begin polling
    pollRef = setInterval(async () => {
        // Guard: if a new mintAndPoll cycle started (re-mint), abandon this tick
        if (activeToken !== token) return;
        try {
            const r = await fetch(`/pair/status?token=${encodeURIComponent(token)}`);
            if (!r.ok) {
                // Token gone server-side → re-mint
                stopPolling();
                renderExpired(stage, () => mintAndPoll(container, { next }));
                return;
            }
            const status = await r.json();
            if (status.claimed) {
                stopPolling();
                renderClaimed(stage, status, next);
                return;
            }
            if (!status.exists || status.expires_in <= 0) {
                stopPolling();
                renderExpired(stage, () => mintAndPoll(container, { next }));
                return;
            }
            // Still waiting — update countdown
            const countdownEl = stage.querySelector(".ob-pair-countdown");
            if (countdownEl) {
                countdownEl.textContent = formatExpiresIn(status.expires_in);
            }
        } catch (e) {
            // Network blip — keep polling, but show inline status (don't kill the QR)
            const errEl = stage.querySelector(".ob-pair-error-inline");
            if (errEl) errEl.textContent = `Status check failed: ${e.message}`;
        }
    }, 2000);
}

function renderQR(stage, token, exp) {
    const expiresIn = Math.max(0, Math.floor(exp - Date.now() / 1000));
    stage.innerHTML = `
        <div class="ob-qr-frame">
            <div class="ob-qr-corner ob-qr-corner-tl" aria-hidden="true"></div>
            <div class="ob-qr-corner ob-qr-corner-tr" aria-hidden="true"></div>
            <div class="ob-qr-corner ob-qr-corner-bl" aria-hidden="true"></div>
            <div class="ob-qr-corner ob-qr-corner-br" aria-hidden="true"></div>
            <img class="ob-qr-img" src="/pair/qr/${encodeURIComponent(token)}" alt="QR code for pairing your phone with this BlackBox" />
        </div>
        <div class="ob-pair-meta">
            <div class="ob-pair-status-row">
                <span class="ob-pair-pulse" aria-hidden="true"></span>
                <span class="ob-pair-status-text">Waiting for your phone&hellip;</span>
            </div>
            <div class="ob-pair-meta-row">
                <span class="ob-pair-meta-label">Token expires in</span>
                <span class="ob-pair-countdown">${formatExpiresIn(expiresIn)}</span>
            </div>
            <div class="ob-pair-error-inline" aria-live="polite"></div>
        </div>
        <details class="ob-disclosure">
            <summary class="ob-disclosure-summary">
                Don't have the <em>BlackBox app</em> installed?
            </summary>
            <div class="ob-walkthrough-body">
                <ol class="ob-walkthrough-steps">
                    <li>On your phone, open the Play Store (Android) or TestFlight (iOS) and search for <strong>AI BlackBox</strong>.</li>
                    <li>Install and launch the app.</li>
                    <li>Tap <strong>Pair this device</strong> on the welcome screen.</li>
                    <li>Scan the QR code above. Pairing happens in seconds.</li>
                    <li>If the token expires before you finish, this page auto-refreshes with a new code.</li>
                </ol>
            </div>
        </details>
    `;
}

function renderClaimed(stage, status, next) {
    const deviceName = status.claimed_by || "your phone";
    const deviceKind = status.device_kind || "device";
    stage.innerHTML = `
        <div class="ob-pair-claimed">
            <div class="ob-pair-claimed-glyph" aria-hidden="true">&check;</div>
            <h2 class="ob-pair-claimed-title">Paired with <em>${escapeHtml(deviceName)}</em></h2>
            <p class="ob-pair-claimed-meta">${escapeHtml(deviceKind)} &middot; ready</p>
            <div class="ob-cta-row">
                <button type="button" class="ob-cta" id="ob-pair-continue">
                    Continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                </button>
            </div>
        </div>
    `;
    document.getElementById("ob-pair-continue").addEventListener("click", next);
}

// E13: success view for wizard re-entry when one or more devices already
// paired (from persistent Manifest/paired_devices.json registry). Highlights
// the most recently paired device + lists prior pairings (if >1) + offers
// "Pair another device" link which falls through to the normal mint/poll
// flow. Identical Continue CTA semantics to renderClaimed.
function renderAlreadyPaired(stage, devices, { next, pairAnother }) {
    // Sort newest-first by claimed_at; coerce missing to 0 so undefined never
    // crashes the comparator.
    const sorted = [...devices].sort(
        (a, b) => (b.claimed_at || 0) - (a.claimed_at || 0),
    );
    const latest = sorted[0];
    const latestName = latest.device_name || "your phone";
    const latestKind = latest.device_kind || "device";
    const others = sorted.slice(1);
    const othersHtml = others.length
        ? `<ul class="ob-pair-others-list">${others
              .map(
                  (d) =>
                      `<li class="ob-pair-others-item"><span class="ob-pair-others-name">${escapeHtml(
                          d.device_name || "unnamed",
                      )}</span><span class="ob-pair-others-kind">${escapeHtml(
                          d.device_kind || "device",
                      )}</span></li>`,
              )
              .join("")}</ul>`
        : "";
    const othersSection = others.length
        ? `<div class="ob-pair-others">
            <div class="ob-pair-others-label">Also paired (${others.length}):</div>
            ${othersHtml}
        </div>`
        : "";
    stage.innerHTML = `
        <div class="ob-pair-claimed">
            <div class="ob-pair-claimed-glyph" aria-hidden="true">&check;</div>
            <h2 class="ob-pair-claimed-title">Paired with <em>${escapeHtml(latestName)}</em></h2>
            <p class="ob-pair-claimed-meta">${escapeHtml(latestKind)} &middot; ready</p>
            ${othersSection}
            <div class="ob-cta-row">
                <button type="button" class="ob-cta" id="ob-pair-continue">
                    Continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                </button>
            </div>
            <div class="ob-pair-another-row">
                <button type="button" class="ob-link-button" id="ob-pair-another">
                    Pair another device <span aria-hidden="true">&rarr;</span>
                </button>
            </div>
        </div>
    `;
    document.getElementById("ob-pair-continue").addEventListener("click", next);
    document.getElementById("ob-pair-another").addEventListener("click", pairAnother);
}

function renderExpired(stage, retry) {
    stage.innerHTML = `
        <div class="ob-pair-expired">
            <div class="ob-pair-expired-glyph" aria-hidden="true">&#9201;</div>
            <h2 class="ob-pair-expired-title">Pairing code expired</h2>
            <p class="ob-step-helper">No worries &mdash; generate a fresh one.</p>
            <div class="ob-cta-row">
                <button type="button" class="ob-cta" id="ob-pair-retry">
                    New pairing code <span class="ob-cta-arrow" aria-hidden="true">&#8635;</span>
                </button>
            </div>
        </div>
    `;
    document.getElementById("ob-pair-retry").addEventListener("click", retry);
}

function renderError(stage, message, retry) {
    stage.innerHTML = `
        <div class="ob-pair-error">
            <div class="ob-pair-error-glyph" aria-hidden="true">!</div>
            <p class="ob-pair-error-msg">${message}</p>
            <div class="ob-cta-row">
                <button type="button" class="ob-cta" id="ob-pair-error-retry">
                    Try again <span class="ob-cta-arrow" aria-hidden="true">&#8635;</span>
                </button>
            </div>
        </div>
    `;
    document.getElementById("ob-pair-error-retry").addEventListener("click", retry);
}

function formatExpiresIn(seconds) {
    if (seconds <= 0) return "expired";
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
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
