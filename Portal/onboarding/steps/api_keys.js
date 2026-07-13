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
//
// Custom model servers (additive section below the provider cards):
// OpenAI-compatible servers on the local network. Rows hydrate from
// GET /onboarding/custom-servers (its redacted listing keeps validated_at —
// /onboarding/current-config has no custom entry). Add flow = POST the
// server FIRST, then validate by server_id so validated_at/last_models
// stamp server-side. No confirm()/alert() dialogs anywhere — remove uses
// an inline "Remove? ✓/✕" toggle. A configured custom server counts as
// "retained existing" for Save-button enablement.

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

// ── Custom model servers state ──────────────────────────────────────────
// Module-level (like `busy`) so updateSaveButton can read it without
// threading a parameter through every existing call site; reset on render().
//   servers: [{server, mode:"view"|"edit", confirmingRemove, busy,
//              statusKind, statusHtml,
//              edit:{alias,base_url,api_key,context_tokens}|null}]
//   adding:  [{id, alias, base_url, api_key, context_tokens, busy,
//              statusKind, statusHtml}]
// context_tokens is held as a STRING in form state (input value) and coerced
// with parseInt on submit; blank = server default (add) / unchanged (edit).
let customState = { servers: [], adding: [] };
let nextCustomAddId = 1;  // monotonically increasing across re-renders

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

    // Hydrate custom model servers from their own endpoint (NOT
    // current-config — its providers dict has no custom entry). Fail-open:
    // a network blip renders the empty section, "+ Add server" still works.
    customState = { servers: [], adding: [] };
    try {
        const r = await fetch("/onboarding/custom-servers");
        if (r.ok) {
            const data = await r.json();
            customState.servers = (data.servers || []).map(sv => ({
                server: sv,
                mode: "view",
                confirmingRemove: false,
                busy: false,
                statusKind: "idle",
                statusHtml: "",
                edit: null,
            }));
        }
    } catch (_e) {
        // Silent fallback — section renders empty, customer can still add.
    }

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
                <div class="ob-custom-servers" id="ob-custom-servers">
                    <div class="ob-operator-section-divider">Custom model servers</div>
                    <p class="ob-provider-desc">
                        OpenAI-compatible servers on your network (llama.cpp,
                        llama-swap, vLLM, Ollama). Models are discovered
                        automatically; per-model context limits are learned
                        automatically when a model reports a smaller window.
                    </p>
                    <div class="ob-providers" id="ob-custom-rows" style="margin: var(--ob-space-4) 0;"></div>
                    <button type="button" class="ob-add-row" id="ob-custom-add">
                        <span aria-hidden="true">+</span> Add server
                    </button>
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
    rerenderCustom(container, state);  // also runs updateSaveButton
    document.getElementById("ob-custom-add").addEventListener("click", () => addCustomRow(container, state));
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

// ════════════════════════════════════════════════════════════════════════
// Custom model servers — OpenAI-compatible servers on the local network.
// Own endpoints (/onboarding/custom-servers + /onboarding/validate with
// provider:"custom"); fully additive next to the PROVIDERS cards above.
// Pattern follows operator.js: state array + full section rerender + rewire
// on structural changes; input events mutate state only (keeps typing focus).
// ════════════════════════════════════════════════════════════════════════

// Shared inline layout styles (stylesheet is intentionally untouched — this
// section reuses the existing card/pill/input/label classes).
const CUSTOM_BTN_GROUP_STYLE = "display:inline-flex; align-items:center; gap: var(--ob-space-2); flex-wrap: wrap;";
const CUSTOM_FIELDS_STYLE = "display:grid; gap: var(--ob-space-1); margin-bottom: var(--ob-space-3);";
const CUSTOM_LABEL_GAP_STYLE = "margin-top: var(--ob-space-2);";

function rerenderCustom(container, state) {
    const rowsEl = container.querySelector("#ob-custom-rows");
    if (!rowsEl) return;

    // Preserve focus across the innerHTML swap: a user typing in add-row B
    // must not lose their caret because row A's async op settled and forced
    // a section rerender. Capture the focused element's id (inputs all have
    // stable ids) and re-focus its replacement after rewire.
    const active = document.activeElement;
    const refocusId = active && rowsEl.contains(active) ? active.id : null;

    let html = "";
    for (const row of customState.servers) {
        html += row.mode === "edit" ? renderCustomEditCard(row) : renderCustomViewCard(row);
    }
    for (const row of customState.adding) {
        html += renderCustomAddCard(row);
    }
    rowsEl.innerHTML = html;
    wireCustomRows(container, state);
    updateSaveButton(container, state);

    if (refocusId) {
        const el = document.getElementById(refocusId);
        if (el) {
            el.focus();
            // Restore the caret to the end for text-like inputs.
            if (typeof el.setSelectionRange === "function" && typeof el.value === "string") {
                const end = el.value.length;
                try { el.setSelectionRange(end, end); } catch (_e) { /* non-text input type */ }
            }
        }
    }
}

