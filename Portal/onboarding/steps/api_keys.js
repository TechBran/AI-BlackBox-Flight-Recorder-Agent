// API keys step — third screen of the onboarding wizard.
// Customer pastes OpenAI / Anthropic / Google keys and validates each.
// Save & continue (active when ≥1 validated OR pre-existing config retained)
// persists keys to .env via /onboarding/save and advances via ctx.next().
//
// Rehydration: on mount we fetch /onboarding/current-config. For each
// provider already present in .env we render an "Already configured" card
// with a Replace button instead of an empty paste field. Pre-existing
// untouched keys are NOT re-posted on save — only newly validated ones.
//
// Pattern: per-provider state object tracks
//   {value, status, result, wasPresent, last4, replacing}.
// Status: "idle" | "validating" | "ok" | "error"
// Visual reference: design system extends welcome + tailscale steps.

const PROVIDERS = [
    {
        id: "openai",
        label: "OpenAI",
        envVar: "OPENAI_API_KEY",
        keyUrl: "https://platform.openai.com/api-keys",
        keyHint: "sk-proj-…",
    },
    {
        id: "anthropic",
        label: "Anthropic",
        envVar: "ANTHROPIC_API_KEY",
        keyUrl: "https://console.anthropic.com/settings/keys",
        keyHint: "sk-ant-…",
    },
    {
        id: "google",
        label: "Google AI",
        envVar: "GOOGLE_API_KEY",
        keyUrl: "https://aistudio.google.com/apikey",
        keyHint: "AIza…",
    },
    {
        id: "xai",
        label: "xAI (Grok)",
        envVar: "XAI_API_KEY",
        keyUrl: "https://console.x.ai",
        keyHint: "xai-…",
    },
    {
        id: "perplexity",
        label: "Perplexity",
        envVar: "PERPLEXITY_API_KEY",
        keyUrl: "https://www.perplexity.ai/settings/api",
        keyHint: "pplx-…",
    },
    // ── Reranker upgrade keys (M10). Same paste/reveal/Validate card as every
    // other provider; the Memory step's reranker selector just picks from
    // whatever's validated here. Honest framing: memory works WITHOUT them.
    {
        id: "voyage",
        label: "Voyage (reranking)",
        envVar: "VOYAGE_API_KEY",
        keyUrl: "https://dashboard.voyageai.com/api-keys",
        keyHint: "pa-…",
        description: "Optional reranker upgrade — sharpens memory recall with a "
            + "dedicated cross-encoder. Embeddings and memory work without it. "
            + "Generous free tier.",
    },
    {
        id: "cohere",
        label: "Cohere (reranking)",
        envVar: "COHERE_API_KEY",
        keyUrl: "https://dashboard.cohere.com/api-keys",
        keyHint: "your Cohere key",
        description: "Optional reranker upgrade — reorders search results with a "
            + "dedicated cross-encoder. Embeddings and memory work without it.",
    },
];

// Per-instance state — reset on each render() call (which fires when wizard
// re-enters this step, e.g., after back-then-next navigation).
function makeInitialState(currentConfig) {
    return PROVIDERS.reduce((acc, p) => {
        const cfg = currentConfig?.providers?.[p.id];
        acc[p.id] = {
            value: "",
            status: "idle",
            result: null,
            wasPresent: !!(cfg && cfg.present),
            last4: cfg?.last4 || null,
            replacing: false,
        };
        return acc;
    }, {});
}

let busy = false;  // prevents save-button double-fire

