// On-box local model stack step (M8). Reads GET /local-models/status (M1) and
// lets the user download weights + deliberately activate STT/TTS/embeddings/
// reranking on-box, per capability (D2: nothing activates implicitly). Every
// status read is fail-open — the step renders even if the stack isn't
// installed yet. Activation flips reuse existing endpoints:
//   embeddings → GPU-idle preflight (blocking) → /embeddings/reembed → on done
//                /toolvault/reload (§5.1 cache coherence)
//   rerank     → /rerank/select {provider:'localstack'}
//   stt / tts  → /local-models/capability (config seed flag; stt also pins
//                STT_PROVIDER=onbox)
import { stepSigilContext } from "../onboarding.js";

let status = null;         // last GET /local-models/status
let busy = {};             // per-capability activation in-flight guard
let downloading = {};      // model key -> {completed,total,statusText}
let pollTimer = null;      // /embeddings/status poll during the reembed cutover

// Static §7 fallback so the table is meaningful before M1's status carries
// per-tier recommendations. Keyed by a coarse tier: 'gpu' | 'cpu'.
const REC_FALLBACK = {
    gpu: {
        embeddings: { label: "Qwen3-Embedding-8B (Q8_0, 4096-dim)", size: "~8 GB", note: "" },
        rerank: { label: "Qwen3-Reranker-8B (Q8_0)", size: "~8.1 GB", note: "Sequential with the embedder (D13); validated by benchmark before selection." },
        stt: { label: "whisper large-v3-turbo (stream) + large-v3 (files)", size: "~5 GB", note: "" },
        tts: { label: "Qwen3-TTS 0.6B-CustomVoice (streaming) · 1.7B (files)", size: "~9 GB", note: "" },
    },
    cpu: {
        embeddings: { label: "Qwen3-Embedding-0.6B (1024-dim)", size: "~0.6 GB", note: "Fast on CPU." },
        rerank: { label: "Qwen3-Reranker-0.6B (CPU)", size: "~1.3 GB", note: "Latency-gated; may fall back to cloud." },
        stt: { label: "whisper large-v3-turbo (int8)", size: "~1.6 GB", note: "Near-realtime for files; streaming may lag." },
        tts: { label: "Cloud recommended", size: "—", note: "On-box TTS is far slower than realtime on CPU — offered as experimental only." },
    },
};

const CAPS = [
    { id: "embeddings", label: "Memory (embeddings)" },
    { id: "rerank", label: "Search reranking" },
    { id: "stt", label: "Speech-to-text" },
    { id: "tts", label: "Text-to-speech" },
];

export async function render(container, { next, back, skip, sigil }) {
    const sig = sigil || stepSigilContext("local_models");
    container.innerHTML = `
        <section class="ob-step ob-local-models">
            <aside class="ob-step-sigil" aria-hidden="true">
                <div class="ob-step-sigil-num"><em>${sig.num}</em></div>
                <div class="ob-step-sigil-rule"></div>
                <div class="ob-step-sigil-label">ON-BOX</div>
            </aside>
            <div class="ob-step-body">
                <div class="ob-step-eyebrow">
                    <span class="ob-step-eyebrow-dot" aria-hidden="true"></span>
                    On-box model stack
                </div>
                <h1 class="ob-step-title">Run speech, memory &amp; search <em>on the box</em>.</h1>
                <p class="ob-step-lede">
                    When your hardware allows, the BlackBox runs transcription,
                    voice, memory embeddings, and search reranking locally — the
                    only thing that leaves the box is the chat model itself.
                    Everything here is optional and turned on one capability at a
                    time. An explicit provider you've already chosen (e.g. your
                    ElevenLabs key) is never overridden.
                </p>
                <div id="ob-lm-body"><div class="ob-loading">Checking your hardware&hellip;</div></div>
                <nav class="ob-step-nav" aria-label="Step navigation">
                    <button type="button" class="ob-back" id="ob-lm-back">
                        <span aria-hidden="true">&larr;</span> Back to ${sig.backLabel ? sig.backLabel.toLowerCase() : "memory & search"}
                    </button>
                    <button type="button" class="ob-cta" id="ob-lm-continue">
                        Continue <span class="ob-cta-arrow" aria-hidden="true">&rarr;</span>
                    </button>
                    <button type="button" class="ob-skip" id="ob-lm-skip">
                        Skip &mdash; set up later <span aria-hidden="true">&rarr;</span>
                    </button>
                </nav>
            </div>
        </section>`;
    document.getElementById("ob-lm-back").addEventListener("click", () => { stopPoll(); back(); });
    document.getElementById("ob-lm-continue").addEventListener("click", () => { stopPoll(); next(); });
    document.getElementById("ob-lm-skip").addEventListener("click", () => { stopPoll(); skip(); });

    status = await fetchJson("/local-models/status");
    renderBody(container);
}

