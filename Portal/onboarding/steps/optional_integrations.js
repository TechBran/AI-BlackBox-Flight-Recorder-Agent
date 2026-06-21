// Optional integrations step — fourth screen of the onboarding wizard.
// Google Workspace OAuth (Gmail + Docs/Sheets/Slides/Drive/Calendar, active) + Google Cloud service-account file (active) +
// ElevenLabs API key (active) + Twilio (v1.1 deferred placeholder).
// Save & continue is always enabled — every integration here is optional.
//
// Rehydration: on mount we fetch /onboarding/current-config + /onboarding/credentials.
// If GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET are already in .env
// we render an "Already configured" Gmail card with a Replace button. If a
// service-account JSON is in credentials/ + linked via GOOGLE_APPLICATION_CREDENTIALS
// we render the credential card in its configured state with Replace + Remove.
// If ELEVENLABS_API_KEY is present (current-config providers.elevenlabs.present)
// we render the ElevenLabs card in its configured state with a Replace button.

const GMAIL_PROVIDER = {
    id: "gmail",
    label: "Google Workspace",
    description: "Connect Google with one sign-in: Gmail (triage, drafts), Docs, Sheets, Slides, Drive, and Calendar \u2014 so the AI can read and create documents, spreadsheets, presentations, files, and calendar events on your behalf.",
    consoleUrl: "https://console.cloud.google.com/apis/credentials",
    docsUrl: "https://developers.google.com/workspace/guides/create-credentials#oauth-client-id",
};

// Sits next to Gmail since both relate to Google services. Distinct from
// API keys: this is a JSON FILE upload (drag-drop), not a key paste.
const CREDENTIAL_PROVIDER = {
    id: "google-service-account",
    label: "Google Cloud Service Account",
    description: "JSON service-account key for Google Cloud TTS, Vertex AI, and other GCP-authenticated services. Drop the .json file you downloaded from the Google Cloud Console.",
    consoleUrl: "https://console.cloud.google.com/iam-admin/serviceaccounts",
};

// API-key card (active). Mirrors the Gmail card's validate→save→rehydrate
// flow, but with a single password input instead of OAuth client id/secret.
// Validation hits the same /onboarding/validate dispatch (provider:"elevenlabs")
// which returns detail={tier, credits_remaining, features} so we can tell the
// customer exactly what their plan unlocks.
const ELEVENLABS_PROVIDER = {
    id: "elevenlabs",
    label: "ElevenLabs",
    description: "Premium voice synthesis — high-fidelity text-to-speech, AI music, sound effects, and voice cloning. Optional: v1 already ships Google + OpenAI TTS.",
    consoleUrl: "https://elevenlabs.io/app/settings/api-keys",
};

const PLACEHOLDER_INTEGRATIONS = [
    {
        id: "twilio",
        label: "Twilio",
        description: "Inbound + outbound phone calls and SMS via Twilio webhooks.",
        v1_1_note: "Available in v1.1. v1 uses your TG200 cellular modem for phone + SMS — no setup needed.",
    },
];

let busy = false;