export async function render(container, { next, back, skip, sigil }) {
    // Fetch current config first so we can rehydrate "already configured"
    // state per provider. Fail-open: empty config means render the original
    // empty-input flow.
    let currentConfig = null;
    try {
        const r = await fetch("/onboarding/current-config");
        if (r.ok) {
            currentConfig = await r.json();
        }
    } catch (e) {
        // Network error — proceed with empty config (acts as if nothing
        // was pre-configured, customer pastes fresh).
        currentConfig = null;
    }

    const state = makeInitialState(currentConfig);

    container.innerHTML = `
        <section class="ob-step ob-api-keys">
            <aside class="ob-step-sigil" aria-hidden="true">
                <div class="ob-step-sigil-num"><em>${sigil ? sigil.num : "03"}</em></div>
                <div class="ob-step-sigil-rule"></div>
                <div class="ob-step-sigil-label">KEYS</div>
            </aside>
            <div class="ob-step-body">
                <div class="ob-step-eyebrow">
                    <span class="ob-step-eyebrow-dot" aria-hidden="true"></span>
                    Bring your own keys
                </div>
                <h1 class="ob-step-title">
                    Connect your <em>AI providers</em>.
                </h1>
                <p class="ob-step-lede">
                    Paste your API keys for the providers you want to use. We
                    validate each key with a free, low-cost call so you'll know
                    immediately if it's working. You pay providers directly &mdash;
                    no middle-man billing on our side.
                </p>
                <div class="ob-providers" id="ob-providers">
                    ${PROVIDERS.map(p => renderProviderCardForState(p, state)).join("")}
                </div>
                <div class="ob-cta-row">
                    <button type="button" class="ob-cta" id="ob-keys-save" disabled>
                        Save &amp; continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                    </button>
                </div>
                <nav class="ob-step-nav" aria-label="Step navigation">
                    <button type="button" class="ob-back" id="ob-keys-back">
                        <span aria-hidden="true">&larr;</span> Back to ${sigil && sigil.backLabel ? sigil.backLabel.toLowerCase() : "tailnet"}
                    </button>
                    <button type="button" class="ob-skip" id="ob-keys-skip">
                        Skip &mdash; I'll add keys later <span aria-hidden="true">&rarr;</span>
                    </button>
                </nav>
            </div>
        </section>
    `;

    wireProviderCards(container, state);
    wireSave(container, state, next);
    updateSaveButton(container, state);
    document.getElementById("ob-keys-back").addEventListener("click", back);
    document.getElementById("ob-keys-skip").addEventListener("click", skip);
}

// Dispatcher: pick configured-state card or input-state card based on
// rehydration state.
function renderProviderCardForState(p, state) {
    const s = state[p.id];
    if (s.wasPresent && !s.replacing) {
        return renderProviderCardConfigured(p, s);
    }
    return renderProviderCard(p);
}

function renderProviderCard(p) {
    return `
        <div class="ob-provider-card" data-provider="${p.id}">
            <div class="ob-provider-header">
                <div class="ob-provider-label">${escapeHtml(p.label)}</div>
                <a class="ob-provider-link" href="${escapeHtml(p.keyUrl)}" target="_blank" rel="noopener">
                    Get a key <span aria-hidden="true">↗</span>
                </a>
            </div>
            ${p.description ? `<p class="ob-provider-desc">${escapeHtml(p.description)}</p>` : ""}
            <div class="ob-provider-input-row">
                <input
                    type="password"
                    class="ob-provider-input"
                    id="ob-input-${p.id}"
                    placeholder="${escapeHtml(p.keyHint)}"
                    autocomplete="off"
                    autocapitalize="off"
                    spellcheck="false"
                    data-provider="${p.id}"
                />
                <button
                    type="button"
                    class="ob-reveal-btn"
                    id="ob-reveal-${p.id}"
                    data-provider="${p.id}"
                    aria-label="Show or hide ${escapeHtml(p.label)} key"
                >👁</button>
                <button
                    type="button"
                    class="ob-validate-btn"
                    id="ob-validate-${p.id}"
                    data-provider="${p.id}"
                    disabled
                >Validate</button>
            </div>
            <div class="ob-provider-status" id="ob-status-${p.id}" data-status="idle"></div>
        </div>
    `;
}