// NOTE: the pure decision/formatting helpers below take an optional `st`
// (defaulting to the module-level `status`) so they can be unit-tested against
// a realistic GET /local-models/status payload without a DOM
// (local_models.render.test.mjs). They read the REAL payload shape:
//   status.hardware = {gpu, gpu_name, vram_mb, ram_mb, source, tier}  (probe())
//   status.disk     = {free_mb, required_mb, ok}
//   status.routing[cap] = {enabled, healthy, decision}                (object!)
//   status.models[] = {model, capability, group, label, running, state, download}
export function tierKey(st = status) {
    // 'gpu' when the hardware probe reports a usable GPU (or a HIGH tier),
    // else 'cpu'. Reads status.hardware (verbatim probe() block) — NOT a
    // top-level status.gpu/status.tier (those keys don't exist, which made
    // this always return 'cpu' and mis-recommend CPU weights on GPU boxes).
    const hw = (st && st.hardware) || {};
    const tier = String(hw.tier || "").toUpperCase();
    return (hw.gpu || tier === "HIGH") ? "gpu" : "cpu";
}

export function rec(capId, st = status) {
    const fromStatus = st && st.recommendations && st.recommendations[capId];
    if (fromStatus && (fromStatus.label || fromStatus.model)) {
        return { label: fromStatus.label || fromStatus.model,
                 size: fromStatus.size_gb ? `~${fromStatus.size_gb} GB` : (fromStatus.size || ""),
                 note: fromStatus.note || "", slug: fromStatus.slug || fromStatus.model || "" };
    }
    return REC_FALLBACK[tierKey(st)][capId] || { label: "—", size: "", note: "" };
}

export function isActive(capId, st = status) {
    // routing[cap] is an OBJECT {enabled, healthy, decision}; "on-box" means the
    // capability is seeded AND the stack is reachable (_routing_decision in
    // Orchestrator/routes/local_models_routes.py). The old code called
    // .toLowerCase() on this object (TypeError) and compared to 'onbox' rather
    // than the hyphenated backend sentinel — the same bug HEAD fixed in the
    // sibling updates-manager.js card.
    const routing = (st && st.routing) || {};
    return (routing[capId] || {}).decision === "on-box";
}

export function modelForCap(capId, st = status) {
    // The weight member backing this capability, if status lists it
    // (models[].capability). Its `model` id is a valid POST /local-models/
    // download artifact ONLY when m.downloadable is true (i.e. the id is a
    // DOWNLOAD_MANIFEST key) — that holds for embeddings + tts, but NOT for
    // rerank (self-converted at install) or stt (whisper auto-pulled on first
    // use). Gate the Download control on isDownloadable(), never on `model`
    // alone, or the button POSTs an unknown artifact and 404s.
    return ((st && st.models) || []).find((m) => m.capability === capId) || null;
}

// True when this member has a fetchable weight artifact — POST /local-models/
// download will accept its id. Driven off the backend `downloadable` flag
// (m.model ∈ DOWNLOAD_MANIFEST), the single source of truth, so the frontend
// never duplicates the manifest and can't drift from it.
export function isDownloadable(m) {
    return !!(m && m.downloadable);
}

