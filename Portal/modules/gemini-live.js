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
import { fetchVoicePresets, filterPresetsByProvider, populatePresetDropdown } from './voice-presets.js';

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

/** WebSocket connection to Orchestrator */
let ws = null;

/** Session ID */
let sessionId = null;

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
let currentGeminiPresetId = null;

/** Audio context for playback (native rate for best quality) */
let playbackContext = null;

/** Native playback sample rate */
let playbackSampleRate = 48000;

/** Audio context for recording (native rate) */
let recordingContext = null;

/** Media stream for recording */
let mediaStream = null;

/** Script processor for audio capture */
let scriptProcessor = null;

/** Source node for media stream */
let sourceNode = null;

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

/** Full session conversation log for BlackBox capture */
let sessionConversation = [];

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
// Reconnection State
// =============================================================================

/** Number of reconnection attempts */
let reconnectAttempts = 0;

/** Maximum reconnection attempts before giving up */
const MAX_RECONNECT_ATTEMPTS = 15;

/** Reconnection timer */
let reconnectTimer = null;

/** Keepalive ping interval timer */
let keepaliveTimer = null;

/** Last time we received a pong from server */
let lastPongTime = 0;

/** Whether mic was recording before disconnect (to resume after reconnect) */
let wasRecordingBeforeDisconnect = false;

/** Whether this is an intentional disconnect (user clicked disconnect) */
let intentionalDisconnect = false;

/** Stored operator for reconnection */
let currentOperator = '';

// =============================================================================
// Audio Utilities
// =============================================================================

/**
 * Convert Float32 audio samples to base64-encoded PCM16
 * @param {Float32Array} float32Array - Audio samples (-1 to 1)
 * @returns {string} Base64 encoded PCM16
 */
function float32ToPCM16Base64(float32Array) {
    const pcm16 = new Int16Array(float32Array.length);
    for (let i = 0; i < float32Array.length; i++) {
        const s = Math.max(-1, Math.min(1, float32Array[i]));
        pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }

    const bytes = new Uint8Array(pcm16.buffer);
    let binary = '';
    for (let i = 0; i < bytes.length; i++) {
        binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary);
}

/**
 * Convert base64-encoded PCM16 to Float32 audio samples
 * @param {string} base64 - Base64 encoded PCM16
 * @returns {Float32Array} Audio samples
 */
function pcm16Base64ToFloat32(base64) {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
        bytes[i] = binary.charCodeAt(i);
    }

    const pcm16 = new Int16Array(bytes.buffer);
    const float32 = new Float32Array(pcm16.length);
    for (let i = 0; i < pcm16.length; i++) {
        float32[i] = pcm16[i] / 32768.0;
    }

    return float32;
}

/**
 * Simple low-pass filter to prevent aliasing before downsampling
 */
function lowPassFilter(input, cutoffRatio = 0.4) {
    const output = new Float32Array(input.length);
    const rc = 1.0 / (cutoffRatio * 2 * Math.PI);
    const dt = 1.0;
    const alpha = dt / (rc + dt);

    output[0] = input[0];
    for (let i = 1; i < input.length; i++) {
        output[i] = output[i - 1] + alpha * (input[i] - output[i - 1]);
    }
    return output;
}

/**
 * Resample audio from source rate to 16kHz for Gemini
 * @param {Float32Array} input - Input samples
 * @param {number} sourceRate - Source sample rate
 * @returns {Float32Array} Resampled audio at 16kHz
 */
function resampleTo16kHz(input, sourceRate) {
    const targetRate = 16000;  // Gemini expects 16kHz

    if (sourceRate === targetRate) {
        return input;
    }

    // Apply low-pass filter to prevent aliasing when downsampling
    const cutoff = targetRate / sourceRate * 0.9;
    const filtered = lowPassFilter(input, cutoff);

    const ratio = sourceRate / targetRate;
    const outputLength = Math.floor(filtered.length / ratio);
    const output = new Float32Array(outputLength);

    // Cubic interpolation for better quality
    for (let i = 0; i < outputLength; i++) {
        const srcIndex = i * ratio;
        const idx = Math.floor(srcIndex);
        const frac = srcIndex - idx;

        const s0 = filtered[Math.max(0, idx - 1)];
        const s1 = filtered[idx];
        const s2 = filtered[Math.min(filtered.length - 1, idx + 1)];
        const s3 = filtered[Math.min(filtered.length - 1, idx + 2)];

        // Catmull-Rom spline
        const a = -0.5 * s0 + 1.5 * s1 - 1.5 * s2 + 0.5 * s3;
        const b = s0 - 2.5 * s1 + 2 * s2 - 0.5 * s3;
        const c = -0.5 * s0 + 0.5 * s2;
        const d = s1;

        output[i] = a * frac * frac * frac + b * frac * frac + c * frac + d;
    }

    return output;
}

