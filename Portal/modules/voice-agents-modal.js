/**
 * voice-agents-modal.js
 * Voice Agents launcher modal — provider tabs (GPT Realtime / Gemini Live /
 * Grok Live) wrapping the existing per-provider modules.
 *
 * Per docs/plans/2026-05-20-portal-tools-section-alignment.md Track 4.
 *
 * Brandon-locked decisions (2026-05-20):
 *  - Voice Agents are launched ONLY from this modal (chat-provider dropdown
 *    no longer offers realtime/gemini-live/grok-live entries).
 *  - Inline banners at index.html:159-352 deleted; this modal is the sole
 *    entrypoint.
 *  - WS lifecycle moves into the modal: closing the modal while connected
 *    disconnects every active provider (no invisible background voice WS).
 *
 * Implementation notes:
 *  - The three underlying modules (gpt-realtime.js / gemini-live.js /
 *    grok-live.js) were refactored to accept `{selectors}` at init time so
 *    the same module code can drive either the old inline banner (now gone)
 *    or these modal ids.
 *  - Provider modules are initialized lazily on first modal open — avoids
 *    catalog fetch on every page load when the user never opens the modal.
 *  - All Phase A/B/D behaviors preserved: catalog fetch + sessionStorage
 *    cache, VAD visibility toggles, audit I4 disable-on-CONNECTED, T14 F1
 *    reconnect state replay, long-form vad_sensitivity_start/_end field
 *    names, UPPERCASE VAD enum + lowercase thinking_level enum.
 */

import {
    initRealtimeUI,
    connect as realtimeConnect,
    disconnect as realtimeDisconnect,
} from './gpt-realtime.js';
import {
    initGeminiLiveUI,
    connect as geminiLiveConnect,
    disconnect as geminiLiveDisconnect,
} from './gemini-live.js';
import {
    initGrokLiveUI,
    connect as grokLiveConnect,
    disconnect as grokLiveDisconnect,
} from './grok-live.js';
import { refreshAllPresetDropdowns, initPresetManageUI, refreshManageUI } from './voice-presets.js';

// =============================================================================
// Per-provider selector tables — must match index.html va* ids exactly
// =============================================================================

// P6a — translation target languages: hardcoded top-20 BCP-47 + free-text
// "Other" (design doc workstream 5 — YAGNI, no backend catalog fetch).
const TRANSLATE_LANGUAGES = [
    ['en', 'English'], ['es', 'Spanish'], ['fr', 'French'], ['de', 'German'],
    ['it', 'Italian'], ['pt-BR', 'Portuguese (Brazil)'], ['ja', 'Japanese'],
    ['ko', 'Korean'], ['zh-CN', 'Chinese (Simplified)'], ['zh-TW', 'Chinese (Traditional)'],
    ['ar', 'Arabic'], ['hi', 'Hindi'], ['ru', 'Russian'], ['nl', 'Dutch'],
    ['pl', 'Polish'], ['tr', 'Turkish'], ['vi', 'Vietnamese'], ['th', 'Thai'],
    ['id', 'Indonesian'], ['uk', 'Ukrainian'],
];

function setupTranslateRow(toggleId, rowId, selectId, otherId) {
    const toggle = document.getElementById(toggleId);
    const row = document.getElementById(rowId);
    const select = document.getElementById(selectId);
    const other = document.getElementById(otherId);
    if (!toggle || !row || !select) return;
    if (!select.options.length) {
        for (const [tag, label] of TRANSLATE_LANGUAGES) {
            const opt = document.createElement('option');
            opt.value = tag;
            opt.textContent = `${label} (${tag})`;
            select.appendChild(opt);
        }
        const opt = document.createElement('option');
        opt.value = '__other__';
        opt.textContent = 'Other (type a BCP-47 tag)';
        select.appendChild(opt);
    }
    toggle.addEventListener('change', () => {
        row.style.display = toggle.checked ? '' : 'none';
    });
    select.addEventListener('change', () => {
        if (other) other.style.display = (select.value === '__other__') ? '' : 'none';
    });
}

const REALTIME_SELECTORS = {
    modelSelect: 'vaRealtimeModelSelect',
    presetSelect: 'vaRealtimePresetSelect',
    voiceSelect: 'vaRealtimeVoiceSelect',
    vadSelect: 'vaRealtimeVadSelect',
    eagernessSelect: 'vaRealtimeEagernessSelect',
    eagernessRow: 'vaRealtimeEagernessRow',
    idleTimeoutInput: 'vaRealtimeIdleTimeoutInput',
    idleRow: 'vaRealtimeIdleRow',
    translateToggle: 'vaRealtimeTranslateToggle',
    translateLangSelect: 'vaRealtimeTranslateLang',
    translateLangOther: 'vaRealtimeTranslateLangOther',
    noiseSelect: 'vaRealtimeNoiseSelect',
    connectButton: 'vaRealtimeConnect',
    micButton: 'vaRealtimeMic',
    disconnectButton: 'vaRealtimeDisconnect',
    statusEl: 'vaRealtimeStatus',
    // Modal has no inline transcript panel / banner / toggle / micText span —
    // null those so the module skips them gracefully.
    transcriptEl: null,
    transcriptSection: null,
    toggleButton: null,
    bannerEl: null,
    micText: null,
};

