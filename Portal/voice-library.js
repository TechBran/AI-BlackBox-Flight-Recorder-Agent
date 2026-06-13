/**
 * voice-library.js
 * ElevenLabs shared community voice-library browse modal (Task 19).
 *
 * Search the public ElevenLabs library, preview a voice (click-to-play, so the
 * WebKitGTK autoplay policy is satisfied — user-initiated), and add a voice to
 * THIS account. After a successful add, the backend busts its voices cache and
 * we re-run populateVoiceCatalog() so the new voice appears in "My Voices" in
 * the TTS picker (and gets selected).
 *
 * Key stays server-side: everything routes through the Orchestrator proxy
 *   GET  /elevenlabs/library?search=&page_size=&gender=&category=
 *   POST /elevenlabs/library/add {public_owner_id, voice_id, name}
 * The whole feature is gated on GET /elevenlabs/status `configured` (the
 * trigger button in tts-stt.js stays hidden without a key).
 *
 * The modal DOM is built lazily on first open (no static HTML in index.html);
 * the .modal / .modal-card / .modal-head shell matches the Portal convention.
 */

import { toastSuccess, toastError } from './modules/core-utils.js';
import { populateVoiceCatalog } from './modules/tts-stt.js';

const MODAL_ID = 'voiceLibraryModal';
const SEARCH_DEBOUNCE_MS = 350;

// Single shared <audio> element for previews — one stream at a time. Reused so
// starting a new preview can always cleanly stop the previous one (the
// audio-overlap-prevention pattern: pause + reset currentTime + clear onended).
let previewAudio = null;
let playingBtn = null;       // the ▶ button currently in the "playing" state
let searchTimer = null;

function escapeHtml(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/** Build "American · Male · Middle aged" from whatever sub-fields exist. */
function subLine(v) {
    const parts = [v.accent, v.gender, v.age]
        .filter(Boolean)
        .map(p => String(p).replace(/_/g, ' '));
    return parts.join(' · ');
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
        <div class="modal-card voice-library-card">
          <div class="modal-head">
            <h3>🔍 Voice Library</h3>
            <button class="modal-close" type="button" aria-label="Close">✕</button>
          </div>
          <div class="modal-body voice-library-body">
            <div class="voice-library-search-row">
              <input id="vlSearchInput" class="voice-library-search" type="text"
                     placeholder="Search the ElevenLabs community library (e.g. narrator, calm, british)…"
                     autocomplete="off" />
              <select id="vlGenderFilter" class="voice-library-filter" title="Filter by gender">
                <option value="">Any gender</option>
                <option value="male">Male</option>
                <option value="female">Female</option>
                <option value="neutral">Neutral</option>
              </select>
            </div>
            <div id="vlStatus" class="voice-library-status"></div>
            <div id="vlResults" class="voice-library-results"></div>
            <p class="voice-library-hint">Previews play one at a time. Adding a voice copies it
              into your account and selects it in the TTS picker. Your plan has a voice limit.</p>
          </div>
        </div>`;
    document.body.appendChild(modal);

    // Close affordances: the ✕ button + clicking the dim backdrop. Each modal
    // wires its own close (no central auto-binding), matching gmail-modal.js.
    modal.querySelector('.modal-close').addEventListener('click', closeVoiceLibrary);
    modal.addEventListener('click', (e) => { if (e.target === modal) closeVoiceLibrary(); });

    // Debounced search input.
    const input = modal.querySelector('#vlSearchInput');
    const gender = modal.querySelector('#vlGenderFilter');
    input.addEventListener('input', scheduleSearch);
    gender.addEventListener('change', () => runSearch());

    return modal;
}

function scheduleSearch() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => runSearch(), SEARCH_DEBOUNCE_MS);
}

/** Query the proxy and render result cards. */
async function runSearch() {
    const modal = document.getElementById(MODAL_ID);
    if (!modal) return;
    const search = modal.querySelector('#vlSearchInput').value.trim();
    const gender = modal.querySelector('#vlGenderFilter').value;
    const statusEl = modal.querySelector('#vlStatus');
    const resultsEl = modal.querySelector('#vlResults');

    statusEl.textContent = 'Searching…';
    const params = new URLSearchParams({ page_size: '30' });
    if (search) params.set('search', search);
    if (gender) params.set('gender', gender);

    try {
        const res = await fetch(`/elevenlabs/library?${params.toString()}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        renderResults(data.voices || [], statusEl, resultsEl, data.has_more);
    } catch (err) {
        statusEl.textContent = '';
        resultsEl.innerHTML =
            `<div class="voice-library-empty">Search failed: ${escapeHtml(err.message)}</div>`;
    }
}