/**
 * Upsample audio from 24kHz to target rate
 * @param {Float32Array} input - Input samples at 24kHz
 * @param {number} targetRate - Target sample rate (e.g., 48000)
 * @returns {Float32Array} Upsampled audio
 */
function upsampleFrom24kHz(input, targetRate) {
    const sourceRate = 24000;  // Gemini outputs 24kHz

    if (targetRate === sourceRate) {
        return input;
    }

    // For exact 2x upsampling (24kHz -> 48kHz)
    if (targetRate === 48000) {
        const output = new Float32Array(input.length * 2);
        for (let i = 0; i < input.length - 1; i++) {
            const idx = i * 2;
            output[idx] = input[i];
            output[idx + 1] = (input[i] + input[i + 1]) * 0.5;
        }
        const lastIdx = (input.length - 1) * 2;
        output[lastIdx] = input[input.length - 1];
        output[lastIdx + 1] = input[input.length - 1];
        return output;
    }

    const ratio = targetRate / sourceRate;
    const outputLength = Math.floor(input.length * ratio);
    const output = new Float32Array(outputLength);

    for (let i = 0; i < outputLength; i++) {
        const srcPos = i / ratio;
        const srcIndex = Math.floor(srcPos);
        const frac = srcPos - srcIndex;

        const s0 = input[Math.max(0, srcIndex - 1)];
        const s1 = input[srcIndex];
        const s2 = input[Math.min(input.length - 1, srcIndex + 1)];
        const s3 = input[Math.min(input.length - 1, srcIndex + 2)];

        const a = -0.5 * s0 + 1.5 * s1 - 1.5 * s2 + 0.5 * s3;
        const b = s0 - 2.5 * s1 + 2 * s2 - 0.5 * s3;
        const c = -0.5 * s0 + 0.5 * s2;
        const d = s1;

        output[i] = a * frac * frac * frac + b * frac * frac + c * frac + d;
    }

    return output;
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
 * Initialize audio context for recording (native sample rate)
 */
async function initRecordingContext() {
    if (!recordingContext) {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextClass) {
            throw new Error('AudioContext not supported on this device');
        }
        recordingContext = new AudioContextClass();
        console.log(`[GEMINI-LIVE] Recording context created at ${recordingContext.sampleRate}Hz`);
    }

    if (recordingContext.state === 'suspended') {
        await recordingContext.resume();
    }
}

/**
 * Play base64-encoded PCM16 audio with seamless scheduling
 * @param {string} base64Audio - Base64 encoded PCM16 at 24kHz
 */
