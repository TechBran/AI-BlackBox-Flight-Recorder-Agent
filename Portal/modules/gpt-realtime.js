/**
 * gpt-realtime.js
 * GPT-4o Realtime API WebSocket client with audio handling
 *
 * Provides real-time voice conversations with semantic memory search.
 * Connects to Orchestrator which bridges to OpenAI Realtime API.
 *
 * Features:
 * - Toggle-to-record audio input (click to start, click to stop)
 * - Real-time audio playback from AI
 * - Text input support alongside voice
 * - AI interrupt capability (barge-in)
 * - Live transcript display
 */

import { $, toast } from './core-utils.js';
import { getOperator, saveHistory } from './state-management.js';
import { addBubble, appendBubble } from './chat-bubbles.js';
import { pauseAllPlayers } from './tts-stt.js';
import { fetchVoicePresets, filterPresetsByProvider, populatePresetDropdown } from './voice-presets.js';
import { claimMic, releaseMic } from './mic-arbiter.js';
import {
    createHighPassFilter, float32ToPCM16Base64, normalizePeak, pcm16Base64ToFloat32, peakLevel,
    resampleAudio, rmsLevel, rmsLevelPCM16Base64, silencePCM16Base64, upsampleAudio,
} from './audio-dsp.js';
import { hasNativeStreaming, startNativeStreamAwaited, stopNativeStream } from './native-bridge.js';
import { micFrameSize, openMicCapture } from './mic-capture.js';
import { LiveSession } from './live-session.js';

/** This module's mic-arbiter owner id. */
const MIC_OWNER = 'realtime';

// =============================================================================
// Selector configuration
// =============================================================================
//
// Track 4 (2026-05-20): inline banners deprecated. Module reads DOM ids from
// the SEL table below; the Voice Agents modal passes its own ids via
// initRealtimeUI({selectors: {...}}) so the same code drives the modal.
// Defaults preserved for backward-compat in case any caller still relies on
// the historical ids — Track 4 deletes the inline banners outright, so in
// production every read goes through the modal-supplied ids.
const SEL = {
    voiceSelect: 'realtimeVoiceSelect',
    modelSelect: 'realtimeModelSelect',
    presetSelect: 'realtimePresetSelect',
    vadSelect: 'realtimeVadSelect',
    eagernessSelect: 'realtimeEagernessSelect',
    eagernessRow: null,        // optional row wrapper for eagerness
    idleTimeoutInput: 'realtimeIdleTimeout',
    idleRow: null,             // optional row wrapper for idle timeout
    noiseSelect: 'realtimeNoiseSelect',
    translateToggle: 'realtimeTranslateToggle',
    translateLangSelect: 'realtimeTranslateLang',
    translateLangOther: 'realtimeTranslateLangOther',
    connectButton: 'realtimeConnect',
    disconnectButton: 'realtimeDisconnect',
    micButton: 'realtimeMic',
    micText: 'realtimeMicText',
    statusEl: 'realtimeStatus',
    transcriptEl: 'realtimeTranscript',
    transcriptSection: 'realtimeTranscriptSection',
    toggleButton: 'realtimeToggleBtn',
    bannerEl: 'realtimeBanner',
};

// =============================================================================
// State
// =============================================================================

/** Selected voice */
let selectedVoice = 'ash';

/**
 * Live-models-v2 config — captured at first connect, replayed on reconnect.
 * Without this, a network blip silently downgrades the user-selected
 * model / VAD shape / idle-timeout to the backend env defaults (T14 F1).
 * Mirrors the persistence pattern already used for selectedVoice.
 */
let currentRealtimeModel = null;
let currentRealtimeVadType = null;
let currentRealtimeVadEagerness = null;
let currentRealtimeIdleTimeoutMs = null;
let currentRealtimeNoiseReduction = null;
let currentRealtimePresetId = null;
let currentRealtimeTranslateMode = null;   // 'translate' | null (P6a)
let currentRealtimeTargetLanguage = null;  // BCP-47 | null

/** Audio context for playback (native rate for best quality) */
let playbackContext = null;

/** Native playback sample rate */
let playbackSampleRate = 48000;

/** Audio context for recording (native rate), kept across captures */
let recordingContext = null;

/** The open browser mic capture (mic-capture.js), while recording */
let micCapture = null;

/** OpenAI Realtime's input and output rate (PCM16) */
const REALTIME_SAMPLE_RATE = 24000;

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

/** Whether connected to Realtime API */
let isConnected = false;

/** Current audio source being played */
let currentAudioSource = null;

/** Transcript buffer for current response */
let transcriptBuffer = '';

/** Current chat bubble for AI response */
let currentBubble = null;

/** Accumulated interim user-transcript text for the in-progress utterance */
let userTranscriptBuffer = '';

/** Transient (non-persisted) live user bubble built from interim deltas */
let liveUserBubble = null;

/** Whether we're currently buffering audio */
let isBufferingAudio = false;