// True when this capability's weights are present on disk. The status endpoint
// carries the download-state entry as models[].download = {state, ...} (or
// {state:"pending"} when absent); read_download_state's terminal success states
// are "downloaded"/"done" (Orchestrator/local_stack.py:model_downloaded). When
// no member backs the capability there's nothing to download.
export function isDownloaded(m) {
    if (!m) return true;
    const s = m.download && m.download.state;
    return s === "downloaded" || s === "done";
}

// ── hardware / disk lines (pure, testable against the real payload) ───────
export function hwLineHtml(st = status) {
    // status.hardware is the verbatim probe() block: {gpu, gpu_name, vram_mb,…}.
    const hw = (st && st.hardware) || {};
    if (hw.gpu || hw.gpu_name || hw.vram_mb) {
        const vram = hw.vram_mb ? ` · ${Math.round(hw.vram_mb / 1024)} GB VRAM` : "";
        return `GPU: <strong>${escapeHtml(hw.gpu_name || "NVIDIA")}</strong>${vram}`;
    }
    return `No GPU detected — <strong>CPU tier</strong>`;
}

export function diskLineHtml(st = status) {
    // status.disk carries free_mb / required_mb (NOT free_gb/required_gb).
    const disk = (st && st.disk) || {};
    if (disk.free_mb == null) return "";
    const freeGb = Math.round(disk.free_mb / 1024);
    const needGb = disk.required_mb ? ` (needs ~${Math.round(disk.required_mb / 1024)} GB)` : "";
    return `Disk free: <strong>${escapeHtml(String(freeGb))} GB</strong>${needGb}`;
}

function renderBody(container) {
    const body = container.querySelector("#ob-lm-body");
    if (!body) return;
    const installed = !!(status && status.installed);
    const healthy = !!(status && status.healthy);
    const disk = (status && status.disk) || {};
    const tier = tierKey();

    const hwLine = hwLineHtml();
    const diskLine = diskLineHtml();
    const diskWarn = (disk.ok === false)
        ? `<p class="ob-lm-warn">Not enough free disk for the full on-box weight set. Free up space before downloading.</p>` : "";
    const cpuWarn = (tier === "cpu")
        ? `<p class="ob-lm-warn">On a CPU box the local models run <strong>much slower than realtime</strong>. Embeddings + files are fine; live voice is best left on a cloud provider. Nothing here is turned on by default.</p>` : "";
    const notInstalled = !installed
        ? `<p class="ob-lm-warn">The on-box stack isn't installed yet. Re-run <code>install.sh</code> (Step 2f) to add it, then return here to download models.</p>` : "";
    const swapNote = `<p class="ob-lm-note">Voice and search share one GPU and take turns. The <strong>first</strong> interaction after you switch between talking and searching takes about <strong>6–10 seconds</strong> while models swap; after an idle spell the first use also takes a few seconds to warm up. Everything in between is fast.</p>`;

    body.innerHTML = `
        <div class="ob-lm-hw">
            <div class="ob-lm-hw-line">${hwLine}${diskLine ? ` &nbsp;·&nbsp; ${diskLine}` : ""}</div>
            ${installed ? `<div class="ob-lm-hw-badge ${healthy ? "ok" : "warn"}">${healthy ? "Stack healthy" : "Stack installed"}</div>` : ""}
        </div>
        ${notInstalled}${diskWarn}${cpuWarn}
        <div class="ob-lm-caps">${CAPS.map((c) => renderCapRow(c, status)).join("")}</div>
        ${audioSectionHtml(status)}
        ${swapNote}
        <p id="ob-lm-hint" class="ob-lm-hint" hidden></p>`;

    CAPS.forEach((c) => wireCapRow(container, c.id));
    wireAudioSection(container);
}