async function playAudio(base64Audio) {
    await initPlaybackContext();

    // Decode PCM16 to float32 (at 24kHz)
    const float32_24k = pcm16Base64ToFloat32(base64Audio);

    // Upsample from 24kHz to native playback rate
    const float32 = upsampleFrom24kHz(float32_24k, playbackSampleRate);

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
// Native Android Detection
// =============================================================================

function isNativeAndroid() {
    return typeof AndroidMic !== 'undefined';
}

function hasMic() {
    return isNativeAndroid() || !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
}

async function stopOtherMicUsers() {
    if (window.micOn && window.stopSTT) {
        console.log('[GEMINI-LIVE] Stopping STT before taking mic');
        window.stopSTT(true);
        await new Promise(resolve => setTimeout(resolve, 200));
    }
}

/**
 * Resample from 24kHz to 16kHz for Gemini
 * Native Android captures at 24kHz, Gemini needs 16kHz
 */
function resample24kTo16k(base64Data) {
    // Decode base64 to PCM16 bytes
    const binaryString = atob(base64Data);
    const bytes = new Uint8Array(binaryString.length);
    for (let i = 0; i < binaryString.length; i++) {
        bytes[i] = binaryString.charCodeAt(i);
    }

    // Convert to Int16Array (PCM16)
    const pcm16 = new Int16Array(bytes.buffer);

    // Resample from 24kHz to 16kHz (ratio = 2/3)
    const ratio = 16000 / 24000;
    const outputLength = Math.floor(pcm16.length * ratio);
    const output = new Int16Array(outputLength);

    for (let i = 0; i < outputLength; i++) {
        const srcIndex = i / ratio;
        const srcIndexFloor = Math.floor(srcIndex);
        const srcIndexCeil = Math.min(srcIndexFloor + 1, pcm16.length - 1);
        const t = srcIndex - srcIndexFloor;

        // Linear interpolation
        output[i] = Math.round(pcm16[srcIndexFloor] * (1 - t) + pcm16[srcIndexCeil] * t);
    }

    // Convert back to base64
    const outputBytes = new Uint8Array(output.buffer);
    let binary = '';
    for (let i = 0; i < outputBytes.length; i++) {
        binary += String.fromCharCode(outputBytes[i]);
    }
    return btoa(binary);
}

/**
 * Start native Android audio streaming
 * Uses AndroidMic.startAudioStreaming() which captures at 24kHz PCM16
 * We resample to 16kHz for Gemini
 */
async function startNativeStreaming() {
    return new Promise((resolve, reject) => {
        // Set up callbacks
        window.onGeminiNativeStreamStart = () => {
            console.log('[GEMINI-LIVE] Native streaming started');
            isRecording = true;
            usingNativeRecording = true;
            updateUI();
            resolve();
        };

        window.onGeminiNativeStreamError = (error) => {
            console.error('[GEMINI-LIVE] Native streaming error:', error);
            isRecording = false;
            usingNativeRecording = false;
            toast('Native audio error: ' + error);
            reject(new Error(error));
        };

        window.onGeminiNativeAudioChunk = (base64Data) => {
            // Send audio chunk to WebSocket
            // Native Android captures at 24kHz PCM16 - resample to 16kHz for Gemini
            // Auto-mute while AI is speaking AND for POST_SPEECH_DELAY_MS after to prevent feedback
            const timeSinceAIStopped = Date.now() - aiStoppedSpeakingAt;
            const inPostSpeechDelay = timeSinceAIStopped < POST_SPEECH_DELAY_MS;

            if (ws && ws.readyState === WebSocket.OPEN && isRecording && !isAISpeaking && !inPostSpeechDelay) {
                const resampled = resample24kTo16k(base64Data);
                // Note: User audio transcription is now handled by the backend
                ws.send(JSON.stringify({
                    type: 'audio_input',
                    data: resampled
                }));
            }
        };

        window.onGeminiNativeStreamStop = () => {
            console.log('[GEMINI-LIVE] Native streaming stopped');
            isRecording = false;
            usingNativeRecording = false;
            updateUI();
        };

        // Start native streaming
        try {
            console.log('[GEMINI-LIVE] Calling AndroidMic.startAudioStreaming()...');

            // Use custom callback names for Gemini to avoid conflict with GPT Realtime
            // Store original callbacks
            const origStart = window.onNativeStreamStart;
            const origError = window.onNativeStreamError;
            const origChunk = window.onNativeAudioChunk;
            const origStop = window.onNativeStreamStop;

            // Redirect callbacks to Gemini handlers
            window.onNativeStreamStart = window.onGeminiNativeStreamStart;
            window.onNativeStreamError = window.onGeminiNativeStreamError;
            window.onNativeAudioChunk = window.onGeminiNativeAudioChunk;
            window.onNativeStreamStop = window.onGeminiNativeStreamStop;

            AndroidMic.startAudioStreaming();
        } catch (e) {
            console.error('[GEMINI-LIVE] Failed to start native streaming:', e);
            reject(e);
        }

        // Timeout if streaming doesn't start within 3 seconds
        setTimeout(() => {
            if (!isRecording) {
                console.error('[GEMINI-LIVE] Native streaming timeout');
                reject(new Error('Native streaming timeout'));
            }
        }, 3000);
    });
}

/**
 * Stop native Android audio streaming
 */
function stopNativeStreaming() {
    if (typeof AndroidMic !== 'undefined' && typeof AndroidMic.stopAudioStreaming === 'function') {
        console.log('[GEMINI-LIVE] Stopping native audio streaming');
        AndroidMic.stopAudioStreaming();
    }

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

    const isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);

    try {
        await stopOtherMicUsers();

        // If native Android, use native streaming (bypasses WebView getUserMedia issues)
        if (isNativeAndroid() && typeof AndroidMic !== 'undefined' && typeof AndroidMic.startAudioStreaming === 'function') {
            console.log('[GEMINI-LIVE] Using native Android audio streaming');
            await startNativeStreaming();
            return;  // Native streaming handles everything
        }

        if (mediaStream) {
            mediaStream.getTracks().forEach(track => track.stop());
            mediaStream = null;
            await new Promise(resolve => setTimeout(resolve, 100));
        }

        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            throw new Error('Microphone not supported. Requires HTTPS or localhost.');
        }

        await initRecordingContext();

        // Audio constraints - use ideal for compatibility, auto-mute handles feedback
        const audioConstraints = {
            audio: {
                sampleRate: { ideal: 48000 },
                channelCount: 1,
                echoCancellation: true,  // Required - not just ideal
                noiseSuppression: true,  // Required - not just ideal
                autoGainControl: true    // Required - not just ideal
            }
        };

        console.log(`[GEMINI-LIVE] Requesting microphone (mobile: ${isMobile})`);

        let retryCount = 0;
        const maxRetries = 5;

        while (retryCount <= maxRetries) {
            try {
                mediaStream = await navigator.mediaDevices.getUserMedia(audioConstraints);
                console.log('[GEMINI-LIVE] Got media stream successfully');
                break;
            } catch (micError) {
                console.error(`[GEMINI-LIVE] Microphone error (attempt ${retryCount + 1}):`, micError.name);

                if (micError.name === 'NotReadableError' && retryCount < maxRetries) {
                    const delay = 500 + (retryCount * 500);
                    console.log(`[GEMINI-LIVE] Mic busy, waiting ${delay}ms...`);
                    await new Promise(resolve => setTimeout(resolve, delay));
                    retryCount++;
                    continue;
                }

                if (micError.name === 'NotAllowedError') {
                    throw new Error('Microphone permission denied.');
                } else if (micError.name === 'NotFoundError') {
                    throw new Error('No microphone found.');
                } else if (micError.name === 'NotReadableError') {
                    throw new Error('Microphone is busy.');
                } else {
                    throw new Error(`Microphone error: ${micError.message || micError.name}`);
                }
            }
        }

        sourceNode = recordingContext.createMediaStreamSource(mediaStream);
        const nativeRate = recordingContext.sampleRate;
        console.log(`[GEMINI-LIVE] Recording at native rate: ${nativeRate}Hz, resampling to 16kHz`);

        const bufferSize = isMobile ? 4096 : 8192;
        scriptProcessor = recordingContext.createScriptProcessor(bufferSize, 1, 1);

        scriptProcessor.onaudioprocess = (e) => {
            if (!isRecording || !ws || ws.readyState !== WebSocket.OPEN) return;

            // Auto-mute while AI is speaking AND for POST_SPEECH_DELAY_MS after to prevent feedback
            const timeSinceAIStopped = Date.now() - aiStoppedSpeakingAt;
            const inPostSpeechDelay = timeSinceAIStopped < POST_SPEECH_DELAY_MS;
            if (isAISpeaking || inPostSpeechDelay) return;

            const inputData = e.inputBuffer.getChannelData(0);

            // Check for actual audio
            let maxLevel = 0;
            for (let i = 0; i < inputData.length; i++) {
                const abs = Math.abs(inputData[i]);
                if (abs > maxLevel) maxLevel = abs;
            }

            // Noise gate: skip frames below noise floor
            if (maxLevel < 0.015) return;

            // High-pass filter to cut sub-100Hz rumble (simple single-pole)
            const hpAlpha = 0.98;
            let hpPrev = 0;
            const filtered = new Float32Array(inputData.length);
            for (let i = 0; i < inputData.length; i++) {
                filtered[i] = hpAlpha * (hpPrev + inputData[i] - (i > 0 ? inputData[i - 1] : 0));
                hpPrev = filtered[i];
            }

            // Normalize (capped gain to prevent noise amplification)
            const normalizedData = new Float32Array(filtered.length);
            const targetLevel = 0.4;
            const gain = maxLevel > 0.03 ? Math.min(targetLevel / maxLevel, 1.5) : 1.0;
            for (let i = 0; i < filtered.length; i++) {
                normalizedData[i] = Math.max(-1, Math.min(1, filtered[i] * gain));
            }

            // Resample to 16kHz for Gemini
            const resampled = resampleTo16kHz(normalizedData, nativeRate);
            const base64 = float32ToPCM16Base64(resampled);

            // Note: User audio transcription is now handled by the backend
            ws.send(JSON.stringify({
                type: 'audio_input',
                data: base64
            }));
        };

        sourceNode.connect(scriptProcessor);
        scriptProcessor.connect(recordingContext.destination);

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
    if (!isRecording) return;

    console.log('[GEMINI-LIVE] Stopping recording...');

    // If using native Android streaming, use native stop
    if (usingNativeRecording) {
        stopNativeStreaming();
        // Send commit message to trigger AI response
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
                type: 'audio_commit'
            }));
        }
        updateUI();
        console.log('[GEMINI-LIVE] Native recording stopped, audio committed');
        return;
    }

    isRecording = false;

    if (scriptProcessor) {
        scriptProcessor.disconnect();
        scriptProcessor = null;
    }
    if (sourceNode) {
        sourceNode.disconnect();
        sourceNode = null;
    }

    if (mediaStream) {
        mediaStream.getTracks().forEach(track => track.stop());
        mediaStream = null;
    }

    // Send commit message to trigger AI response
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
            type: 'audio_commit'
        }));
    }

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
    if (isConnected || (ws && ws.readyState === WebSocket.CONNECTING)) {
        console.log('[GEMINI-LIVE] Already connected or connecting');
        return;
    }

    operator = operator || getOperator();
    if (!operator) {
        toast('Select an operator first');
        return;
    }

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

    // Voice Agent preset (P4 registry) — sent as `agent`; backend precedence
    // is explicit params > preset > defaults, so sending both is safe.
    const presetSelect = SEL.presetSelect ? $(SEL.presetSelect) : null;
    const presetId = (presetSelect && presetSelect.value) ? presetSelect.value : undefined;

    // Capture v2 config into module state so reconnect can replay it
    // (otherwise reconnect rebuilds with only {type, operator, voice} and
    // the backend silently falls back to env defaults — T14 F1).
    currentGeminiModel = selectedModel || null;
    currentGeminiVadStart = vadSensitivityStart || null;
    currentGeminiVadEnd = vadSensitivityEnd || null;
    currentGeminiThinkingLevel = thinkingLevel || null;
    currentGeminiPresetId = presetId || null;

    // Reset session state
    sessionConversation = [];
    accumulatedSamples = new Float32Array(0);
    isBufferingAudio = false;
    nextPlaybackTime = 0;

    sessionId = crypto.randomUUID();
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProtocol}//${window.location.host}/ws/gemini-live/${sessionId}`;

    console.log('[GEMINI-LIVE] Connecting to:', wsUrl);
    updateStatus('Connecting...');

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log('[GEMINI-LIVE] WebSocket connected');
        updateStatus('Initializing...');
        currentOperator = operator;
        intentionalDisconnect = false;

        // Build connect message with new live-models-v2 fields; omit undefined to keep wire clean
        const connectMsg = {
            type: 'connect',
            operator: operator,
            voice: selectedVoice
        };
        if (selectedModel) connectMsg.model = selectedModel;
        if (vadSensitivityStart) connectMsg.vad_sensitivity_start = vadSensitivityStart;
        if (vadSensitivityEnd) connectMsg.vad_sensitivity_end = vadSensitivityEnd;
        if (thinkingLevel) connectMsg.thinking_level = thinkingLevel;
        if (presetId) connectMsg.agent = presetId;
        ws.send(JSON.stringify(connectMsg));
    };

    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            handleMessage(msg);
        } catch (err) {
            console.error('[GEMINI-LIVE] Failed to parse message:', err);
        }
    };

    ws.onclose = (event) => {
        console.log('[GEMINI-LIVE] WebSocket closed:', event.code, event.reason);
        stopKeepalive();

        // Normal close (1000) or intentional disconnect — no reconnect
        if (event.code === 1000 || intentionalDisconnect) {
            isConnected = false;
            updateStatus('Disconnected');
            updateUI();
            return;
        }

        // Unexpected close — attempt auto-reconnect
        isConnected = false;
        wasRecordingBeforeDisconnect = isRecording;
        if (isRecording) {
            stopRecordingAndSend();
        }
        attemptReconnect();
    };

    ws.onerror = (err) => {
        console.error('[GEMINI-LIVE] WebSocket error:', err);
    };
}

