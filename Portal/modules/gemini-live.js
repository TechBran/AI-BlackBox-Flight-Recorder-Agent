/**
 * gemini-live.js
 * Gemini Live API WebSocket client with audio handling
 *
 * Provides real-time voice conversations with Gemini using semantic memory search.
 * Connects to Orchestrator which bridges to Google Gemini Live API.
 *
 * Features:
 * - Toggle-to-record audio input (click to start, click to stop)
 * - Real-time audio playback from AI (24kHz)
 * - Audio input at 16kHz (Gemini standard)
 * - Voice selection (Charon, Puck, Kore, Aoede, Fenrir, Orus)
 * - Text input support alongside voice
 * - AI interrupt capability (barge-in)
 * - Live transcript display
 * - Session snapshot to BlackBox on disconnect
 */

import { $, toast } from './core-utils.js';
import { getOperator, saveHistory } from './state-management.js';
import { addBubble, appendBubble } from './chat-bubbles.js';
import { pauseAllPlayers } from './tts-stt.js';
import { fetchVoicePresets, filterPresetsByProvider, populatePresetDropdown } from './voice-presets.js';
import { claimMic, releaseMic } from './mic-arbiter.js';
import {
    conditionMicFrame, createHighPassFilter, pcm16Base64ToFloat32, resamplePCM16Base64, upsampleAudio,
} from './audio-dsp.js';
import { hasNativeStreaming, NATIVE_MIC_SAMPLE_RATE, startNativeStreamAwaited, stopNativeStream } from './native-bridge.js';
import { micFrameSize, openMicCapture } from './mic-capture.js';
import { LiveSession } from './live-session.js';

/** This module's mic-arbiter owner id. */
const MIC_OWNER = 'gemini-live';

// =============================================================================
// Selector configuration
// =============================================================================
//
// Track 4 (2026-05-20): inline banners deprecated. Module reads DOM ids from
// the SEL table below; the Voice Agents modal passes its own ids via
// initGeminiLiveUI({selectors: {...}}) so the same code drives the modal.
// Defaults preserved for backward-compat; production uses modal-supplied ids.
const SEL = {
    voiceSelect: 'geminiVoiceSelect',
    modelSelect: 'geminiModelSelect',
    presetSelect: 'geminiPresetSelect',
    vadStartSelect: 'geminiVadStartSelect',
    vadEndSelect: 'geminiVadEndSelect',
    thinkingSelect: 'geminiThinkingSelect',
    thinkingRow: null,         // optional row wrapper for thinking
    affectiveToggle: 'geminiAffectiveToggle',
    proactiveToggle: 'geminiProactiveToggle',
    translateToggle: 'geminiTranslateToggle',
    translateLangSelect: 'geminiTranslateLang',
    translateLangOther: 'geminiTranslateLangOther',
    connectButton: 'geminiConnect',
    disconnectButton: 'geminiDisconnect',
    micButton: 'geminiMic',
    micText: 'geminiMicText',
    statusEl: 'geminiLiveStatus',
    transcriptEl: 'geminiTranscript',
    transcriptSection: 'geminiTranscriptSection',
    toggleButton: 'geminiToggleBtn',
    bannerEl: 'geminiLiveBanner',
};

// =============================================================================
// State
// =============================================================================

/** Selected voice */
let selectedVoice = 'Charon';

/**
 * Live-models-v2 config — captured at first connect, replayed on reconnect.
 * Without this, a network blip silently downgrades the user-selected
 * model / VAD shape / thinking level to the backend env defaults (T14 F1).
 * Mirrors the persistence pattern already used for selectedVoice.
 */
let currentGeminiModel = null;
let currentGeminiVadStart = null;
let currentGeminiVadEnd = null;
let currentGeminiThinkingLevel = null;
let currentGeminiAffective = false;
let currentGeminiProactive = false;
let currentGeminiPresetId = null;
let currentGeminiTranslateMode = null;   // 'translate' | null (P6a)
let currentGeminiTargetLanguage = null;  // BCP-47 | null

/** Audio context for playback (native rate for best quality) */
let playbackContext = null;

/** Native playback sample rate */
let playbackSampleRate = 48000;

/** Audio context for recording (native rate), kept across captures */
let recordingContext = null;

/** The open browser mic capture (mic-capture.js), while recording */
let micCapture = null;

/** Gemini's input rate: the bridge sends audio/pcm;rate=16000 */
const GEMINI_INPUT_SAMPLE_RATE = 16000;

/** The model's output rate */
const GEMINI_OUTPUT_SAMPLE_RATE = 24000;

/** Whether currently recording */
let isRecording = false;

/** Whether AI is currently speaking */
let isAISpeaking = false;

/** Timestamp when AI stopped speaking (for post-speech delay) */
let aiStoppedSpeakingAt = 0;

/** Delay in ms after AI stops speaking before accepting mic input (prevents feedback) */
const POST_SPEECH_DELAY_MS = 800;

/** Whether using native Android recording */
let usingNativeRecording = false;

/** Whether connected to Gemini Live API */
let isConnected = false;