// Configured server — read-only row: alias, base_url · key preview,
// validated-state pill, Re-validate / Edit / [×] Remove (inline confirm).
// ── Detected model modalities ───────────────────────────────────────────
// Auto-ingestion classifies each discovered model (chat / image / tts / stt /
// embedding). We surface that seed so the customer can REVIEW and CORRECT it,
// but the default is zero-effort: pre-filled, accept as-is.
const MODALITY_OPTIONS = [
    { value: "chat", label: "Chat" },
    { value: "image", label: "Image" },
    { value: "tts", label: "Text-to-speech" },
    { value: "stt", label: "Speech-to-text" },
    { value: "embedding", label: "Embedding" },
    { value: "ignore", label: "Ignore" },
];
// Friendly nouns for the one-line summary (ignore omitted — not "set up").
const MODALITY_SUMMARY_LABELS = {
    chat: "chat", image: "image", tts: "speech-out", stt: "speech-in", embedding: "embedding",
};
const MODALITY_SUMMARY_ORDER = ["chat", "image", "tts", "stt", "embedding"];
// Recognized <select> values; an unknown persisted value renders as "chat".
const MODALITY_VALUES = new Set(MODALITY_OPTIONS.map(o => o.value));

// ── Audio (STT/TTS) model ids ────────────────────────────────────────────
// A custom server can host audio behind /v1/audio/* + /v1/realtime, but those
// models are NOT in /v1/models — so their ids can't be auto-discovered. The
// validate probe reports capability bools (stt/tts/streaming); the customer
// CONFIRMS / EDITS the model ids (prefilled with the Speaches defaults) so the
// BlackBox can route audio. Defaults track the Speaches project's recommended
// small models.
const SPEACHES_STT_DEFAULT = "deepdml/faster-whisper-large-v3-turbo-ct2";
const SPEACHES_TTS_DEFAULT = "speaches-ai/Kokoro-82M-v1.0-ONNX";

// Compact "Detected models" block — rendered only for a validated server with
// ≥1 discovered model. One <select> per model, pre-filled from the seeded
// modality map (unmapped / pre-feature servers default every model to "chat").
// Returns "" when the server isn't validated or has no discovered models.
function renderCustomModalitiesBlock(sv, disabled) {
    const models = Array.isArray(sv.last_models) ? sv.last_models : [];
    if (!sv.validated_at || models.length === 0) return "";
    const map = sv.model_modalities || {};
    const dis = disabled ? "disabled" : "";

    // Summary counts, computed from the (possibly empty) modality map.
    const counts = {};
    for (const m of models) {
        const cur = MODALITY_VALUES.has(map[String(m)]) ? map[String(m)] : "chat";
        counts[cur] = (counts[cur] || 0) + 1;
    }
    const parts = MODALITY_SUMMARY_ORDER
        .filter(k => counts[k])
        .map(k => `${counts[k]} ${MODALITY_SUMMARY_LABELS[k]}`);
    const summary = parts.length
        ? `Detected: ${parts.join(" &middot; ")} &mdash; set up automatically. Adjust any that look wrong.`
        : `Detected models &mdash; set up automatically. Adjust any that look wrong.`;

    const rows = models.map(m => {
        const mid = String(m);
        const cur = MODALITY_VALUES.has(map[mid]) ? map[mid] : "chat";
        const opts = MODALITY_OPTIONS.map(o =>
            `<option value="${o.value}"${o.value === cur ? " selected" : ""}>${o.label}</option>`
        ).join("");
        return `
            <div style="display:flex; align-items:center; justify-content:space-between; gap: var(--ob-space-3);">
                <span title="${escapeHtml(mid)}" style="flex:1 1 auto; min-width:0; font-family: var(--ob-font-body); font-size: var(--ob-text-xs); color: var(--ob-text-secondary); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(mid)}</span>
                <select class="ob-provider-input" style="flex:0 0 auto; padding: var(--ob-space-1) var(--ob-space-2);"
                    data-modality-server-id="${escapeHtml(sv.id)}" data-modality-model="${escapeHtml(mid)}" ${dis}>
                    ${opts}
                </select>
            </div>`;
    }).join("");

    return `
        <div class="ob-custom-modalities" style="margin: 0 0 var(--ob-space-3);">
            <div class="ob-field-label" style="${CUSTOM_LABEL_GAP_STYLE}">Detected models</div>
            <p class="ob-provider-desc" style="margin: var(--ob-space-1) 0 var(--ob-space-2);">${summary}</p>
            <div style="display:grid; gap: var(--ob-space-2);">
                ${rows}
            </div>
        </div>`;
}