/**
 * Save session conversation to BlackBox
 */
async function saveSessionToBlackBox() {
    if (sessionConversation.length === 0) {
        console.log('[GEMINI-LIVE] No conversation to save');
        return;
    }

    const operator = getOperator();
    if (!operator) {
        console.warn('[GEMINI-LIVE] No operator set, cannot save session');
        return;
    }

    // Format conversation as readable transcript
    const transcript = sessionConversation.map(msg => {
        const role = msg.role === 'user' ? 'User' : 'AI';
        return `[${role}]: ${msg.content}`;
    }).join('\n\n');

    const sessionSummary = `=== Gemini Live Voice Session ===
Session ID: ${sessionId || 'unknown'}
Voice: ${selectedVoice}
Timestamp: ${new Date().toISOString()}
Messages: ${sessionConversation.length}

--- Transcript ---
${transcript}
--- End Session ---`;

    try {
        // Send to /chat endpoint for BlackBox capture using Gemini 2.5 Pro
        const response = await fetch('/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                operator: operator,
                messages: [{
                    role: 'user',
                    content: `[Voice Session Transcript]\n${sessionSummary}`
                }],
                provider: 'google',
                model: 'gemini-2.5-pro',
                streaming: false,
                auto_checkpoint: false
            })
        });

        if (response.ok) {
            console.log('[GEMINI-LIVE] Session saved to BlackBox');
            toast('Voice session saved to BlackBox');
        } else {
            console.error('[GEMINI-LIVE] Failed to save session:', await response.text());
        }
    } catch (err) {
        console.error('[GEMINI-LIVE] Error saving session:', err);
    }

    sessionConversation = [];
}

