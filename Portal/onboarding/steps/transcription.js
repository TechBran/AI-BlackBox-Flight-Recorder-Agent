// Transcription (speech-to-text) step — fifth screen of the onboarding wizard.
// The BlackBox can transcribe voice with one of two providers:
//
//   - OpenAI: gpt-realtime-whisper streaming + gpt-4o-transcribe for files.
//     Uses the OpenAI API key entered in the Keys step.
//   - Google: Cloud Speech-to-Text v2 (chirp_2) streaming + files. Uses a
//     Google service-account JSON credential.
//
// STT_PROVIDER is a *preference*, not a secret. An empty value means "auto"
// (the backend picks whichever provider is configured). This step lets the
// user pin a provider explicitly.
//
// This step:
//   1. GET /stt/catalog            — per-provider {available, blurb, models}
//      GET /onboarding/current-config — stt.provider ("" == auto)
//   2. Render two radio-style provider cards (only one selected). Each shows
//      label, blurb, streaming + file model names, and a Ready / Needs setup
//      badge. The currently-selected provider is pre-checked; "" shows an
//      "Auto" note and nothing explicitly checked.
//   3. Selecting a card POSTs /onboarding/save {secrets:{STT_PROVIDER:id}}
//      (mirrors how api_keys persists), marks it selected, enables Continue.
//   4. Unavailable providers stay informational — the card points the user at
//      what's still needed (OpenAI key, or a Google service-account JSON).
//   5. Continue is allowed once a provider is chosen, OR if at least one
//      provider is available (auto is a valid resolution). Skip + Back always
//      available.
//
// Reuses the .ob-cli-agent-* card/badge/grid CSS (same selectable-card shape
// as the CLI Agents step) plus a thin inline "selected" highlight, since the
// onboarding stylesheet has no dedicated radio-card modifier yet.

const PROVIDERS = [
    {
        id: "openai",
        vendor: "OpenAI",
        needsHint: "Add your OpenAI API key in the Keys step.",
    },
    {
        id: "google",
        vendor: "Google Cloud",
        needsHint: "Upload a Google service-account JSON (Keys / Extras).",
    },
    {
        id: "elevenlabs",
        vendor: "ElevenLabs",
        needsHint: "Add your ElevenLabs API key in the Keys / Extras step.",
    },
    {
        id: "local",
        vendor: "Custom server",
        needsHint: "Register a custom model server hosting a speech-to-text model (e.g. whisper) in the Keys step.",
    },
];

// Inline highlight for the selected card. The shared stylesheet only ships
// per-data-state border colors (ready/needs-auth/missing) — none of which mean
// "user picked this", so we layer a thin accent ring inline.
// Selected-card highlight is the shared .ob-card-selected class (onboarding.css).

let catalog = null;       // {providers, resolved, default}
let selected = "";        // currently-chosen provider id ("" == auto)
let saving = false;       // prevents save double-fire

export async function render(container, { next, back, skip, sigil }) {
    container.innerHTML = `
        <section class="ob-step ob-transcription">
            <aside class="ob-step-sigil" aria-hidden="true">
                <div class="ob-step-sigil-num"><em>${sigil ? sigil.num : "05"}</em></div>
                <div class="ob-step-sigil-rule"></div>
                <div class="ob-step-sigil-label">SPEECH</div>
            </aside>
            <div class="ob-step-body">
                <div class="ob-step-eyebrow">
                    <span class="ob-step-eyebrow-dot" aria-hidden="true"></span>
                    Speech-to-text provider
                </div>
                <h1 class="ob-step-title">
                    Choose how the BlackBox <em>hears you</em>.
                </h1>
                <p class="ob-step-lede">
                    Voice features transcribe what you say with one of two
                    providers. <strong>OpenAI</strong> uses
                    <code>gpt-realtime-whisper</code> for live streaming and
                    <code>gpt-4o-transcribe</code> for files, billed to your
                    OpenAI key. <strong>Google</strong> uses Cloud
                    Speech-to-Text (<code>chirp&#95;2</code>) via a
                    service-account JSON. Leave it on <strong>Auto</strong> and
                    the BlackBox uses whichever you've configured.
                </p>
                <div id="ob-stt-grid" class="ob-cli-agent-grid">
                    <div class="ob-loading">Loading transcription options&hellip;</div>
                </div>
                <p id="ob-stt-auto-note" class="ob-step-helper" hidden></p>
                <nav class="ob-step-nav" aria-label="Step navigation">
                    <button type="button" class="ob-back" id="ob-stt-back">
                        <span aria-hidden="true">&larr;</span> Back to ${sigil && sigil.backLabel ? sigil.backLabel.toLowerCase() : "extras"}
                    </button>
                    <button type="button" class="ob-cta" id="ob-stt-continue" disabled>
                        Continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                    </button>
                    <button type="button" class="ob-skip" id="ob-stt-skip">
                        Skip &mdash; set up later <span aria-hidden="true">&rarr;</span>
                    </button>
                </nav>
            </div>
        </section>
    `;

    document.getElementById("ob-stt-back").addEventListener("click", back);
    document.getElementById("ob-stt-skip").addEventListener("click", skip);
    document.getElementById("ob-stt-continue").addEventListener("click", next);

    // Fetch catalog + current selection in parallel. Fail-open: if either call
    // fails we still render the cards we know about (availability unknown ->
    // treated as not-ready, which is informational, not blocking).
    const [cat, cfg] = await Promise.all([
        fetchJson("/stt/catalog"),
        fetchJson("/onboarding/current-config"),
    ]);

    catalog = cat || { providers: [], resolved: "", default: "" };
    selected = ((cfg && cfg.stt && cfg.stt.provider) || "").trim().toLowerCase();

    renderGrid(container);
    updateContinue(container);
}