// Compact "Audio" confirm block — rendered only for a validated server whose
// probe reported STT and/or TTS capability. Those models live behind
// /v1/audio/* + /v1/realtime and are NOT in /v1/models, so their ids can't be
// auto-discovered — the customer CONFIRMS / EDITS them here (prefilled with the
// Speaches defaults) so the BlackBox can route audio. Returns "" unless the
// server is validated and the probe found audio. Mirrors the modalities block.
function renderCustomAudioBlock(sv, disabled) {
    if (!sv.validated_at || !sv.audio || !(sv.audio.stt || sv.audio.tts)) return "";
    const a = sv.audio;
    const sid = escapeHtml(sv.id);
    const dis = disabled ? "disabled" : "";

    // One-line capability summary (the "Audio:" lead echoes the header, like the
    // modalities block's "Detected:" line echoes its "Detected models" header).
    const parts = [];
    if (a.stt) {
        parts.push(`speech-to-text &check; ${a.streaming ? "(realtime + files)" : "(files only)"}`);
    }
    if (a.tts) {
        parts.push(`text-to-speech &check; (~50 voices)`);
    }
    const summary = `Audio: ${parts.join(" &middot; ")}`;

    const sttField = a.stt ? `
                <label class="ob-field-label" for="ob-custom-stt-${sid}" style="${CUSTOM_LABEL_GAP_STYLE}">STT model id</label>
                <input type="text" class="ob-provider-input" id="ob-custom-stt-${sid}"
                    data-audio-server-id="${sid}" data-audio-field="stt_model"
                    value="${escapeHtml(a.stt_model || SPEACHES_STT_DEFAULT)}"
                    autocomplete="off" autocapitalize="off" spellcheck="false" ${dis} />` : "";
    const ttsField = a.tts ? `
                <label class="ob-field-label" for="ob-custom-tts-${sid}" style="${CUSTOM_LABEL_GAP_STYLE}">TTS model id</label>
                <input type="text" class="ob-provider-input" id="ob-custom-tts-${sid}"
                    data-audio-server-id="${sid}" data-audio-field="tts_model"
                    value="${escapeHtml(a.tts_model || SPEACHES_TTS_DEFAULT)}"
                    autocomplete="off" autocapitalize="off" spellcheck="false" ${dis} />` : "";

    return `
        <div class="ob-custom-audio" style="margin: 0 0 var(--ob-space-3);">
            <div class="ob-field-label" style="${CUSTOM_LABEL_GAP_STYLE}">Audio</div>
            <p class="ob-provider-desc" style="margin: var(--ob-space-1) 0 var(--ob-space-2);">${summary}</p>
            <div style="display:grid; gap: var(--ob-space-1);">
                ${sttField}
                ${ttsField}
            </div>
        </div>`;
}

function renderCustomViewCard(row) {
    const sv = row.server;
    const sid = escapeHtml(sv.id);
    const keyPreview = sv.key_present ? "••••" + (sv.key_last4 || "") : "no key";
    // Numbers only in the detail line — coerce, never interpolate raw strings.
    const ctxNum = parseInt(sv.context_tokens, 10);
    const ctxText = Number.isFinite(ctxNum) && ctxNum > 0
        ? ` &middot; ${ctxNum.toLocaleString()} tok`
        : "";
    const pill = sv.validated_at
        ? `<span class="ob-status-pill ob-status-pill-ok"><span class="ob-status-pill-glyph" aria-hidden="true">&check;</span> Validated</span>`
        : `<span class="ob-status-pill">Not validated</span>`;
    const dis = row.busy ? "disabled" : "";
    const removeControls = row.confirmingRemove
        ? `<span style="${CUSTOM_BTN_GROUP_STYLE} font-family: var(--ob-font-body); font-size: var(--ob-text-xs); color: var(--ob-text-secondary); text-transform: uppercase; letter-spacing: 0.08em;">
                Remove?
                <button type="button" class="ob-replace-btn" data-action="remove-confirm" data-server-id="${sid}" ${dis} aria-label="Confirm remove ${escapeHtml(sv.alias)}">&check;</button>
                <button type="button" class="ob-replace-btn" data-action="remove-cancel" data-server-id="${sid}" ${dis} aria-label="Keep ${escapeHtml(sv.alias)}">&#10005;</button>
            </span>`
        : `<button type="button" class="ob-row-remove" data-action="remove" data-server-id="${sid}" ${dis} aria-label="Remove server ${escapeHtml(sv.alias)}">&times;</button>`;
    return `
        <div class="ob-provider-card ob-custom-server-card" data-server-id="${sid}">
            <div class="ob-provider-header">
                <div class="ob-provider-label">${escapeHtml(sv.alias)}</div>
                ${pill}
            </div>
            <p class="ob-provider-desc">${escapeHtml(sv.base_url)} &middot; ${escapeHtml(keyPreview)}${ctxText}</p>
            ${renderCustomModalitiesBlock(sv, row.busy)}
            ${renderCustomAudioBlock(sv, row.busy)}
            <div class="ob-provider-configured-row">
                <span style="${CUSTOM_BTN_GROUP_STYLE}">
                    <button type="button" class="ob-validate-btn" data-action="revalidate" data-server-id="${sid}" ${dis}>Re-validate</button>
                    <button type="button" class="ob-replace-btn" data-action="edit" data-server-id="${sid}" ${dis}>Edit</button>
                </span>
                ${removeControls}
            </div>
            <div class="ob-provider-status" data-status="${escapeHtml(row.statusKind || "idle")}">${row.statusHtml || ""}</div>
        </div>
    `;
}