// ── Audio two-card download section (M-C Task C1) ─────────────────────────
// A dedicated STT + TTS card pair mirroring embeddings.js's two-card shell,
// consuming the A4 per-artifact rows the backend hangs off the audio MEMBERS
// (status.models[].artifacts — {key,label,downloadable,downloaded,size_gb,
// repo_pending_g3}). STT card = one whisper download button + a "best fit for
// your GPU: <model>" note (no manual dropdown, phase-1 decision). TTS card =
// the three per-variant Qwen download buttons (labels + sizes). Every button is
// wired to the SAME NDJSON startDownload({artifact:<key>}) the cap rows use.
//
// INERT WHEN OFF (additive invariant): the backend only emits `artifacts` when
// the stack is installed (no [local_models] section → is_installed() False →
// empty list), so both audioArtifacts() come back empty and the whole section
// renders "" — a dev box with the stack off shows no audio cards at all.

// The per-artifact rows for one audio capability, read off the member backing
// it (modelForCap). [] when the stack is off / older backend without artifacts.
export function audioArtifacts(capId, st = status) {
    const m = modelForCap(capId, st);
    return (m && Array.isArray(m.artifacts)) ? m.artifacts : [];
}

// One decimal, trailing-zero-trimmed, for the "~X GB" size chips.
function fmtGb(n) {
    const v = Number(n);
    if (!isFinite(v)) return "";
    return String(Math.round(v * 10) / 10);
}

// The download control for one artifact. A `repo_pending_g3` artifact (its HF
// repo ids are still placeholders, pinned on MS02 during the first GPU
// bring-up) renders a DISABLED button with a clear reason — NEVER a live button
// whose POST would 404 the unknown/unpinned artifact. `downloadingMap` is
// injectable so the render test can assert progress markup without the module
// state.
export function audioArtifactBtnHtml(a, downloadingMap = downloading) {
    const key = a && a.key;
    if (!key) return "";
    const dl = downloadingMap[key];
    if (dl) {
        const pct = dl.total ? Math.min(100, Math.floor((dl.completed / dl.total) * 100)) : 0;
        return `<div class="ob-lm-progress ob-lm-audio-progress" data-dl-key="${escapeHtml(key)}">
            <div class="ob-lm-progress-track"><div class="ob-lm-progress-fill" style="width:${pct}%"></div></div>
            <span class="ob-lm-progress-text">${pct}% ${escapeHtml(dl.statusText || "downloading")}</span></div>`;
    }
    if (a.repo_pending_g3) {
        // Pinned during first GPU bring-up — disabled, honest, never a 404 POST.
        return `<button type="button" class="ob-lm-btn ob-lm-audio-dl" disabled
                    title="Pinned during first GPU bring-up">Pinned during first GPU bring-up</button>`;
    }
    if (a.downloaded) {
        return `<span class="ob-lm-audio-done">Downloaded &check;</span>`;
    }
    const size = a.size_gb ? `~${escapeHtml(fmtGb(a.size_gb))} GB` : "";
    return `<button type="button" class="ob-lm-btn ob-lm-audio-dl ob-lm-btn-activate"
                data-dl="${escapeHtml(key)}">Download ${size}</button>`;
}

// One artifact row: label + size chip + its download control.
function audioArtifactRowHtml(a) {
    const size = a && a.size_gb ? ` &middot; ~${escapeHtml(fmtGb(a.size_gb))} GB` : "";
    return `
        <div class="ob-lm-audio-row" data-artifact="${escapeHtml((a && a.key) || "")}">
            <div class="ob-lm-audio-row-head">
                <span class="ob-lm-audio-label">${escapeHtml((a && (a.label || a.key)) || "")}</span>
                <span class="ob-lm-audio-size">${size}</span>
            </div>
            <div class="ob-lm-audio-action">${audioArtifactBtnHtml(a)}</div>
        </div>`;
}

function sttCardHtml(artifacts, st) {
    // Best-fit whisper is auto-selected (no dropdown, phase-1). Name it from the
    // tier recommendation so the user sees what will be pulled.
    const fit = escapeHtml(rec("stt", st).label || "the best-fitting model");
    const rows = artifacts.map((a) => audioArtifactRowHtml(a)).join("");
    return `
        <div class="ob-lm-audio-card" id="ob-lm-audio-stt">
            <div class="ob-lm-audio-card-title">Speech-to-text</div>
            <p class="ob-lm-audio-card-sub">On-box Whisper transcription. The best
               model that fits your GPU is chosen for you &mdash; no picking required.</p>
            <p class="ob-lm-audio-fit">Best fit for your GPU: <strong>${fit}</strong></p>
            <div class="ob-lm-audio-rows">${rows}</div>
        </div>`;
}

