/**
 * voice-lab.js
 * ElevenLabs "Voice Lab" panel (Task 24).
 *
 * Four zones in one modal (stacked sections):
 *   1. Clone a voice  — record (MediaRecorder) OR upload audio files, name +
 *      optional description + an explicit consent checkbox. Clone is disabled
 *      until name AND audio AND consent are all present.
 *        POST /elevenlabs/voices/clone  (multipart: name, files[], consent="true",
 *        description?, remove_background_noise?) -> {voice_id, requires_verification}
 *   2. Design a voice — text description -> 3 preview voices (each click-to-play),
 *      "Use this one" reveals name + Save.
 *        POST /elevenlabs/voices/design        {voice_description, text?}
 *             -> {text, previews:[{generated_voice_id, audio_url, ...}]}
 *        POST /elevenlabs/voices/design/save    {generated_voice_id, name, description}
 *             -> {voice_id}
 *   3. My Voices      — list account voices with preview + delete.
 *        GET    /elevenlabs/voices             -> {my_voices:[...], premade:[...]}
 *        DELETE /elevenlabs/voices/{voice_id}  -> {ok, in_use:[operator names]}
 *   4. Grok (xAI) voices — clone (≤120s clip + consent) / list / delete via /xai/voices; gated on GET /xai/voices configured.
 *
 * Key stays server-side — everything routes through the Orchestrator proxy. The
 * whole feature is gated on GET /elevenlabs/status `configured` (the trigger
 * button in tts-stt.js stays hidden without a key); the Clone zone is further
 * gated on features.instant_voice_cloning (free tier shows an upgrade explainer
 * instead of the form).
 *
 * After any mutation that adds/removes an account voice we re-run
 * populateVoiceCatalog() so the TTS picker stays in sync (mirrors voice-library.js).
 *
 * The modal DOM is built lazily on first open (no static HTML in index.html);
 * the .modal / .modal-card / .modal-head shell matches the Portal convention.
 * Conventions (modal build, click-to-play preview with overlap-prevention,
 * toastSuccess/toastError, status gating, dynamic-import trigger) MIRROR the
 * sibling voice-library.js.
 */

import { toastSuccess, toastError } from './modules/core-utils.js';
import { populateVoiceCatalog } from './modules/tts-stt.js';

const MODAL_ID = 'voiceLabModal';

// Aim for ≥30s of speech for a good clone; we surface this as a hint + a soft
// "✓ enough" cue once elapsed, but never hard-block (ElevenLabs accepts less).
const CLONE_TARGET_SECS = 30;

// ── Shared preview <audio> ──────────────────────────────────────────────────
// One stream at a time, reused so starting a new preview can always cleanly stop
// the previous one (overlap-prevention: pause + reset currentTime + clear onended).
let previewAudio = null;
let playingBtn = null;       // the ▶ button currently in the "playing" state

// ── Clone-zone recording state ──────────────────────────────────────────────
let mediaRec = null;         // active MediaRecorder
let mediaStream = null;      // active getUserMedia stream (tracks stopped on stop)
let recChunks = [];          // collected Blob chunks
let recordedBlob = null;     // finished recording Blob (the audio to clone)
let recTimer = null;         // setInterval handle for the elapsed timer
let recStartMs = 0;
let uploadedFiles = [];      // File[] chosen via the file input

// ── Design-zone state ───────────────────────────────────────────────────────
let selectedPreviewId = null;  // generated_voice_id picked via "Use this one"

function escapeHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtClock(secs) {
    const m = Math.floor(secs / 60);
    const s = Math.floor(secs % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
}

/** Stop any in-flight preview and reset its button (overlap prevention). */
function stopPreview() {
    if (previewAudio) {
        previewAudio.pause();
        previewAudio.currentTime = 0;
        previewAudio.onended = null;   // critical: drop the listener
    }
    if (playingBtn) {
        playingBtn.classList.remove('playing');
        playingBtn.textContent = '▶';
        playingBtn = null;
    }
}

/** Click-to-play preview with overlap prevention (shared across all zones). */
function togglePreview(url, btn) {
    if (!url) return;
    // Clicking the already-playing button = stop.
    if (playingBtn === btn) {
        stopPreview();
        return;
    }
    stopPreview();  // stop whatever else was playing first

    if (!previewAudio) previewAudio = new Audio();
    previewAudio.src = url;
    previewAudio.currentTime = 0;
    playingBtn = btn;
    btn.classList.add('playing');
    btn.textContent = '⏸';

    previewAudio.onended = () => {
        btn.classList.remove('playing');
        btn.textContent = '▶';
        if (playingBtn === btn) playingBtn = null;
    };
    // User-initiated, so the WebKitGTK autoplay policy is satisfied.
    previewAudio.play().catch((e) => {
        console.error('preview play failed', e);
        stopPreview();
        toastError('Preview failed to play');
    });
}

// =============================================================================
// Modal construction
// =============================================================================

/** Lazily build (once) and return the modal element. */
function ensureModal() {
    let modal = document.getElementById(MODAL_ID);
    if (modal) return modal;

    modal = document.createElement('div');
    modal.id = MODAL_ID;
    modal.className = 'modal hide';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.innerHTML = `
        <div class="modal-card voice-lab-card">
          <div class="modal-head">
            <h3>🎙 Voice Lab</h3>
            <button class="modal-close" type="button" aria-label="Close">✕</button>
          </div>
          <div class="modal-body voice-lab-body">

            <!-- ── Zone 1: Clone a voice ── -->
            <section class="vlab-zone" id="vlabCloneZone">
              <h4 class="vlab-zone-title">Clone a voice</h4>
              <div id="vlabCloneInner"><!-- form OR upgrade explainer injected on open --></div>
            </section>

            <!-- ── Zone 2: Design a voice ── -->
            <section class="vlab-zone" id="vlabDesignZone">
              <h4 class="vlab-zone-title">Design a voice from text</h4>
              <p class="vlab-zone-hint">Describe a voice and generate three samples to choose from.</p>
              <textarea id="vlabDesignDesc" class="vlab-input vlab-textarea"
                        placeholder="e.g. a gravelly old sea captain, weathered and warm" rows="2"></textarea>
              <div class="vlab-row vlab-row-end">
                <button id="vlabDesignGenBtn" class="vlab-btn vlab-btn-accent" type="button">Generate previews</button>
              </div>
              <div id="vlabDesignSampleText" class="vlab-sample-text" hidden></div>
              <div id="vlabDesignPreviews" class="vlab-previews"></div>
              <div id="vlabDesignSaveRow" class="vlab-save-row" hidden>
                <input id="vlabDesignName" class="vlab-input" type="text"
                       placeholder="Name this voice" autocomplete="off" />
                <button id="vlabDesignSaveBtn" class="vlab-btn vlab-btn-accent" type="button">Save voice</button>
              </div>
            </section>

            <!-- ── Zone 3: My Voices ── -->
            <section class="vlab-zone" id="vlabManageZone">
              <h4 class="vlab-zone-title">My voices</h4>
              <div id="vlabMyStatus" class="vlab-status"></div>
              <div id="vlabMyList" class="vlab-my-list"></div>
            </section>

            <!-- ── Zone 4: Grok (xAI) voices — hidden until GET /xai/voices says configured ── -->
            <section class="vlab-zone" id="vlabXaiZone" hidden>
              <h4 class="vlab-zone-title">Grok (xAI) voices</h4>
              <p class="vlab-zone-hint">Clone a voice for Grok voice sessions — one clip, max 120 seconds.
                Cloned voices are selectable in Grok voice mode (not the TTS picker).</p>
              <input id="vlabXaiFile" class="vlab-file-input" type="file"
                     accept="audio/wav,audio/mpeg,audio/mp3,audio/x-m4a,audio/mp4,audio/webm,.wav,.mp3,.m4a,.webm" />
              <input id="vlabXaiName" class="vlab-input" type="text" placeholder="Name this voice" autocomplete="off" />
              <input id="vlabXaiDesc" class="vlab-input" type="text" placeholder="Description (optional)" autocomplete="off" />
              <label class="vlab-consent">
                <input id="vlabXaiConsent" type="checkbox" />
                <span>I confirm I own this voice or have permission to clone it.</span>
              </label>
              <div class="vlab-row vlab-row-end">
                <button id="vlabXaiCloneBtn" class="vlab-btn vlab-btn-accent" type="button" disabled>Clone Grok voice</button>
              </div>
              <div id="vlabXaiStatus" class="vlab-status"></div>
              <div id="vlabXaiList" class="vlab-my-list"></div>
            </section>

            <!-- ── Zone 5: Qwen3-TTS (On-Box) — hidden until /local-models/status healthy ── -->
            <section class="vlab-zone" id="vlabQwenZone" hidden>
              <h4 class="vlab-zone-title">Qwen3-TTS (On-Box)</h4>
              <p class="vlab-zone-hint">Clone a voice from ~3s of clear speech, or design one from a
                description — all on your box, no API key. On-box streaming uses the 0.6B voice tier for
                low latency; the 1.7B voices are used for batch/file quality.</p>

              <!-- Clone -->
              <div class="vlab-method-label">Clone a voice</div>
              <input id="vlabQwenFile" class="vlab-file-input" type="file"
                     accept="audio/wav,audio/mpeg,audio/mp3,audio/x-m4a,audio/mp4,audio/webm,.wav,.mp3,.m4a,.webm" />
              <input id="vlabQwenCloneName" class="vlab-input" type="text" placeholder="Name this voice" autocomplete="off" />
              <label class="vlab-consent">
                <input id="vlabQwenConsent" type="checkbox" />
                <span>I confirm I own this voice or have permission to clone it.</span>
              </label>
              <div class="vlab-row vlab-row-end">
                <button id="vlabQwenCloneBtn" class="vlab-btn vlab-btn-accent" type="button" disabled>Clone voice</button>
              </div>

              <!-- Design -->
              <div class="vlab-method-label">Design a voice from text</div>
              <textarea id="vlabQwenDesignDesc" class="vlab-input vlab-textarea"
                        placeholder="e.g. a gravelly old sea captain, weathered and warm" rows="2"></textarea>
              <div class="vlab-row vlab-row-end">
                <button id="vlabQwenDesignGenBtn" class="vlab-btn vlab-btn-accent" type="button">Generate previews</button>
              </div>
              <div id="vlabQwenDesignPreviews" class="vlab-previews"></div>
              <div id="vlabQwenDesignSaveRow" class="vlab-save-row" hidden>
                <input id="vlabQwenDesignName" class="vlab-input" type="text" placeholder="Name this voice" autocomplete="off" />
                <button id="vlabQwenDesignSaveBtn" class="vlab-btn vlab-btn-accent" type="button">Save voice</button>
              </div>

              <!-- Manage -->
              <div class="vlab-method-label">My on-box voices</div>
              <div id="vlabQwenStatus" class="vlab-status"></div>
              <div id="vlabQwenList" class="vlab-my-list"></div>
            </section>

            <p class="vlab-foot-hint">Previews play one at a time. Cloning/designing adds a voice to
              your account (your plan has a voice limit) and it appears in the TTS picker.</p>
          </div>
        </div>`;
    document.body.appendChild(modal);

    // Close affordances: the ✕ button + clicking the dim backdrop (matches the
    // other modal modules — each wires its own close).
    modal.querySelector('.modal-close').addEventListener('click', closeVoiceLab);
    modal.addEventListener('click', (e) => { if (e.target === modal) closeVoiceLab(); });

    // ── Design zone wiring (static; the clone zone is rebuilt per-open) ──
    modal.querySelector('#vlabDesignGenBtn').addEventListener('click', runDesign);
    modal.querySelector('#vlabDesignSaveBtn').addEventListener('click', saveDesign);

    // ── xAI zone wiring (static; gate/list is refreshed per-open) ──
    modal.querySelector('#vlabXaiFile').addEventListener('change', refreshXaiCloneButton);
    modal.querySelector('#vlabXaiName').addEventListener('input', refreshXaiCloneButton);
    modal.querySelector('#vlabXaiConsent').addEventListener('change', refreshXaiCloneButton);
    modal.querySelector('#vlabXaiCloneBtn').addEventListener('click', submitXaiClone);

    // ── Qwen (on-box) zone wiring (static; gate/list refreshed per-open) ──
    modal.querySelector('#vlabQwenFile').addEventListener('change', refreshQwenCloneButton);
    modal.querySelector('#vlabQwenCloneName').addEventListener('input', refreshQwenCloneButton);
    modal.querySelector('#vlabQwenConsent').addEventListener('change', refreshQwenCloneButton);
    modal.querySelector('#vlabQwenCloneBtn').addEventListener('click', submitQwenClone);
    modal.querySelector('#vlabQwenDesignGenBtn').addEventListener('click', runQwenDesign);
    modal.querySelector('#vlabQwenDesignSaveBtn').addEventListener('click', saveQwenDesign);

    return modal;
}

// =============================================================================
// Zone 1 — Clone a voice
// =============================================================================

/** Build the clone zone — either the full form, or an upgrade explainer when the
 *  plan lacks instant voice cloning. Called on every open (after status fetch). */
function renderCloneZone(canClone) {
    const inner = document.getElementById('vlabCloneInner');
    if (!inner) return;

    if (!canClone) {
        inner.innerHTML = `
            <div class="vlab-upgrade">
              <p>Instant Voice Cloning isn't available on your current plan.</p>
              <p class="vlab-upgrade-sub">Upgrade to ElevenLabs <strong>Starter</strong> or higher to clone a
                voice from a recording or audio files. Designing a voice from text and managing your
                voices are available on every plan.</p>
            </div>`;
        return;
    }

    inner.innerHTML = `
        <p class="vlab-zone-hint">Record yourself (aim for ${CLONE_TARGET_SECS}s+ of clear speech) or upload audio files.</p>
        <div class="vlab-clone-methods">
          <!-- Record -->
          <div class="vlab-method">
            <div class="vlab-method-label">Record</div>
            <div class="vlab-row">
              <button id="vlabRecBtn" class="vlab-btn vlab-rec-btn" type="button">● Record</button>
              <span id="vlabRecTimer" class="vlab-rec-timer">0:00</span>
              <span id="vlabRecHint" class="vlab-rec-hint"></span>
            </div>
            <div id="vlabRecPlayback" class="vlab-rec-playback" hidden>
              <button class="vlab-preview-btn" id="vlabRecPlayBtn" type="button" title="Play recording">▶</button>
              <span class="vlab-rec-meta" id="vlabRecMeta">Recorded clip ready</span>
              <button class="vlab-link-btn" id="vlabRecClearBtn" type="button">Discard</button>
            </div>
          </div>
          <!-- Upload -->
          <div class="vlab-method">
            <div class="vlab-method-label">Or upload</div>
            <input id="vlabUploadInput" class="vlab-file-input" type="file"
                   accept="audio/wav,audio/mpeg,audio/mp3,audio/x-m4a,audio/mp4,audio/webm,.wav,.mp3,.m4a,.webm"
                   multiple />
            <div id="vlabUploadList" class="vlab-upload-list"></div>
          </div>
        </div>
        <input id="vlabCloneName" class="vlab-input" type="text" placeholder="Name this voice" autocomplete="off" />
        <input id="vlabCloneDesc" class="vlab-input" type="text" placeholder="Description (optional)" autocomplete="off" />
        <label class="vlab-consent">
          <input id="vlabConsent" type="checkbox" />
          <span>I confirm I own this voice or have permission to clone it.</span>
        </label>
        <div class="vlab-row vlab-row-end">
          <button id="vlabCloneBtn" class="vlab-btn vlab-btn-accent" type="button" disabled>Clone voice</button>
        </div>`;

    // Wire the freshly-built form.
    inner.querySelector('#vlabRecBtn').addEventListener('click', toggleRecording);
    inner.querySelector('#vlabRecPlayBtn').addEventListener('click', (e) => {
        if (recordedBlob) togglePreview(URL.createObjectURL(recordedBlob), e.currentTarget);
    });
    inner.querySelector('#vlabRecClearBtn').addEventListener('click', clearRecording);
    inner.querySelector('#vlabUploadInput').addEventListener('change', onUploadChange);
    inner.querySelector('#vlabCloneName').addEventListener('input', refreshCloneButton);
    inner.querySelector('#vlabConsent').addEventListener('change', refreshCloneButton);
    inner.querySelector('#vlabCloneBtn').addEventListener('click', submitClone);
}

/** Clone button is enabled only when name AND audio (recording OR files) AND consent. */
function refreshCloneButton() {
    const btn = document.getElementById('vlabCloneBtn');
    if (!btn) return;
    const name = (document.getElementById('vlabCloneName')?.value || '').trim();
    const consent = !!document.getElementById('vlabConsent')?.checked;
    const hasAudio = !!recordedBlob || uploadedFiles.length > 0;
    btn.disabled = !(name && consent && hasAudio);
}

async function toggleRecording() {
    const btn = document.getElementById('vlabRecBtn');
    if (mediaRec && mediaRec.state === 'recording') {
        stopRecording();
        return;
    }
    // Start a fresh recording — clears any prior recorded clip.
    if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) {
        toastError('Microphone not supported in this browser');
        return;
    }
    try {
        // Same constraints the STT path uses (mono + echo/noise/gain) — ElevenLabs
        // cloning wants clean speech.
        mediaStream = await navigator.mediaDevices.getUserMedia({
            audio: {
                channelCount: 1,
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
            },
        });
    } catch (e) {
        console.error('getUserMedia failed', e);
        toastError(`Mic error: ${e.message || e.name}`);
        return;
    }

    recChunks = [];
    recordedBlob = null;
    document.getElementById('vlabRecPlayback')?.setAttribute('hidden', '');
    mediaRec = new MediaRecorder(mediaStream);
    mediaRec.ondataavailable = (e) => { if (e.data && e.data.size > 0) recChunks.push(e.data); };
    mediaRec.onstop = () => {
        // Stop the mic tracks so the OS recording indicator clears.
        if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
        recordedBlob = recChunks.length ? new Blob(recChunks, { type: 'audio/webm' }) : null;
        const playback = document.getElementById('vlabRecPlayback');
        if (recordedBlob && playback) {
            const elapsed = (Date.now() - recStartMs) / 1000;
            const meta = document.getElementById('vlabRecMeta');
            if (meta) meta.textContent = `Recorded clip (${fmtClock(elapsed)})`;
            playback.removeAttribute('hidden');
        }
        refreshCloneButton();
    };
    mediaRec.start();
    recStartMs = Date.now();
    btn.textContent = '■ Stop';
    btn.classList.add('recording');
    startRecTimer();
}