// Edit mode — alias/base_url prefilled; api_key blank means "unchanged"
// (PATCH only includes fields the user actually changed/filled).
function renderCustomEditCard(row) {
    const sv = row.server;
    const sid = escapeHtml(sv.id);
    const dis = row.busy ? "disabled" : "";
    const keyLabel = sv.key_present
        ? "API key &mdash; leave blank to keep the current key"
        : "API key &mdash; optional (server currently keyless)";
    return `
        <div class="ob-provider-card ob-custom-server-card" data-server-id="${sid}">
            <div class="ob-provider-header">
                <div class="ob-provider-label">${escapeHtml(sv.alias)}</div>
                <span class="ob-status-pill">Editing</span>
            </div>
            <div style="${CUSTOM_FIELDS_STYLE}">
                <label class="ob-field-label" for="ob-custom-alias-${sid}">Alias</label>
                <input type="text" class="ob-provider-input" id="ob-custom-alias-${sid}"
                    data-field="alias" data-server-id="${sid}"
                    value="${escapeHtml(row.edit.alias)}" maxlength="64"
                    autocomplete="off" autocapitalize="off" spellcheck="false" ${dis} />
                <label class="ob-field-label" for="ob-custom-url-${sid}" style="${CUSTOM_LABEL_GAP_STYLE}">Base URL</label>
                <input type="text" class="ob-provider-input" id="ob-custom-url-${sid}"
                    data-field="base_url" data-server-id="${sid}"
                    value="${escapeHtml(row.edit.base_url)}"
                    autocomplete="off" autocapitalize="off" spellcheck="false" ${dis} />
                <label class="ob-field-label" for="ob-custom-key-${sid}" style="${CUSTOM_LABEL_GAP_STYLE}">${keyLabel}</label>
                <input type="password" class="ob-provider-input" id="ob-custom-key-${sid}"
                    data-field="api_key" data-server-id="${sid}"
                    value="${escapeHtml(row.edit.api_key)}" placeholder="${sv.key_present ? "unchanged" : "no key"}"
                    autocomplete="off" autocapitalize="off" spellcheck="false" ${dis} />
                <label class="ob-field-label" for="ob-custom-ctx-${sid}" style="${CUSTOM_LABEL_GAP_STYLE}">Context window (tokens) &mdash; blank to keep current</label>
                <input type="number" class="ob-provider-input" id="ob-custom-ctx-${sid}"
                    data-field="context_tokens" data-server-id="${sid}"
                    value="${escapeHtml(row.edit.context_tokens)}" placeholder="32768" min="1" step="1"
                    title="Server-wide window. Per-model limits are learned automatically when a model reports a smaller window."
                    autocomplete="off" ${dis} />
            </div>
            <div class="ob-provider-configured-row">
                <span style="${CUSTOM_BTN_GROUP_STYLE}">
                    <button type="button" class="ob-validate-btn" data-action="edit-save" data-server-id="${sid}" ${dis}>Save changes</button>
                    <button type="button" class="ob-replace-btn" data-action="edit-cancel" data-server-id="${sid}" ${dis}>Cancel</button>
                </span>
            </div>
            <div class="ob-provider-status" data-status="${escapeHtml(row.statusKind || "idle")}">${row.statusHtml || ""}</div>
        </div>
    `;
}

// Pending "+ Add server" row — POSTed on Validate & Add (POST first, then
// validate by server_id so validated_at/last_models persist server-side).
function renderCustomAddCard(row) {
    const rid = escapeHtml(String(row.id));
    const dis = row.busy ? "disabled" : "";
    const canSubmit = !row.busy && row.alias.trim().length > 0 && row.base_url.trim().length > 0;
    return `
        <div class="ob-provider-card ob-custom-server-card" data-add-id="${rid}">
            <div class="ob-provider-header">
                <div class="ob-provider-label">New server</div>
                <button type="button" class="ob-row-remove" data-action="add-discard" data-add-id="${rid}" ${dis} aria-label="Discard new server row">&times;</button>
            </div>
            <div style="${CUSTOM_FIELDS_STYLE}">
                <label class="ob-field-label" for="ob-custom-new-alias-${rid}">Alias</label>
                <input type="text" class="ob-provider-input" id="ob-custom-new-alias-${rid}"
                    data-field="alias" data-add-id="${rid}"
                    value="${escapeHtml(row.alias)}" placeholder="e.g. workstation-llama" maxlength="64"
                    autocomplete="off" autocapitalize="off" spellcheck="false" ${dis} />
                <label class="ob-field-label" for="ob-custom-new-url-${rid}" style="${CUSTOM_LABEL_GAP_STYLE}">Base URL</label>
                <input type="text" class="ob-provider-input" id="ob-custom-new-url-${rid}"
                    data-field="base_url" data-add-id="${rid}"
                    value="${escapeHtml(row.base_url)}" placeholder="http://192.168.1.50:8080/v1"
                    autocomplete="off" autocapitalize="off" spellcheck="false" ${dis} />
                <label class="ob-field-label" for="ob-custom-new-key-${rid}" style="${CUSTOM_LABEL_GAP_STYLE}">API key &mdash; optional (leave blank for keyless servers)</label>
                <input type="password" class="ob-provider-input" id="ob-custom-new-key-${rid}"
                    data-field="api_key" data-add-id="${rid}"
                    value="${escapeHtml(row.api_key)}" placeholder="sk-&hellip;"
                    autocomplete="off" autocapitalize="off" spellcheck="false" ${dis} />
                <label class="ob-field-label" for="ob-custom-new-ctx-${rid}" style="${CUSTOM_LABEL_GAP_STYLE}">Context window (tokens) &mdash; optional (blank = 32768 default)</label>
                <input type="number" class="ob-provider-input" id="ob-custom-new-ctx-${rid}"
                    data-field="context_tokens" data-add-id="${rid}"
                    value="${escapeHtml(row.context_tokens)}" placeholder="32768" min="1" step="1"
                    title="Server-wide window. Per-model limits are learned automatically when a model reports a smaller window."
                    autocomplete="off" ${dis} />
            </div>
            <div class="ob-provider-configured-row">
                <button type="button" class="ob-validate-btn" data-action="add-validate" data-add-id="${rid}" ${canSubmit ? "" : "disabled"}>Validate &amp; Add</button>
            </div>
            <div class="ob-provider-status" data-status="${escapeHtml(row.statusKind || "idle")}">${row.statusHtml || ""}</div>
        </div>
    `;
}