/** Current audio source being played */
let currentAudioSource = null;

/** Transcript buffer for current response */
let transcriptBuffer = '';

/** Accumulated interim user-transcript text for the in-progress utterance */
let userTranscriptBuffer = '';

/** Transient (non-persisted) live user bubble built from interim deltas */
let liveUserBubble = null;

/** Accumulated audio samples for continuous playback */
let accumulatedSamples = new Float32Array(0);

/** Scheduled playback time for seamless audio */
let nextPlaybackTime = 0;

/** Whether we're currently buffering audio */
let isBufferingAudio = false;

/** Whether the response is complete (API sent response_complete) but audio may still be playing */
let responseCompleteReceived = false;

// Note: User audio transcription is now handled by the backend (Orchestrator)
// The backend buffers audio chunks, transcribes with Whisper on audio_commit,
// and sends user_transcript events to the Portal.

// =============================================================================
// Connection (live-session.js: keepalive, reconnect with backoff, teardown)
// =============================================================================

/** Whether mic was recording before disconnect (to resume after reconnect) */
let wasRecordingBeforeDisconnect = false;

/** Stored operator for reconnection */
let currentOperator = '';

const liveSession = new LiveSession({
    path: '/ws/gemini-live/',
    logPrefix: '[GEMINI-LIVE]',
    buildConnectMsg,
    onMessage: handleMessage,
    onStatus: updateStatus,
    onDropped: () => {
        isConnected = false;
        wasRecordingBeforeDisconnect = isRecording;
        if (isRecording) {
            stopRecordingAndSend();
        }
    },
    onClosed: () => {
        isConnected = false;
        updateStatus('Disconnected');
        updateUI();
    },
    onGiveUp: (exhausted) => {
        updateStatus('Disconnected');
        updateUI();
        if (exhausted) {
            toast('Gemini Live connection lost');
        }
    },
    onResumed: () => {
        // Resume recording after a reconnect if the mic was on when it dropped.
        if (wasRecordingBeforeDisconnect) {
            wasRecordingBeforeDisconnect = false;
            setTimeout(() => startRecording(), 500);
        }
    },
});

/**
 * The connect message — the first connect and every reconnect send the same
 * one (a reconnect that dropped fields silently downgraded the session to the
 * backend env defaults, T14 F1). Undefined fields are omitted.
 */
function buildConnectMsg() {
    const connectMsg = {
        type: 'connect',
        operator: currentOperator,
        voice: selectedVoice
    };
    if (currentGeminiModel) connectMsg.model = currentGeminiModel;
    if (currentGeminiVadStart) connectMsg.vad_sensitivity_start = currentGeminiVadStart;
    if (currentGeminiVadEnd) connectMsg.vad_sensitivity_end = currentGeminiVadEnd;
    if (currentGeminiThinkingLevel) connectMsg.thinking_level = currentGeminiThinkingLevel;
    if (currentGeminiAffective) connectMsg.affective = true;
    if (currentGeminiProactive) connectMsg.proactive = true;
    if (currentGeminiPresetId) connectMsg.agent = currentGeminiPresetId;
    if (currentGeminiTranslateMode) {
        connectMsg.mode = currentGeminiTranslateMode;
        connectMsg.target_language = currentGeminiTargetLanguage;
    }
    return connectMsg;
}

// =============================================================================
// Audio Playback
// =============================================================================

/**
 * Initialize audio context for playback at native sample rate
 */
async function initPlaybackContext() {
    if (!playbackContext) {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        playbackContext = new AudioContextClass();
        playbackSampleRate = playbackContext.sampleRate;
        console.log(`[GEMINI-LIVE] Playback context created at ${playbackSampleRate}Hz (native)`);
    }

    if (playbackContext.state === 'suspended') {
        await playbackContext.resume();
    }
}

/**
 * Play base64-encoded PCM16 audio with seamless scheduling
 * @param {string} base64Audio - Base64 encoded PCM16 at 24kHz
 */
async function playAudio(base64Audio) {
    await initPlaybackContext();

    // Decode PCM16 to float32 (at 24kHz), upsample to the native playback rate
    const float32 = upsampleAudio(pcm16Base64ToFloat32(base64Audio), GEMINI_OUTPUT_SAMPLE_RATE,
                                  playbackSampleRate);

    // Add to accumulated samples
    const newAccumulated = new Float32Array(accumulatedSamples.length + float32.length);
    newAccumulated.set(accumulatedSamples);
    newAccumulated.set(float32, accumulatedSamples.length);
    accumulatedSamples = newAccumulated;

    // Start playback if we have enough samples
    const minSamplesForPlayback = playbackSampleRate * 0.15;  // 150ms minimum buffer
    if (!currentAudioSource && accumulatedSamples.length >= minSamplesForPlayback) {
        playAccumulatedAudio();
    }
}

/**
 * Play all accumulated audio samples as a continuous stream
 */
