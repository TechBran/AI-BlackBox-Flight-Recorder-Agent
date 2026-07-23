// Transcription (speech-to-text) step — fifth screen of the onboarding wizard.
// The BlackBox can transcribe voice with one of several providers:
//
//   - OpenAI: gpt-realtime-whisper streaming + gpt-4o-transcribe for files.
//     Uses the OpenAI API key entered in the Keys step.
//   - Google: Cloud Speech-to-Text v2 (chirp_2) streaming + files. Uses a
//     Google service-account JSON credential.
//   - ElevenLabs: Scribe v2 realtime + files. Uses the ElevenLabs API key.
//   - Custom server: a registered OpenAI-compatible LAN whisper server.
//   - On-box (local): faster-whisper via the on-box model stack — no cloud, no key.
//
// STT_PROVIDER is a *preference*, not a secret. An empty value means "auto"
// (the backend picks whichever provider is configured). This step lets the
// user pin a provider explicitly.
//
// This step:
//   1. GET /stt/catalog            — per-provider {available, blurb, models}
//      GET /onboarding/current-config — stt.provider ("" == auto)
//   2. Render one radio-style provider card per PROVIDERS entry (only one
//      selected). Each shows label, blurb, streaming + file model names, and a
//      Ready / Needs setup badge. The currently-selected provider is pre-checked;
//      "" shows an "Auto" note and nothing explicitly checked.
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
//
// ── VOICE (the other direction) ──────────────────────────────────────────
// Below the STT provider cards, this step also covers how the BlackBox
// *speaks*: the on-box Qwen3-TTS variant downloads (the SAME rows/machinery as
// the On-Box Models step — audioArtifacts/audioArtifactRowHtml/
// runArtifactDownload are imported from local_models.js, single source of
// truth, that step keeps its section too), plus the two voice-creation
// mini-flows (clone from a short recording, design from a text description)
// backed by POST /qwen/voices/{clone,design,design/save}.
// Fail-open: no /local-models/status (older backend / fetch error) → the whole
// voice section is hidden; stack off or unhealthy → a single-line honest note,
// no buttons and no errors.

import { audioArtifacts, audioArtifactRowHtml, runArtifactDownload } from "./local_models.js";

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
    {
        id: "onbox",
        vendor: "On-box (local)",
        needsHint: "Set up the on-box model stack in the On-Box Models step (whisper runs locally — no cloud STT).",
    },
];

// Inline highlight for the selected card. The shared stylesheet only ships
// per-data-state border colors (ready/needs-auth/missing) — none of which mean
// "user picked this", so we layer a thin accent ring inline.
// Selected-card highlight is the shared .ob-card-selected class (onboarding.css).

let catalog = null;       // {providers, resolved, default}
let selected = "";        // currently-chosen provider id ("" == auto)
let saving = false;       // prevents save double-fire
let lmStatus = null;      // last GET /local-models/status (null → hide voice UI)
let cloneBusy = false;    // clone POST in-flight guard
let designBusy = false;   // design POST in-flight guard
let designSelectedId = null;  // generated_voice_id picked among the previews

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
                    Voice features transcribe what you say with one of several
                    providers &mdash; cloud (<strong>OpenAI</strong>,
                    <strong>Google</strong>, <strong>ElevenLabs</strong>), a
                    <strong>custom</strong> LAN whisper server, or fully
                    <strong>on-box</strong> (faster-whisper via the on-box model
                    stack, no cloud and no key). Leave it on
                    <strong>Auto</strong> and the BlackBox uses whichever you've
                    configured.
                </p>
                <div id="ob-stt-grid" class="ob-cli-agent-grid">
                    <div class="ob-loading">Loading transcription options&hellip;</div>
                </div>
                <p id="ob-stt-auto-note" class="ob-step-helper" hidden></p>
                <div id="ob-stt-voice" class="ob-stt-voice"></div>
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

    // Fetch catalog + current selection + local-stack status in parallel.
    // Fail-open: if any call fails we still render the cards we know about
    // (availability unknown -> treated as not-ready, which is informational,
    // not blocking); a missing /local-models/status (older backend 404) just
    // hides the voice section.
    const [cat, cfg, lm] = await Promise.all([
        fetchJson("/stt/catalog"),
        fetchJson("/onboarding/current-config"),
        fetchJson("/local-models/status"),
    ]);

    catalog = cat || { providers: [], resolved: "", default: "" };
    selected = ((cfg && cfg.stt && cfg.stt.provider) || "").trim().toLowerCase();
    lmStatus = lm;

    renderGrid(container);
    updateContinue(container);
    renderVoiceSection(container);
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