function renderGrid(container) {
    const grid = container.querySelector("#ob-stt-grid");
    const byId = {};
    (catalog.providers || []).forEach((p) => { byId[p.id] = p; });

    grid.innerHTML = PROVIDERS.map((p) => renderCard(byId[p.id], p)).join("");

    // Wire each card: clicking a card (or its choose button) selects it.
    PROVIDERS.forEach((p) => {
        const card = grid.querySelector(`.ob-cli-agent-card[data-provider="${p.id}"]`);
        if (card) {
            card.addEventListener("click", () => choose(container, p.id));
        }
    });

    // Auto note: shown only when nothing is explicitly pinned.
    const note = container.querySelector("#ob-stt-auto-note");
    if (note) {
        if (!selected) {
            const resolved = catalog.resolved
                ? ` Right now that resolves to <strong>${escapeHtml(catalog.resolved)}</strong>.`
                : "";
            note.innerHTML = `Auto &mdash; uses whichever provider is configured.${resolved}`;
            note.hidden = false;
        } else {
            note.hidden = true;
        }
    }
}

function renderCard(cat, meta) {
    // cat may be undefined if /stt/catalog failed — degrade gracefully.
    const id = meta.id;
    // Label + vendor come from the live catalog / per-provider meta — never a
    // hardcoded openai/google ternary, so a new provider (e.g. elevenlabs)
    // renders with its own name rather than being mislabeled.
    const label = (cat && cat.label) || meta.vendor || id;
    const vendor = meta.vendor || (cat && cat.label) || id;
    const blurb = (cat && cat.blurb) || "";
    const available = !!(cat && cat.available);
    const models = (cat && cat.models) || {};
    const isSelected = selected === id;

    const badge = available
        ? `<span class="ob-cli-agent-badge ob-cli-agent-badge-ok">&check; Ready</span>`
        : `<span class="ob-cli-agent-badge ob-cli-agent-badge-needs-auth">! Needs setup</span>`;

    const modelRows = `
        <div class="ob-cli-agent-meta-row">
            <span class="ob-cli-agent-meta-label">Streaming</span>
            <code class="ob-cli-agent-bin">${escapeHtml(models.streaming || "—")}</code>
        </div>
        <div class="ob-cli-agent-meta-row">
            <span class="ob-cli-agent-meta-label">Files</span>
            <code class="ob-cli-agent-bin">${escapeHtml(models.file || "—")}</code>
        </div>
    `;

    // Unavailable -> informational pointer at what's still needed. Available ->
    // a check/choose affordance. Whole card is clickable either way.
    const footer = available
        ? `<p class="ob-cli-agent-ready-blurb">${isSelected
              ? "Selected as your transcription provider."
              : "Click to use this provider."}</p>`
        : `<p class="ob-cli-agent-auth-blurb">${escapeHtml(meta.needsHint)}</p>`;

    const dataState = available ? "ready" : "needs-auth";
    const selectedClass = isSelected ? " ob-card-selected" : "";

    return `
        <div class="ob-cli-agent-card${selectedClass}" data-provider="${escapeHtml(id)}"
             data-state="${dataState}" role="radio"
             aria-checked="${isSelected ? "true" : "false"}" tabindex="0">
            <div class="ob-cli-agent-head">
                <div class="ob-cli-agent-title">
                    <span class="ob-cli-agent-name">${escapeHtml(label)}${isSelected ? " &check;" : ""}</span>
                    <span class="ob-cli-agent-vendor">${escapeHtml(vendor)}</span>
                </div>
                ${badge}
            </div>
            ${blurb ? `<p class="ob-cli-agent-auth-blurb">${escapeHtml(blurb)}</p>` : ""}
            ${modelRows}
            <div class="ob-cli-agent-actions">${footer}</div>
        </div>
    `;
}

async function choose(container, id) {
    if (saving) return;
    if (selected === id) return;  // already chosen — no-op
    saving = true;

    const prev = selected;
    try {
        const r = await fetch("/onboarding/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ secrets: { STT_PROVIDER: id } }),
        });
        if (!r.ok) throw new Error(`save returned ${r.status}`);
        selected = id;
        renderGrid(container);
        updateContinue(container);
    } catch (e) {
        selected = prev;  // roll back the visual selection
        showHint(container, `Couldn't save your choice: ${e.message}. Try again.`, true);
    } finally {
        saving = false;
    }
}

// Continue is allowed when the user has pinned a provider, OR when at least one
// provider is available (so "Auto" will resolve to something real).
function updateContinue(container) {
    const cont = container.querySelector("#ob-stt-continue");
    if (!cont) return;
    const anyAvailable = (catalog.providers || []).some((p) => p.available);
    cont.disabled = !(selected || anyAvailable);
}

function showHint(container, msg, isError) {
    let hint = container.querySelector("#ob-stt-hint");
    if (!hint) {
        hint = document.createElement("div");
        hint.id = "ob-stt-hint";
        hint.className = "ob-cli-agent-hint";
        const grid = container.querySelector("#ob-stt-grid");
        if (grid) grid.insertAdjacentElement("afterend", hint);
    }
    hint.classList.toggle("ob-cli-agent-hint-error", !!isError);
    hint.textContent = msg;
}

async function fetchJson(url) {
    try {
        const r = await fetch(url);
        if (!r.ok) return null;
        return await r.json();
    } catch (_) {
        return null;
    }
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