const GEMINI_SELECTORS = {
    modelSelect: 'vaGeminiModelSelect',
    presetSelect: 'vaGeminiPresetSelect',
    voiceSelect: 'vaGeminiVoiceSelect',
    vadStartSelect: 'vaGeminiVadStartSelect',
    vadEndSelect: 'vaGeminiVadEndSelect',
    thinkingSelect: 'vaGeminiThinkingSelect',
    thinkingRow: 'vaGeminiThinkingRow',
    affectiveToggle: 'vaGeminiAffectiveToggle',
    proactiveToggle: 'vaGeminiProactiveToggle',
    translateToggle: 'vaGeminiTranslateToggle',
    translateLangSelect: 'vaGeminiTranslateLang',
    translateLangOther: 'vaGeminiTranslateLangOther',
    connectButton: 'vaGeminiConnect',
    micButton: 'vaGeminiMic',
    disconnectButton: 'vaGeminiDisconnect',
    statusEl: 'vaGeminiStatus',
    transcriptEl: null,
    transcriptSection: null,
    toggleButton: null,
    bannerEl: null,
    micText: null,
};

const GROK_SELECTORS = {
    modelSelect: 'vaGrokModelSelect',
    reasoningSelect: 'vaGrokReasoningSelect',
    presetSelect: 'vaGrokPresetSelect',
    voiceSelect: 'vaGrokVoiceSelect',
    connectButton: 'vaGrokConnect',
    micButton: 'vaGrokMic',
    disconnectButton: 'vaGrokDisconnect',
    statusEl: 'vaGrokStatus',
    transcriptEl: null,
    transcriptSection: null,
    toggleButton: null,
    sendTextButton: null,
    textInput: null,
    interruptButton: null,
    speakingIndicator: null,
    micText: null,
};

// =============================================================================
// Provider module init — lazy on first modal open
// =============================================================================

let providersInitialized = false;

function ensureProvidersInit() {
    if (providersInitialized) return;
    initRealtimeUI({ selectors: REALTIME_SELECTORS });
    initGeminiLiveUI({ selectors: GEMINI_SELECTORS });
    initGrokLiveUI({ selectors: GROK_SELECTORS });
    initPresetManageUI();
    setupTranslateRow('vaRealtimeTranslateToggle', 'vaRealtimeTranslateLangRow',
        'vaRealtimeTranslateLang', 'vaRealtimeTranslateLangOther');
    setupTranslateRow('vaGeminiTranslateToggle', 'vaGeminiTranslateLangRow',
        'vaGeminiTranslateLang', 'vaGeminiTranslateLangOther');
    providersInitialized = true;
    console.log('[VOICE-AGENTS] Provider modules initialized');
}

// =============================================================================
// Tab switching
// =============================================================================

function setupTabs() {
    const modal = document.getElementById('voiceAgentsModal');
    if (!modal) return;
    const tabs = modal.querySelectorAll('.voice-agents-tabs .va-tab');
    const panes = modal.querySelectorAll('.voice-agents-body .va-pane');
    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const provider = tab.dataset.provider;
            tabs.forEach(t => t.classList.toggle('active', t === tab));
            panes.forEach(p => {
                const active = (p.dataset.pane === provider);
                p.style.display = active ? '' : 'none';
                p.classList.toggle('va-pane-active', active);
            });
        });
    });
}

// =============================================================================
// Modal open/close — close-while-connected disconnects every provider
// =============================================================================

function openModal() {
    ensureProvidersInit();
    refreshManageUI();
    const modal = document.getElementById('voiceAgentsModal');
    if (modal) modal.classList.remove('hide');
}

function closeModal() {
    const modal = document.getElementById('voiceAgentsModal');
    if (modal) modal.classList.add('hide');
    // Brandon-locked: WS lifecycle bound to modal. Disconnect every provider
    // on close — no invisible background voice connection. Each module's
    // disconnect() is a safe no-op when not connected.
    try { realtimeDisconnect(); } catch (e) { /* ignore */ }
    try { geminiLiveDisconnect(); } catch (e) { /* ignore */ }
    try { grokLiveDisconnect(); } catch (e) { /* ignore */ }
}

// =============================================================================
// Init — call from app-init.js
// =============================================================================

export function initVoiceAgentsModal() {
    setupTabs();
    document.getElementById('btnVoiceAgents')?.addEventListener('click', openModal);
    // Composer shortcut (ctlVoiceAgent) — the soundwave button in the chat
    // input bubble opens the SAME live voice-agent modal. Distinct from ctlMic
    // (Whisper dictation), which only transcribes into the text box.
    document.getElementById('ctlVoiceAgent')?.addEventListener('click', openModal);
    // Close button uses the existing .modal-close pattern + a dedicated
    // listener so we can run our disconnect-on-close hook.
    const modal = document.getElementById('voiceAgentsModal');
    modal?.querySelector('.modal-close')?.addEventListener('click', closeModal);
    console.log('[VOICE-AGENTS] Modal launcher wired');
}