function playAccumulatedAudio() {
    if (accumulatedSamples.length === 0) {
        currentAudioSource = null;
        isBufferingAudio = false;
        return;
    }

    const samplesToPlay = accumulatedSamples;
    accumulatedSamples = new Float32Array(0);

    const audioBuffer = playbackContext.createBuffer(1, samplesToPlay.length, playbackSampleRate);
    audioBuffer.copyToChannel(samplesToPlay, 0);

    const source = playbackContext.createBufferSource();
    source.buffer = audioBuffer;

    const gainNode = playbackContext.createGain();
    gainNode.gain.setValueAtTime(1.0, playbackContext.currentTime);
    source.connect(gainNode);
    gainNode.connect(playbackContext.destination);

    const currentTime = playbackContext.currentTime;
    const startTime = Math.max(currentTime + 0.001, nextPlaybackTime);

    source.start(startTime);
    nextPlaybackTime = startTime + audioBuffer.duration;
    currentAudioSource = source;

    source.onended = () => {
        if (accumulatedSamples.length > 0) {
            playAccumulatedAudio();
        } else {
            currentAudioSource = null;
            isBufferingAudio = false;

            // Audio playback actually finished - NOW we can say AI stopped speaking
            if (responseCompleteReceived) {
                console.log('[GEMINI-LIVE] Audio playback finished - setting aiStoppedSpeakingAt');
                isAISpeaking = false;
                aiStoppedSpeakingAt = Date.now();
                responseCompleteReceived = false;
                updateStatus('Ready');
            }
        }
    };
}

/**
 * Stop current audio playback
 */
function stopAudio() {
    if (currentAudioSource) {
        try {
            currentAudioSource.stop();
        } catch (e) {}
        currentAudioSource = null;
    }
    accumulatedSamples = new Float32Array(0);
    isBufferingAudio = false;
    nextPlaybackTime = 0;
}

// =============================================================================
// Native Android streaming (native-bridge.js)
// =============================================================================

async function stopOtherMicUsers() {
    // Dictation, the Gemini recorder and the other voice agents let go
    // through the shared mic arbiter (window.micOn/stopSTT never existed).
    await claimMic(MIC_OWNER, stopRecordingAndSend);
}

/**
 * Start native Android audio streaming: AndroidMic captures 24kHz PCM16,
 * resampled to Gemini's 16kHz.
 */
async function startNativeStreaming() {
    console.log('[GEMINI-LIVE] Calling AndroidMic.startAudioStreaming()...');
    await startNativeStreamAwaited(MIC_OWNER, {
        onStart: () => {
            console.log('[GEMINI-LIVE] Native streaming started');
            isRecording = true;
            usingNativeRecording = true;
            updateUI();
        },
        onError: (error) => {
            console.error('[GEMINI-LIVE] Native streaming error:', error);
            isRecording = false;
            usingNativeRecording = false;
            toast('Native audio error: ' + error);
        },
        onChunk: (base64Data) => {
            // Auto-mute while AI is speaking AND for POST_SPEECH_DELAY_MS after to prevent feedback
            const timeSinceAIStopped = Date.now() - aiStoppedSpeakingAt;
            const inPostSpeechDelay = timeSinceAIStopped < POST_SPEECH_DELAY_MS;

            if (liveSession.isOpen && isRecording && !isAISpeaking && !inPostSpeechDelay) {
                liveSession.send({
                    type: 'audio_input',
                    data: resamplePCM16Base64(base64Data, NATIVE_MIC_SAMPLE_RATE, GEMINI_INPUT_SAMPLE_RATE)
                });
            }
        },
        onStop: () => {
            console.log('[GEMINI-LIVE] Native streaming stopped');
            isRecording = false;
            usingNativeRecording = false;
            updateUI();
        },
    });
}

/**
 * Stop native Android audio streaming
 */
function stopNativeStreaming() {
    console.log('[GEMINI-LIVE] Stopping native audio streaming');
    stopNativeStream(MIC_OWNER);

    usingNativeRecording = false;
    isRecording = false;
    updateUI();
}

// =============================================================================
// Audio Recording
// =============================================================================

/**
 * Start audio recording - captures at native rate, resamples to 16kHz
 */
async function startRecording() {
    if (isRecording) return;

    try {
        await stopOtherMicUsers();

        // If native Android, use native streaming (bypasses WebView getUserMedia issues)
        if (hasNativeStreaming()) {
            console.log('[GEMINI-LIVE] Using native Android audio streaming');
            await startNativeStreaming();
            return;  // Native streaming handles everything
        }

        if (micCapture) {
            micCapture.close();
            micCapture = null;
            await new Promise(resolve => setTimeout(resolve, 100));
        }

        // Each capture gets its own high-pass state (it carries across frames).
        const highPass = createHighPassFilter();
        micCapture = await openMicCapture({
            context: recordingContext,
            frameSize: micFrameSize(),
            logPrefix: '[GEMINI-LIVE]',
            onFrame: (inputData, nativeRate) => {
                if (!isRecording || !liveSession.isOpen) return;

                // Auto-mute while AI is speaking AND for POST_SPEECH_DELAY_MS after to prevent feedback
                const timeSinceAIStopped = Date.now() - aiStoppedSpeakingAt;
                const inPostSpeechDelay = timeSinceAIStopped < POST_SPEECH_DELAY_MS;
                if (isAISpeaking || inPostSpeechDelay) return;

                // Noise gate, high-pass, capped-gain normalize, resample to 16kHz.
                const base64 = conditionMicFrame(inputData, {
                    sourceRate: nativeRate, targetRate: GEMINI_INPUT_SAMPLE_RATE, highPass,
                });
                if (base64) {
                    liveSession.send({ type: 'audio_input', data: base64 });
                }
            },
        });
        recordingContext = micCapture.context;

        isRecording = true;
        updateUI();
        console.log('[GEMINI-LIVE] Recording started');

    } catch (err) {
        console.error('[GEMINI-LIVE] Failed to start recording:', err);
        toast('Microphone access failed: ' + err.message);
    }
}