function ttsCardHtml(artifacts) {
    const rows = artifacts.map((a) => audioArtifactRowHtml(a)).join("");
    return `
        <div class="ob-lm-audio-card" id="ob-lm-audio-tts">
            <div class="ob-lm-audio-card-title">Text-to-speech</div>
            <p class="ob-lm-audio-card-sub">On-box Qwen3-TTS: a standard voice, a
               3-second zero-shot clone, and text-described voice design. Download
               only the variants you want.</p>
            <div class="ob-lm-audio-rows">${rows}</div>
        </div>`;
}

// The whole two-card audio section, or "" when there are no artifacts to show
// (stack off / older backend) — the inert-when-off guarantee lives here.
// Exported + st-parameterised so the render test can assert on returned HTML
// without a DOM.
export function audioSectionHtml(st = status) {
    const sttArtifacts = audioArtifacts("stt", st);
    const ttsArtifacts = audioArtifacts("tts", st);
    if (!sttArtifacts.length && !ttsArtifacts.length) return "";
    const sttCard = sttArtifacts.length ? sttCardHtml(sttArtifacts, st) : "";
    const ttsCard = ttsArtifacts.length ? ttsCardHtml(ttsArtifacts) : "";
    return `
        <div class="ob-lm-audio" id="ob-lm-audio">
            <div class="ob-lm-audio-eyebrow">On-box audio downloads</div>
            <div class="ob-lm-audio-cards">${sttCard}${ttsCard}</div>
        </div>`;
}

// Wire the audio download buttons to the shared NDJSON startDownload — the
// disabled repo_pending_g3 button has no data-dl, so it's never wired.
function wireAudioSection(container) {
    container.querySelectorAll(".ob-lm-audio-dl[data-dl]").forEach((btn) => {
        btn.addEventListener("click", () => startDownload(container, btn.getAttribute("data-dl")));
    });
}

export function renderCapRow(cap, st = status) {
    const r = rec(cap.id, st);
    const active = isActive(cap.id, st);
    const m = modelForCap(cap.id, st);
    const dl = downloading[m && m.model];
    const downloaded = isDownloaded(m);
    // A member "needs a download" only when it has a fetchable artifact AND its
    // weights aren't present yet. Non-manifest members (rerank/stt) skip the
    // Download button entirely and fall through to activation — a note below
    // explains they're provisioned automatically.
    const needsDownload = isDownloadable(m) && !downloaded;

    let control;
    if (dl) {
        const pct = dl.total ? Math.min(100, Math.floor((dl.completed / dl.total) * 100)) : 0;
        control = `<div class="ob-lm-progress"><div class="ob-lm-progress-track"><div class="ob-lm-progress-fill" style="width:${pct}%"></div></div><span class="ob-lm-progress-text">${pct}% ${escapeHtml(dl.statusText || "downloading")}</span></div>`;
    } else if (needsDownload) {
        control = `<button type="button" class="ob-lm-btn" data-dl="${escapeHtml(m.model)}">Download ${escapeHtml(r.size || "")}</button>`;
    } else if (active) {
        control = `<button type="button" class="ob-lm-btn ob-lm-btn-on" data-off="${cap.id}">On-box active — turn off</button>`;
    } else {
        control = `<button type="button" class="ob-lm-btn ob-lm-btn-activate" data-on="${cap.id}">Use on-box</button>`;
    }

    // Non-downloadable member not yet on disk: there's no Download button, so
    // explain how it IS provisioned — HONESTLY per capability. The reranker is
    // NOT auto-converted at install (that was a lie): its 8B GGUF is self-
    // converted + benchmark-validated by setup (or the Memory step), a ~40-60
    // min job. STT (whisper) really is pulled automatically on first use.
    const autoNote = (m && !isDownloadable(m) && !downloaded && !active)
        ? (cap.id === "rerank"
            ? `<p class="ob-lm-cap-note">No manual download here — the on-box reranker is self-converted &amp; benchmark-validated by setup (or the Memory step), not at install.</p>`
            : `<p class="ob-lm-cap-note">No manual download — pulled automatically on first use.</p>`)
        : "";

    return `
        <div class="ob-lm-cap" data-cap="${cap.id}">
            <div class="ob-lm-cap-head">
                <span class="ob-lm-cap-name">${escapeHtml(cap.label)}${active ? ' <span class="ob-lm-dot" title="On-box active">●</span>' : ""}</span>
                <span class="ob-lm-cap-model">${escapeHtml(r.label)}${r.size ? ` · ${escapeHtml(r.size)}` : ""}</span>
            </div>
            ${r.note ? `<p class="ob-lm-cap-note">${escapeHtml(r.note)}</p>` : ""}
            ${autoNote}
            <div class="ob-lm-cap-action">${control}</div>
        </div>`;
}