export async function render(container, { next, back, skip }) {
    // Fetch current config + credentials in parallel — both inform rehydrate
    // state for the Gmail card and the service-account credential card.
    // Fail-open: empty responses mean render the original empty-input flow.
    let currentConfig = null;
    let credsResp = null;
    try {
        const [cfgR, credR] = await Promise.all([
            fetch("/onboarding/current-config"),
            fetch("/onboarding/credentials"),
        ]);
        if (cfgR.ok) currentConfig = await cfgR.json();
        if (credR.ok) credsResp = await credR.json();
    } catch (e) {
        // Fail-open — keep nulls and render empty-input variants.
    }

    const gmailCfg = currentConfig?.providers?.gmail || null;
    const elevenCfg = currentConfig?.providers?.elevenlabs || null;
    // E15 (Brandon 2026-05-17): generate the exact redirect URIs the customer
    // needs to add to their Google OAuth client. We expose both localhost (for
    // when the wizard is accessed locally on the BlackBox) and the tailnet
    // HTTPS form (for when accessed from a remote browser via Tailscale serve).
    // The tailnet URI is the load-bearing one for production use; localhost is
    // convenience for sitting-at-the-device flow.
    const tailnetHostname = currentConfig?.tailscale?.detail?.hostname || "";
    const state = {
        gmail: {
            client_id: "",
            client_secret: "",
            status: "idle",
            result: null,
            wasPresent: !!(gmailCfg && gmailCfg.present),
            // gmail differs from openai/anthropic/google: full client_id is
            // public per Google OAuth docs, only secret_last4 is redacted.
            existingClientId: gmailCfg?.client_id || null,
            secretLast4: gmailCfg?.secret_last4 || null,
            replacing: false,
            redirectUriLocal: "http://localhost:9091/auth/gmail/callback",
            redirectUriTailnet: tailnetHostname
                ? `https://${tailnetHostname}/auth/gmail/callback`
                : null,
        },
        creds: {
            files: credsResp?.files || [],
            activeCreds: credsResp?.google_application_credentials || null,
            uploading: false,
            error: null,
        },
        elevenlabs: {
            api_key: "",
            status: "idle",
            result: null,
            // current-config surfaces ELEVENLABS_API_KEY presence as
            // providers.elevenlabs.present (redacted last4 only — never the key).
            wasPresent: !!(elevenCfg && elevenCfg.present),
            keyLast4: elevenCfg?.last4 || null,
            replacing: false,
        },
    };

    container.innerHTML = `
        <section class="ob-step ob-optional">
            <aside class="ob-step-sigil" aria-hidden="true">
                <div class="ob-step-sigil-num"><em>04</em></div>
                <div class="ob-step-sigil-rule"></div>
                <div class="ob-step-sigil-label">EXTRAS</div>
            </aside>
            <div class="ob-step-body">
                <div class="ob-step-eyebrow">
                    <span class="ob-step-eyebrow-dot" aria-hidden="true"></span>
                    Optional integrations
                </div>
                <h1 class="ob-step-title">
                    Wire up <em>extras</em>.
                </h1>
                <p class="ob-step-lede">
                    Each of these is optional. Add the ones you want now, or
                    skip and configure later from the System Menu.
                </p>
                <div class="ob-providers" id="ob-integrations">
                    ${renderGmailCardForState(GMAIL_PROVIDER, state)}
                    ${renderCredCard(CREDENTIAL_PROVIDER, state)}
                    ${renderElevenLabsCardForState(ELEVENLABS_PROVIDER, state)}
                    ${PLACEHOLDER_INTEGRATIONS.map(renderPlaceholderCard).join("")}
                </div>
                <div class="ob-cta-row">
                    <button type="button" class="ob-cta" id="ob-extras-save">
                        Save &amp; continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                    </button>
                </div>
                <nav class="ob-step-nav" aria-label="Step navigation">
                    <button type="button" class="ob-back" id="ob-extras-back">
                        <span aria-hidden="true">&larr;</span> Back to API keys
                    </button>
                    <button type="button" class="ob-skip" id="ob-extras-skip">
                        Skip everything &mdash; configure later <span aria-hidden="true">&rarr;</span>
                    </button>
                </nav>
            </div>
        </section>
    `;

    wireGmailCardForState(container, state, GMAIL_PROVIDER);
    wireCredCard(container, state, CREDENTIAL_PROVIDER);
    wireElevenLabsCardForState(container, state, ELEVENLABS_PROVIDER);
    wireSave(container, state, next);
    document.getElementById("ob-extras-back").addEventListener("click", back);
    document.getElementById("ob-extras-skip").addEventListener("click", skip);
}

// Dispatcher: pick configured-state card or input-state card based on
// rehydration state.
function renderGmailCardForState(p, state) {
    const s = state.gmail;
    if (s.wasPresent && !s.replacing) {
        return renderGmailCardConfigured(p, s);
    }
    return renderGmailCard(p, s);
}

// Configured-state Gmail card: shown when GOOGLE_OAUTH_CLIENT_ID +
// GOOGLE_OAUTH_CLIENT_SECRET are already in .env. Replace button swaps
// to the input form via in-place re-render (see startReplacingGmail).
function renderGmailCardConfigured(p, s) {
    const clientIdDisplay = s.existingClientId || "(unknown)";
    const secretPreview = formatSecretPreview(s.secretLast4);
    return `
        <div class="ob-provider-card ob-integration-card ob-provider-configured" data-provider="${p.id}">
            <div class="ob-provider-header">
                <div class="ob-provider-label">${escapeHtml(p.label)}</div>
                <a class="ob-provider-link" href="${escapeHtml(p.consoleUrl)}" target="_blank" rel="noopener">
                    Google Cloud Console <span aria-hidden="true">↗</span>
                </a>
            </div>
            <p class="ob-integration-desc">${escapeHtml(p.description)}</p>
            <div class="ob-provider-configured-row">
                <span class="ob-status-pill ob-status-pill-ok">
                    <span class="ob-status-pill-glyph" aria-hidden="true">&check;</span>
                    Already configured
                </span>
                <button type="button" class="ob-replace-btn" data-provider="${p.id}">Reconnect</button>
            </div>
            <dl class="ob-gmail-configured-detail">
                <dt>Client ID</dt>
                <dd><code>${escapeHtml(clientIdDisplay)}</code></dd>
                <dt>Secret</dt>
                <dd><code>${escapeHtml(secretPreview)}</code></dd>
            </dl>
            <p class="ob-gmail-uri-hint">Already connected for Gmail? If you connected before Docs/Sheets/Slides/Calendar were added, click Reconnect once to grant the new Google Workspace permissions.</p>
        </div>
    `;
}

// Reduce the server-rendered redacted preview down to a short, readable
// suffix: 4 leading bullets + the trailing alphanumeric tail (typically the
// real last 4 characters of the secret).
function formatSecretPreview(raw) {
    if (!raw) return "set";
    const m = String(raw).match(/([A-Za-z0-9_\-]+)$/);
    const tail = m ? m[1] : "";
    if (!tail) return "set";
    return "••••" + tail;
}