/** Scheduled playback time for seamless audio */
let nextPlaybackTime = 0;

/** Whether the response is complete (API sent response_complete) but audio may still be playing */
let responseCompleteReceived = false;

/** Accumulated audio samples for continuous playback */
let accumulatedSamples = new Float32Array(0);

// =============================================================================
// Client-Side VAD (Voice Activity Detection)
// Gates audio before sending to OpenAI to prevent Whisper hallucinations
// =============================================================================

/** RMS energy threshold for speech detection (lowered from 0.025 — most browser mics produce 0.005-0.02 for speech) */
const VAD_SPEECH_RMS = 0.008;

/** Consecutive speech frames needed to activate (1 = instant activation on speech) */
const VAD_MIN_SPEECH_FRAMES = 1;

/** Consecutive silence frames needed to deactivate (allows natural pauses) */
const VAD_MAX_SILENCE_FRAMES = 8;

/** Number of pre-buffer frames to keep (catches speech onset) */
const VAD_PRE_BUFFER_SIZE = 3;

/** Frame counter for periodic RMS logging */
let vadLogCounter = 0;

/** Whether VAD considers user to be currently speaking */
let vadIsSpeaking = false;

/** Count of consecutive frames above speech threshold */
let vadSpeechFrameCount = 0;

/** Count of consecutive frames below speech threshold */
let vadSilenceFrameCount = 0;

/** Ring buffer of recent encoded audio frames for pre-buffering */
let vadPreBuffer = [];

/** High-pass filter of the browser capture (its state persists across frames) */
let micHighPass = createHighPassFilter();

/** 20 ms of PCM16 silence at 24kHz, for the end-of-speech burst */
const SILENCE_20MS_B64 = silencePCM16Base64(480);

/** Forget the client VAD's state (muted, or a new capture). */
function resetVad() {
    vadIsSpeaking = false;
    vadSpeechFrameCount = 0;
    vadSilenceFrameCount = 0;
    vadPreBuffer = [];
}

/**
 * The client VAD gate, for the browser and the native frames alike: speech
 * frames go out (with the pre-buffered onset), silence is held back, and the
 * end of speech is followed by ~800ms of silence so OpenAI's server VAD sees
 * the turn end (without it the server just waits).
 * @param {string} base64 - The frame, base64 PCM16 at 24kHz
 * @param {number} rms - The frame's RMS level (raw, before any filtering)
 */
function gateSpeechFrame(base64, rms) {
    const isSpeechFrame = rms >= VAD_SPEECH_RMS;

    if (isSpeechFrame) {
        vadSpeechFrameCount++;
        vadSilenceFrameCount = 0;
    } else {
        vadSilenceFrameCount++;
        vadSpeechFrameCount = 0;
    }

    if (!vadIsSpeaking) {
        // Not currently speaking — buffer frames and watch for speech onset
        vadPreBuffer.push(base64);
        if (vadPreBuffer.length > VAD_PRE_BUFFER_SIZE) {
            vadPreBuffer.shift();
        }

        // Activate speech if enough consecutive speech frames
        if (vadSpeechFrameCount >= VAD_MIN_SPEECH_FRAMES) {
            vadIsSpeaking = true;
            console.log(`[REALTIME-VAD] Speech detected (RMS: ${rms.toFixed(4)})`);

            // Flush pre-buffer to catch speech onset
            for (const bufferedFrame of vadPreBuffer) {
                liveSession.send({ type: 'audio_input', data: bufferedFrame });
            }
            vadPreBuffer = [];
        }
    }

    if (vadIsSpeaking) {
        // Currently speaking — send audio to OpenAI
        liveSession.send({ type: 'audio_input', data: base64 });

        // Check for speech end
        if (vadSilenceFrameCount >= VAD_MAX_SILENCE_FRAMES) {
            vadIsSpeaking = false;
            vadPreBuffer = [];
            console.log(`[REALTIME-VAD] Speech ended (${vadSilenceFrameCount} silent frames) — sending silence burst`);
            for (let burst = 0; burst < 40; burst++) {  // 40 * 20ms = 800ms
                liveSession.send({ type: 'audio_input', data: SILENCE_20MS_B64 });
            }
        }
    }
}

// =============================================================================
// Connection (live-session.js: keepalive, reconnect with backoff, teardown)
// =============================================================================

/** Whether mic was recording before disconnect (to resume after reconnect) */
let wasRecordingBeforeDisconnect = false;

/** Stored operator for reconnection */
let currentOperator = '';