function wireCapRow(container, capId) {
    const row = container.querySelector(`.ob-lm-cap[data-cap="${capId}"]`);
    if (!row) return;
    const dlBtn = row.querySelector("[data-dl]");
    if (dlBtn) dlBtn.addEventListener("click", () => startDownload(container, dlBtn.getAttribute("data-dl")));
    const onBtn = row.querySelector("[data-on]");
    if (onBtn) onBtn.addEventListener("click", () => activate(container, capId, true));
    const offBtn = row.querySelector("[data-off]");
    if (offBtn) offBtn.addEventListener("click", () => activate(container, capId, false));
}

// ── Downloads (NDJSON progress, cloned from the embeddings-pull pattern) ──
async function startDownload(container, key) {
    if (downloading[key]) return;
    downloading[key] = { completed: 0, total: 0, statusText: "starting" };
    renderBody(container);
    try {
        const r = await fetch("/local-models/download", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ artifact: key }),  // LocalModelDownloadRequest.artifact
        });
        if (!r.ok && r.status !== 409) throw new Error(`download returned ${r.status}`);
        // Stream NDJSON lines: {model,status,completed,total}. If the body
        // isn't streamable (409 already-running / older backend), fall back
        // to a status refresh.
        if (r.body && r.body.getReader) {
            const reader = r.body.getReader();
            const dec = new TextDecoder();
            let buf = "";
            for (;;) {
                const { done, value } = await reader.read();
                if (done) break;
                buf += dec.decode(value, { stream: true });
                let nl;
                while ((nl = buf.indexOf("\n")) >= 0) {
                    const line = buf.slice(0, nl).trim();
                    buf = buf.slice(nl + 1);
                    if (!line) continue;
                    try {
                        const p = JSON.parse(line);
                        downloading[key] = { completed: Number(p.completed) || downloading[key].completed,
                                             total: Number(p.total) || downloading[key].total,
                                             statusText: p.status || "downloading" };
                        updateDownloadBar(container, key);
                    } catch (_) { /* skip malformed line */ }
                }
            }
        }
    } catch (e) {
        showHint(container, `Couldn't download: ${e.message}. Try again.`, true);
    }
    delete downloading[key];
    status = await fetchJson("/local-models/status");  // reflect downloaded=true
    renderBody(container);
}

function updateDownloadBar(container, key) {
    const dl = downloading[key];
    const row = [...container.querySelectorAll(".ob-lm-cap")]
        .find((el) => (modelForCap(el.getAttribute("data-cap")) || {}).model === key);
    const fill = row && row.querySelector(".ob-lm-progress-fill");
    const text = row && row.querySelector(".ob-lm-progress-text");
    if (!fill || !text || !dl) { renderBody(container); return; }
    const pct = dl.total ? Math.min(100, Math.floor((dl.completed / dl.total) * 100)) : 0;
    fill.style.width = pct + "%";
    text.textContent = `${pct}% ${dl.statusText || "downloading"}`;
}