/**
 * Stop recording and send audio to AI
 */
function stopRecordingAndSend() {
    releaseMic(MIC_OWNER);
    if (!isRecording) return;

    console.log('[GEMINI-LIVE] Stopping recording...');

    // If using native Android streaming, use native stop
    if (usingNativeRecording) {
        stopNativeStreaming();
        // Send commit message to trigger AI response
        liveSession.send({ type: 'audio_commit' });
        updateUI();
        console.log('[GEMINI-LIVE] Native recording stopped, audio committed');
        return;
    }

    isRecording = false;

    if (micCapture) {
        micCapture.close();
        micCapture = null;
    }

    // Send commit message to trigger AI response
    liveSession.send({ type: 'audio_commit' });

    updateUI();
    console.log('[GEMINI-LIVE] Recording stopped, audio committed');
}

// =============================================================================
// WebSocket Connection
// =============================================================================

/**
 * Connect to Gemini Live API via Orchestrator
 * @param {string} operator - Operator name
 */
export async function connect(operator) {
    if (isConnected || liveSession.isConnecting) {
        console.log('[GEMINI-LIVE] Already connected or connecting');
        return;
    }

    operator = operator || getOperator();
    if (!operator) {
        toast('Select an operator first');
        return;
    }

    // Bubble audio and auto-TTS stop as the session starts: it plays through an
    // AudioContext, which tts-stt.js's one-player guard cannot see.
    pauseAllPlayers();

    // Get selected voice
    const voiceSelect = SEL.voiceSelect ? $(SEL.voiceSelect) : null;
    selectedVoice = voiceSelect ? voiceSelect.value : 'Charon';

    // Read new model + VAD + thinking config fields from dropdowns (audit M3 — no JS-side catalog)
    const modelSelect = SEL.modelSelect ? $(SEL.modelSelect) : null;
    const vadStartSelect = SEL.vadStartSelect ? $(SEL.vadStartSelect) : null;
    const vadEndSelect = SEL.vadEndSelect ? $(SEL.vadEndSelect) : null;
    const thinkingSelect = SEL.thinkingSelect ? $(SEL.thinkingSelect) : null;

    const selectedModel = (modelSelect && modelSelect.value) ? modelSelect.value : undefined;
    const vadSensitivityStart = (vadStartSelect && vadStartSelect.value) ? vadStartSelect.value : undefined;
    const vadSensitivityEnd = (vadEndSelect && vadEndSelect.value) ? vadEndSelect.value : undefined;
    // thinking_level only valid for gemini-3.1-flash-live-preview; omit otherwise
    const thinkingLevel = (selectedModel === 'gemini-3.1-flash-live-preview' && thinkingSelect && thinkingSelect.value)
        ? thinkingSelect.value : undefined;

    // affective/proactive only valid on the 2.5-native-audio family (v1alpha)
    const affectiveToggle = SEL.affectiveToggle ? $(SEL.affectiveToggle) : null;
    const proactiveToggle = SEL.proactiveToggle ? $(SEL.proactiveToggle) : null;
    const affective = !!(isAffectiveCapableModel(selectedModel) && affectiveToggle && affectiveToggle.checked);
    const proactive = !!(isAffectiveCapableModel(selectedModel) && proactiveToggle && proactiveToggle.checked);

    // Translation mode (P6a) — modal-only UI; selectors are null elsewhere.
    const translateToggle = SEL.translateToggle ? $(SEL.translateToggle) : null;
    const translateOn = !!(translateToggle && translateToggle.checked);
    let targetLanguage;
    if (translateOn) {
        const langSel = SEL.translateLangSelect ? $(SEL.translateLangSelect) : null;
        const otherInput = SEL.translateLangOther ? $(SEL.translateLangOther) : null;
        targetLanguage = (langSel && langSel.value === '__other__')
            ? ((otherInput && otherInput.value.trim()) || 'en')
            : ((langSel && langSel.value) || 'en');
    }

    // Voice Agent preset (P4 registry) — sent as `agent`; backend precedence
    // is explicit params > preset > defaults, so sending both is safe.
    const presetSelect = SEL.presetSelect ? $(SEL.presetSelect) : null;
    const presetId = (presetSelect && presetSelect.value) ? presetSelect.value : undefined;

    // Capture the config into module state: buildConnectMsg() sends it on the
    // first connect and replays it on every reconnect (T14 F1).
    currentGeminiModel = selectedModel || null;
    currentGeminiVadStart = vadSensitivityStart || null;
    currentGeminiVadEnd = vadSensitivityEnd || null;
    currentGeminiThinkingLevel = thinkingLevel || null;
    currentGeminiAffective = affective;
    currentGeminiProactive = proactive;
    currentGeminiPresetId = presetId || null;
    currentGeminiTranslateMode = translateOn ? 'translate' : null;   // P6a
    currentGeminiTargetLanguage = translateOn ? targetLanguage : null;

    // Reset session state
    accumulatedSamples = new Float32Array(0);
    isBufferingAudio = false;
    nextPlaybackTime = 0;

    currentOperator = operator;
    liveSession.connect();
}