function renderGmailCard(p, s) {
    // E15 (Brandon 2026-05-17): generate the exact redirect URIs for THIS
    // machine and surface them with copy buttons. Customer copies → pastes
    // into Google Cloud Console's Authorized redirect URIs list. Removes
    // the ambiguity that bit Brandon on MSO2 Ultra ('what URI do I use?').
    const localUri = s?.redirectUriLocal || "http://localhost:9091/auth/gmail/callback";
    const tailnetUri = s?.redirectUriTailnet || null;
    const redirectUrisHtml = `
        <li>
            Under <strong>Authorized redirect URIs</strong>, add the URI(s)
            below for this specific BlackBox. Click Copy to grab each one,
            then paste into Google's form (click "Add URI" for each):
            <div class="ob-gmail-uri-list">
                <div class="ob-gmail-uri-row">
                    <span class="ob-gmail-uri-label">Local:</span>
                    <code class="ob-gmail-uri-code">${escapeHtml(localUri)}</code>
                    <button type="button" class="ob-gmail-uri-copy" data-copy="${escapeHtml(localUri)}">Copy</button>
                </div>
                ${tailnetUri ? `
                <div class="ob-gmail-uri-row">
                    <span class="ob-gmail-uri-label">Tailnet (HTTPS):</span>
                    <code class="ob-gmail-uri-code">${escapeHtml(tailnetUri)}</code>
                    <button type="button" class="ob-gmail-uri-copy" data-copy="${escapeHtml(tailnetUri)}">Copy</button>
                </div>
                <p class="ob-gmail-uri-hint">
                    Add <strong>both</strong>. Local is for OAuth started while sitting at this device;
                    Tailnet is for OAuth started from a remote browser via your Tailscale connection
                    (required for the production flow).
                </p>
                ` : `
                <p class="ob-gmail-uri-hint">
                    Tailscale isn't configured yet on this BlackBox &mdash; only the local URI is
                    available right now. Complete the Tailscale step first to get the HTTPS tailnet URI
                    (which you'll want for remote OAuth flows).
                </p>
                `}
            </div>
        </li>
    `;
    return `
        <div class="ob-provider-card ob-integration-card" data-provider="${p.id}">
            <div class="ob-provider-header">
                <div class="ob-provider-label">${escapeHtml(p.label)}</div>
                <a class="ob-provider-link" href="${escapeHtml(p.consoleUrl)}" target="_blank" rel="noopener">
                    Google Cloud Console <span aria-hidden="true">↗</span>
                </a>
            </div>
            <p class="ob-integration-desc">${escapeHtml(p.description)}</p>
            <details class="ob-disclosure ob-walkthrough" open>
                <summary class="ob-disclosure-summary">
                    <span class="ob-disclosure-q">
                        Walk me through <em>Google Cloud OAuth setup</em>
                    </span>
                    <span class="ob-disclosure-toggle" aria-hidden="true">Show / Hide</span>
                </summary>
                <div class="ob-walkthrough-body">
                    <ol class="ob-walkthrough-steps">
                        <li>In <a href="${escapeHtml(p.consoleUrl)}" target="_blank" rel="noopener">Google Cloud Console &rarr; Credentials</a>, click <strong>Create Credentials &rarr; OAuth client ID</strong>.</li>
                        <li>Pick <strong>Web application</strong> as the type. Name it something like "AI BlackBox".</li>
                        ${redirectUrisHtml}
                        <li>Click <strong>Create</strong>. Google shows you a <em>Client ID</em> and <em>Client Secret</em> &mdash; paste both below.</li>
                        <li>Enable the required Google APIs in the <a href="https://console.cloud.google.com/apis/library" target="_blank" rel="noopener">API Library</a>: <strong>Gmail API</strong>, <strong>Google Docs API</strong>, <strong>Google Sheets API</strong>, <strong>Google Slides API</strong>, <strong>Google Drive API</strong>, and <strong>Google Calendar API</strong>. (A granted scope still returns 403 at call time if its API isn't enabled in the project.)</li>
                    </ol>
                </div>
            </details>
            <div class="ob-gmail-fields">
                <label class="ob-field-label" for="ob-gmail-client-id">Client ID</label>
                <input
                    type="text"
                    class="ob-provider-input"
                    id="ob-gmail-client-id"
                    placeholder="123456789-abc...apps.googleusercontent.com"
                    autocomplete="off"
                    autocapitalize="off"
                    spellcheck="false"
                />
                <label class="ob-field-label" for="ob-gmail-client-secret">Client Secret</label>
                <div class="ob-provider-input-row">
                    <input
                        type="password"
                        class="ob-provider-input"
                        id="ob-gmail-client-secret"
                        placeholder="GOCSPX-..."
                        autocomplete="off"
                        autocapitalize="off"
                        spellcheck="false"
                    />
                    <button type="button" class="ob-reveal-btn" id="ob-gmail-reveal" aria-label="Show or hide Client Secret">&#128065;</button>
                    <button type="button" class="ob-validate-btn" id="ob-gmail-validate" disabled>Validate</button>
                </div>
            </div>
            <div class="ob-provider-status" id="ob-gmail-status" data-status="idle"></div>
        </div>
    `;
}

function renderPlaceholderCard(p) {
    return `
        <div class="ob-provider-card ob-integration-card ob-integration-deferred" data-provider="${p.id}" aria-disabled="true">
            <div class="ob-provider-header">
                <div class="ob-provider-label">${escapeHtml(p.label)}</div>
                <span class="ob-deferred-pill">Coming in v1.1</span>
            </div>
            <p class="ob-integration-desc">${escapeHtml(p.description)}</p>
            <p class="ob-integration-deferred-note">${escapeHtml(p.v1_1_note)}</p>
        </div>
    `;
}

// ─────────────────────────────────────────────────────────────────────────
// ElevenLabs API-key card (active) — mirrors the Gmail card's dispatcher /
// configured / input / wire / validate quartet, with a single password input.
// Validate hits /onboarding/validate (provider:"elevenlabs"); the actual save
// is deferred to the wizard's "Save & continue" (wireSave) just like Gmail.
// ─────────────────────────────────────────────────────────────────────────

// Dispatcher: configured-state card (already in .env) or input-state card.
function renderElevenLabsCardForState(p, state) {
    const s = state.elevenlabs;
    if (s.wasPresent && !s.replacing) {
        return renderElevenLabsCardConfigured(p, s);
    }
    return renderElevenLabsCard(p, s);
}

// Configured-state card: shown when ELEVENLABS_API_KEY is already in .env.
// Replace swaps to the input form (see startReplacingElevenLabs).
function renderElevenLabsCardConfigured(p, s) {
    const keyPreview = formatSecretPreview(s.keyLast4);
    return `
        <div class="ob-provider-card ob-integration-card ob-provider-configured" data-provider="${p.id}">
            <div class="ob-provider-header">
                <div class="ob-provider-label">${escapeHtml(p.label)}</div>
                <a class="ob-provider-link" href="${escapeHtml(p.consoleUrl)}" target="_blank" rel="noopener">
                    ElevenLabs API keys <span aria-hidden="true">↗</span>
                </a>
            </div>
            <p class="ob-integration-desc">${escapeHtml(p.description)}</p>
            <div class="ob-provider-configured-row">
                <span class="ob-status-pill ob-status-pill-ok">
                    <span class="ob-status-pill-glyph" aria-hidden="true">&check;</span>
                    ElevenLabs configured
                </span>
                <button type="button" class="ob-replace-btn" data-provider="${p.id}">Replace</button>
            </div>
            <dl class="ob-gmail-configured-detail">
                <dt>API key</dt>
                <dd><code>${escapeHtml(keyPreview)}</code></dd>
            </dl>
        </div>
    `;
}

// Input-state card: single password input + reveal + validate, matching the
// Gmail secret field's row layout and status block.
function renderElevenLabsCard(p, s) {
    return `
        <div class="ob-provider-card ob-integration-card" data-provider="${p.id}">
            <div class="ob-provider-header">
                <div class="ob-provider-label">${escapeHtml(p.label)}</div>
                <a class="ob-provider-link" href="${escapeHtml(p.consoleUrl)}" target="_blank" rel="noopener">
                    ElevenLabs API keys <span aria-hidden="true">↗</span>
                </a>
            </div>
            <p class="ob-integration-desc">${escapeHtml(p.description)}</p>
            <div class="ob-gmail-fields">
                <label class="ob-field-label" for="ob-elevenlabs-key">ElevenLabs API key</label>
                <p class="ob-step-helper">
                    Unlocks premium voices, AI music, sound effects, and voice cloning.
                    Grab a key from your
                    <a href="${escapeHtml(p.consoleUrl)}" target="_blank" rel="noopener">ElevenLabs API keys</a>
                    page and paste it below.
                </p>
                <div class="ob-provider-input-row">
                    <input
                        type="password"
                        class="ob-provider-input"
                        id="ob-elevenlabs-key"
                        placeholder="sk_..."
                        autocomplete="off"
                        autocapitalize="off"
                        spellcheck="false"
                    />
                    <button type="button" class="ob-reveal-btn" id="ob-elevenlabs-reveal" aria-label="Show or hide API key">&#128065;</button>
                    <button type="button" class="ob-validate-btn" id="ob-elevenlabs-validate" disabled>Validate</button>
                </div>
            </div>
            <div class="ob-provider-status" id="ob-elevenlabs-status" data-status="idle"></div>
        </div>
    `;
}

// Wire either the configured-state Replace button OR the input-state form.
function wireElevenLabsCardForState(container, state, p) {
    const s = state.elevenlabs;
    if (s.wasPresent && !s.replacing) {
        const replaceBtn = container.querySelector(
            `.ob-provider-card[data-provider="${p.id}"] .ob-replace-btn`
        );
        if (replaceBtn) {
            replaceBtn.addEventListener("click", () => startReplacingElevenLabs(p, state, container));
        }
        return;
    }
    wireElevenLabsCard(container, state, p);
}

// Swap the card from configured -> input state when Replace is clicked.
function startReplacingElevenLabs(p, state, container) {
    state.elevenlabs.replacing = true;
    state.elevenlabs.wasPresent = false;
    state.elevenlabs.keyLast4 = null;
    state.elevenlabs.status = "idle";
    state.elevenlabs.result = null;
    state.elevenlabs.api_key = "";

    const card = container.querySelector(`.ob-provider-card[data-provider="${p.id}"]`);
    if (card) {
        const tmp = document.createElement("div");
        tmp.innerHTML = renderElevenLabsCard(p, state.elevenlabs).trim();
        const newCard = tmp.firstElementChild;
        card.replaceWith(newCard);
        wireElevenLabsCard(container, state, p);
        const newInput = container.querySelector("#ob-elevenlabs-key");
        if (newInput) newInput.focus();
    }
}

function wireElevenLabsCard(container, state, p) {
    const keyInput = container.querySelector("#ob-elevenlabs-key");
    const revealBtn = container.querySelector("#ob-elevenlabs-reveal");
    const validateBtn = container.querySelector("#ob-elevenlabs-validate");
    const statusEl = container.querySelector("#ob-elevenlabs-status");

    if (!keyInput || !revealBtn || !validateBtn || !statusEl) return;

    function updateValidateButton() {
        validateBtn.disabled = !state.elevenlabs.api_key;
    }

    function resetStatus() {
        if (state.elevenlabs.status !== "idle") {
            state.elevenlabs.status = "idle";
            state.elevenlabs.result = null;
            statusEl.dataset.status = "idle";
            statusEl.innerHTML = "";
        }
    }

    keyInput.addEventListener("input", () => {
        state.elevenlabs.api_key = keyInput.value.trim();
        resetStatus();
        updateValidateButton();
    });
    revealBtn.addEventListener("click", () => {
        const isPassword = keyInput.type === "password";
        keyInput.type = isPassword ? "text" : "password";
        revealBtn.innerHTML = isPassword ? "&#128584;" : "&#128065;";
    });
    validateBtn.addEventListener("click", () => validateElevenLabs(container, state));
}

async function validateElevenLabs(container, state) {
    const validateBtn = container.querySelector("#ob-elevenlabs-validate");
    const statusEl = container.querySelector("#ob-elevenlabs-status");
    const keyInput = container.querySelector("#ob-elevenlabs-key");

    if (state.elevenlabs.status === "validating") return;
    state.elevenlabs.status = "validating";
    statusEl.dataset.status = "validating";
    statusEl.innerHTML = `<span class="ob-status-pill ob-status-pill-validating">Checking your ElevenLabs key&hellip;</span>`;
    validateBtn.disabled = true;
    keyInput.disabled = true;

    try {
        const r = await fetch("/onboarding/validate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                provider: "elevenlabs",
                credentials: { api_key: state.elevenlabs.api_key },
            }),
        });
        const result = await r.json();
        state.elevenlabs.result = result;
        if (result.ok) {
            state.elevenlabs.status = "ok";
            statusEl.dataset.status = "ok";
            // detail = {tier, credits_remaining, features} — surface what the
            // plan unlocks so the customer sees the value of their key.
            const detail = result.detail || {};
            const tier = detail.tier ? escapeHtml(String(detail.tier)) : "unknown";
            const features = detail.features ? escapeHtml(String(detail.features)) : "";
            const credits = Number.isFinite(detail.credits_remaining)
                ? `<p class="ob-step-helper">${detail.credits_remaining.toLocaleString()} credits remaining this period.</p>`
                : "";
            statusEl.innerHTML = `
                <span class="ob-status-pill ob-status-pill-ok">
                    <span class="ob-status-pill-glyph" aria-hidden="true">&check;</span>
                    Key valid — ${tier} plan${features ? ` &middot; ${features}` : ""} &middot; ${result.latency_ms}ms
                </span>
                ${credits}
            `;
        } else {
            state.elevenlabs.status = "error";
            statusEl.dataset.status = "error";
            // Mirror Gmail: prefer detail when present, fall back to error,
            // strip any "FooError:" prefix, and cap length.
            const raw = result.detail || result.error || "validation failed";
            const errMsg = String(raw).replace(/^\w+Error:\s*/, "");
            statusEl.innerHTML = `
                <span class="ob-status-pill ob-status-pill-error">
                    <span class="ob-status-pill-glyph" aria-hidden="true">!</span>
                    ${escapeHtml(errMsg.slice(0, 160))}
                </span>
            `;
        }
    } catch (e) {
        state.elevenlabs.status = "error";
        statusEl.dataset.status = "error";
        statusEl.innerHTML = `
            <span class="ob-status-pill ob-status-pill-error">
                <span class="ob-status-pill-glyph" aria-hidden="true">!</span>
                Network error: ${escapeHtml(e.message.slice(0, 120))}
            </span>
        `;
    } finally {
        keyInput.disabled = false;
        validateBtn.disabled = !state.elevenlabs.api_key;
    }
}