function wireCustomRows(container, state) {
    const rowsEl = container.querySelector("#ob-custom-rows");
    if (!rowsEl) return;

    // Field inputs → mutate state only (no rerender, so typing keeps focus).
    rowsEl.querySelectorAll(".ob-provider-input[data-field]").forEach(input => {
        input.addEventListener("input", () => {
            const field = input.dataset.field;
            if (input.dataset.addId != null) {
                const row = customState.adding.find(r => String(r.id) === input.dataset.addId);
                if (!row) return;
                row[field] = input.value;
                // Toggle this card's Validate & Add without a rerender.
                const card = input.closest("[data-add-id]");
                const btn = card && card.querySelector('[data-action="add-validate"]');
                if (btn) btn.disabled = row.busy || !(row.alias.trim() && row.base_url.trim());
            } else if (input.dataset.serverId) {
                const row = customState.servers.find(r => r.server.id === input.dataset.serverId);
                if (row && row.edit) row.edit[field] = input.value;
            }
        });
    });

    // Action buttons (view + edit + add cards).
    rowsEl.querySelectorAll("[data-action]").forEach(btn => {
        btn.addEventListener("click", () => {
            const action = btn.dataset.action;
            const srow = btn.dataset.serverId
                ? customState.servers.find(r => r.server.id === btn.dataset.serverId)
                : null;
            const arow = btn.dataset.addId != null
                ? customState.adding.find(r => String(r.id) === btn.dataset.addId)
                : null;
            switch (action) {
                case "revalidate":
                    if (srow) revalidateCustomServer(srow, container, state);
                    break;
                case "edit":
                    if (srow) startCustomEdit(srow, container, state);
                    break;
                case "edit-save":
                    if (srow) saveCustomEdit(srow, container, state);
                    break;
                case "edit-cancel":
                    if (srow && !srow.busy) {
                        srow.mode = "view";
                        srow.edit = null;
                        setCustomStatus(srow, "idle", "");
                        rerenderCustom(container, state);
                    }
                    break;
                case "remove":
                    if (srow && !srow.busy) {
                        srow.confirmingRemove = true;
                        rerenderCustom(container, state);
                    }
                    break;
                case "remove-cancel":
                    if (srow && !srow.busy) {
                        srow.confirmingRemove = false;
                        rerenderCustom(container, state);
                    }
                    break;
                case "remove-confirm":
                    if (srow) removeCustomServer(srow, container, state);
                    break;
                case "add-discard":
                    if (arow && !arow.busy) {
                        customState.adding = customState.adding.filter(r => r !== arow);
                        rerenderCustom(container, state);
                    }
                    break;
                case "add-validate":
                    if (arow) validateAndAddCustomServer(arow, container, state);
                    break;
            }
        });
    });

    // Detected-model modality selects → PATCH the full modality map on change.
    rowsEl.querySelectorAll("select[data-modality-server-id]").forEach(sel => {
        sel.addEventListener("change", () => {
            const srow = customState.servers.find(r => r.server.id === sel.dataset.modalityServerId);
            if (!srow) return;
            updateCustomModality(srow, sel.dataset.modalityModel, sel.value, container, state);
        });
    });

    // Audio model-id inputs → PATCH the audio config on change AND blur, so
    // merely leaving a field confirms its prefilled Speaches default. The
    // shared row.busy guard collapses the change+blur double-fire into one PATCH.
    rowsEl.querySelectorAll("input[data-audio-server-id]").forEach(input => {
        const commit = () => {
            const srow = customState.servers.find(r => r.server.id === input.dataset.audioServerId);
            if (!srow) return;
            updateCustomAudio(srow, input.dataset.audioField, input.value.trim(), container, state);
        };
        input.addEventListener("change", commit);
        input.addEventListener("blur", commit);
    });
}

function addCustomRow(container, state) {
    const id = nextCustomAddId++;
    customState.adding.push({
        id, alias: "", base_url: "", api_key: "", context_tokens: "",
        busy: false, statusKind: "idle", statusHtml: "",
    });
    rerenderCustom(container, state);
    const input = container.querySelector(`.ob-provider-input[data-field="alias"][data-add-id="${id}"]`);
    if (input) input.focus();
}

function setCustomStatus(row, kind, html) {
    row.statusKind = kind;
    row.statusHtml = html;
}

function customValidatingPill(text) {
    return `<span class="ob-status-pill ob-status-pill-validating">${escapeHtml(text)}&hellip;</span>`;
}

function customErrorPill(msg) {
    return `<span class="ob-status-pill ob-status-pill-error"><span class="ob-status-pill-glyph" aria-hidden="true">!</span> ${escapeHtml(msg)}</span>`;
}

// Success detail, e.g. "3 models: gemma-26b, gemma-12b, gemma-31b" from the
// ValidationResult's detail:{model_count, models}. NOTE: formatDetail above
// only serves the PROVIDERS card paths — this section renders its own.
function formatCustomDetail(detail) {
    if (!detail) return "";
    const models = Array.isArray(detail.models) ? detail.models : [];
    const count = typeof detail.model_count === "number" ? detail.model_count : models.length;
    if (!count) return "";
    let text = `${count} model${count === 1 ? "" : "s"}`;
    const shown = models.slice(0, 3).map(m => String(m));
    if (shown.length) {
        text += `: ${shown.join(", ")}${count > shown.length ? ", …" : ""}`;
    }
    return text;
}