function startRecTimer() {
    clearInterval(recTimer);
    const timerEl = document.getElementById('vlabRecTimer');
    const hintEl = document.getElementById('vlabRecHint');
    recTimer = setInterval(() => {
        const elapsed = (Date.now() - recStartMs) / 1000;
        if (timerEl) timerEl.textContent = fmtClock(elapsed);
        if (hintEl) {
            hintEl.textContent = elapsed >= CLONE_TARGET_SECS
                ? '✓ enough for a good clone'
                : `${Math.ceil(CLONE_TARGET_SECS - elapsed)}s to target`;
        }
    }, 250);
}

function stopRecording() {
    clearInterval(recTimer);
    const btn = document.getElementById('vlabRecBtn');
    if (btn) { btn.textContent = '● Record'; btn.classList.remove('recording'); }
    if (mediaRec && mediaRec.state === 'recording') {
        mediaRec.stop();   // fires onstop -> builds the Blob, stops tracks
    }
}

function clearRecording() {
    stopPreview();
    recordedBlob = null;
    recChunks = [];
    document.getElementById('vlabRecPlayback')?.setAttribute('hidden', '');
    const timerEl = document.getElementById('vlabRecTimer');
    if (timerEl) timerEl.textContent = '0:00';
    const hintEl = document.getElementById('vlabRecHint');
    if (hintEl) hintEl.textContent = '';
    refreshCloneButton();
}