// ─────────────────────────────────────────────────────────────────────────
// Service-account credential card (T2.5.2) — drag-drop JSON file upload
// ─────────────────────────────────────────────────────────────────────────

// Find the file in state.creds.files that matches GOOGLE_APPLICATION_CREDENTIALS.
// We compare by basename since the env var stores an absolute path while the
// list response carries filenames only.
function findActiveCredFile(state) {
    if (!state.creds.activeCreds) return null;
    const activeBasename = state.creds.activeCreds.split("/").pop();
    return state.creds.files.find(f => f.filename === activeBasename) || null;
}

function renderCredCard(p, state) {
    const active = findActiveCredFile(state);
    return `
        <div class="ob-provider-card ob-integration-card ob-credential-card" data-provider="${p.id}">
            <div class="ob-provider-header">
                <div class="ob-provider-label">${escapeHtml(p.label)}</div>
                <a class="ob-provider-link" href="${escapeHtml(p.consoleUrl)}" target="_blank" rel="noopener">
                    Google Cloud Console <span aria-hidden="true">↗</span>
                </a>
            </div>
            <p class="ob-integration-desc">${escapeHtml(p.description)}</p>
            <div class="ob-creds-body">
                ${renderCredCardBody(state, active)}
            </div>
            <input type="file" id="ob-creds-file-picker" accept="application/json,.json" hidden />
        </div>
    `;
}