const liveSession = new LiveSession({
    path: '/ws/realtime/',
    logPrefix: '[REALTIME]',
    buildConnectMsg,
    onMessage: handleMessage,
    onStatus: updateStatus,
    onDropped: () => {
        isConnected = false;
        wasRecordingBeforeDisconnect = isRecording;
        if (isRecording) {
            stopRecordingFully();
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
            toast('Realtime connection lost');
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
    if (currentRealtimeModel) connectMsg.model = currentRealtimeModel;
    if (currentRealtimeVadType) connectMsg.vad_type = currentRealtimeVadType;
    if (currentRealtimeVadEagerness) connectMsg.vad_eagerness = currentRealtimeVadEagerness;
    if (currentRealtimeIdleTimeoutMs !== null) connectMsg.idle_timeout_ms = currentRealtimeIdleTimeoutMs;
    if (currentRealtimeNoiseReduction) connectMsg.noise_reduction = currentRealtimeNoiseReduction;
    if (currentRealtimePresetId) connectMsg.agent = currentRealtimePresetId;
    if (currentRealtimeTranslateMode) {
        connectMsg.mode = currentRealtimeTranslateMode;
        connectMsg.target_language = currentRealtimeTargetLanguage;
    }
    return connectMsg;
}

// =============================================================================
// Audio Playback
// =============================================================================

/**
 * Initialize audio context for playback at native sample rate
 * Using native rate gives best quality - we upsample OpenAI's 24kHz ourselves
 */
async function initPlaybackContext() {
    if (!playbackContext) {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        // Let browser use native sample rate for best quality
        playbackContext = new AudioContextClass();
        playbackSampleRate = playbackContext.sampleRate;
        console.log(`[REALTIME] Playback context created at ${playbackSampleRate}Hz (native)`);
    }

    // Resume if suspended (browser autoplay policy)
    if (playbackContext.state === 'suspended') {
        await playbackContext.resume();
    }
}

/**
 * Play base64-encoded PCM16 audio with seamless scheduling
 * Uses continuous buffer approach for gapless playback
 * @param {string} base64Audio - Base64 encoded PCM16
 */
async function playAudio(base64Audio) {
    await initPlaybackContext();

    // Decode PCM16 to float32, upsample from 24kHz to the native playback rate
    const float32 = upsampleAudio(pcm16Base64ToFloat32(base64Audio), REALTIME_SAMPLE_RATE,
                                  playbackSampleRate);

    // Add to accumulated samples for continuous playback
    const newAccumulated = new Float32Array(accumulatedSamples.length + float32.length);
    newAccumulated.set(accumulatedSamples);
    newAccumulated.set(float32, accumulatedSamples.length);
    accumulatedSamples = newAccumulated;

    // If we have enough samples and no source playing, start playback
    const minSamplesForPlayback = playbackSampleRate * 0.15;  // 150ms minimum buffer
    if (!currentAudioSource && accumulatedSamples.length >= minSamplesForPlayback) {
        playAccumulatedAudio();
    }
}

/**
 * Play all accumulated audio samples as a continuous stream
 * Uses larger buffers to reduce scheduling overhead and gaps
 */
function playAccumulatedAudio() {
    if (accumulatedSamples.length === 0) {
        currentAudioSource = null;
        isBufferingAudio = false;
        return;
    }

    // Take all accumulated samples
    const samplesToPlay = accumulatedSamples;
    accumulatedSamples = new Float32Array(0);

    // Create audio buffer
    const audioBuffer = playbackContext.createBuffer(1, samplesToPlay.length, playbackSampleRate);
    audioBuffer.copyToChannel(samplesToPlay, 0);

    // Create source
    const source = playbackContext.createBufferSource();
    source.buffer = audioBuffer;

    // Connect through gain node for volume control
    const gainNode = playbackContext.createGain();
    gainNode.gain.setValueAtTime(1.0, playbackContext.currentTime);
    source.connect(gainNode);
    gainNode.connect(playbackContext.destination);

    // Schedule precisely after previous audio
    const currentTime = playbackContext.currentTime;
    const startTime = Math.max(currentTime + 0.001, nextPlaybackTime);  // Tiny offset to prevent overlap glitches

    source.start(startTime);
    nextPlaybackTime = startTime + audioBuffer.duration;
    currentAudioSource = source;

    source.onended = () => {
        // Check if more audio accumulated while this was playing
        if (accumulatedSamples.length > 0) {
            playAccumulatedAudio();
        } else {
            currentAudioSource = null;
            isBufferingAudio = false;

            // Audio playback actually finished - NOW we can say AI stopped speaking
            if (responseCompleteReceived) {
                console.log('[REALTIME] Audio playback finished - setting aiStoppedSpeakingAt');
                isAISpeaking = false;
                aiStoppedSpeakingAt = Date.now();
                responseCompleteReceived = false;
                updateStatus('Ready');
            }
        }
    };
}

/**
 * Queue audio chunk with buffering for smoother playback
 * Uses continuous accumulation for gapless audio
 * @param {string} base64Audio - Base64 encoded PCM16
 */
function queueAudioChunk(base64Audio) {
    // Initialize playback timing if this is a fresh start
    if (!isBufferingAudio && !currentAudioSource) {
        isBufferingAudio = true;
        nextPlaybackTime = playbackContext ? playbackContext.currentTime : 0;
        accumulatedSamples = new Float32Array(0);  // Clear accumulated samples
    }

    // Directly process and accumulate instead of queuing
    playAudio(base64Audio);
}

/**
 * Stop current audio playback
 */
function stopAudio() {
    if (currentAudioSource) {
        try {
            currentAudioSource.stop();
        } catch (e) {
            // Already stopped
        }
        currentAudioSource = null;
    }
    accumulatedSamples = new Float32Array(0);  // Clear accumulated samples
    isBufferingAudio = false;
    nextPlaybackTime = 0;
}

// =============================================================================
// Native Android streaming (native-bridge.js)
// =============================================================================

/**
 * Stop other modules that may be using the microphone (dictation, the Gemini
 * recorder, the other voice agents) through the shared mic arbiter.
 */
async function stopOtherMicUsers() {
    await claimMic(MIC_OWNER, stopRecordingFully);
}

/**
 * Start native Android audio streaming: AndroidMic captures 24kHz PCM16,
 * exactly what OpenAI Realtime takes (no resampling).
 */
async function startNativeStreaming() {
    console.log('[REALTIME] Calling AndroidMic.startAudioStreaming()...');
    await startNativeStreamAwaited(MIC_OWNER, {
        onStart: () => {
            console.log('[REALTIME] Native streaming started');
            isRecording = true;
            usingNativeRecording = true;
            updateUI();
        },
        onError: (error) => {
            console.error('[REALTIME] Native streaming error:', error);
            isRecording = false;
            usingNativeRecording = false;
            toast('Native audio error: ' + error);
        },
        onChunk: (base64Data) => {
            // Auto-mute while AI is speaking AND for POST_SPEECH_DELAY_MS after
            const timeSinceAIStopped = Date.now() - aiStoppedSpeakingAt;
            const inPostSpeechDelay = timeSinceAIStopped < POST_SPEECH_DELAY_MS;

            if (!liveSession.isOpen || !isRecording || isAISpeaking || inPostSpeechDelay) {
                resetVad();
                return;
            }
            // VAD gate on the frame's RMS energy (same as the browser path)
            gateSpeechFrame(base64Data, rmsLevelPCM16Base64(base64Data));
        },
        onStop: () => {
            console.log('[REALTIME] Native streaming stopped');
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
    console.log('[REALTIME] Stopping native audio streaming');
    stopNativeStream(MIC_OWNER);

    // Send commit to trigger AI response
    liveSession.send({ type: 'audio_commit' });

    isRecording = false;
    usingNativeRecording = false;
    updateUI();
    console.log('[REALTIME] Native streaming stopped, audio committed');
}

// =============================================================================
// Audio Recording
// =============================================================================

/**
 * Start audio recording - uses native Android streaming when available
 */
async function startRecording() {
    if (isRecording) return;

    try {
        // First, stop any other modules using the microphone
        await stopOtherMicUsers();

        // If native Android, use native streaming (bypasses WebView getUserMedia issues)
        if (hasNativeStreaming()) {
            console.log('[REALTIME] Using native Android audio streaming');
            await startNativeStreaming();
            return;  // Native streaming handles everything
        }

        // Release any existing capture
        if (micCapture) {
            console.log('[REALTIME] Releasing existing media stream...');
            micCapture.close();
            micCapture = null;
            await new Promise(resolve => setTimeout(resolve, 100));
        }

        // A new capture starts the high-pass filter and the VAD from rest.
        micHighPass = createHighPassFilter();
        resetVad();
        micCapture = await openMicCapture({
            context: recordingContext,
            frameSize: micFrameSize(),
            logPrefix: '[REALTIME]',
            // Samsung devices need the retries; the longer texts are this panel's.
            messages: {
                NotAllowedError: 'Microphone permission denied. Please allow microphone access.',
                NotFoundError: 'No microphone found. Please connect a microphone.',
                NotReadableError: 'Microphone is busy. Please close other apps or wait a moment.',
            },
            onFrame: (inputData, nativeRate) => {
                if (!isRecording || !liveSession.isOpen) return;

                // Auto-mute while AI is speaking AND for POST_SPEECH_DELAY_MS after to prevent feedback
                const timeSinceAIStopped = Date.now() - aiStoppedSpeakingAt;
                const inPostSpeechDelay = timeSinceAIStopped < POST_SPEECH_DELAY_MS;
                if (isAISpeaking || inPostSpeechDelay) {
                    // Reset VAD + filter state when muted
                    resetVad();
                    micHighPass.reset();
                    return;
                }

                // RMS on the RAW audio for the VAD decision (before any filtering)
                const rms = rmsLevel(inputData);
                const maxLevel = peakLevel(inputData);

                // Log RMS every ~2 seconds for diagnostics
                vadLogCounter++;
                if (vadLogCounter % 12 === 0) {
                    console.log(`[REALTIME-VAD] RMS: ${rms.toFixed(5)} | threshold: ${VAD_SPEECH_RMS} | speaking: ${vadIsSpeaking} | peak: ${maxLevel.toFixed(4)}`);
                }

                // High-pass (persistent state), capped-gain normalize, resample to 24kHz
                const normalizedData = normalizePeak(micHighPass.process(inputData), maxLevel);
                const base64 = float32ToPCM16Base64(resampleAudio(normalizedData, nativeRate, REALTIME_SAMPLE_RATE));

                gateSpeechFrame(base64, rms);
            },
        });
        recordingContext = micCapture.context;

        isRecording = true;
        updateUI();
        console.log('[REALTIME] Recording started');

    } catch (err) {
        console.error('[REALTIME] Failed to start recording:', err);
        toast('Microphone access failed: ' + err.message);
    }
}

/**
 * Mute recording - stop sending audio but keep mic open
 * Server-side VAD handles turn detection automatically
 */
function muteRecording() {
    if (!isRecording) return;

    console.log('[REALTIME] Muting mic (continuous mode)...');
    isRecording = false;
    // Don't release media stream - keep mic open for continuous mode
    // Don't send audio_commit - server VAD handles turn detection
    updateUI();
    console.log('[REALTIME] Mic muted - stream still active');
}

/**
 * Unmute recording - resume sending audio
 */
function unmuteRecording() {
    if (isRecording) return;
    if (!micCapture) {
        // The capture was released (or this is the native stream), need to restart
        console.log('[REALTIME] Media stream not active, starting fresh...');
        startRecording();
        return;
    }

    console.log('[REALTIME] Unmuting mic...');
    isRecording = true;
    updateUI();
    updateStatus('Listening...');
    console.log('[REALTIME] Mic unmuted - resuming audio stream');
}

/**
 * Fully stop recording and release resources (for disconnect)
 */
function stopRecordingFully() {
    console.log('[REALTIME] Fully stopping recording...');
    releaseMic(MIC_OWNER);

    // If using native Android streaming, use native stop
    if (usingNativeRecording) {
        stopNativeStreaming();
        isRecording = false;
        return;
    }

    // Stop recording
    isRecording = false;

    // Release the capture (audio nodes, mic tracks)
    if (micCapture) {
        console.log('[REALTIME] Releasing media stream tracks...');
        micCapture.close();
        micCapture = null;
        console.log('[REALTIME] Media stream released - mic free');
    }

    updateUI();
    console.log('[REALTIME] Recording fully stopped');
}

// =============================================================================
// WebSocket Connection
// =============================================================================

/**
 * Connect to Realtime API via Orchestrator
 * @param {string} operator - Operator name
 */
export async function connect(operator) {
    if (isConnected || liveSession.isConnecting) {
        console.log('[REALTIME] Already connected or connecting');
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

    // Reset session state for new connection
    isBufferingAudio = false;
    nextPlaybackTime = 0;

    // Reset VAD state
    resetVad();

    // Get selected voice
    const voiceSelect = SEL.voiceSelect ? $(SEL.voiceSelect) : null;
    selectedVoice = voiceSelect ? voiceSelect.value : 'ash';

    // Read new model + VAD config fields from dropdowns (audit M3 — no JS-side catalog)
    const modelSelect = SEL.modelSelect ? $(SEL.modelSelect) : null;
    const vadSelect = SEL.vadSelect ? $(SEL.vadSelect) : null;
    const eagernessSelect = SEL.eagernessSelect ? $(SEL.eagernessSelect) : null;
    const idleTimeoutInput = SEL.idleTimeoutInput ? $(SEL.idleTimeoutInput) : null;

    const selectedModel = (modelSelect && modelSelect.value) ? modelSelect.value : undefined;
    const vadType = (vadSelect && vadSelect.value) ? vadSelect.value : undefined;
    const vadEagerness = (vadType === 'semantic_vad' && eagernessSelect && eagernessSelect.value)
        ? eagernessSelect.value : undefined;
    const idleTimeoutRaw = (vadType === 'server_vad' && idleTimeoutInput && idleTimeoutInput.value)
        ? parseInt(idleTimeoutInput.value, 10) : NaN;
    const idleTimeoutMs = Number.isFinite(idleTimeoutRaw) ? idleTimeoutRaw : undefined;

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

    // noise_reduction (near_field|far_field|off) — gpt-realtime-2.1 feature (P2)
    const noiseSelect = SEL.noiseSelect ? $(SEL.noiseSelect) : null;
    const noiseReduction = (noiseSelect && noiseSelect.value) ? noiseSelect.value : undefined;

    // Voice Agent preset (P4 registry) — sent as `agent`; backend precedence
    // is explicit params > preset > defaults, so sending both is safe.
    const presetSelect = SEL.presetSelect ? $(SEL.presetSelect) : null;
    const presetId = (presetSelect && presetSelect.value) ? presetSelect.value : undefined;
    currentRealtimePresetId = presetId || null;

    // Capture the config into module state: buildConnectMsg() sends it on the
    // first connect and replays it on every reconnect (T14 F1).
    currentRealtimeModel = selectedModel || null;
    currentRealtimeVadType = vadType || null;
    currentRealtimeVadEagerness = vadEagerness || null;
    currentRealtimeIdleTimeoutMs = (idleTimeoutMs !== undefined) ? idleTimeoutMs : null;
    currentRealtimeNoiseReduction = noiseReduction || null;
    currentRealtimeTranslateMode = translateOn ? 'translate' : null;   // P6a
    currentRealtimeTargetLanguage = translateOn ? targetLanguage : null;

    currentOperator = operator;
    liveSession.connect();
}

/**
 * Disconnect from Realtime API
 */
export function disconnect() {
    // NOTE: Session transcript is saved by the BACKEND when WebSocket closes

    // No auto-reconnect: tell the server, close the socket.
    liveSession.disconnect();

    stopRecordingFully();
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
            toast('GPT-4o Realtime connected');
            console.log('[REALTIME] Connected:', msg.data);
            break;

        case 'status':
            updateStatus(msg.data);
            break;

        case 'audio_delta':
            // Queue audio for buffered playback (smoother)
            isAISpeaking = true;
            updateStatus('Speaking...');
            queueAudioChunk(msg.data);
            break;

        case 'transcript_delta':
            // Update transcript display
            transcriptBuffer += msg.data;
            updateTranscript(transcriptBuffer);
            break;

        case 'text_delta':
            // Text response (for text input mode)
            transcriptBuffer += msg.data;
            updateTranscript(transcriptBuffer);
            break;

        case 'response_complete':
            // Mark response as complete - but audio may still be playing
            responseCompleteReceived = true;

            // If no audio is playing/buffered, we can immediately mark as done
            // Otherwise, the onended callback will handle it when audio finishes
            if (!currentAudioSource && accumulatedSamples.length === 0) {
                console.log('[REALTIME] No audio playing - immediately marking AI stopped');
                isAISpeaking = false;
                aiStoppedSpeakingAt = Date.now();
                responseCompleteReceived = false;
                updateStatus('Ready');
            } else {
                console.log('[REALTIME] Audio still playing - waiting for playback to finish');
                // Status will be updated by onended callback
            }

            // Add complete response to chat history and session log (persisted to localStorage)
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
            console.log('[REALTIME] Image generation task:', msg.data);
            // Track with task manager
            import('./task-manager.js').then(({ taskManager }) => {
                const { task_id, prompt, count } = msg.data;
                taskManager.addTask(task_id, 'image_generation', prompt, null, count || 1);
                console.log(`[REALTIME] Tracking image generation: ${task_id}`);
            });
            break;

        case 'video_task':
            console.log('[REALTIME] Video generation task:', msg.data);
            // Track with task manager
            import('./task-manager.js').then(({ taskManager }) => {
                const { task_id, prompt, duration, resolution } = msg.data;
                taskManager.addTask(task_id, 'video_generation', prompt, { duration, resolution });
                console.log(`[REALTIME] Tracking video generation: ${task_id}`);
            });
            break;

        case 'music_task':
            console.log('[REALTIME] Music generation task:', msg.data);
            // Track with task manager - count is 5th param for lyria_music
            import('./task-manager.js').then(({ taskManager }) => {
                const { task_id, prompt, sample_count } = msg.data;
                taskManager.addTask(task_id, 'lyria_music', prompt, null, sample_count || 1);
                console.log(`[REALTIME] Tracking music generation: ${task_id}`);
            });
            break;

        case 'user_transcript_delta':
            // Incremental (interim) user transcription chunk — live word-by-word.
            // Mirrors the AI-side transcript_delta pattern: accumulate into a
            // buffer and render into a transient (non-persisted) user bubble that
            // updates as chunks arrive. The final user_transcript commits it.
            if (msg.data) {
                userTranscriptBuffer += msg.data;
                if (!liveUserBubble) {
                    // appendBubble adds to the DOM WITHOUT persisting to history,
                    // so interim chunks don't spam localStorage.
                    liveUserBubble = appendBubble('user', userTranscriptBuffer);
                } else {
                    const span = liveUserBubble.querySelector('.bubble-text');
                    if (span) span.textContent = userTranscriptBuffer;
                }
            }
            break;

        case 'user_transcript':
            // User's voice input transcribed by Whisper (authoritative final).
            if (msg.data) {
                console.log('[REALTIME] User voice transcript:', msg.data);
                if (liveUserBubble) {
                    // Finalize the live bubble in place, then persist via addBubble
                    // and drop the transient one so there's no duplicate.
                    liveUserBubble.remove();
                    liveUserBubble = null;
                }
                // Add to chat (persisted to localStorage)
                addBubble('user', msg.data);
            }
            // Reset for the next utterance (covers delta-less providers too).
            userTranscriptBuffer = '';
            break;

        case 'speech_started':
            // VAD detected speech start (if using VAD mode)
            break;

        case 'speech_stopped':
            // VAD detected speech end (if using VAD mode)
            break;

        case 'error':
            console.error('[REALTIME] Error:', msg.data);
            toast('Realtime error: ' + msg.data);
            updateStatus('Error');
            break;

        case 'disconnected':
            isConnected = false;
            updateStatus('Disconnected');
            updateUI();
            break;

        case 'reconnecting':
            console.log('[REALTIME] Backend reconnecting:', msg.data);
            updateStatus(`Reconnecting (${msg.data.attempt}/${msg.data.max})...`);
            break;

        case 'reconnected':
            console.log('[REALTIME] Backend reconnected on attempt', msg.data.attempt);
            updateStatus('Connected');
            break;

        case 'pong':
            break;  // keepalive (live-session.js)

        default:
            console.log('[REALTIME] Unknown message type:', type, msg);
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
        toast('Not connected to Realtime API');
        return;
    }

    if (!text.trim()) return;

    // Add user message to chat (persisted to localStorage)
    addBubble('user', text);


    // Send to server
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

    // Stop audio playback
    stopAudio();
    isAISpeaking = false;
    aiStoppedSpeakingAt = Date.now();  // Track when AI stopped for post-speech delay
    responseCompleteReceived = false;  // Reset response state

    // Send interrupt to server
    liveSession.send({
        type: 'interrupt'
    });

    updateStatus('Interrupted');
}

// =============================================================================
// UI Updates
// =============================================================================

/**
 * Update status display
 * @param {string} status - Status text
 */
function updateStatus(status) {
    const statusEl = SEL.statusEl ? $(SEL.statusEl) : null;
    if (statusEl) {
        statusEl.textContent = status;

        // Update CSS class based on status
        statusEl.className = 'realtime-status';
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

/**
 * Update transcript display
 * @param {string} text - Transcript text
 */
function updateTranscript(text) {
    const transcriptEl = SEL.transcriptEl ? $(SEL.transcriptEl) : null;
    if (transcriptEl) {
        transcriptEl.textContent = text;
        transcriptEl.scrollTop = transcriptEl.scrollHeight;
    }
}

/**
 * Clear transcript display
 */
function clearTranscript() {
    const transcriptEl = SEL.transcriptEl ? $(SEL.transcriptEl) : null;
    if (transcriptEl) {
        transcriptEl.textContent = '';
    }
}

/**
 * Update UI button states
 */
function updateUI() {
    const banner = SEL.bannerEl ? $(SEL.bannerEl) : null;
    const connectBtn = SEL.connectButton ? $(SEL.connectButton) : null;
    const disconnectBtn = SEL.disconnectButton ? $(SEL.disconnectButton) : null;
    const micBtn = SEL.micButton ? $(SEL.micButton) : null;
    const micText = SEL.micText ? $(SEL.micText) : null;

    // Update banner state classes for visual feedback
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

    // Audit I4: model + vad_type are schema-changing (URL-rebuild vs setup-rebuild),
    // so they must be locked while CONNECTED. Voice + eagerness + idle_timeout
    // stay always-enabled (hot-swappable via session.update).
    const modelSelect = SEL.modelSelect ? $(SEL.modelSelect) : null;
    const vadSelect = SEL.vadSelect ? $(SEL.vadSelect) : null;
    if (modelSelect) modelSelect.disabled = isConnected;
    if (vadSelect) vadSelect.disabled = isConnected;

    if (micBtn) {
        micBtn.disabled = !isConnected;
        micBtn.classList.toggle('recording', isRecording);

        // Update mic button text (in the .btn-label span)
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

/**
 * Toggle recording state (continuous mode with server-side VAD)
 * If recording: mute (stop sending audio, keep mic open)
 * If not recording/muted: start/unmute
 * Server-side VAD automatically detects turn boundaries
 */
export function toggleRecording() {
    if (isRecording) {
        // Mute - stop sending audio but keep mic open
        muteRecording();
        updateStatus('Muted');
    } else {
        // Interrupt AI if currently speaking
        if (isAISpeaking) {
            interrupt();
        }
        // Start or unmute
        if (micCapture) {
            unmuteRecording();
        } else {
            startRecording();
        }
        updateStatus('Listening...');
    }
}

// =============================================================================
// Initialization
// =============================================================================

/**
 * Fetch realtime catalog from /realtime/status with 5min sessionStorage cache.
 * Mirrors state-management.js fetchAvailableModels() pattern (audit M3 — no JS-side catalog).
 * Populates model dropdown from models[] and sets default selection from model_default.
 * @returns {Promise<Object|null>} catalog object, or null on failure
 */
async function fetchRealtimeCatalog() {
    const CACHE_TTL_MS = 5 * 60 * 1000;  // 5 minutes
    const cacheKey = 'bb_realtime_catalog';

    try {
        const cached = JSON.parse(sessionStorage.getItem(cacheKey) || 'null');
        if (cached && Date.now() - cached.ts < CACHE_TTL_MS && cached.data) {
            console.log(`[REALTIME] Catalog cache hit (age ${Math.round((Date.now() - cached.ts) / 1000)}s)`);
            return cached.data;
        }
    } catch (_) { /* corrupted cache — fall through */ }

    try {
        const res = await fetch('/realtime/status');
        if (res.ok) {
            const data = await res.json();
            try {
                sessionStorage.setItem(cacheKey, JSON.stringify({ ts: Date.now(), data }));
            } catch (_) { /* sessionStorage full or disabled */ }
            return data;
        }
    } catch (err) {
        console.error('[REALTIME] Failed to fetch catalog:', err);
    }
    return null;
}

/**
 * Populate the model dropdown from the catalog. Default selection = model_default.
 */
function populateRealtimeModelDropdown(catalog) {
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
    console.log(`[REALTIME] Model dropdown populated with ${catalog.models.length} entries, default=${catalog.model_default}`);
}

/**
 * Toggle visibility of eagerness vs idle_timeout based on selected vad_type.
 */
function updateRealtimeVadVisibility() {
    const vadSelect = SEL.vadSelect ? $(SEL.vadSelect) : null;
    const eagernessSelect = SEL.eagernessSelect ? $(SEL.eagernessSelect) : null;
    const idleTimeoutInput = SEL.idleTimeoutInput ? $(SEL.idleTimeoutInput) : null;
    // Optional row wrappers (modal layout) — fall back to toggling the
    // control itself when the row id isn't supplied (inline-banner shape).
    const eagernessRow = SEL.eagernessRow ? $(SEL.eagernessRow) : null;
    const idleRow = SEL.idleRow ? $(SEL.idleRow) : null;
    if (!vadSelect) return;
    const vadType = vadSelect.value;
    const eagernessTarget = eagernessRow || eagernessSelect;
    const idleTarget = idleRow || idleTimeoutInput;
    if (eagernessTarget) {
        eagernessTarget.style.display = (vadType === 'semantic_vad') ? '' : 'none';
    }
    if (idleTarget) {
        idleTarget.style.display = (vadType === 'server_vad') ? '' : 'none';
    }
}

/**
 * Initialize Realtime UI and event handlers.
 *
 * @param {Object} [config] - Optional configuration.
 * @param {Object} [config.selectors] - Override DOM ids (Track 4 modal hook).
 *   Any omitted key falls back to the historical inline-banner default in SEL.
 */
export function initRealtimeUI(config) {
    if (config && config.selectors && typeof config.selectors === 'object') {
        Object.assign(SEL, config.selectors);
    }
    const connectBtn = SEL.connectButton ? $(SEL.connectButton) : null;
    const disconnectBtn = SEL.disconnectButton ? $(SEL.disconnectButton) : null;
    const micBtn = SEL.micButton ? $(SEL.micButton) : null;
    const toggleBtn = SEL.toggleButton ? $(SEL.toggleButton) : null;
    const transcriptSection = SEL.transcriptSection ? $(SEL.transcriptSection) : null;
    const vadSelect = SEL.vadSelect ? $(SEL.vadSelect) : null;

    // Populate model dropdown from /realtime/status (5min sessionStorage cache)
    fetchRealtimeCatalog().then(catalog => {
        if (catalog) populateRealtimeModelDropdown(catalog);
    });

    // Preset dropdown from GET /voice-agents (P4 registry; row hidden pre-P4)
    fetchVoicePresets().then(presets => {
        const presetSelect = SEL.presetSelect ? $(SEL.presetSelect) : null;
        populatePresetDropdown(presetSelect, filterPresetsByProvider(presets, ['openai', 'realtime', 'gpt-realtime']));
    });

    // Wire VAD type change → show/hide eagerness vs idle_timeout
    if (vadSelect) {
        vadSelect.addEventListener('change', updateRealtimeVadVisibility);
        updateRealtimeVadVisibility();  // initial state
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

    // Transcript toggle button
    if (toggleBtn && transcriptSection) {
        toggleBtn.onclick = () => {
            const isCollapsed = transcriptSection.classList.toggle('collapsed');
            toggleBtn.textContent = isCollapsed ? '▼' : '▲';
            toggleBtn.title = isCollapsed ? 'Show transcript' : 'Hide transcript';

            // Update history padding for new banner height
            const history = $('history');
            if (history) {
                const baseHeaderPadding = 60;
                const bannerHeight = isCollapsed ? 50 : 200;
                history.style.paddingTop = `${baseHeaderPadding + bannerHeight}px`;
            }
        };
    }

    // Initial UI state
    updateUI();
    console.log('[REALTIME] UI initialized');
}

/**
 * Check if Realtime is available on server
 * @returns {Promise<boolean>}
 */
export async function checkRealtimeAvailable() {
    try {
        const res = await fetch('/realtime/status');
        if (res.ok) {
            const data = await res.json();
            return data.available;
        }
    } catch (err) {
        console.error('[REALTIME] Failed to check availability:', err);
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