function onUploadChange(e) {
    uploadedFiles = Array.from(e.target.files || []);
    const list = document.getElementById('vlabUploadList');
    if (list) {
        list.innerHTML = uploadedFiles.length
            ? uploadedFiles.map(f => `<div class="vlab-upload-item">${escapeHtml(f.name)}</div>`).join('')
            : '';
    }
    refreshCloneButton();
}

async function submitClone() {
    const btn = document.getElementById('vlabCloneBtn');
    if (!btn || btn.disabled) return;
    const name = (document.getElementById('vlabCloneName').value || '').trim();
    const description = (document.getElementById('vlabCloneDesc').value || '').trim();

    const fd = new FormData();
    fd.append('name', name);
    fd.append('consent', 'true');
    if (description) fd.append('description', description);
    // Recording first (if present), then any uploaded files — all under "files".
    if (recordedBlob) fd.append('files', recordedBlob, 'recording.webm');
    for (const f of uploadedFiles) fd.append('files', f, f.name);

    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = 'Cloning…';
    try {
        const res = await fetch('/elevenlabs/voices/clone', { method: 'POST', body: fd });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const msg = (data && data.detail) ? data.detail : `HTTP ${res.status}`;
            toastError(`Clone failed: ${msg}`);
            btn.disabled = false;
            btn.textContent = original;
            return;
        }
        const note = data.requires_verification ? ' (verification required before use)' : '';
        toastSuccess(`Cloned "${name}"${note}`);
        // Reset the clone form, refresh My Voices + the picker, select the new voice.
        clearRecording();
        uploadedFiles = [];
        const upInput = document.getElementById('vlabUploadInput'); if (upInput) upInput.value = '';
        const upList = document.getElementById('vlabUploadList'); if (upList) upList.innerHTML = '';
        document.getElementById('vlabCloneName').value = '';
        document.getElementById('vlabCloneDesc').value = '';
        document.getElementById('vlabConsent').checked = false;
        btn.textContent = original;
        const newId = data.voice_id ? `elevenlabs:${data.voice_id}` : null;
        await Promise.all([loadMyVoices(), populateVoiceCatalog(newId)]);
    } catch (err) {
        toastError(`Clone failed: ${err.message}`);
        btn.disabled = false;
        btn.textContent = original;
    }
}

// =============================================================================
// Zone 2 — Design a voice
// =============================================================================

async function runDesign() {
    const modal = document.getElementById(MODAL_ID);
    if (!modal) return;
    const desc = (modal.querySelector('#vlabDesignDesc').value || '').trim();
    if (!desc) { toastError('Describe the voice you want first'); return; }

    const genBtn = modal.querySelector('#vlabDesignGenBtn');
    const previewsEl = modal.querySelector('#vlabDesignPreviews');
    const sampleEl = modal.querySelector('#vlabDesignSampleText');
    const saveRow = modal.querySelector('#vlabDesignSaveRow');

    // Reset any prior result/selection.
    stopPreview();
    selectedPreviewId = null;
    saveRow.hidden = true;
    sampleEl.hidden = true;
    previewsEl.innerHTML = '<div class="vlab-status">Generating previews…</div>';
    genBtn.disabled = true;
    const genOrig = genBtn.textContent;
    genBtn.textContent = 'Generating…';

    try {
        const res = await fetch('/elevenlabs/voices/design', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ voice_description: desc }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const msg = (data && data.detail) ? data.detail : `HTTP ${res.status}`;
            previewsEl.innerHTML = `<div class="vlab-status">Design failed: ${escapeHtml(msg)}</div>`;
            return;
        }
        const previews = data.previews || [];
        if (data.text) {
            sampleEl.textContent = `Sample: “${data.text}”`;
            sampleEl.hidden = false;
        }
        renderDesignPreviews(previews, previewsEl);
    } catch (err) {
        previewsEl.innerHTML = `<div class="vlab-status">Design failed: ${escapeHtml(err.message)}</div>`;
    } finally {
        genBtn.disabled = false;
        genBtn.textContent = genOrig;
    }
}