// The body of the credential card swaps between three visual states:
// uploading (spinner), configured (filename + actions), or empty (drop-zone).
// Errors are appended below regardless of state.
function renderCredCardBody(state, active) {
    if (state.creds.uploading) {
        return `<div class="ob-creds-uploading">Uploading&hellip;</div>`;
    }
    const errorBlock = state.creds.error
        ? `<div class="ob-creds-error">${escapeHtml(state.creds.error)}</div>`
        : "";
    if (active) {
        const sizeKb = (active.size_bytes / 1024).toFixed(1);
        const saPip = active.is_google_service_account
            ? `<span class="ob-status-pill ob-status-pill-ok">
                   <span class="ob-status-pill-glyph" aria-hidden="true">&check;</span>
                   Service account
               </span>`
            : `<span class="ob-status-pill ob-status-pill-error">
                   <span class="ob-status-pill-glyph" aria-hidden="true">!</span>
                   Not a service account
               </span>`;
        return `
            <div class="ob-creds-configured">
                <div class="ob-creds-configured-info">
                    <span class="ob-creds-configured-filename">${escapeHtml(active.filename)}</span>
                    <span class="ob-creds-configured-meta">${sizeKb} KB &middot; linked to GOOGLE_APPLICATION_CREDENTIALS</span>
                    <span class="ob-creds-configured-saline">${saPip}</span>
                </div>
                <div class="ob-creds-configured-actions">
                    <button type="button" class="ob-replace-btn" data-creds-action="replace">Replace</button>
                    <button type="button" class="ob-row-remove" data-creds-action="remove" data-filename="${escapeHtml(active.filename)}" aria-label="Remove ${escapeHtml(active.filename)}">×</button>
                </div>
            </div>
            ${errorBlock}
        `;
    }
    // Empty state: show drop zone.
    return `
        <div class="ob-creds-dropzone" tabindex="0" role="button" aria-label="Drop or browse for a service account JSON file">
            <span class="ob-creds-dropzone-icon" aria-hidden="true">+</span>
            <span class="ob-creds-dropzone-text">
                Drag a service account <code>.json</code> file here
            </span>
            <span class="ob-creds-dropzone-hint">or click to browse</span>
        </div>
        ${errorBlock}
    `;
}