/**
 * Disconnect from Gemini Live API
 */
export function disconnect() {
    // Note: Session saving is now handled by the backend on disconnect
    // This ensures all Whisper transcriptions are captured before saving

    // No auto-reconnect: tell the server, close the socket.
    liveSession.disconnect();

    stopRecordingAndSend();
    stopAudio();

    isConnected = false;
    updateStatus('Disconnected');
    updateUI();
}

/**
 * Handle incoming WebSocket message
 * @param {Object} msg - Message object
 */
function handleMessage(msg) {
    const type = msg.type;

    switch (type) {
        case 'connected':
            isConnected = true;
            updateStatus('Connected');
            updateUI();
            toast(`Gemini Live connected (${msg.data.voice})`);
            console.log('[GEMINI-LIVE] Connected:', msg.data);
            break;

        case 'setup_complete':
            console.log('[GEMINI-LIVE] Setup complete');
            break;

        case 'status':
            updateStatus(msg.data);
            break;

        case 'audio_delta':
            // Note: User audio transcription is now handled by the backend (Orchestrator)
            // via Whisper on audio_commit. The backend sends user_transcript events.
            isAISpeaking = true;
            updateStatus('Speaking...');
            // Initialize buffering if starting fresh
            if (!isBufferingAudio && !currentAudioSource) {
                isBufferingAudio = true;
                nextPlaybackTime = playbackContext ? playbackContext.currentTime : 0;
                accumulatedSamples = new Float32Array(0);
            }
            playAudio(msg.data);
            break;

        case 'transcript_delta':
            transcriptBuffer += msg.data;
            updateTranscript(transcriptBuffer);
            break;

        case 'text_delta':
            // Text-modality delta (backend may emit for text-only turns) —
            // same accumulation path as transcript_delta.
            transcriptBuffer += msg.data;
            updateTranscript(transcriptBuffer);
            break;

        case 'response_complete':
            // Mark response as complete - but audio may still be playing
            responseCompleteReceived = true;

            // If no audio is playing/buffered, we can immediately mark as done
            // Otherwise, the onended callback will handle it when audio finishes
            if (!currentAudioSource && accumulatedSamples.length === 0) {
                console.log('[GEMINI-LIVE] No audio playing - immediately marking AI stopped');
                isAISpeaking = false;
                aiStoppedSpeakingAt = Date.now();
                responseCompleteReceived = false;
                updateStatus('Ready');
            } else {
                console.log('[GEMINI-LIVE] Audio still playing - waiting for playback to finish');
                // Status will be updated by onended callback
            }

            if (transcriptBuffer.trim()) {
                addBubble('assistant', transcriptBuffer.trim());
            }
            transcriptBuffer = '';
            clearTranscript();
            break;

        case 'tool_call':
            updateStatus(`Searching: ${msg.data.arguments?.query || '...'}`);
            break;

        case 'tool_result':
            updateStatus('Processing results...');
            break;

        case 'image_task':
            console.log('[GEMINI-LIVE] Image generation task:', msg.data);
            // Track with task manager
            import('./task-manager.js').then(({ taskManager }) => {
                const { task_id, prompt, count } = msg.data;
                taskManager.addTask(task_id, 'image_generation', prompt, null, count || 1);
                console.log(`[GEMINI-LIVE] Tracking image generation: ${task_id}`);
            });
            break;

        case 'video_task':
            console.log('[GEMINI-LIVE] Video generation task:', msg.data);
            // Track with task manager
            import('./task-manager.js').then(({ taskManager }) => {
                const { task_id, prompt, duration, resolution } = msg.data;
                taskManager.addTask(task_id, 'video_generation', prompt, { duration, resolution });
                console.log(`[GEMINI-LIVE] Tracking video generation: ${task_id}`);
            });
            break;

        case 'music_task':
            console.log('[GEMINI-LIVE] Music generation task:', msg.data);
            // Track with task manager - count is 5th param for lyria_music
            import('./task-manager.js').then(({ taskManager }) => {
                const { task_id, prompt, sample_count } = msg.data;
                taskManager.addTask(task_id, 'lyria_music', prompt, null, sample_count || 1);
                console.log(`[GEMINI-LIVE] Tracking music generation: ${task_id}`);
            });
            break;

        case 'user_transcript_delta':
            // Incremental (interim) user transcription chunk — live word-by-
            // word. Mirrors grok-live.js/gpt-realtime.js: accumulate into a
            // buffer and render a transient (non-persisted) user bubble that
            // updates as chunks arrive; the final user_transcript commits it.
            // Pre-native-transcription backends never emit this — unchanged.
            if (msg.data) {
                userTranscriptBuffer += msg.data;
                if (!liveUserBubble) {
                    // appendBubble adds to the DOM WITHOUT persisting to
                    // history, so interim chunks don't spam localStorage.
                    liveUserBubble = appendBubble('user', userTranscriptBuffer);
                } else {
                    const span = liveUserBubble.querySelector('.bubble-text');
                    if (span) span.textContent = userTranscriptBuffer;
                }
            }
            break;

        case 'user_transcript':
            if (msg.data) {
                console.log('[GEMINI-LIVE] User voice transcript:', msg.data);
                if (liveUserBubble) {
                    // Drop the transient live bubble, then persist via
                    // addBubble so there's exactly one final bubble.
                    liveUserBubble.remove();
                    liveUserBubble = null;
                }
                addBubble('user', msg.data);
            }
            // Reset for the next utterance (covers delta-less backends too).
            userTranscriptBuffer = '';
            break;

        case 'speech_started':
            console.log('[GEMINI-LIVE] User speech detected');
            break;

        case 'speech_stopped':
            console.log('[GEMINI-LIVE] User speech stopped');
            break;

        case 'error':
            console.error('[GEMINI-LIVE] Error:', msg.data);
            toast('Gemini Live error: ' + msg.data);
            updateStatus('Error');
            break;

        case 'disconnected':
            isConnected = false;
            updateStatus('Disconnected');
            updateUI();
            break;

        case 'reconnecting':
            console.log('[GEMINI-LIVE] Backend reconnecting:', msg.data);
            updateStatus(`Reconnecting (${msg.data.attempt}/${msg.data.max})...`);
            break;

        case 'reconnected':
            console.log('[GEMINI-LIVE] Backend reconnected on attempt', msg.data.attempt);
            updateStatus('Connected');
            break;

        case 'pong':
            break;  // keepalive (live-session.js)

        default:
            console.log('[GEMINI-LIVE] Unknown message type:', type, msg);
    }
}