function renderDesignPreviews(previews, container) {
    if (!previews.length) {
        container.innerHTML = '<div class="vlab-status">No previews returned. Try a different description.</div>';
        return;
    }
    container.innerHTML = '';
    previews.forEach((p, i) => {
        const card = document.createElement('div');
        card.className = 'vlab-preview-card';
        const dur = p.duration_secs ? ` · ${Math.round(p.duration_secs)}s` : '';
        const lang = p.language ? ` · ${escapeHtml(p.language)}` : '';
        card.innerHTML = `
            <button class="vlab-preview-btn" type="button" title="Play preview" ${p.audio_url ? '' : 'disabled'}>▶</button>
            <div class="vlab-preview-meta">
              <div class="vlab-preview-name">Option ${i + 1}<span class="vlab-preview-sub">${dur}${lang}</span></div>
            </div>
            <button class="vlab-btn vlab-use-btn" type="button">Use this one</button>`;

        const playBtn = card.querySelector('.vlab-preview-btn');
        const useBtn = card.querySelector('.vlab-use-btn');
        if (p.audio_url) playBtn.addEventListener('click', () => togglePreview(p.audio_url, playBtn));
        useBtn.addEventListener('click', () => selectDesignPreview(p.generated_voice_id, card, container));
        container.appendChild(card);
    });
}

function selectDesignPreview(generatedVoiceId, card, container) {
    selectedPreviewId = generatedVoiceId;
    // Visually mark the chosen card.
    container.querySelectorAll('.vlab-preview-card').forEach(c => c.classList.remove('selected'));
    card.classList.add('selected');
    container.querySelectorAll('.vlab-use-btn').forEach(b => { b.textContent = 'Use this one'; });
    card.querySelector('.vlab-use-btn').textContent = '✓ Selected';

    // Reveal name + Save, default the name from the description.
    const modal = document.getElementById(MODAL_ID);
    const saveRow = modal.querySelector('#vlabDesignSaveRow');
    saveRow.hidden = false;
    const nameInput = modal.querySelector('#vlabDesignName');
    if (nameInput && !nameInput.value.trim()) {
        const desc = (modal.querySelector('#vlabDesignDesc').value || '').trim();
        nameInput.value = desc.slice(0, 40);
    }
    nameInput?.focus();
}

async function saveDesign() {
    if (!selectedPreviewId) { toastError('Pick a preview with "Use this one" first'); return; }
    const modal = document.getElementById(MODAL_ID);
    const name = (modal.querySelector('#vlabDesignName').value || '').trim();
    if (!name) { toastError('Name the voice before saving'); return; }
    const description = (modal.querySelector('#vlabDesignDesc').value || '').trim();

    const btn = modal.querySelector('#vlabDesignSaveBtn');
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = 'Saving…';
    try {
        const res = await fetch('/elevenlabs/voices/design/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ generated_voice_id: selectedPreviewId, name, description }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const msg = (data && data.detail) ? data.detail : `HTTP ${res.status}`;
            toastError(`Save failed: ${msg}`);
            btn.disabled = false;
            btn.textContent = orig;
            return;
        }
        toastSuccess(`Saved "${name}" to your voices`);
        // Reset the design zone and refresh My Voices + picker (select the new voice).
        stopPreview();
        selectedPreviewId = null;
        modal.querySelector('#vlabDesignSaveRow').hidden = true;
        modal.querySelector('#vlabDesignPreviews').innerHTML = '';
        modal.querySelector('#vlabDesignSampleText').hidden = true;
        modal.querySelector('#vlabDesignDesc').value = '';
        modal.querySelector('#vlabDesignName').value = '';
        btn.disabled = false;
        btn.textContent = orig;
        const newId = data.voice_id ? `elevenlabs:${data.voice_id}` : null;
        await Promise.all([loadMyVoices(), populateVoiceCatalog(newId)]);
    } catch (err) {
        toastError(`Save failed: ${err.message}`);
        btn.disabled = false;
        btn.textContent = orig;
    }
}

// =============================================================================
// Zone 3 — My Voices (manage)
// =============================================================================

async function loadMyVoices() {
    const listEl = document.getElementById('vlabMyList');
    const statusEl = document.getElementById('vlabMyStatus');
    if (!listEl) return;
    stopPreview();
    if (statusEl) statusEl.textContent = 'Loading…';
    listEl.innerHTML = '';
    try {
        const res = await fetch('/elevenlabs/voices');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const mine = data.my_voices || [];
        if (statusEl) statusEl.textContent = mine.length
            ? `${mine.length} voice${mine.length === 1 ? '' : 's'}`
            : '';
        if (!mine.length) {
            listEl.innerHTML = '<div class="vlab-empty">No custom voices yet. Clone or design one above.</div>';
            return;
        }
        for (const v of mine) renderMyVoiceRow(v, listEl);
    } catch (err) {
        if (statusEl) statusEl.textContent = '';
        listEl.innerHTML = `<div class="vlab-empty">Failed to load voices: ${escapeHtml(err.message)}</div>`;
    }
}

function renderMyVoiceRow(v, listEl) {
    const row = document.createElement('div');
    row.className = 'vlab-voice-row';
    row.innerHTML = `
        <button class="vlab-preview-btn" type="button" title="Preview" ${v.preview_url ? '' : 'disabled'}>▶</button>
        <div class="vlab-voice-main">
          <div class="vlab-voice-name">${escapeHtml(v.name || 'Unnamed voice')}</div>
          ${v.description ? `<div class="vlab-voice-desc">${escapeHtml(v.description)}</div>` : ''}
        </div>
        <button class="vlab-btn vlab-delete-btn" type="button">Delete</button>`;

    const previewBtn = row.querySelector('.vlab-preview-btn');
    const deleteBtn = row.querySelector('.vlab-delete-btn');
    if (v.preview_url) previewBtn.addEventListener('click', () => togglePreview(v.preview_url, previewBtn));
    deleteBtn.addEventListener('click', () => deleteVoice(v, deleteBtn));
    listEl.appendChild(row);
}