// Re-render the body subtree in place. Saves a full card teardown on every
// state transition (drop → uploading → configured).
function rerenderCredBody(container, state, p) {
    const card = container.querySelector(`.ob-credential-card[data-provider="${p.id}"]`);
    if (!card) return;
    const body = card.querySelector(".ob-creds-body");
    if (!body) return;
    const active = findActiveCredFile(state);
    body.innerHTML = renderCredCardBody(state, active);
    wireCredCardBody(container, state, p);
}

// Wire the entire card: file picker + drop zone + configured-state actions.
// Called once on mount, then again each time the body re-renders.
function wireCredCard(container, state, p) {
    wireCredCardBody(container, state, p);

    // File picker is rendered ONCE at the card root and not destroyed by
    // body re-renders. Wire it once.
    const card = container.querySelector(`.ob-credential-card[data-provider="${p.id}"]`);
    if (!card) return;
    const filePicker = card.querySelector("#ob-creds-file-picker");
    if (filePicker) {
        filePicker.addEventListener("change", async (e) => {
            const file = e.target.files && e.target.files[0];
            // Reset the input value so picking the SAME file twice still fires change.
            e.target.value = "";
            if (file) await uploadCredentialFile(file, container, state, p);
        });
    }
}

function wireCredCardBody(container, state, p) {
    const card = container.querySelector(`.ob-credential-card[data-provider="${p.id}"]`);
    if (!card) return;

    // Drop zone (empty state)
    const dropZone = card.querySelector(".ob-creds-dropzone");
    const filePicker = card.querySelector("#ob-creds-file-picker");
    if (dropZone && filePicker) {
        dropZone.addEventListener("click", () => filePicker.click());
        dropZone.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                filePicker.click();
            }
        });
        dropZone.addEventListener("dragover", (e) => {
            e.preventDefault();
            dropZone.classList.add("ob-drop-active");
        });
        dropZone.addEventListener("dragleave", () => {
            dropZone.classList.remove("ob-drop-active");
        });
        dropZone.addEventListener("drop", async (e) => {
            e.preventDefault();
            dropZone.classList.remove("ob-drop-active");
            const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
            if (!file) return;
            if (!file.name.toLowerCase().endsWith(".json")) {
                state.creds.error = "Only .json files are accepted.";
                rerenderCredBody(container, state, p);
                return;
            }
            await uploadCredentialFile(file, container, state, p);
        });
    }

    // Configured-state actions: Replace + Remove
    const replaceBtn = card.querySelector('[data-creds-action="replace"]');
    if (replaceBtn) {
        replaceBtn.addEventListener("click", () => filePicker && filePicker.click());
    }
    const removeBtn = card.querySelector('[data-creds-action="remove"]');
    if (removeBtn) {
        removeBtn.addEventListener("click", () => {
            const filename = removeBtn.dataset.filename;
            removeCredentialFile(filename, container, state, p);
        });
    }
}