// =============================================================================
// Text Input
// =============================================================================

/**
 * Send text message
 * @param {string} text - Message text
 */
export function sendText(text) {
    if (!isConnected || !liveSession.isOpen) {
        toast('Not connected to Gemini Live API');
        return;
    }

    if (!text.trim()) return;

    addBubble('user', text);

    liveSession.send({
        type: 'text_input',
        text: text
    });

    updateStatus('Processing...');
}

/**
 * Interrupt AI response (barge-in)
 */
export function interrupt() {
    if (!isConnected || !liveSession.isOpen) return;

    stopAudio();
    isAISpeaking = false;
    aiStoppedSpeakingAt = Date.now();  // Track when AI stopped for post-speech delay
    responseCompleteReceived = false;  // Reset response state

    liveSession.send({
        type: 'interrupt'
    });

    updateStatus('Interrupted');
}

// =============================================================================
// UI Updates
// =============================================================================

function updateStatus(status) {
    const statusEl = SEL.statusEl ? $(SEL.statusEl) : null;
    if (statusEl) {
        statusEl.textContent = status;

        statusEl.className = 'gemini-status';
        if (status === 'Connected' || status === 'Ready') {
            statusEl.classList.add('status-connected');
        } else if (status === 'Recording...') {
            statusEl.classList.add('status-recording');
        } else if (status === 'Speaking...') {
            statusEl.classList.add('status-speaking');
        } else if (status === 'Connecting...' || status === 'Initializing...') {
            statusEl.classList.add('status-connecting');
        } else if (status === 'Error' || status === 'Disconnected') {
            statusEl.classList.add('status-error');
        }
    }
}

function updateTranscript(text) {
    const transcriptEl = SEL.transcriptEl ? $(SEL.transcriptEl) : null;
    if (transcriptEl) {
        transcriptEl.textContent = text;
        transcriptEl.scrollTop = transcriptEl.scrollHeight;
    }
}

function clearTranscript() {
    const transcriptEl = SEL.transcriptEl ? $(SEL.transcriptEl) : null;
    if (transcriptEl) {
        transcriptEl.textContent = '';
    }
}