function renderProviderCardConfigured(p, s) {
    // Server returns last4 as a fully-masked string with the real last 4
    // characters at the end (e.g., "••••••••XYZW"). Trim to the trailing
    // meaningful suffix so the pill stays readable on narrow widths.
    const preview = formatLast4Preview(s.last4);
    return `
        <div class="ob-provider-card ob-provider-configured" data-provider="${p.id}">
            <div class="ob-provider-header">
                <div class="ob-provider-label">${escapeHtml(p.label)}</div>
                <a class="ob-provider-link" href="${escapeHtml(p.keyUrl)}" target="_blank" rel="noopener">
                    Get a new key <span aria-hidden="true">↗</span>
                </a>
            </div>
            ${p.description ? `<p class="ob-provider-desc">${escapeHtml(p.description)}</p>` : ""}
            <div class="ob-provider-configured-row">
                <span class="ob-status-pill ob-status-pill-ok">
                    <span class="ob-status-pill-glyph" aria-hidden="true">&check;</span>
                    Already configured &middot; ${escapeHtml(preview)}
                </span>
                <button type="button" class="ob-validate-btn ob-validate-existing" data-provider="${p.id}">Validate</button>
                <button type="button" class="ob-replace-btn" data-provider="${p.id}">Replace</button>
            </div>
            <div class="ob-provider-status" id="ob-status-${p.id}" data-status="idle"></div>
        </div>
    `;
}

// Reduce the server-rendered redacted preview down to a short, readable
// suffix: 4 leading bullets + the trailing alphanumeric tail (typically the
// real last 4 characters of the key).
function formatLast4Preview(raw) {
    if (!raw) return "set";
    // Pull off the trailing non-bullet chars (the real last4-ish suffix).
    const m = String(raw).match(/([A-Za-z0-9_\-]+)$/);
    const tail = m ? m[1] : "";
    if (!tail) return "set";
    return "••••" + tail;
}

function wireProviderCards(container, state) {
    PROVIDERS.forEach(p => wireSingleProviderCard(container, state, p));
}

// Wire a single provider card. If the card is in the configured state, only
// the Replace button needs wiring. If it's in the input state, the input,
// reveal, and validate controls need wiring.
function wireSingleProviderCard(container, state, p) {
    const s = state[p.id];

    if (s.wasPresent && !s.replacing) {
        // Configured-state card: wire Replace + re-Validate (troubleshooting).
        const cardSel = `.ob-provider-card[data-provider="${p.id}"]`;
        const replaceBtn = container.querySelector(`${cardSel} .ob-replace-btn`);
        if (replaceBtn) {
            replaceBtn.addEventListener("click", () => startReplacing(p, state, container));
        }
        const reValidateBtn = container.querySelector(`${cardSel} .ob-validate-existing`);
        if (reValidateBtn) {
            reValidateBtn.addEventListener("click", () => validateStoredProvider(p, container));
        }
        return;
    }

    // Input-state card: wire input + reveal + validate.
    const input = container.querySelector(`#ob-input-${p.id}`);
    const validateBtn = container.querySelector(`#ob-validate-${p.id}`);
    const revealBtn = container.querySelector(`#ob-reveal-${p.id}`);
    const statusEl = container.querySelector(`#ob-status-${p.id}`);

    if (!input || !validateBtn || !revealBtn || !statusEl) return;

    // Input: track value + enable/disable validate button
    input.addEventListener("input", () => {
        state[p.id].value = input.value.trim();
        // Reset status when user changes the value
        if (state[p.id].status !== "idle") {
            state[p.id].status = "idle";
            state[p.id].result = null;
            statusEl.dataset.status = "idle";
            statusEl.innerHTML = "";
            updateSaveButton(container, state);
        }
        validateBtn.disabled = state[p.id].value.length === 0;
    });

    // Reveal toggle
    revealBtn.addEventListener("click", () => {
        const isPassword = input.type === "password";
        input.type = isPassword ? "text" : "password";
        revealBtn.textContent = isPassword ? "🙈" : "👁";
    });

    // Validate
    validateBtn.addEventListener("click", () => validateProvider(p, state, container));
}