// Exported for the render test only (transcription_voice.render.test.mjs pins
// the STT card markup so the voice-section addition can't regress it).
export function renderCard(cat, meta) {
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

// ═══════════════════════════════════════════════════════════════════════
// Voice — text-to-speech (on-box), clone, and design
// Pure html builders take an explicit `st` (a GET /local-models/status
// payload) so the render test can exercise them without a DOM, mirroring
// local_models.js's audioSectionHtml pattern.
// ═══════════════════════════════════════════════════════════════════════

// Qwen3-TTS artifact keys (localstack_downloads.MEMBER_ARTIFACTS["qwen-tts"]).
const QWEN_CUSTOM_VOICE = "qwen-tts-custom-voice";
const QWEN_VOICE_DESIGN = "qwen-tts-voice-design";

export function stackHealthy(st) {
    return !!(st && st.installed && st.healthy);
}

export function variantDownloaded(st, key) {
    const a = audioArtifacts("tts", st).find((x) => x && x.key === key);
    return !!(a && a.downloaded);
}

// Cloning runs on the Custom Voice checkpoint; design on the Voice Design one.
export function cloneReady(st) {
    return stackHealthy(st) && variantDownloaded(st, QWEN_CUSTOM_VOICE);
}
export function designReady(st) {
    return stackHealthy(st) && variantDownloaded(st, QWEN_VOICE_DESIGN);
}

// The whole voice section:
//   st == null            → "" (older backend without /local-models/status,
//                              or the fetch failed — hide cleanly, no errors)
//   stack off / unhealthy → one honest line, no buttons
//   healthy               → variant download rows (SHARED with the On-Box
//                           Models step) + the clone/design mini-flows.
export function voiceSectionHtml(st = lmStatus) {
    if (!st) return "";
    const artifacts = audioArtifacts("tts", st);
    const eyebrow = `<div class="ob-stt-voice-eyebrow">Text-to-speech (on-box)</div>`;
    if (!st.installed || !artifacts.length) {
        return `${eyebrow}
            <p class="ob-stt-voice-note">On-box voice runs on a box with the local model stack &mdash; this box speaks through your cloud voice providers instead.</p>`;
    }
    if (!st.healthy) {
        return `${eyebrow}
            <p class="ob-stt-voice-note">On-box voice runs on a box with the local model stack &mdash; the stack is installed here but not running right now.</p>`;
    }
    const rows = artifacts.map((a) => audioArtifactRowHtml(a)).join("");
    return `${eyebrow}
        <p class="ob-stt-voice-lede">Give the BlackBox a voice that never leaves the box. Download only the Qwen3-TTS variants you want &mdash; then clone or design a voice of your own below.</p>
        <div class="ob-lm-audio-rows ob-stt-voice-rows">${rows}</div>
        ${cloneBlockHtml(st)}
        ${designBlockHtml(st)}`;
}

export function cloneBlockHtml(st = lmStatus) {
    const title = `<div class="ob-stt-voice-block-title">Clone a voice</div>`;
    if (!cloneReady(st)) {
        return `<div class="ob-stt-voice-block" id="ob-stt-clone" data-ready="false">
            ${title}
            <p class="ob-stt-voice-note">Download the <strong>Custom Voice</strong> variant above to clone a voice from a short recording &mdash; on-box, no cloud.</p>
        </div>`;
    }
    // Consent starts unchecked → the clone button starts DISABLED; the change
    // handler in wireVoiceSection is the only thing that enables it.
    return `<div class="ob-stt-voice-block" id="ob-stt-clone" data-ready="true">
        ${title}
        <p class="ob-stt-voice-sub">Upload a clean speech recording &mdash; at least ~3 seconds &mdash; and name the voice.</p>
        <div class="ob-stt-voice-form">
            <input type="file" id="ob-stt-clone-file" class="ob-stt-voice-file" accept="audio/*">
            <input type="text" id="ob-stt-clone-name" class="ob-provider-input" placeholder="Voice name" maxlength="60">
            <label class="ob-stt-consent">
                <input type="checkbox" id="ob-stt-clone-consent">
                <span>I have this person&#39;s explicit consent to clone their voice (or it&#39;s my own).</span>
            </label>
            <button type="button" class="ob-lm-btn ob-lm-btn-activate" id="ob-stt-clone-btn" disabled>Clone voice</button>
        </div>
        <p class="ob-stt-voice-result" id="ob-stt-clone-result" hidden></p>
    </div>`;
}

export function designBlockHtml(st = lmStatus) {
    const title = `<div class="ob-stt-voice-block-title">Design a voice from text</div>`;
    if (!designReady(st)) {
        return `<div class="ob-stt-voice-block" id="ob-stt-design" data-ready="false">
            ${title}
            <p class="ob-stt-voice-note">Download the <strong>Voice Design</strong> variant above to create a voice from a written description &mdash; on-box, no cloud.</p>
        </div>`;
    }
    return `<div class="ob-stt-voice-block" id="ob-stt-design" data-ready="true">
        ${title}
        <p class="ob-stt-voice-sub">Describe the voice you want (&ldquo;a warm, low narrator with a slight rasp&rdquo;) and preview it before saving.</p>
        <div class="ob-stt-voice-form">
            <textarea id="ob-stt-design-desc" class="ob-provider-input ob-stt-voice-textarea" rows="2" placeholder="Describe the voice&hellip;"></textarea>
            <input type="text" id="ob-stt-design-text" class="ob-provider-input" placeholder="Optional: sample sentence for the preview">
            <button type="button" class="ob-lm-btn ob-lm-btn-activate" id="ob-stt-design-btn">Generate previews</button>
        </div>
        <div id="ob-stt-design-previews" class="ob-stt-design-previews"></div>
        <div id="ob-stt-design-save" class="ob-stt-voice-form" hidden>
            <input type="text" id="ob-stt-design-name" class="ob-provider-input" placeholder="Voice name" maxlength="60">
            <button type="button" class="ob-lm-btn ob-lm-btn-activate" id="ob-stt-design-save-btn">Save voice</button>
        </div>
        <p class="ob-stt-voice-result" id="ob-stt-design-result" hidden></p>
    </div>`;
}

function renderVoiceSection(container) {
    const host = container.querySelector("#ob-stt-voice");
    if (!host) return;
    designSelectedId = null;  // previews don't survive a re-render
    host.innerHTML = voiceSectionHtml(lmStatus);
    wireVoiceSection(container);
}

function wireVoiceSection(container) {
    const host = container.querySelector("#ob-stt-voice");
    if (!host) return;
    // Variant download buttons → the SAME shared NDJSON pathway as the On-Box
    // Models step (one live progress map across both steps).
    host.querySelectorAll(".ob-lm-audio-dl[data-dl]").forEach((btn) => {
        btn.addEventListener("click", () => startVoiceDownload(container, btn.getAttribute("data-dl")));
    });
    // Clone: consent checkbox is the sole gate on the button.
    const consent = host.querySelector("#ob-stt-clone-consent");
    const cloneBtn = host.querySelector("#ob-stt-clone-btn");
    if (consent && cloneBtn) {
        consent.addEventListener("change", () => { cloneBtn.disabled = !consent.checked || cloneBusy; });
        cloneBtn.addEventListener("click", () => submitClone(container));
    }
    const designBtn = host.querySelector("#ob-stt-design-btn");
    if (designBtn) designBtn.addEventListener("click", () => submitDesign(container));
    const designSaveBtn = host.querySelector("#ob-stt-design-save-btn");
    if (designSaveBtn) designSaveBtn.addEventListener("click", () => submitDesignSave(container));
}

async function startVoiceDownload(container, key) {
    await runArtifactDownload(key, {
        onProgress: (dl) => updateVoiceDownloadBar(container, key, dl),
        onError: (e) => showHint(container, `Couldn't download: ${e.message}. Try again.`, true),
        onDone: async () => {
            lmStatus = await fetchJson("/local-models/status");  // downloaded=true → unlock flows
            renderVoiceSection(container);
        },
    });
}

function updateVoiceDownloadBar(container, key, dl) {
    // Keys are fixed manifest tokens (qwen-tts-*) — safe in an attr selector.
    const bar = container.querySelector(`#ob-stt-voice .ob-lm-audio-progress[data-dl-key="${key}"]`);
    if (!bar || !dl) { renderVoiceSection(container); return; }  // first tick: render the bar
    const pct = dl.total ? Math.min(100, Math.floor((dl.completed / dl.total) * 100)) : 0;
    const fill = bar.querySelector(".ob-lm-progress-fill");
    const text = bar.querySelector(".ob-lm-progress-text");
    if (fill) fill.style.width = pct + "%";
    if (text) text.textContent = `${pct}% ${dl.statusText || "downloading"}`;
}

function voiceResult(container, sel, msgHtml, isError) {
    const el = container.querySelector(sel);
    if (!el) return;
    el.classList.toggle("ob-stt-voice-result-error", !!isError);
    el.innerHTML = msgHtml;
    el.hidden = false;
}

// Non-OK responses surface the backend detail VERBATIM (the 422 consent gate,
// upstream qwen-tts errors) — no rewording.
async function verbatimDetail(r) {
    try {
        const j = await r.json();
        if (typeof j.detail === "string") return j.detail;
        if (j.detail != null) return JSON.stringify(j.detail);
    } catch (_) { /* non-JSON body */ }
    return `HTTP ${r.status}`;
}

async function submitClone(container) {
    if (cloneBusy) return;
    const fileInput = container.querySelector("#ob-stt-clone-file");
    const nameInput = container.querySelector("#ob-stt-clone-name");
    const consent = container.querySelector("#ob-stt-clone-consent");
    const btn = container.querySelector("#ob-stt-clone-btn");
    const file = fileInput && fileInput.files && fileInput.files[0];
    const name = ((nameInput && nameInput.value) || "").trim();
    if (!file) return voiceResult(container, "#ob-stt-clone-result", "Pick an audio file first (~3 seconds or more of clean speech).", true);
    if (!name) return voiceResult(container, "#ob-stt-clone-result", "Name the voice first.", true);
    cloneBusy = true;
    if (btn) { btn.disabled = true; btn.textContent = "Cloning…"; }
    try {
        const fd = new FormData();
        fd.append("name", name);
        // consent=true ONLY when the box is actually checked — otherwise the
        // field is omitted and the backend's 422 gate answers honestly.
        if (consent && consent.checked) fd.append("consent", "true");
        fd.append("files", file, file.name);
        const r = await fetch("/qwen/voices/clone", { method: "POST", body: fd });
        if (!r.ok) throw new Error(await verbatimDetail(r));
        const j = await r.json().catch(() => ({}));
        const vid = j.voice_id || name;
        voiceResult(container, "#ob-stt-clone-result",
            `Voice cloned: <code>${escapeHtml(vid)}</code> &mdash; it&#39;s now in your voice picker.`, false);
    } catch (e) {
        voiceResult(container, "#ob-stt-clone-result", escapeHtml(e.message), true);
    } finally {
        cloneBusy = false;
        if (btn) { btn.textContent = "Clone voice"; btn.disabled = !(consent && consent.checked); }
    }
}

async function submitDesign(container) {
    if (designBusy) return;
    const desc = ((container.querySelector("#ob-stt-design-desc") || {}).value || "").trim();
    if (!desc) return voiceResult(container, "#ob-stt-design-result", "Describe the voice you want first.", true);
    const btn = container.querySelector("#ob-stt-design-btn");
    const previews = container.querySelector("#ob-stt-design-previews");
    designBusy = true;
    designSelectedId = null;
    const saveRow = container.querySelector("#ob-stt-design-save");
    if (saveRow) saveRow.hidden = true;
    if (btn) { btn.disabled = true; btn.textContent = "Designing…"; }
    if (previews) previews.innerHTML = `<p class="ob-stt-voice-note">Generating previews&hellip;</p>`;
    try {
        const body = { voice_description: desc };
        const sample = ((container.querySelector("#ob-stt-design-text") || {}).value || "").trim();
        if (sample) body.text = sample;
        const r = await fetch("/qwen/voices/design", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        if (!r.ok) throw new Error(await verbatimDetail(r));
        const j = await r.json().catch(() => ({}));
        renderDesignPreviews(container, j.previews || []);
    } catch (e) {
        if (previews) previews.innerHTML = "";
        voiceResult(container, "#ob-stt-design-result", escapeHtml(e.message), true);
    } finally {
        designBusy = false;
        if (btn) { btn.disabled = false; btn.textContent = "Generate previews"; }
    }
}

// Previews come back as {generated_voice_id, audio_b64, sample_rate}; the
// member is loopback-only so a data: URL is what the browser can play (same
// contract the Voice Lab uses).
function renderDesignPreviews(container, previews) {
    const host = container.querySelector("#ob-stt-design-previews");
    if (!host) return;
    if (!previews.length) {
        host.innerHTML = `<p class="ob-stt-voice-note">No previews returned. Try a different description.</p>`;
        return;
    }
    host.innerHTML = previews.map((p, i) => {
        const src = p.audio_b64 ? `data:audio/wav;base64,${p.audio_b64}` : "";
        return `<div class="ob-stt-design-preview" data-gvid="${escapeHtml(p.generated_voice_id || "")}">
            <span class="ob-stt-design-preview-name">Option ${i + 1}</span>
            ${src ? `<audio controls preload="none" src="${src}"></audio>` : ""}
            <button type="button" class="ob-lm-btn ob-stt-design-use">Use this one</button>
        </div>`;
    }).join("");
    host.querySelectorAll(".ob-stt-design-preview").forEach((card) => {
        const useBtn = card.querySelector(".ob-stt-design-use");
        if (!useBtn) return;
        useBtn.addEventListener("click", () => {
            designSelectedId = card.getAttribute("data-gvid") || null;
            host.querySelectorAll(".ob-stt-design-preview").forEach((c) => c.classList.remove("ob-stt-design-selected"));
            host.querySelectorAll(".ob-stt-design-use").forEach((b) => { b.textContent = "Use this one"; });
            card.classList.add("ob-stt-design-selected");
            useBtn.textContent = "✓ Selected";
            const saveRow = container.querySelector("#ob-stt-design-save");
            if (saveRow) saveRow.hidden = false;
            const nameInput = container.querySelector("#ob-stt-design-name");
            if (nameInput && !nameInput.value.trim()) {
                const desc = ((container.querySelector("#ob-stt-design-desc") || {}).value || "").trim();
                nameInput.value = desc.slice(0, 40);
            }
        });
    });
}

async function submitDesignSave(container) {
    if (!designSelectedId) return voiceResult(container, "#ob-stt-design-result", "Pick a preview with “Use this one” first.", true);
    const name = ((container.querySelector("#ob-stt-design-name") || {}).value || "").trim();
    if (!name) return voiceResult(container, "#ob-stt-design-result", "Name the voice before saving.", true);
    const btn = container.querySelector("#ob-stt-design-save-btn");
    if (btn) { btn.disabled = true; btn.textContent = "Saving…"; }
    try {
        const r = await fetch("/qwen/voices/design/save", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ generated_voice_id: designSelectedId, name }),
        });
        if (!r.ok) throw new Error(await verbatimDetail(r));
        const j = await r.json().catch(() => ({}));
        const vid = j.voice_id || name;
        voiceResult(container, "#ob-stt-design-result",
            `Voice saved: <code>${escapeHtml(vid)}</code> &mdash; it&#39;s now in your voice picker.`, false);
        designSelectedId = null;
        const saveRow = container.querySelector("#ob-stt-design-save");
        if (saveRow) saveRow.hidden = true;
    } catch (e) {
        voiceResult(container, "#ob-stt-design-result", escapeHtml(e.message), true);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = "Save voice"; }
    }
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