// Apply a ValidationResult {ok, latency_ms, error, detail} to a configured
// row. On ok the backend has already stamped validated_at/last_models
// (server_id path) — mirror locally so the pill flips without a re-fetch.
function applyCustomValidateResult(row, result) {
    if (result && result.ok) {
        row.server.validated_at = new Date().toISOString();
        if (result.detail && Array.isArray(result.detail.models)) {
            row.server.last_models = result.detail.models;
        }
        // Seed / refresh the detected per-model modality map so the "Detected
        // models" block renders (and can be corrected) without a re-fetch.
        // Prefer the fresh seed; otherwise keep whatever was persisted before.
        // Merge the fresh name-pattern seed UNDER any existing (wizard-confirmed)
        // map so a user's correction survives re-validate — matches the backend
        // seed-merge: the persisted map wins, and new models get their seed.
        row.server.model_modalities = Object.assign(
            {}, (result.detail && result.detail.model_modalities) || {},
            row.server.model_modalities || {});
        // Merge the audio-capability probe: the fresh bools (stt/tts/streaming)
        // win, but any confirmed model ids are preserved (the probe carries
        // none). Then seed the ids with the Speaches defaults so the confirm
        // inputs render with a working value the customer can accept as-is.
        const pa = (result.detail && result.detail.audio) || {};
        const ea = row.server.audio || {};
        row.server.audio = Object.assign({}, ea, pa);
        if (row.server.audio.stt && !row.server.audio.stt_model) {
            row.server.audio.stt_model = SPEACHES_STT_DEFAULT;
        }
        if (row.server.audio.tts && !row.server.audio.tts_model) {
            row.server.audio.tts_model = SPEACHES_TTS_DEFAULT;
        }
        const detailText = formatCustomDetail(result.detail);
        setCustomStatus(row, "ok", `
            <span class="ob-status-pill ob-status-pill-ok">
                <span class="ob-status-pill-glyph" aria-hidden="true">&check;</span>
                Validated &middot; ${Number(result.latency_ms) || 0}ms${detailText ? ` &middot; ${escapeHtml(detailText)}` : ""}
            </span>`);
    } else {
        // Error strings are already user-actionable ("Server unreachable at
        // …", "API key rejected (401) …") — strip the exception-class prefix
        // like the provider cards do. A validate on a server deleted in
        // another tab comes back as a plain 400 {"detail": "..."} with no
        // error field, so fall through to detail before the generic message.
        const raw = (result && (result.error || result.detail)) || "validation failed";
        const errMsg = String(raw).replace(/^\w+Error:\s*/, "");
        setCustomStatus(row, "error", customErrorPill(errMsg.slice(0, 160)));
    }
}

async function revalidateCustomServer(row, container, state) {
    if (row.busy) return;
    row.busy = true;
    setCustomStatus(row, "validating", customValidatingPill("Validating"));
    rerenderCustom(container, state);
    try {
        const r = await fetch("/onboarding/validate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ provider: "custom", credentials: { server_id: row.server.id } }),
        });
        const result = await r.json();
        applyCustomValidateResult(row, result);
    } catch (e) {
        setCustomStatus(row, "error", customErrorPill(`Network error: ${String(e.message || e).slice(0, 120)}`));
    } finally {
        row.busy = false;
        rerenderCustom(container, state);
    }
}

function startCustomEdit(row, container, state) {
    if (row.busy) return;
    row.mode = "edit";
    row.confirmingRemove = false;
    row.edit = {
        alias: row.server.alias,
        base_url: row.server.base_url,
        api_key: "",
        // Prefill so the current window is visible; blank means "unchanged".
        context_tokens: row.server.context_tokens != null ? String(row.server.context_tokens) : "",
    };
    setCustomStatus(row, "idle", "");
    rerenderCustom(container, state);
    const input = container.querySelector(`.ob-provider-input[data-field="alias"][data-server-id="${row.server.id}"]`);
    if (input) input.focus();
}