// Swap a card from configured -> input state when Replace is clicked.
function startReplacing(p, state, container) {
    state[p.id].replacing = true;
    state[p.id].wasPresent = false;  // treat as fresh entry going forward
    state[p.id].last4 = null;
    state[p.id].status = "idle";
    state[p.id].result = null;
    state[p.id].value = "";

    // Re-render this single card in-place
    const card = container.querySelector(`.ob-provider-card[data-provider="${p.id}"]`);
    if (card) {
        const tmp = document.createElement("div");
        tmp.innerHTML = renderProviderCard(p).trim();
        const newCard = tmp.firstElementChild;
        card.replaceWith(newCard);
        // Re-wire the new card's handlers
        wireSingleProviderCard(container, state, p);
        // Focus the input so the user can paste immediately
        const newInput = container.querySelector(`#ob-input-${p.id}`);
        if (newInput) newInput.focus();
    }
    updateSaveButton(container, state);
}

async function validateProvider(p, state, container) {
    const input = container.querySelector(`#ob-input-${p.id}`);
    const validateBtn = container.querySelector(`#ob-validate-${p.id}`);
    const statusEl = container.querySelector(`#ob-status-${p.id}`);
    const value = state[p.id].value;
    if (!value) return;
    if (state[p.id].status === "validating") return;  // re-entrancy guard

    state[p.id].status = "validating";
    statusEl.dataset.status = "validating";
    statusEl.innerHTML = `<span class="ob-status-pill ob-status-pill-validating">Validating&hellip;</span>`;
    validateBtn.disabled = true;
    input.disabled = true;

    try {
        const r = await fetch("/onboarding/validate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                provider: p.id,
                credentials: { api_key: value },
            }),
        });
        const result = await r.json();
        state[p.id].result = result;
        if (result.ok) {
            state[p.id].status = "ok";
            statusEl.dataset.status = "ok";
            const detailText = formatDetail(p.id, result.detail);
            statusEl.innerHTML = `
                <span class="ob-status-pill ob-status-pill-ok">
                    <span class="ob-status-pill-glyph" aria-hidden="true">&check;</span>
                    Validated &middot; ${result.latency_ms}ms${detailText ? ` &middot; ${escapeHtml(detailText)}` : ""}
                </span>
            `;
        } else {
            state[p.id].status = "error";
            statusEl.dataset.status = "error";
            const errMsg = (result.error || "validation failed").replace(/^\w+Error:\s*/, "");
            statusEl.innerHTML = `
                <span class="ob-status-pill ob-status-pill-error">
                    <span class="ob-status-pill-glyph" aria-hidden="true">!</span>
                    ${escapeHtml(errMsg.slice(0, 120))}
                </span>
            `;
        }
    } catch (e) {
        state[p.id].status = "error";
        statusEl.dataset.status = "error";
        statusEl.innerHTML = `
            <span class="ob-status-pill ob-status-pill-error">
                <span class="ob-status-pill-glyph" aria-hidden="true">!</span>
                Network error: ${escapeHtml(e.message.slice(0, 120))}
            </span>
        `;
    } finally {
        input.disabled = false;
        validateBtn.disabled = state[p.id].value.length === 0;
        updateSaveButton(container, state);
    }
}