/**
 * Disconnect from Gemini Live API
 */
export function disconnect() {
    // Note: Session saving is now handled by the backend on disconnect
    // This ensures all Whisper transcriptions are captured before saving

    // Suppress auto-reconnect
    intentionalDisconnect = true;
    reconnectAttempts = MAX_RECONNECT_ATTEMPTS;
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
    stopKeepalive();

    if (ws) {
        try {
            ws.send(JSON.stringify({ type: 'disconnect' }));
        } catch (e) {}
        ws.close();
        ws = null;
    }

    stopRecordingAndSend();
    stopAudio();

    isConnected = false;
    sessionId = null;
    reconnectAttempts = 0;
    sessionConversation = [];  // Clear local state
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
            reconnectAttempts = 0;
            updateStatus('Connected');
            updateUI();
            startKeepalive();
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
                sessionConversation.push({
                    role: 'assistant',
                    content: transcriptBuffer.trim(),
                    timestamp: new Date().toISOString()
                });
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
                sessionConversation.push({
                    role: 'user',
                    content: msg.data,
                    timestamp: new Date().toISOString(),
                    source: 'voice'
                });
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
            reconnectAttempts = 0;
            updateStatus('Connected');
            break;

        case 'pong':
            lastPongTime = Date.now();
            break;

        default:
            console.log('[GEMINI-LIVE] Unknown message type:', type, msg);
    }
}