function updateUI() {
    const banner = SEL.bannerEl ? $(SEL.bannerEl) : null;
    const connectBtn = SEL.connectButton ? $(SEL.connectButton) : null;
    const disconnectBtn = SEL.disconnectButton ? $(SEL.disconnectButton) : null;
    const micBtn = SEL.micButton ? $(SEL.micButton) : null;
    const micText = SEL.micText ? $(SEL.micText) : null;

    if (banner) {
        banner.classList.toggle('connected', isConnected);
        banner.classList.toggle('recording', isRecording);
    }

    if (connectBtn) {
        connectBtn.disabled = isConnected;
    }

    if (disconnectBtn) {
        disconnectBtn.disabled = !isConnected;
    }

    // Audit I4: model + vad_start + vad_end are schema-changing (setup-message-rebuild),
    // so they must be locked while CONNECTED. Voice + thinking_level stay always-enabled
    // (hot-swappable post-setup).
    const modelSelect = SEL.modelSelect ? $(SEL.modelSelect) : null;
    const vadStartSelect = SEL.vadStartSelect ? $(SEL.vadStartSelect) : null;
    const vadEndSelect = SEL.vadEndSelect ? $(SEL.vadEndSelect) : null;
    if (modelSelect) modelSelect.disabled = isConnected;
    if (vadStartSelect) vadStartSelect.disabled = isConnected;
    if (vadEndSelect) vadEndSelect.disabled = isConnected;

    // Affective/proactive are setup-binding (audit I4) AND URL-version-binding:
    // locked while connected; re-derive 2.5-family availability when idle.
    if (isConnected) {
        [SEL.affectiveToggle, SEL.proactiveToggle].forEach(id => {
            const t = id ? $(id) : null;
            if (t) t.disabled = true;
        });
    } else {
        updateGeminiAffectiveAvailability();
    }

    if (micBtn) {
        micBtn.disabled = !isConnected;
        micBtn.classList.toggle('recording', isRecording);

        if (micText) {
            micText.textContent = isRecording ? 'Stop' : 'Mic';
        }

        if (isRecording) {
            micBtn.title = 'Click to stop recording and send';
        } else {
            micBtn.title = 'Click to start recording';
        }
    }
}

// =============================================================================
// Toggle Recording
// =============================================================================

export function toggleRecording() {
    if (isRecording) {
        stopRecordingAndSend();
    } else {
        if (isAISpeaking) {
            interrupt();
        }
        startRecording();
        updateStatus('Recording...');
    }
}

// =============================================================================
// Initialization
// =============================================================================

/**
 * Fetch Gemini Live catalog from /gemini-live/status with 5min sessionStorage cache.
 * Mirrors state-management.js fetchAvailableModels() pattern (audit M3 — no JS-side catalog).
 * @returns {Promise<Object|null>} catalog object, or null on failure
 */
async function fetchGeminiLiveCatalog() {
    const CACHE_TTL_MS = 5 * 60 * 1000;  // 5 minutes
    const cacheKey = 'bb_gemini_live_catalog';

    try {
        const cached = JSON.parse(sessionStorage.getItem(cacheKey) || 'null');
        if (cached && Date.now() - cached.ts < CACHE_TTL_MS && cached.data) {
            console.log(`[GEMINI-LIVE] Catalog cache hit (age ${Math.round((Date.now() - cached.ts) / 1000)}s)`);
            return cached.data;
        }
    } catch (_) { /* corrupted cache */ }

    try {
        const res = await fetch('/gemini-live/status');
        if (res.ok) {
            const data = await res.json();
            try {
                sessionStorage.setItem(cacheKey, JSON.stringify({ ts: Date.now(), data }));
            } catch (_) { /* sessionStorage full or disabled */ }
            return data;
        }
    } catch (err) {
        console.error('[GEMINI-LIVE] Failed to fetch catalog:', err);
    }
    return null;
}

/**
 * Populate the Gemini model dropdown from catalog. Default = model_default.
 */
function populateGeminiModelDropdown(catalog) {
    const modelSelect = SEL.modelSelect ? $(SEL.modelSelect) : null;
    if (!modelSelect || !catalog || !Array.isArray(catalog.models)) return;
    modelSelect.innerHTML = '';
    catalog.models.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.id;
        opt.textContent = m.name || m.id;
        if (m.id === catalog.model_default) opt.selected = true;
        modelSelect.appendChild(opt);
    });
    console.log(`[GEMINI-LIVE] Model dropdown populated with ${catalog.models.length} entries, default=${catalog.model_default}`);
}

/**
 * Populate the Gemini voice dropdown from catalog (30 entries).
 * Label = `${name} (${descriptor})` when descriptor present, else just name.
 */
function populateGeminiVoiceDropdown(catalog) {
    const voiceSelect = SEL.voiceSelect ? $(SEL.voiceSelect) : null;
    if (!voiceSelect || !catalog || !Array.isArray(catalog.voices)) return;
    const descriptors = catalog.voice_descriptors || {};
    voiceSelect.innerHTML = '';
    catalog.voices.forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        const desc = descriptors[name];
        opt.textContent = desc ? `${name} (${desc})` : name;
        if (name === catalog.voice_default) opt.selected = true;
        voiceSelect.appendChild(opt);
    });
    console.log(`[GEMINI-LIVE] Voice dropdown populated with ${catalog.voices.length} entries, default=${catalog.voice_default}`);
}

/**
 * Toggle thinking-level dropdown visibility based on selected model (3.1 only).
 */