// Re-validate an ALREADY-CONFIGURED key (troubleshooting). The backend reads
// the stored .env value and validates it — the client never re-handles the
// secret. On success the backend stamps validated_at, which clears the
// "needs attention" state on the console hub + done summary.
async function validateStoredProvider(p, container) {
    const card = container.querySelector(`.ob-provider-card[data-provider="${p.id}"]`);
    const btn = card ? card.querySelector(".ob-validate-existing") : null;
    const statusEl = container.querySelector(`#ob-status-${p.id}`);
    if (!statusEl) return;
    if (btn) btn.disabled = true;
    statusEl.dataset.status = "validating";
    statusEl.innerHTML = `<span class="ob-status-pill ob-status-pill-validating">Validating&hellip;</span>`;
    try {
        const r = await fetch("/onboarding/validate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            // No credentials — the backend validates the stored key for this provider.
            body: JSON.stringify({ provider: p.id }),
        });
        const result = await r.json();
        if (result.ok) {
            statusEl.dataset.status = "ok";
            const detailText = formatDetail(p.id, result.detail);
            statusEl.innerHTML = `
                <span class="ob-status-pill ob-status-pill-ok">
                    <span class="ob-status-pill-glyph" aria-hidden="true">&check;</span>
                    Validated &middot; ${result.latency_ms}ms${detailText ? ` &middot; ${escapeHtml(detailText)}` : ""}
                </span>`;
        } else {
            statusEl.dataset.status = "error";
            const errMsg = (result.error || "validation failed").replace(/^\w+Error:\s*/, "");
            statusEl.innerHTML = `
                <span class="ob-status-pill ob-status-pill-error">
                    <span class="ob-status-pill-glyph" aria-hidden="true">!</span>
                    ${escapeHtml(errMsg.slice(0, 120))}
                </span>`;
        }
    } catch (e) {
        statusEl.dataset.status = "error";
        statusEl.innerHTML = `
            <span class="ob-status-pill ob-status-pill-error">
                <span class="ob-status-pill-glyph" aria-hidden="true">!</span>
                Network error: ${escapeHtml(e.message.slice(0, 120))}
            </span>`;
    } finally {
        if (btn) btn.disabled = false;
    }
}

function formatDetail(providerId, detail) {
    if (!detail) return "";
    if (providerId === "openai" || providerId === "google") {
        return detail.model_count ? `${detail.model_count} models` : "";
    }
    if (providerId === "anthropic" || providerId === "xai" || providerId === "perplexity") {
        return detail.model ? detail.model : "";
    }
    if (providerId === "voyage") {
        return detail.model ? detail.model : "";
    }
    if (providerId === "cohere") {
        return detail.organization ? detail.organization : "";
    }
    return "";
}

// Save button is enabled when there is something we can advance with:
//   - a newly validated key (status === "ok"), OR
//   - a pre-existing key that the customer chose to keep (wasPresent && !replacing).
// Label flips between "Save & continue" (something to POST) and "Continue"
// (everything is already configured and untouched, so save is a no-op).
function updateSaveButton(container, state) {
    const saveBtn = container.querySelector("#ob-keys-save");
    if (!saveBtn) return;

    const anyNewlyValidated = PROVIDERS.some(p => state[p.id].status === "ok");
    const anyRetainedExisting = PROVIDERS.some(
        p => state[p.id].wasPresent && !state[p.id].replacing
    );

    saveBtn.disabled = !(anyNewlyValidated || anyRetainedExisting);

    const label = anyNewlyValidated ? "Save &amp; continue" : "Continue";
    saveBtn.innerHTML = `${label} <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>`;
}

function wireSave(container, state, next) {
    const saveBtn = container.querySelector("#ob-keys-save");
    saveBtn.addEventListener("click", async () => {
        if (busy) return;
        if (saveBtn.disabled) return;
        busy = true;
        saveBtn.disabled = true;
        const orig = saveBtn.innerHTML;
        saveBtn.innerHTML = "Saving&hellip;";

        // Only POST keys that were newly validated. Pre-existing untouched
        // keys stay in .env as-is.
        const secrets = {};
        PROVIDERS.forEach(p => {
            if (state[p.id].status === "ok") {
                secrets[p.envVar] = state[p.id].value;
            }
        });

        // If nothing changed, skip the POST entirely and just advance.
        const nothingToSave = Object.keys(secrets).length === 0;

        try {
            if (!nothingToSave) {
                const r = await fetch("/onboarding/save", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ secrets }),
                });
                if (!r.ok) {
                    throw new Error(`Save failed: ${r.status}`);
                }
            }
            await next();
        } catch (e) {
            saveBtn.innerHTML = orig;
            saveBtn.disabled = false;
            // Show transient error somewhere visible
            const providers = container.querySelector("#ob-providers");
            const toast = document.createElement("div");
            toast.className = "ob-step-error-inline";
            toast.textContent = `Couldn't save keys: ${e.message}. Try again.`;
            providers.parentNode.insertBefore(toast, providers.nextSibling);
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