async function deleteVoice(v, btn) {
    if (btn.disabled) return;
    if (!window.confirm(`Delete voice "${v.name || v.id}"? This cannot be undone.`)) return;
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = 'Deleting…';
    try {
        const res = await fetch(`/elevenlabs/voices/${encodeURIComponent(v.id)}`, { method: 'DELETE' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) {
            const msg = (data && data.detail) ? data.detail : `HTTP ${res.status}`;
            toastError(`Delete failed: ${msg}`);
            btn.disabled = false;
            btn.textContent = orig;
            return;
        }
        const inUse = Array.isArray(data.in_use) ? data.in_use : [];
        if (inUse.length) {
            toastError(`Deleted "${v.name}" — was in use by: ${inUse.join(', ')}. Reassign their voice.`);
        } else {
            toastSuccess(`Deleted "${v.name}"`);
        }
        // Refresh the list + the picker (the deleted voice must drop out of both).
        await Promise.all([loadMyVoices(), populateVoiceCatalog()]);
    } catch (err) {
        toastError(`Delete failed: ${err.message}`);
        btn.disabled = false;
        btn.textContent = orig;
    }
}

// =============================================================================
// Zone 4 — Grok (xAI) voices (clone / list / delete). Gated on GET /xai/voices
// `configured` — no XAI key, or xAI unreachable, hides the whole zone. Cloned
// ids are Grok SESSION voices, so no populateVoiceCatalog() refresh here.
// =============================================================================

/** Clone button enabled only when name AND one file AND consent (mirrors Zone 1). */
function refreshXaiCloneButton() {
    const btn = document.getElementById('vlabXaiCloneBtn');
    if (!btn) return;
    const name = (document.getElementById('vlabXaiName')?.value || '').trim();
    const consent = !!document.getElementById('vlabXaiConsent')?.checked;
    const hasFile = !!(document.getElementById('vlabXaiFile')?.files || []).length;
    btn.disabled = !(name && consent && hasFile);
}

async function submitXaiClone() {
    const btn = document.getElementById('vlabXaiCloneBtn');
    if (!btn || btn.disabled) return;
    const name = (document.getElementById('vlabXaiName').value || '').trim();
    const description = (document.getElementById('vlabXaiDesc').value || '').trim();
    const file = document.getElementById('vlabXaiFile').files[0];

    const fd = new FormData();
    fd.append('name', name);
    fd.append('consent', 'true');
    if (description) fd.append('description', description);
    fd.append('file', file, file.name);

    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = 'Cloning…';
    try {
        const res = await fetch('/xai/voices', { method: 'POST', body: fd });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const msg = (data && data.detail) ? data.detail : `HTTP ${res.status}`;
            toastError(`Clone failed: ${msg}`);
            btn.disabled = false;
            btn.textContent = original;
            return;
        }
        toastSuccess(`Cloned "${name}" — selectable as a Grok voice`);
        document.getElementById('vlabXaiName').value = '';
        document.getElementById('vlabXaiDesc').value = '';
        document.getElementById('vlabXaiFile').value = '';
        document.getElementById('vlabXaiConsent').checked = false;
        btn.textContent = original;
        await loadXaiVoices();
    } catch (err) {
        toastError(`Clone failed: ${err.message}`);
        btn.disabled = false;
        btn.textContent = original;
    }
}

/** Fetch /xai/voices; show the zone only when the box has a working XAI key. */
async function loadXaiVoices() {
    const zone = document.getElementById('vlabXaiZone');
    const listEl = document.getElementById('vlabXaiList');
    const statusEl = document.getElementById('vlabXaiStatus');
    if (!zone || !listEl) return;
    try {
        const res = await fetch('/xai/voices');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!data.configured) { zone.hidden = true; return; }
        zone.hidden = false;
        const voices = data.voices || [];
        if (statusEl) statusEl.textContent = voices.length
            ? `${voices.length} cloned voice${voices.length === 1 ? '' : 's'}` : '';
        listEl.innerHTML = voices.length ? '' : '<div class="vlab-empty">No cloned Grok voices yet.</div>';
        for (const v of voices) renderXaiVoiceRow(v, listEl);
    } catch {
        zone.hidden = true;   // fail quiet — unreachable == unconfigured for the UI
    }
}

function renderXaiVoiceRow(v, listEl) {
    const id = v.voice_id || v.id || '';
    const row = document.createElement('div');
    row.className = 'vlab-voice-row';
    row.innerHTML = `
        <div class="vlab-voice-main">
          <div class="vlab-voice-name">${escapeHtml(v.name || 'Unnamed voice')}</div>
          <div class="vlab-voice-desc">${escapeHtml(id)}</div>
        </div>
        <button class="vlab-btn vlab-delete-btn" type="button">Delete</button>`;
    row.querySelector('.vlab-delete-btn').addEventListener('click',
        (e) => deleteXaiVoice(id, v.name, e.currentTarget));
    listEl.appendChild(row);
}