// ── Per-capability activation ────────────────────────────────────────────
async function activate(container, capId, on) {
    if (busy[capId]) return;
    busy[capId] = true;
    try {
        if (capId === "embeddings") return await activateEmbeddings(container, on);
        if (capId === "rerank") return await activateRerank(container, on);
        // stt / tts → the seed-flag endpoint (stt also pins STT_PROVIDER).
        const r = await fetch("/local-models/capability", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ capability: capId, enabled: on }),
        });
        if (!r.ok) throw new Error(await safeDetail(r));
        status = await fetchJson("/local-models/status");
        renderBody(container);
    } catch (e) {
        showHint(container, `Couldn't change ${capId}: ${e.message}`, true);
    } finally {
        busy[capId] = false;
    }
}

async function activateRerank(container, on) {
    const r = await fetch("/rerank/select", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            provider: "localstack",
            model: rec("rerank").slug || "qwen3-reranker-8b-local",
            enabled: on,
        }),
    });
    if (!r.ok) throw new Error(await safeDetail(r));
    status = await fetchJson("/local-models/status");
    renderBody(container);
}

async function activateEmbeddings(container, on) {
    if (!on) {
        showHint(container, "To move memory back to cloud, pick a cloud model in the Memory step — the on-box corpus stays searchable meanwhile.", false);
        return;
    }
    // BLOCKING GPU-idle precondition (Phase-2 Step-0): the retrieval group
    // lazy-loads ~11.5-13GB on the first re-embed and OOMs if the old pinned
    // embedder/reranker is still resident.
    const pf = await fetchJson("/local-models/gpu-preflight");
    if (pf && pf.ok === false) {
        showHint(container, pf.detail || "Free the GPU before moving memory on-box, then retry.", true);
        return;
    }
    const target = rec("embeddings").slug || (tierKey() === "gpu" ? "qwen3-embedding-8b-local" : "qwen3-embedding-0.6b-local");
    const r = await fetch("/embeddings/reembed", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target }),
    });
    if (r.status === 409) { showHint(container, "A memory rebuild is already running — see the Memory step for progress.", false); startEmbedPoll(container, target); return; }
    if (!r.ok) throw new Error(await safeDetail(r));
    showHint(container, "Rebuilding your memory index on-box. Voice features may be slow until it finishes — track detailed progress in the Memory step.", false);
    startEmbedPoll(container, target);
}

// Poll /embeddings/status; when the cutover job is done, fire /toolvault/reload
// (§5.1 cache-coherence: ToolVault + code embeddings must re-embed at the new
// dimension or the first hot query mixes dims).
function startEmbedPoll(container, target) {
    stopPoll();
    pollTimer = setInterval(async () => {
        const es = await fetchJson("/embeddings/status");
        const job = es && es.job;
        if (job && job.state === "running") {
            const pct = job.total ? Math.floor((job.done / job.total) * 100) : 0;
            showHint(container, `Rebuilding memory on-box: ${job.done || 0}/${job.total || "?"} (${pct}%)…`, false);
            return;
        }
        stopPoll();
        if (es && (es.active || "").endsWith("-local")) {
            await fetch("/toolvault/reload", { method: "POST" }).catch(() => {});
            showHint(container, "On-box memory active. Tool + code search caches refreshed.", false);
        }
        status = await fetchJson("/local-models/status");
        renderBody(container);
    }, 3000);
}

function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

// ── helpers ──────────────────────────────────────────────────────────────
function showHint(container, msg, isError) {
    const hint = container.querySelector("#ob-lm-hint");
    if (!hint) return;
    hint.className = "ob-lm-hint" + (isError ? " ob-lm-hint-error" : "");
    hint.textContent = msg;
    hint.hidden = false;
}
async function fetchJson(url) {
    try { const r = await fetch(url, { cache: "no-store" }); if (!r.ok) return null; return await r.json(); }
    catch (_) { return null; }
}
async function safeDetail(r) {
    try { const j = await r.json(); return j.detail || `HTTP ${r.status}`; } catch (_) { return `HTTP ${r.status}`; }
}
function escapeHtml(s) {
    if (s == null) return "";
    return String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}