function updateGeminiThinkingVisibility() {
    const modelSelect = SEL.modelSelect ? $(SEL.modelSelect) : null;
    const thinkingSelect = SEL.thinkingSelect ? $(SEL.thinkingSelect) : null;
    const thinkingRow = SEL.thinkingRow ? $(SEL.thinkingRow) : null;
    if (!modelSelect || !thinkingSelect) return;
    const show = (modelSelect.value === 'gemini-3.1-flash-live-preview');
    const target = thinkingRow || thinkingSelect;
    target.style.display = show ? '' : 'none';
}

/** 2.5-native-audio family gate for affective dialog + proactive audio (v1alpha only). */
function isAffectiveCapableModel(model) {
    return typeof model === 'string' && model.startsWith('gemini-2.5-flash-native-audio');
}

/**
 * Enable the affective/proactive toggles only when a 2.5-native-audio model is
 * selected (backend allowlist: GEMINI_LIVE_AFFECTIVE_CAPABLE_MODELS). A disabled
 * toggle is also unchecked so switching to 3.1 can't smuggle stale flags.
 */
function updateGeminiAffectiveAvailability() {
    const modelSelect = SEL.modelSelect ? $(SEL.modelSelect) : null;
    if (!modelSelect) return;
    const capable = isAffectiveCapableModel(modelSelect.value);
    [SEL.affectiveToggle, SEL.proactiveToggle].forEach(id => {
        const t = id ? $(id) : null;
        if (!t) return;
        t.disabled = !capable;
        if (!capable) t.checked = false;
    });
}

/**
 * Initialize Gemini Live UI and event handlers.
 *
 * @param {Object} [config] - Optional configuration.
 * @param {Object} [config.selectors] - Override DOM ids (Track 4 modal hook).
 *   Any omitted key falls back to the historical inline-banner default in SEL.
 */
export function initGeminiLiveUI(config) {
    if (config && config.selectors && typeof config.selectors === 'object') {
        Object.assign(SEL, config.selectors);
    }
    const connectBtn = SEL.connectButton ? $(SEL.connectButton) : null;
    const disconnectBtn = SEL.disconnectButton ? $(SEL.disconnectButton) : null;
    const micBtn = SEL.micButton ? $(SEL.micButton) : null;
    const toggleBtn = SEL.toggleButton ? $(SEL.toggleButton) : null;
    const transcriptSection = SEL.transcriptSection ? $(SEL.transcriptSection) : null;
    const modelSelect = SEL.modelSelect ? $(SEL.modelSelect) : null;

    // Populate model + voice dropdowns from /gemini-live/status (5min sessionStorage cache)
    fetchGeminiLiveCatalog().then(catalog => {
        if (catalog) {
            populateGeminiModelDropdown(catalog);
            populateGeminiVoiceDropdown(catalog);
            updateGeminiThinkingVisibility();  // gate thinking dropdown after dropdown populated
            updateGeminiAffectiveAvailability();
        }
    });

    // Preset dropdown from GET /voice-agents (P4 registry; row hidden pre-P4)
    fetchVoicePresets().then(presets => {
        const presetSelect = SEL.presetSelect ? $(SEL.presetSelect) : null;
        populatePresetDropdown(presetSelect, filterPresetsByProvider(presets, ['gemini', 'gemini-live', 'google']));
    });

    // Wire model change → toggle thinking dropdown visibility
    if (modelSelect) {
        modelSelect.addEventListener('change', updateGeminiThinkingVisibility);
        modelSelect.addEventListener('change', updateGeminiAffectiveAvailability);
    }

    if (connectBtn) {
        connectBtn.onclick = () => {
            const operator = getOperator();
            connect(operator);
        };
    }

    if (disconnectBtn) {
        disconnectBtn.onclick = () => {
            disconnect();
        };
    }

    if (micBtn) {
        micBtn.onclick = () => {
            toggleRecording();
        };
    }

    if (toggleBtn && transcriptSection) {
        toggleBtn.onclick = () => {
            const isCollapsed = transcriptSection.classList.toggle('collapsed');
            toggleBtn.textContent = isCollapsed ? '▼' : '▲';
            toggleBtn.title = isCollapsed ? 'Show transcript' : 'Hide transcript';

            const history = $('history');
            if (history) {
                const baseHeaderPadding = 60;
                const bannerHeight = isCollapsed ? 50 : 200;
                history.style.paddingTop = `${baseHeaderPadding + bannerHeight}px`;
            }
        };
    }

    updateUI();
    console.log('[GEMINI-LIVE] UI initialized');
}

/**
 * Check if Gemini Live is available on server
 * @returns {Promise<boolean>}
 */
export async function checkGeminiLiveAvailable() {
    try {
        const res = await fetch('/gemini-live/status');
        if (res.ok) {
            const data = await res.json();
            return data.available;
        }
    } catch (err) {
        console.error('[GEMINI-LIVE] Failed to check availability:', err);
    }
    return false;
}

// =============================================================================
// Exports
// =============================================================================

export {
    isConnected,
    isRecording,
    isAISpeaking,
    selectedVoice
};