async function deleteXaiVoice(voiceId, name, btn) {
    if (btn.disabled) return;
    if (!window.confirm(`Delete Grok voice "${name || voiceId}"? This cannot be undone.`)) return;
    btn.disabled = true;
    try {
        const res = await fetch(`/xai/voices/${encodeURIComponent(voiceId)}`, { method: 'DELETE' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) {
            const msg = (data && data.detail) ? data.detail : `HTTP ${res.status}`;
            toastError(`Delete failed: ${msg}`);
            btn.disabled = false;
            return;
        }
        toastSuccess(`Deleted "${name || voiceId}"`);
        await loadXaiVoices();
    } catch (err) {
        toastError(`Delete failed: ${err.message}`);
        btn.disabled = false;
    }
}

// =============================================================================
// Zone 5 — Qwen3-TTS (On-Box): clone (consent) / design (preview→save) / manage.
// Gated on GET /local-models/status healthy (no API key). Mirrors the ElevenLabs
// zones; refreshes populateVoiceCatalog() after every mutation.
// =============================================================================
let qwenSelectedPreviewId = null;

/** Is the on-box TTS capability available? (defensive across M1 shapes.) */
async function qwenTabAvailable() {
    try {
        const res = await fetch('/local-models/status');
        if (!res.ok) return false;
        const s = await res.json();
        return !!(s && (s.healthy === true || s.status === 'healthy'
                 || (s.capabilities && s.capabilities.tts && s.capabilities.tts.enabled)));
    } catch { return false; }
}

function refreshQwenCloneButton() {
    const btn = document.getElementById('vlabQwenCloneBtn');
    if (!btn) return;
    const name = (document.getElementById('vlabQwenCloneName')?.value || '').trim();
    const consent = !!document.getElementById('vlabQwenConsent')?.checked;
    const hasFile = !!(document.getElementById('vlabQwenFile')?.files || []).length;
    btn.disabled = !(name && consent && hasFile);
}

async function submitQwenClone() {
    const btn = document.getElementById('vlabQwenCloneBtn');
    if (!btn || btn.disabled) return;
    const name = (document.getElementById('vlabQwenCloneName').value || '').trim();
    const file = document.getElementById('vlabQwenFile').files[0];
    const fd = new FormData();
    fd.append('name', name);
    fd.append('consent', 'true');
    fd.append('files', file, file.name);
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = 'Cloning…';
    try {
        const res = await fetch('/qwen/voices/clone', { method: 'POST', body: fd });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            toastError(`Clone failed: ${(data && data.detail) ? data.detail : 'HTTP ' + res.status}`);
            btn.disabled = false; btn.textContent = orig; return;
        }
        toastSuccess(`Cloned "${name}"`);
        document.getElementById('vlabQwenCloneName').value = '';
        document.getElementById('vlabQwenFile').value = '';
        document.getElementById('vlabQwenConsent').checked = false;
        btn.textContent = orig;
        const newId = data.voice_id ? `qwen:${data.voice_id}` : null;
        await Promise.all([loadQwenVoices(), populateVoiceCatalog(newId)]);
    } catch (err) {
        toastError(`Clone failed: ${err.message}`);
        btn.disabled = false; btn.textContent = orig;
    }
}

async function runQwenDesign() {
    const desc = (document.getElementById('vlabQwenDesignDesc').value || '').trim();
    if (!desc) { toastError('Describe the voice you want first'); return; }
    const genBtn = document.getElementById('vlabQwenDesignGenBtn');
    const previewsEl = document.getElementById('vlabQwenDesignPreviews');
    const saveRow = document.getElementById('vlabQwenDesignSaveRow');
    stopPreview();
    qwenSelectedPreviewId = null;
    saveRow.hidden = true;
    previewsEl.innerHTML = '<div class="vlab-status">Generating previews…</div>';
    genBtn.disabled = true;
    const genOrig = genBtn.textContent;
    genBtn.textContent = 'Generating…';
    try {
        const res = await fetch('/qwen/voices/design', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ voice_description: desc }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            previewsEl.innerHTML = `<div class="vlab-status">Design failed: ${escapeHtml((data && data.detail) || ('HTTP ' + res.status))}</div>`;
            return;
        }
        renderQwenDesignPreviews(data.previews || [], previewsEl);
    } catch (err) {
        previewsEl.innerHTML = `<div class="vlab-status">Design failed: ${escapeHtml(err.message)}</div>`;
    } finally {
        genBtn.disabled = false;
        genBtn.textContent = genOrig;
    }
}

function renderQwenDesignPreviews(previews, container) {
    if (!previews.length) {
        container.innerHTML = '<div class="vlab-status">No previews returned. Try a different description.</div>';
        return;
    }
    container.innerHTML = '';
    previews.forEach((p, i) => {
        // M6 /v1/voices/design returns previews as {generated_voice_id, audio_b64,
        // sample_rate} — build the playable data: URL from the base64 WAV here (the
        // member is loopback-only, so a data: URL is what the browser can play).
        // `audio_url` kept as a defensive fallback if the contract ever emits one.
        const audioUrl = p.audio_url
            || (p.audio_b64 ? `data:audio/wav;base64,${p.audio_b64}` : null);
        const card = document.createElement('div');
        card.className = 'vlab-preview-card';
        card.innerHTML = `
            <button class="vlab-preview-btn" type="button" title="Play preview" ${audioUrl ? '' : 'disabled'}>▶</button>
            <div class="vlab-preview-meta"><div class="vlab-preview-name">Option ${i + 1}</div></div>
            <button class="vlab-btn vlab-use-btn" type="button">Use this one</button>`;
        const playBtn = card.querySelector('.vlab-preview-btn');
        const useBtn = card.querySelector('.vlab-use-btn');
        if (audioUrl) playBtn.addEventListener('click', () => togglePreview(audioUrl, playBtn));
        useBtn.addEventListener('click', () => {
            qwenSelectedPreviewId = p.generated_voice_id;
            container.querySelectorAll('.vlab-preview-card').forEach(c => c.classList.remove('selected'));
            card.classList.add('selected');
            container.querySelectorAll('.vlab-use-btn').forEach(b => { b.textContent = 'Use this one'; });
            useBtn.textContent = '✓ Selected';
            const saveRow = document.getElementById('vlabQwenDesignSaveRow');
            saveRow.hidden = false;
            const nameInput = document.getElementById('vlabQwenDesignName');
            if (nameInput && !nameInput.value.trim()) {
                nameInput.value = (document.getElementById('vlabQwenDesignDesc').value || '').trim().slice(0, 40);
            }
            nameInput?.focus();
        });
        container.appendChild(card);
    });
}