async function uploadCredentialFile(file, container, state, p) {
    if (state.creds.uploading) return;
    state.creds.uploading = true;
    state.creds.error = null;
    rerenderCredBody(container, state, p);

    const formData = new FormData();
    formData.append("file", file);
    try {
        const r = await fetch("/onboarding/credentials/upload", {
            method: "POST",
            body: formData,
        });
        let result = null;
        try {
            result = await r.json();
        } catch (_) {
            result = null;
        }
        if (!r.ok) {
            state.creds.error = (result && result.detail) || `Upload failed (HTTP ${r.status})`;
        } else {
            await reloadCreds(state);
            state.creds.error = null;
        }
    } catch (e) {
        state.creds.error = `Network error: ${e.message}`;
    } finally {
        state.creds.uploading = false;
        rerenderCredBody(container, state, p);
    }
}

async function removeCredentialFile(filename, container, state, p) {
    if (!filename) return;
    const ok = window.confirm(
        `Remove ${filename}? This will also clear GOOGLE_APPLICATION_CREDENTIALS if it points to this file.`
    );
    if (!ok) return;
    try {
        const r = await fetch(`/onboarding/credentials/${encodeURIComponent(filename)}`, {
            method: "DELETE",
        });
        if (!r.ok) {
            let detail = `HTTP ${r.status}`;
            try {
                const j = await r.json();
                if (j.detail) detail = j.detail;
            } catch (_) { /* ignore */ }
            state.creds.error = `Remove failed: ${detail}`;
        } else {
            await reloadCreds(state);
            state.creds.error = null;
        }
    } catch (e) {
        state.creds.error = `Network error: ${e.message}`;
    } finally {
        rerenderCredBody(container, state, p);
    }
}

async function reloadCreds(state) {
    try {
        const r = await fetch("/onboarding/credentials");
        if (r.ok) {
            const data = await r.json();
            state.creds.files = data.files || [];
            state.creds.activeCreds = data.google_application_credentials || null;
        }
    } catch (_) {
        // Leave existing state on transient failure; error block will surface
        // anything caller stuffed into state.creds.error.
    }
}

// Wire either the configured-state Replace button OR the input-state form,
// depending on which variant is currently rendered.
function wireGmailCardForState(container, state, p) {
    const s = state.gmail;
    if (s.wasPresent && !s.replacing) {
        const replaceBtn = container.querySelector(
            `.ob-provider-card[data-provider="${p.id}"] .ob-replace-btn`
        );
        if (replaceBtn) {
            replaceBtn.addEventListener("click", () => startReplacingGmail(p, state, container));
        }
        return;
    }
    wireGmailCard(container, state, p);
}

// Swap the Gmail card from configured -> input state when Replace is clicked.
function startReplacingGmail(p, state, container) {
    state.gmail.replacing = true;
    state.gmail.wasPresent = false;
    state.gmail.existingClientId = null;
    state.gmail.secretLast4 = null;
    state.gmail.status = "idle";
    state.gmail.result = null;
    state.gmail.client_id = "";
    state.gmail.client_secret = "";

    const card = container.querySelector(`.ob-provider-card[data-provider="${p.id}"]`);
    if (card) {
        const tmp = document.createElement("div");
        tmp.innerHTML = renderGmailCard(p, state.gmail).trim();
        const newCard = tmp.firstElementChild;
        card.replaceWith(newCard);
        wireGmailCard(container, state, p);
        const newInput = container.querySelector("#ob-gmail-client-id");
        if (newInput) newInput.focus();
    }
}