// =============================================================================
// Auto-Reconnect + Keepalive
// =============================================================================

/**
 * Attempt to reconnect to the existing session
 */
function attemptReconnect() {
    if (intentionalDisconnect || reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
        console.log('[GEMINI-LIVE] Not reconnecting (intentional or max attempts reached)');
        updateStatus('Disconnected');
        updateUI();
        if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
            toast('Gemini Live connection lost');
        }
        return;
    }

    reconnectAttempts++;
    // Exponential backoff: 1s, 2s, 4s, 8s, max 15s
    const delay = Math.min(1000 * Math.pow(2, reconnectAttempts - 1), 30000);
    console.log(`[GEMINI-LIVE] Reconnecting (${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS}) in ${delay}ms...`);
    updateStatus(`Reconnecting (${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS})...`);

    reconnectTimer = setTimeout(() => {
        reconnectToExistingSession();
    }, delay);
}

/**
 * Reconnect to the existing session
 */
function reconnectToExistingSession() {
    if (!sessionId || !currentOperator) {
        console.log('[GEMINI-LIVE] No session to reconnect to');
        return;
    }

    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProtocol}//${window.location.host}/ws/gemini-live/${sessionId}`;

    console.log('[GEMINI-LIVE] Reconnecting to:', wsUrl);
    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log('[GEMINI-LIVE] Reconnect WebSocket opened');
        intentionalDisconnect = false;
        // Restore v2 config from module state — prevents silent
        // server-default downgrade on network blip (T14 F1).
        const reconnectMsg = {
            type: 'connect',
            operator: currentOperator,
            voice: selectedVoice
        };
        if (currentGeminiModel) reconnectMsg.model = currentGeminiModel;
        if (currentGeminiVadStart) reconnectMsg.vad_sensitivity_start = currentGeminiVadStart;
        if (currentGeminiVadEnd) reconnectMsg.vad_sensitivity_end = currentGeminiVadEnd;
        if (currentGeminiThinkingLevel) reconnectMsg.thinking_level = currentGeminiThinkingLevel;
        if (currentGeminiPresetId) reconnectMsg.agent = currentGeminiPresetId;
        ws.send(JSON.stringify(reconnectMsg));
    };

    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            handleMessage(msg);
            // On successful connected message, resume recording if needed
            if (msg.type === 'connected' && wasRecordingBeforeDisconnect) {
                wasRecordingBeforeDisconnect = false;
                setTimeout(() => startRecording(), 500);
            }
        } catch (err) {
            console.error('[GEMINI-LIVE] Failed to parse message:', err);
        }
    };

    ws.onclose = (event) => {
        console.log('[GEMINI-LIVE] Reconnect WebSocket closed:', event.code);
        stopKeepalive();
        if (!intentionalDisconnect && event.code !== 1000) {
            attemptReconnect();
        } else {
            isConnected = false;
            updateStatus('Disconnected');
            updateUI();
        }
    };

    ws.onerror = (err) => {
        console.error('[GEMINI-LIVE] Reconnect WebSocket error:', err);
    };
}