async function saveQwenDesign() {
    if (!qwenSelectedPreviewId) { toastError('Pick a preview with "Use this one" first'); return; }
    const name = (document.getElementById('vlabQwenDesignName').value || '').trim();
    if (!name) { toastError('Name the voice before saving'); return; }
    const btn = document.getElementById('vlabQwenDesignSaveBtn');
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = 'Saving…';
    try {
        const res = await fetch('/qwen/voices/design/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ generated_voice_id: qwenSelectedPreviewId, name }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            toastError(`Save failed: ${(data && data.detail) ? data.detail : 'HTTP ' + res.status}`);
            btn.disabled = false; btn.textContent = orig; return;
        }
        toastSuccess(`Saved "${name}"`);
        stopPreview();
        qwenSelectedPreviewId = null;
        document.getElementById('vlabQwenDesignSaveRow').hidden = true;
        document.getElementById('vlabQwenDesignPreviews').innerHTML = '';
        document.getElementById('vlabQwenDesignDesc').value = '';
        document.getElementById('vlabQwenDesignName').value = '';
        btn.disabled = false; btn.textContent = orig;
        const newId = data.voice_id ? `qwen:${data.voice_id}` : null;
        await Promise.all([loadQwenVoices(), populateVoiceCatalog(newId)]);
    } catch (err) {
        toastError(`Save failed: ${err.message}`);
        btn.disabled = false; btn.textContent = orig;
    }
}

async function loadQwenVoices() {
    const listEl = document.getElementById('vlabQwenList');
    const statusEl = document.getElementById('vlabQwenStatus');
    if (!listEl) return;
    stopPreview();
    if (statusEl) statusEl.textContent = 'Loading…';
    listEl.innerHTML = '';
    try {
        const res = await fetch('/qwen/voices');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const mine = data.voices || [];
        if (statusEl) statusEl.textContent = mine.length ? `${mine.length} on-box voice${mine.length === 1 ? '' : 's'}` : '';
        if (!mine.length) {
            listEl.innerHTML = '<div class="vlab-empty">No on-box voices yet. Clone or design one above.</div>';
            return;
        }
        for (const v of mine) {
            const row = document.createElement('div');
            row.className = 'vlab-voice-row';
            row.innerHTML = `
                <div class="vlab-voice-main">
                  <div class="vlab-voice-name">${escapeHtml(v.name || v.slug)}</div>
                  <div class="vlab-voice-desc">${escapeHtml(v.variant || '')}</div>
                </div>
                <button class="vlab-btn vlab-delete-btn" type="button">Delete</button>`;
            row.querySelector('.vlab-delete-btn').addEventListener('click',
                (e) => deleteQwenVoice(v.slug, v.name, e.currentTarget));
            listEl.appendChild(row);
        }
    } catch (err) {
        if (statusEl) statusEl.textContent = '';
        listEl.innerHTML = `<div class="vlab-empty">Failed to load voices: ${escapeHtml(err.message)}</div>`;
    }
}

async function deleteQwenVoice(slug, name, btn) {
    if (btn.disabled) return;
    if (!window.confirm(`Delete on-box voice "${name || slug}"? This cannot be undone.`)) return;
    btn.disabled = true;
    try {
        const res = await fetch(`/qwen/voices/${encodeURIComponent(slug)}`, { method: 'DELETE' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) {
            toastError(`Delete failed: ${(data && data.detail) ? data.detail : 'HTTP ' + res.status}`);
            btn.disabled = false; return;
        }
        toastSuccess(`Deleted "${name || slug}"`);
        await Promise.all([loadQwenVoices(), populateVoiceCatalog()]);
    } catch (err) {
        toastError(`Delete failed: ${err.message}`);
        btn.disabled = false;
    }
}

// =============================================================================
// Open / close
// =============================================================================

/** Open the modal (build lazily, gate the clone zone on plan, load My Voices). */
export async function openVoiceLab() {
    const modal = ensureModal();
    modal.classList.remove('hide');

    // Gate the clone zone on plan features. Default to showing the form on a
    // fetch failure is risky (the POST would 400), so default to the upgrade
    // explainer and only show the form when instant_voice_cloning is true.
    let canClone = false;
    try {
        const res = await fetch('/elevenlabs/status');
        if (res.ok) {
            const s = await res.json();
            canClone = !!(s && s.features && s.features.instant_voice_cloning);
        }
    } catch { /* keep canClone=false -> upgrade explainer */ }

    // ElevenLabs zones (clone/design/manage) only make sense with a key —
    // hide all three on a Qwen-only box so the modal isn't full of 400s.
    const elevenOk = canClone || false;   // canClone already implies a working key
    const elevenConfigured = await (async () => {
        try {
            const r = await fetch('/elevenlabs/status');
            return r.ok ? !!(await r.json()).configured : false;
        } catch { return false; }
    })();
    document.getElementById('vlabCloneZone').hidden = !elevenConfigured;
    document.getElementById('vlabDesignZone').hidden = !elevenConfigured;
    document.getElementById('vlabManageZone').hidden = !elevenConfigured;
    if (elevenConfigured) {
        renderCloneZone(canClone);
        loadMyVoices();
    }

    // Gate + load the Grok (xAI) zone — hidden when no XAI key.
    loadXaiVoices();

    // Gate + load the on-box Qwen zone — hidden unless the local stack is healthy.
    const qwenOk = await qwenTabAvailable();
    document.getElementById('vlabQwenZone').hidden = !qwenOk;
    if (qwenOk) loadQwenVoices();
}

export function closeVoiceLab() {
    stopPreview();
    // If a recording is still in progress, stop it cleanly (and release the mic).
    if (mediaRec && mediaRec.state === 'recording') stopRecording();
    if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
    clearInterval(recTimer);
    const modal = document.getElementById(MODAL_ID);
    if (modal) modal.classList.add('hide');
}

/**
 * No-op-friendly init hook (kept for symmetry with the other modal modules). The
 * trigger button + status gating live in tts-stt.js's setupVoiceLabTrigger();
 * this module is otherwise driven entirely by openVoiceLab().
 */
export function initVoiceLab() { /* trigger wired in tts-stt.js */ }