function wireGmailCard(container, state, p) {
    const idInput = container.querySelector("#ob-gmail-client-id");
    const secretInput = container.querySelector("#ob-gmail-client-secret");
    const revealBtn = container.querySelector("#ob-gmail-reveal");
    const validateBtn = container.querySelector("#ob-gmail-validate");
    const statusEl = container.querySelector("#ob-gmail-status");

    if (!idInput || !secretInput || !revealBtn || !validateBtn || !statusEl) return;

    // E15: wire Copy buttons on the per-machine redirect URI list
    container.querySelectorAll(".ob-gmail-uri-copy").forEach(btn => {
        btn.addEventListener("click", async () => {
            const text = btn.dataset.copy || "";
            const orig = btn.textContent;
            try {
                await navigator.clipboard.writeText(text);
                btn.textContent = "Copied ✓";
            } catch {
                btn.textContent = "Copy failed";
            }
            setTimeout(() => { btn.textContent = orig; }, 1500);
        });
    });

    function updateValidateButton() {
        validateBtn.disabled = !(state.gmail.client_id && state.gmail.client_secret);
    }

    function resetStatus() {
        if (state.gmail.status !== "idle") {
            state.gmail.status = "idle";
            state.gmail.result = null;
            statusEl.dataset.status = "idle";
            statusEl.innerHTML = "";
        }
    }

    idInput.addEventListener("input", () => {
        state.gmail.client_id = idInput.value.trim();
        resetStatus();
        updateValidateButton();
    });
    secretInput.addEventListener("input", () => {
        state.gmail.client_secret = secretInput.value.trim();
        resetStatus();
        updateValidateButton();
    });
    revealBtn.addEventListener("click", () => {
        const isPassword = secretInput.type === "password";
        secretInput.type = isPassword ? "text" : "password";
        revealBtn.innerHTML = isPassword ? "&#128584;" : "&#128065;";
    });
    validateBtn.addEventListener("click", () => validateGmail(container, state));
}

async function validateGmail(container, state) {
    const validateBtn = container.querySelector("#ob-gmail-validate");
    const statusEl = container.querySelector("#ob-gmail-status");
    const idInput = container.querySelector("#ob-gmail-client-id");
    const secretInput = container.querySelector("#ob-gmail-client-secret");

    if (state.gmail.status === "validating") return;
    state.gmail.status = "validating";
    statusEl.dataset.status = "validating";
    statusEl.innerHTML = `<span class="ob-status-pill ob-status-pill-validating">Validating OAuth flow&hellip;</span>`;
    validateBtn.disabled = true;
    idInput.disabled = true;
    secretInput.disabled = true;

    try {
        const r = await fetch("/onboarding/validate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                provider: "gmail",
                credentials: {
                    client_id: state.gmail.client_id,
                    client_secret: state.gmail.client_secret,
                },
            }),
        });
        const result = await r.json();
        state.gmail.result = result;
        if (result.ok) {
            state.gmail.status = "ok";
            statusEl.dataset.status = "ok";
            statusEl.innerHTML = `
                <span class="ob-status-pill ob-status-pill-ok">
                    <span class="ob-status-pill-glyph" aria-hidden="true">&check;</span>
                    OAuth flow constructed &middot; ${result.latency_ms}ms
                </span>
                <p class="ob-step-helper">
                    You'll complete the Google Workspace authorization once setup is done &mdash; we'll
                    launch the Google sign-in from the System Menu so you can grant access to
                    Gmail, Docs, Sheets, Slides, Drive, and Calendar.
                </p>
            `;
        } else {
            state.gmail.status = "error";
            statusEl.dataset.status = "error";
            const errMsg = (result.error || "validation failed").replace(/^\w+Error:\s*/, "");
            statusEl.innerHTML = `
                <span class="ob-status-pill ob-status-pill-error">
                    <span class="ob-status-pill-glyph" aria-hidden="true">!</span>
                    ${escapeHtml(errMsg.slice(0, 160))}
                </span>
            `;
        }
    } catch (e) {
        state.gmail.status = "error";
        statusEl.dataset.status = "error";
        statusEl.innerHTML = `
            <span class="ob-status-pill ob-status-pill-error">
                <span class="ob-status-pill-glyph" aria-hidden="true">!</span>
                Network error: ${escapeHtml(e.message.slice(0, 120))}
            </span>
        `;
    } finally {
        idInput.disabled = false;
        secretInput.disabled = false;
        validateBtn.disabled = !(state.gmail.client_id && state.gmail.client_secret);
    }
}

function wireSave(container, state, next) {
    const saveBtn = container.querySelector("#ob-extras-save");
    saveBtn.addEventListener("click", async () => {
        if (busy) return;
        busy = true;
        saveBtn.disabled = true;
        const orig = saveBtn.innerHTML;
        saveBtn.innerHTML = "Saving&hellip;";

        // Only POST credentials that were newly validated this session. If the
        // customer is keeping pre-existing creds (wasPresent + !replacing), the
        // keys already in .env stay untouched.
        const secrets = {};
        if (state.gmail.status === "ok") {
            secrets.GOOGLE_OAUTH_CLIENT_ID = state.gmail.client_id;
            secrets.GOOGLE_OAUTH_CLIENT_SECRET = state.gmail.client_secret;
        }
        if (state.elevenlabs.status === "ok") {
            secrets.ELEVENLABS_API_KEY = state.elevenlabs.api_key;
        }

        try {
            // Always POST /save (server handles empty secrets gracefully)
            const r = await fetch("/onboarding/save", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ secrets }),
            });
            if (!r.ok) {
                throw new Error(`Save failed: ${r.status}`);
            }
            await next();
        } catch (e) {
            saveBtn.innerHTML = orig;
            saveBtn.disabled = false;
            const integrations = container.querySelector("#ob-integrations");
            const toast = document.createElement("div");
            toast.className = "ob-step-error-inline";
            toast.textContent = `Couldn't save: ${e.message}. Try again.`;
            integrations.parentNode.insertBefore(toast, integrations.nextSibling);
            setTimeout(() => toast.remove(), 5000);
        } finally {
            busy = false;
        }
    });
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