// PATCH only what changed: blank api_key = "unchanged" (the backend treats
// an EXPLICIT empty api_key as "clear the key", so we must omit it).
async function saveCustomEdit(row, container, state) {
    if (row.busy) return;
    const sv = row.server;
    const alias = (row.edit.alias || "").trim();
    const baseUrl = (row.edit.base_url || "").trim();
    const apiKey = (row.edit.api_key || "").trim();
    // Context window: numbers only (parseInt + NaN reject with the field's
    // own error pill); blank = unchanged.
    const ctxRaw = String(row.edit.context_tokens == null ? "" : row.edit.context_tokens).trim();
    let ctxTokens = null;
    if (ctxRaw) {
        ctxTokens = parseInt(ctxRaw, 10);
        if (!Number.isFinite(ctxTokens) || ctxTokens <= 0) {
            setCustomStatus(row, "error", customErrorPill("Context window must be a positive whole number of tokens"));
            rerenderCustom(container, state);
            return;
        }
    }
    const body = {};
    if (alias && alias !== sv.alias) body.alias = alias;
    if (baseUrl && baseUrl !== sv.base_url) body.base_url = baseUrl;
    if (apiKey) body.api_key = apiKey;
    if (ctxTokens !== null && ctxTokens !== Number(sv.context_tokens)) body.context_tokens = ctxTokens;

    if (Object.keys(body).length === 0) {
        // Nothing changed — just close the editor.
        row.mode = "view";
        row.edit = null;
        setCustomStatus(row, "idle", "");
        rerenderCustom(container, state);
        return;
    }

    row.busy = true;
    setCustomStatus(row, "validating", customValidatingPill("Saving"));
    rerenderCustom(container, state);
    try {
        const r = await fetch(`/onboarding/custom-servers/${encodeURIComponent(sv.id)}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await r.json().catch(() => ({}));
        if (r.ok && data.server) {
            row.server = data.server;
            row.mode = "view";
            row.edit = null;
            // PATCHing base_url clears validated_at server-side (deliberate)
            // — the pill drops to "Not validated"; nudge a re-validate.
            if (!data.server.validated_at) {
                setCustomStatus(row, "idle", `<span class="ob-status-pill">Saved &middot; re-validate to confirm the server responds</span>`);
            } else {
                setCustomStatus(row, "idle", "");
            }
        } else {
            // Stay in edit mode so the user can fix the field (e.g. duplicate alias).
            const detail = (data && (data.detail || data.error)) || `HTTP ${r.status}`;
            setCustomStatus(row, "error", customErrorPill(String(detail).slice(0, 160)));
        }
    } catch (e) {
        setCustomStatus(row, "error", customErrorPill(`Network error: ${String(e.message || e).slice(0, 120)}`));
    } finally {
        row.busy = false;
        rerenderCustom(container, state);
    }
}

// Persist a corrected model modality. Optimistically applies the choice so the
// <select> holds it through the "Saving" rerender, PATCHes the FULL merged map
// (the backend replaces model_modalities wholesale), and reverts on failure.
// Mirrors saveCustomEdit's PATCH/error idiom; guarded by the shared row.busy.
async function updateCustomModality(row, modelId, value, container, state) {
    if (row.busy) return;
    const sv = row.server;
    const prev = sv.model_modalities || {};
    const updated = Object.assign({}, prev, { [String(modelId)]: value });

    sv.model_modalities = updated;  // optimistic — survive the rerender below
    row.busy = true;
    setCustomStatus(row, "validating", customValidatingPill("Saving"));
    rerenderCustom(container, state);
    try {
        const r = await fetch(`/onboarding/custom-servers/${encodeURIComponent(sv.id)}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ model_modalities: updated }),
        });
        const data = await r.json().catch(() => ({}));
        if (r.ok) {
            // Prefer the server's echoed map; fall back to the optimistic one.
            // Only touch model_modalities so a trimmed PATCH response can't drop
            // last_models / validated_at and make the block vanish.
            row.server.model_modalities = (data.server && data.server.model_modalities) || updated;
            setCustomStatus(row, "idle", "");
        } else {
            row.server.model_modalities = prev;  // persist rejected — revert
            const detail = (data && (data.detail || data.error)) || `HTTP ${r.status}`;
            setCustomStatus(row, "error", customErrorPill(String(detail).slice(0, 160)));
        }
    } catch (e) {
        row.server.model_modalities = prev;  // network error — revert
        setCustomStatus(row, "error", customErrorPill(`Network error: ${String(e.message || e).slice(0, 120)}`));
    } finally {
        row.busy = false;
        rerenderCustom(container, state);
    }
}

// Persist a confirmed / edited audio model id. Mirrors updateCustomModality:
// optimistically applies the value so the input holds it through the "Saving"
// rerender, PATCHes the FULL merged audio object (backend replaces `audio` with
// what we send — the capability bools ride along), and reverts on failure.
// Guarded by the shared row.busy; wired to both change and blur.
async function updateCustomAudio(row, field, value, container, state) {
    const sv = row.server;
    if (row.busy) {
        // A PATCH is already in flight. Merge the latest value(s) into a pending
        // batch and apply optimistically; the in-flight call flushes them on
        // completion, so a fast cross-field edit (STT then TTS) isn't silently
        // dropped by the busy guard.
        row._audioPending = Object.assign({}, row._audioPending, { [field]: value });
        sv.audio = Object.assign({}, sv.audio || {}, { [field]: value });
        return;
    }
    const prev = sv.audio || {};
    const updated = Object.assign({}, prev, { [field]: value });

    sv.audio = updated;  // optimistic — survive the rerender below
    row.busy = true;
    setCustomStatus(row, "validating", customValidatingPill("Saving"));
    rerenderCustom(container, state);
    try {
        const r = await fetch(`/onboarding/custom-servers/${encodeURIComponent(sv.id)}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ audio: updated }),
        });
        const data = await r.json().catch(() => ({}));
        if (r.ok) {
            // Prefer the server's echoed audio; fall back to the optimistic one.
            // Only touch `audio` so a trimmed PATCH response can't drop
            // last_models / validated_at and make sibling blocks vanish.
            row.server.audio = (data.server && data.server.audio) || updated;
            setCustomStatus(row, "idle", "");
        } else {
            row.server.audio = prev;  // persist rejected — revert
            const detail = (data && (data.detail || data.error)) || `HTTP ${r.status}`;
            setCustomStatus(row, "error", customErrorPill(String(detail).slice(0, 160)));
        }
    } catch (e) {
        row.server.audio = prev;  // network error — revert
        setCustomStatus(row, "error", customErrorPill(`Network error: ${String(e.message || e).slice(0, 120)}`));
    } finally {
        row.busy = false;
        const pending = row._audioPending;
        row._audioPending = null;
        rerenderCustom(container, state);
        if (pending) {
            // Flush edit(s) made while this PATCH was in flight as one follow-up
            // PATCH (sv.audio already holds the merged pending values, even if a
            // successful echo above overwrote them).
            Object.assign(sv.audio, pending);
            const [f, v] = Object.entries(pending)[0];
            updateCustomAudio(row, f, v, container, state);
        }
    }
}

async function removeCustomServer(row, container, state) {
    if (row.busy) return;
    row.busy = true;
    setCustomStatus(row, "validating", customValidatingPill("Removing"));
    rerenderCustom(container, state);
    try {
        const r = await fetch(`/onboarding/custom-servers/${encodeURIComponent(row.server.id)}`, {
            method: "DELETE",
        });
        // Idempotent delete: 404 means the server was already removed (e.g.
        // in another tab) — the row must not stick around as a ghost.
        if (r.ok || r.status === 404) {
            customState.servers = customState.servers.filter(x => x !== row);
        } else {
            const data = await r.json().catch(() => ({}));
            const detail = (data && (data.detail || data.error)) || `HTTP ${r.status}`;
            row.confirmingRemove = false;
            setCustomStatus(row, "error", customErrorPill(`Couldn't remove: ${String(detail).slice(0, 120)}`));
        }
    } catch (e) {
        row.confirmingRemove = false;
        setCustomStatus(row, "error", customErrorPill(`Network error: ${String(e.message || e).slice(0, 120)}`));
    } finally {
        row.busy = false;
        rerenderCustom(container, state);
    }
}