function renderResults(voices, statusEl, resultsEl, hasMore) {
    stopPreview();  // any visible cards are being replaced
    if (!voices.length) {
        statusEl.textContent = '';
        resultsEl.innerHTML =
            `<div class="voice-library-empty">No voices found. Try a different search.</div>`;
        return;
    }
    statusEl.textContent =
        `${voices.length} voice${voices.length === 1 ? '' : 's'}${hasMore ? ' (refine to narrow)' : ''}`;
    resultsEl.innerHTML = '';

    for (const v of voices) {
        const card = document.createElement('div');
        card.className = 'vl-card';
        const sub = subLine(v);
        card.innerHTML = `
            <div class="vl-card-main">
              <div class="vl-card-name">${escapeHtml(v.name || 'Unnamed voice')}</div>
              ${sub ? `<div class="vl-card-sub">${escapeHtml(sub)}</div>` : ''}
              ${v.description ? `<div class="vl-card-desc">${escapeHtml(v.description)}</div>` : ''}
            </div>
            <div class="vl-card-actions">
              <button class="vl-preview-btn" type="button" title="Preview" ${v.preview_url ? '' : 'disabled'}>▶</button>
              <button class="vl-add-btn" type="button">+ Add to my voices</button>
            </div>`;

        const previewBtn = card.querySelector('.vl-preview-btn');
        const addBtn = card.querySelector('.vl-add-btn');

        if (v.preview_url) {
            previewBtn.addEventListener('click', () => togglePreview(v.preview_url, previewBtn));
        }
        addBtn.addEventListener('click', () => addVoice(v, addBtn));

        resultsEl.appendChild(card);
    }
}

/** Click-to-play preview with overlap prevention. */
function togglePreview(url, btn) {
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
    // User-initiated, so WebKitGTK autoplay policy is satisfied.
    previewAudio.play().catch((e) => {
        console.error('preview play failed', e);
        stopPreview();
        toastError('Preview failed to play');
    });
}

/** POST the add, then refresh the picker so the new voice shows in My Voices. */
async function addVoice(v, btn) {
    if (btn.disabled) return;
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = 'Adding…';
    try {
        const res = await fetch('/elevenlabs/library/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                public_owner_id: v.public_owner_id,
                voice_id: v.voice_id,
                name: v.name,
            }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) {
            const msg = (data && data.detail) ? data.detail : `HTTP ${res.status}`;
            toastError(`Add failed: ${msg}`);
            btn.disabled = false;
            btn.textContent = original;
            return;
        }
        toastSuccess(`Added "${v.name}" to your voices`);
        btn.textContent = '✓ Added';
        btn.classList.add('added');
        // Refresh the catalog so the new voice surfaces in My Voices, and select
        // it. The catalog id format is `elevenlabs:<voice_id>` (the id returned
        // is the NEW id in this account).
        const newId = data.voice_id ? `elevenlabs:${data.voice_id}` : null;
        await populateVoiceCatalog(newId);
    } catch (err) {
        toastError(`Add failed: ${err.message}`);
        btn.disabled = false;
        btn.textContent = original;
    }
}

/** Open the modal (build lazily, run an initial empty search). */
export function openVoiceLibrary() {
    const modal = ensureModal();
    modal.classList.remove('hide');
    const input = modal.querySelector('#vlSearchInput');
    // Initial load shows whatever the library returns for an empty query so the
    // modal is never blank; focus the search for immediate typing.
    runSearch();
    setTimeout(() => input.focus(), 50);
}

export function closeVoiceLibrary() {
    stopPreview();
    const modal = document.getElementById(MODAL_ID);
    if (modal) modal.classList.add('hide');
}

/**
 * No-op-friendly init hook (kept for symmetry with other modal modules and in
 * case app-init wants to wire it). The trigger button + status gating live in
 * tts-stt.js's setupVoiceLibraryTrigger(); this module is otherwise driven
 * entirely by openVoiceLibrary().
 */
export function initVoiceLibrary() { /* trigger wired in tts-stt.js */ }