/**
 * Start keepalive ping interval
 */
function startKeepalive() {
    stopKeepalive();
    lastPongTime = Date.now();

    keepaliveTimer = setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'ping' }));

            // Check if pong was received recently (within 25s)
            if (lastPongTime && (Date.now() - lastPongTime > 25000)) {
                console.log('[GEMINI-LIVE] No pong received in 25s, closing for reconnect');
                ws.close();
            }
        }
    }, 15000);
}

/**
 * Stop keepalive ping interval
 */
function stopKeepalive() {
    if (keepaliveTimer) {
        clearInterval(keepaliveTimer);
        keepaliveTimer = null;
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
    if (!isConnected || !ws || ws.readyState !== WebSocket.OPEN) {
        toast('Not connected to Gemini Live API');
        return;
    }

    if (!text.trim()) return;

    addBubble('user', text);
    sessionConversation.push({
        role: 'user',
        content: text.trim(),
        timestamp: new Date().toISOString()
    });

    ws.send(JSON.stringify({
        type: 'text_input',
        text: text
    }));

    updateStatus('Processing...');
}

/**
 * Interrupt AI response (barge-in)
 */
export function interrupt() {
    if (!isConnected || !ws) return;

    stopAudio();
    isAISpeaking = false;
    aiStoppedSpeakingAt = Date.now();  // Track when AI stopped for post-speech delay
    responseCompleteReceived = false;  // Reset response state

    ws.send(JSON.stringify({
        type: 'interrupt'
    }));

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