// Validate & Add: POST the server FIRST (persist), then validate by
// server_id so validated_at/last_models stamp server-side. On validation
// failure the row stays configured-but-error-pilled with Remove available —
// the server may simply be powered off right now.
async function validateAndAddCustomServer(row, container, state) {
    if (row.busy) return;
    const alias = row.alias.trim();
    const baseUrl = row.base_url.trim();
    const apiKey = (row.api_key || "").trim();
    if (!alias || !baseUrl) return;

    // Context window: numbers only (parseInt + NaN reject with the field's
    // own error pill); blank = server default (32768).
    const ctxRaw = String(row.context_tokens == null ? "" : row.context_tokens).trim();
    let ctxTokens = null;
    if (ctxRaw) {
        ctxTokens = parseInt(ctxRaw, 10);
        if (!Number.isFinite(ctxTokens) || ctxTokens <= 0) {
            setCustomStatus(row, "error", customErrorPill("Context window must be a positive whole number of tokens"));
            rerenderCustom(container, state);
            return;
        }
    }

    row.busy = true;
    setCustomStatus(row, "validating", customValidatingPill("Adding"));
    rerenderCustom(container, state);

    // Step 1: persist the server.
    let server = null;
    try {
        const body = { alias, base_url: baseUrl };
        if (apiKey) body.api_key = apiKey;  // keyless servers: omit entirely
        if (ctxTokens !== null) body.context_tokens = ctxTokens;  // blank: backend default
        const r = await fetch("/onboarding/custom-servers", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok || !data.server) {
            const detail = (data && (data.detail || data.error)) || `HTTP ${r.status}`;
            row.busy = false;
            setCustomStatus(row, "error", customErrorPill(String(detail).slice(0, 160)));
            rerenderCustom(container, state);
            return;
        }
        server = data.server;
    } catch (e) {
        row.busy = false;
        setCustomStatus(row, "error", customErrorPill(`Network error: ${String(e.message || e).slice(0, 120)}`));
        rerenderCustom(container, state);
        return;
    }

    // Step 2: server persisted — swap the add-row for a configured row.
    customState.adding = customState.adding.filter(r => r !== row);
    const srow = {
        server,
        mode: "view",
        confirmingRemove: false,
        busy: true,
        statusKind: "validating",
        statusHtml: customValidatingPill("Validating"),
        edit: null,
    };
    customState.servers.push(srow);
    rerenderCustom(container, state);

    // Step 3: validate the stored server.
    try {
        const r = await fetch("/onboarding/validate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ provider: "custom", credentials: { server_id: server.id } }),
        });
        const result = await r.json();
        applyCustomValidateResult(srow, result);
    } catch (e) {
        setCustomStatus(srow, "error", customErrorPill(`Network error: ${String(e.message || e).slice(0, 120)}`));
    } finally {
        srow.busy = false;
        rerenderCustom(container, state);
    }
}

// Save button is enabled when there is something we can advance with:
//   - a newly validated key (status === "ok"), OR
//   - a pre-existing key that the customer chose to keep (wasPresent && !replacing), OR
//   - a configured custom model server (counts as "retained existing" —
//     custom servers persist immediately via their own endpoints, so a
//     custom-only box can advance and /onboarding/save has nothing to POST).
// Label flips between "Save & continue" (something to POST) and "Continue"
// (everything is already configured and untouched, so save is a no-op).
function updateSaveButton(container, state) {
    const saveBtn = container.querySelector("#ob-keys-save");
    if (!saveBtn) return;

    const anyNewlyValidated = PROVIDERS.some(p => state[p.id].status === "ok");
    const anyRetainedExisting = PROVIDERS.some(
        p => state[p.id].wasPresent && !state[p.id].replacing
    );
    const anyCustomConfigured = customState.servers.length > 0;

    saveBtn.disabled = !(anyNewlyValidated || anyRetainedExisting || anyCustomConfigured);

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
