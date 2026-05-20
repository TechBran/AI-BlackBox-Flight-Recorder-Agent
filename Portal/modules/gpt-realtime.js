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
import { addBubble } from './chat-bubbles.js';

// =============================================================================
// State
// =============================================================================

/** WebSocket connection to Orchestrator */
let ws = null;

/** Session ID */
let sessionId = null;

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

/** Buffer for native Android audio chunks */
let nativeAudioBuffer = [];

/** Whether connected to Realtime API */
let isConnected = false;

/** Audio playback queue */
let audioQueue = [];

/** Current audio source being played */
let currentAudioSource = null;

/** Transcript buffer for current response */
let transcriptBuffer = '';

/** Current chat bubble for AI response */
let currentBubble = null;

/** Full session conversation log for BlackBox capture */
let sessionConversation = [];

/** Audio buffer for smoother playback - collect chunks before playing */
let audioBufferChunks = [];

/** Minimum audio chunks to buffer before starting playback (higher = smoother but more latency) */
const AUDIO_BUFFER_MIN_CHUNKS = 12;  // Increased for smoother audio

/** Whether we're currently buffering audio */
let isBufferingAudio = false;

/** Scheduled playback time for seamless audio */
let nextPlaybackTime = 0;

/** Whether the response is complete (API sent response_complete) but audio may still be playing */
let responseCompleteReceived = false;

/** AudioWorklet node for gapless playback */
let audioWorkletNode = null;

/** Whether AudioWorklet is initialized */
let audioWorkletReady = false;

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

/** High-pass filter state (persists across frames for proper filtering) */
let hpFilterPrev = 0;
let hpFilterPrevInput = 0;

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
        // Clamp and convert to 16-bit
        const s = Math.max(-1, Math.min(1, float32Array[i]));
        pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }

    // Convert to base64
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
 * Initialize audio context for recording (native sample rate)
 */
async function initRecordingContext() {
    if (!recordingContext) {
        // Use browser's native sample rate for recording
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextClass) {
            throw new Error('AudioContext not supported on this device');
        }
        recordingContext = new AudioContextClass();
        console.log(`[REALTIME] Recording context created at ${recordingContext.sampleRate}Hz`);
    }

    // Resume AudioContext - required after user interaction on mobile
    if (recordingContext.state === 'suspended') {
        console.log('[REALTIME] Resuming suspended AudioContext...');
        await recordingContext.resume();
    }

    // Double-check it's running
    if (recordingContext.state !== 'running') {
        console.warn(`[REALTIME] AudioContext state: ${recordingContext.state}`);
    }
}

/**
 * Simple low-pass filter to prevent aliasing before downsampling
 * @param {Float32Array} input - Input samples
 * @param {number} cutoffRatio - Cutoff as ratio of sample rate (0.5 = Nyquist)
 * @returns {Float32Array} Filtered audio
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
 * Resample audio from source rate to target rate with anti-aliasing
 * @param {Float32Array} input - Input samples
 * @param {number} sourceRate - Source sample rate
 * @param {number} targetRate - Target sample rate (24000 for OpenAI)
 * @returns {Float32Array} Resampled audio
 */
function resampleAudio(input, sourceRate, targetRate) {
    if (sourceRate === targetRate) {
        return input;
    }

    // Apply low-pass filter to prevent aliasing when downsampling
    const cutoff = targetRate / sourceRate * 0.9;  // Slightly below Nyquist
    const filtered = lowPassFilter(input, cutoff);

    const ratio = sourceRate / targetRate;
    const outputLength = Math.floor(filtered.length / ratio);
    const output = new Float32Array(outputLength);

    // Cubic interpolation for better quality
    for (let i = 0; i < outputLength; i++) {
        const srcIndex = i * ratio;
        const idx = Math.floor(srcIndex);
        const frac = srcIndex - idx;

        // Get 4 samples for cubic interpolation
        const s0 = filtered[Math.max(0, idx - 1)];
        const s1 = filtered[idx];
        const s2 = filtered[Math.min(filtered.length - 1, idx + 1)];
        const s3 = filtered[Math.min(filtered.length - 1, idx + 2)];

        // Cubic interpolation (Catmull-Rom)
        const a = -0.5 * s0 + 1.5 * s1 - 1.5 * s2 + 0.5 * s3;
        const b = s0 - 2.5 * s1 + 2 * s2 - 0.5 * s3;
        const c = -0.5 * s0 + 0.5 * s2;
        const d = s1;

        output[i] = a * frac * frac * frac + b * frac * frac + c * frac + d;
    }

    return output;
}

/**
 * Upsample audio from 24kHz to target rate with high quality
 * Uses linear interpolation for speed with optional higher quality mode
 * @param {Float32Array} input - Input samples at 24kHz
 * @param {number} targetRate - Target sample rate (e.g., 48000)
 * @returns {Float32Array} Upsampled audio
 */
function upsampleAudio(input, targetRate) {
    const sourceRate = 24000;  // OpenAI Realtime always sends 24kHz

    if (targetRate === sourceRate) {
        return input;
    }

    // For exact 2x upsampling (24kHz -> 48kHz), use optimized path
    if (targetRate === 48000) {
        return upsample2x(input);
    }

    const ratio = targetRate / sourceRate;
    const outputLength = Math.floor(input.length * ratio);
    const output = new Float32Array(outputLength);

    // Use cubic interpolation for good quality with reasonable performance
    for (let i = 0; i < outputLength; i++) {
        const srcPos = i / ratio;
        const srcIndex = Math.floor(srcPos);
        const frac = srcPos - srcIndex;

        // Get 4 samples for cubic interpolation (Catmull-Rom)
        const s0 = input[Math.max(0, srcIndex - 1)];
        const s1 = input[srcIndex];
        const s2 = input[Math.min(input.length - 1, srcIndex + 1)];
        const s3 = input[Math.min(input.length - 1, srcIndex + 2)];

        // Catmull-Rom spline interpolation
        const a = -0.5 * s0 + 1.5 * s1 - 1.5 * s2 + 0.5 * s3;
        const b = s0 - 2.5 * s1 + 2 * s2 - 0.5 * s3;
        const c = -0.5 * s0 + 0.5 * s2;
        const d = s1;

        output[i] = a * frac * frac * frac + b * frac * frac + c * frac + d;
    }

    return output;
}

/**
 * Optimized 2x upsampling for 24kHz -> 48kHz
 * Uses linear interpolation with zero-stuffing for speed
 * @param {Float32Array} input - Input samples at 24kHz
 * @returns {Float32Array} Upsampled audio at 48kHz
 */
function upsample2x(input) {
    const output = new Float32Array(input.length * 2);

    for (let i = 0; i < input.length - 1; i++) {
        const idx = i * 2;
        output[idx] = input[i];
        // Linear interpolation for the intermediate sample
        output[idx + 1] = (input[i] + input[i + 1]) * 0.5;
    }

    // Handle last sample
    const lastIdx = (input.length - 1) * 2;
    output[lastIdx] = input[input.length - 1];
    output[lastIdx + 1] = input[input.length - 1];

    return output;
}

/**
 * Play base64-encoded PCM16 audio with seamless scheduling
 * Uses continuous buffer approach for gapless playback
 * @param {string} base64Audio - Base64 encoded PCM16
 */
async function playAudio(base64Audio) {
    await initPlaybackContext();

    // Decode PCM16 to float32
    const float32_24k = pcm16Base64ToFloat32(base64Audio);

    // Upsample from 24kHz to native playback rate for better quality
    const float32 = upsampleAudio(float32_24k, playbackSampleRate);

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
        } else if (audioBufferChunks.length > 0) {
            // Process any remaining queued chunks
            const batch = audioBufferChunks.splice(0, audioBufferChunks.length);
            const combined = concatenateAudioChunks(batch);
            playAudio(combined);
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
 * Concatenate multiple audio chunks into one for smoother playback
 * @param {string[]} chunks - Array of base64 encoded PCM16 chunks
 * @returns {string} Single concatenated base64 chunk
 */
function concatenateAudioChunks(chunks) {
    if (chunks.length === 0) return '';
    if (chunks.length === 1) return chunks[0];

    // Decode all chunks
    const floatArrays = chunks.map(chunk => pcm16Base64ToFloat32(chunk));

    // Calculate total length
    const totalLength = floatArrays.reduce((sum, arr) => sum + arr.length, 0);

    // Concatenate
    const combined = new Float32Array(totalLength);
    let offset = 0;
    for (const arr of floatArrays) {
        combined.set(arr, offset);
        offset += arr.length;
    }

    // Convert back to base64 PCM16
    return float32ToPCM16Base64(combined);
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
    audioQueue = [];
    audioBufferChunks = [];
    accumulatedSamples = new Float32Array(0);  // Clear accumulated samples
    isBufferingAudio = false;
    nextPlaybackTime = 0;
}

// =============================================================================
// Native Android Detection
// =============================================================================

/**
 * Check if running in native Android app with AndroidMic bridge
 * @returns {boolean}
 */
function isNativeAndroid() {
    return typeof AndroidMic !== 'undefined';
}

/**
 * Check if microphone is available
 * @returns {boolean}
 */
function hasMic() {
    return isNativeAndroid() || !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
}

/**
 * Stop other modules that may be using the microphone
 * Ensures we don't conflict with STT or Gemini recorder
 */
async function stopOtherMicUsers() {
    // Check if STT is recording
    if (window.micOn && window.stopSTT) {
        console.log('[REALTIME] Stopping STT before taking mic');
        window.stopSTT(true);  // Cancel without processing
        await new Promise(resolve => setTimeout(resolve, 200));
    }

    // Check if Gemini recorder is active
    if (window.geminiRecording) {
        console.log('[REALTIME] Gemini recorder is active - waiting for it to finish');
        // Don't forcefully stop it, just wait a moment
        await new Promise(resolve => setTimeout(resolve, 300));
    }
}

/**
 * Release the native Android microphone and wait for confirmation
 * @returns {Promise<void>} Resolves when mic is released
 */
async function releaseNativeMicAndWait() {
    return new Promise((resolve) => {
        // Set up callback for when mic is released
        const originalCallback = window.onNativeMicReleased;
        let resolved = false;

        window.onNativeMicReleased = () => {
            console.log('[REALTIME] Native mic released callback received');
            resolved = true;
            window.onNativeMicReleased = originalCallback;
            resolve();
        };

        try {
            if (typeof AndroidMic !== 'undefined') {
                if (typeof AndroidMic.releaseMic === 'function') {
                    console.log('[REALTIME] Calling AndroidMic.releaseMic()...');
                    AndroidMic.releaseMic();
                } else if (typeof AndroidMic.stopRecording === 'function') {
                    console.log('[REALTIME] Calling AndroidMic.stopRecording()...');
                    AndroidMic.stopRecording();
                    // stopRecording doesn't have a callback, so resolve after delay
                    setTimeout(() => {
                        if (!resolved) {
                            console.log('[REALTIME] stopRecording timeout - assuming mic released');
                            window.onNativeMicReleased = originalCallback;
                            resolve();
                        }
                    }, 500);
                    return;
                }
            }
        } catch (e) {
            console.warn('[REALTIME] Error releasing native mic:', e);
        }

        // Timeout fallback - if callback doesn't come within 1 second, proceed anyway
        setTimeout(() => {
            if (!resolved) {
                console.log('[REALTIME] Native mic release timeout - proceeding anyway');
                window.onNativeMicReleased = originalCallback;
                resolve();
            }
        }, 1000);
    });
}

/**
 * Start native Android audio streaming
 * Uses AndroidMic.startAudioStreaming() which captures at 24kHz PCM16
 * Audio is already in OpenAI Realtime format - no resampling needed
 */
async function startNativeStreaming() {
    return new Promise((resolve, reject) => {
        // Set up callbacks
        window.onNativeStreamStart = () => {
            console.log('[REALTIME] Native streaming started');
            isRecording = true;
            usingNativeRecording = true;
            updateUI();
            resolve();
        };

        window.onNativeStreamError = (error) => {
            console.error('[REALTIME] Native streaming error:', error);
            isRecording = false;
            usingNativeRecording = false;
            toast('Native audio error: ' + error);
            reject(new Error(error));
        };

        window.onNativeAudioChunk = (base64Data) => {
            // Native Android captures at 24kHz PCM16 - exactly what OpenAI needs
            // Auto-mute while AI is speaking AND for POST_SPEECH_DELAY_MS after
            const timeSinceAIStopped = Date.now() - aiStoppedSpeakingAt;
            const inPostSpeechDelay = timeSinceAIStopped < POST_SPEECH_DELAY_MS;

            if (!ws || ws.readyState !== WebSocket.OPEN || !isRecording || isAISpeaking || inPostSpeechDelay) {
                vadIsSpeaking = false;
                vadSpeechFrameCount = 0;
                vadSilenceFrameCount = 0;
                vadPreBuffer = [];
                return;
            }

            // Decode base64 PCM16 to calculate RMS energy for VAD
            const binaryString = atob(base64Data);
            const bytes = new Uint8Array(binaryString.length);
            for (let i = 0; i < binaryString.length; i++) {
                bytes[i] = binaryString.charCodeAt(i);
            }
            const pcm16 = new Int16Array(bytes.buffer);
            let sumSquares = 0;
            for (let i = 0; i < pcm16.length; i++) {
                const sample = pcm16[i] / 32768.0;
                sumSquares += sample * sample;
            }
            const rms = Math.sqrt(sumSquares / pcm16.length);

            // VAD state machine (same as browser path)
            const isSpeechFrame = rms >= VAD_SPEECH_RMS;
            if (isSpeechFrame) {
                vadSpeechFrameCount++;
                vadSilenceFrameCount = 0;
            } else {
                vadSilenceFrameCount++;
                vadSpeechFrameCount = 0;
            }

            if (!vadIsSpeaking) {
                vadPreBuffer.push(base64Data);
                if (vadPreBuffer.length > VAD_PRE_BUFFER_SIZE) {
                    vadPreBuffer.shift();
                }
                if (vadSpeechFrameCount >= VAD_MIN_SPEECH_FRAMES) {
                    vadIsSpeaking = true;
                    for (const buffered of vadPreBuffer) {
                        ws.send(JSON.stringify({ type: 'audio_input', data: buffered }));
                    }
                    vadPreBuffer = [];
                }
            }

            if (vadIsSpeaking) {
                ws.send(JSON.stringify({ type: 'audio_input', data: base64Data }));
                if (vadSilenceFrameCount >= VAD_MAX_SILENCE_FRAMES) {
                    vadIsSpeaking = false;
                    vadPreBuffer = [];
                    // Send silence burst for OpenAI's server-side VAD
                    const silenceSamples = 480;
                    const silencePcm16 = new Int16Array(silenceSamples);
                    const silenceBytes = new Uint8Array(silencePcm16.buffer);
                    let silenceBinary = '';
                    for (let j = 0; j < silenceBytes.length; j++) {
                        silenceBinary += String.fromCharCode(silenceBytes[j]);
                    }
                    const silenceB64 = btoa(silenceBinary);
                    for (let burst = 0; burst < 40; burst++) {
                        ws.send(JSON.stringify({ type: 'audio_input', data: silenceB64 }));
                    }
                }
            }
        };

        window.onNativeStreamStop = () => {
            console.log('[REALTIME] Native streaming stopped');
            isRecording = false;
            usingNativeRecording = false;
            updateUI();
        };

        // Start native streaming
        try {
            console.log('[REALTIME] Calling AndroidMic.startAudioStreaming()...');
            AndroidMic.startAudioStreaming();
        } catch (e) {
            console.error('[REALTIME] Failed to start native streaming:', e);
            reject(e);
        }

        // Timeout if streaming doesn't start within 3 seconds
        setTimeout(() => {
            if (!isRecording) {
                console.error('[REALTIME] Native streaming timeout');
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
        console.log('[REALTIME] Stopping native audio streaming');
        AndroidMic.stopAudioStreaming();
    }

    // Send commit to trigger AI response
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
            type: 'audio_commit'
        }));
    }

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

    // Detect mobile for compatibility adjustments
    const isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);

    try {
        // First, stop any other modules using the microphone
        await stopOtherMicUsers();

        // If native Android, use native streaming (bypasses WebView getUserMedia issues)
        if (isNativeAndroid() && typeof AndroidMic.startAudioStreaming === 'function') {
            console.log('[REALTIME] Using native Android audio streaming');
            await startNativeStreaming();
            return;  // Native streaming handles everything
        }

        // Release any existing media stream
        if (mediaStream) {
            console.log('[REALTIME] Releasing existing media stream...');
            mediaStream.getTracks().forEach(track => track.stop());
            mediaStream = null;
            await new Promise(resolve => setTimeout(resolve, 100));
        }

        // Check if we have mediaDevices API
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            throw new Error('Microphone not supported. Requires HTTPS or localhost.');
        }

        // Initialize recording context at native sample rate
        await initRecordingContext();

        // Get microphone stream with mono forced and audio processing enabled
        // Audio constraints - require echo cancellation for real-time voice
        const audioConstraints = {
            audio: {
                sampleRate: { ideal: 48000 },
                channelCount: 1,              // FORCE MONO - Critical for audio interfaces
                echoCancellation: true,       // Required - not just ideal
                noiseSuppression: true,       // Required - not just ideal
                autoGainControl: true         // Required - not just ideal
            }
        };

        console.log(`[REALTIME] Requesting microphone in MONO with processing (mobile: ${isMobile}, native: ${isNativeAndroid()})`);

        // Retry logic for mic access - Samsung devices need more retries
        let retryCount = 0;
        const maxRetries = 5;  // More retries for slow audio HAL

        while (retryCount <= maxRetries) {
            try {
                mediaStream = await navigator.mediaDevices.getUserMedia(audioConstraints);
                console.log('[REALTIME] Got media stream successfully');
                break;
            } catch (micError) {
                console.error(`[REALTIME] Microphone error (attempt ${retryCount + 1}):`, micError.name, micError.message);

                if (micError.name === 'NotReadableError' && retryCount < maxRetries) {
                    // Mic busy - wait longer each retry (exponential backoff)
                    const delay = 500 + (retryCount * 500);  // 500ms, 1000ms, 1500ms, 2000ms, 2500ms
                    console.log(`[REALTIME] Mic busy, waiting ${delay}ms and retrying...`);
                    await new Promise(resolve => setTimeout(resolve, delay));
                    retryCount++;
                    continue;
                }

                if (micError.name === 'NotAllowedError') {
                    throw new Error('Microphone permission denied. Please allow microphone access.');
                } else if (micError.name === 'NotFoundError') {
                    throw new Error('No microphone found. Please connect a microphone.');
                } else if (micError.name === 'NotReadableError') {
                    throw new Error('Microphone is busy. Please close other apps or wait a moment.');
                } else {
                    throw new Error(`Microphone error: ${micError.message || micError.name}`);
                }
            }
        }

        // Create source from stream using recording context
        sourceNode = recordingContext.createMediaStreamSource(mediaStream);
        const nativeRate = recordingContext.sampleRate;
        console.log(`[REALTIME] Recording at native rate: ${nativeRate}Hz`);

        // Create script processor for audio capture
        // Use 4096 on mobile (better compatibility), 8192 on desktop (better quality)
        // Note: ScriptProcessorNode is deprecated but widely supported
        const bufferSize = isMobile ? 4096 : 8192;
        console.log(`[REALTIME] Using buffer size: ${bufferSize}`);
        scriptProcessor = recordingContext.createScriptProcessor(bufferSize, 1, 1);

        scriptProcessor.onaudioprocess = (e) => {
            if (!isRecording || !ws || ws.readyState !== WebSocket.OPEN) return;

            // Auto-mute while AI is speaking AND for POST_SPEECH_DELAY_MS after to prevent feedback
            const timeSinceAIStopped = Date.now() - aiStoppedSpeakingAt;
            const inPostSpeechDelay = timeSinceAIStopped < POST_SPEECH_DELAY_MS;
            if (isAISpeaking || inPostSpeechDelay) {
                // Reset VAD + filter state when muted
                vadIsSpeaking = false;
                vadSpeechFrameCount = 0;
                vadSilenceFrameCount = 0;
                vadPreBuffer = [];
                hpFilterPrev = 0;
                hpFilterPrevInput = 0;
                return;
            }

            const inputData = e.inputBuffer.getChannelData(0);

            // Calculate RMS on RAW audio for VAD decision (before any filtering)
            let sumSquares = 0;
            let maxLevel = 0;
            for (let i = 0; i < inputData.length; i++) {
                sumSquares += inputData[i] * inputData[i];
                const abs = Math.abs(inputData[i]);
                if (abs > maxLevel) maxLevel = abs;
            }
            const rms = Math.sqrt(sumSquares / inputData.length);

            // Log RMS every ~2 seconds for diagnostics
            vadLogCounter++;
            if (vadLogCounter % 12 === 0) {
                console.log(`[REALTIME-VAD] RMS: ${rms.toFixed(5)} | threshold: ${VAD_SPEECH_RMS} | speaking: ${vadIsSpeaking} | peak: ${maxLevel.toFixed(4)}`);
            }

            // High-pass filter with PERSISTENT state across frames
            const hpAlpha = 0.98;
            const filtered = new Float32Array(inputData.length);
            for (let i = 0; i < inputData.length; i++) {
                const currentInput = inputData[i];
                filtered[i] = hpAlpha * (hpFilterPrev + currentInput - hpFilterPrevInput);
                hpFilterPrev = filtered[i];
                hpFilterPrevInput = currentInput;
            }

            // Normalize (capped gain)
            const targetLevel = 0.4;
            const gain = maxLevel > 0.03 ? Math.min(targetLevel / maxLevel, 1.5) : 1.0;
            const normalizedData = new Float32Array(filtered.length);
            for (let i = 0; i < filtered.length; i++) {
                normalizedData[i] = Math.max(-1, Math.min(1, filtered[i] * gain));
            }
            const resampled = resampleAudio(normalizedData, nativeRate, 24000);
            const base64 = float32ToPCM16Base64(resampled);

            // --- VAD State Machine ---
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
                        ws.send(JSON.stringify({
                            type: 'audio_input',
                            data: bufferedFrame
                        }));
                    }
                    vadPreBuffer = [];
                }
            }

            if (vadIsSpeaking) {
                // Currently speaking — send audio to OpenAI
                ws.send(JSON.stringify({
                    type: 'audio_input',
                    data: base64
                }));

                // Check for speech end
                if (vadSilenceFrameCount >= VAD_MAX_SILENCE_FRAMES) {
                    vadIsSpeaking = false;
                    vadPreBuffer = [];
                    console.log(`[REALTIME-VAD] Speech ended (${vadSilenceFrameCount} silent frames) — sending silence burst`);

                    // Send ~800ms of silence so OpenAI's server-side VAD
                    // can detect end-of-speech and trigger a response.
                    // Without this, OpenAI never "sees" the silence and just waits.
                    const silenceSamples = 480;  // 20ms at 24kHz
                    const silencePcm16 = new Int16Array(silenceSamples);  // all zeros = silence
                    const silenceBytes = new Uint8Array(silencePcm16.buffer);
                    let silenceBinary = '';
                    for (let j = 0; j < silenceBytes.length; j++) {
                        silenceBinary += String.fromCharCode(silenceBytes[j]);
                    }
                    const silenceB64 = btoa(silenceBinary);
                    for (let burst = 0; burst < 40; burst++) {  // 40 * 20ms = 800ms
                        ws.send(JSON.stringify({
                            type: 'audio_input',
                            data: silenceB64
                        }));
                    }
                }
            }
        };

        sourceNode.connect(scriptProcessor);
        scriptProcessor.connect(recordingContext.destination);

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
    if (!mediaStream || !scriptProcessor) {
        // Media stream was released, need to restart
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

    // If using native Android streaming, use native stop
    if (usingNativeRecording) {
        stopNativeStreaming();
        isRecording = false;
        return;
    }

    // Stop recording
    isRecording = false;

    // Disconnect audio nodes
    if (scriptProcessor) {
        scriptProcessor.disconnect();
        scriptProcessor = null;
    }
    if (sourceNode) {
        sourceNode.disconnect();
        sourceNode = null;
    }

    // Stop media stream - release mic
    if (mediaStream) {
        console.log('[REALTIME] Releasing media stream tracks...');
        mediaStream.getTracks().forEach(track => {
            console.log(`[REALTIME] Stopping track: ${track.kind}, state: ${track.readyState}`);
            track.stop();
        });
        mediaStream = null;
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
    if (isConnected || (ws && ws.readyState === WebSocket.CONNECTING)) {
        console.log('[REALTIME] Already connected or connecting');
        return;
    }

    operator = operator || getOperator();
    if (!operator) {
        toast('Select an operator first');
        return;
    }

    // Reset session state for new connection
    sessionConversation = [];
    audioBufferChunks = [];
    isBufferingAudio = false;
    nextPlaybackTime = 0;

    // Reset VAD state
    vadIsSpeaking = false;
    vadSpeechFrameCount = 0;
    vadSilenceFrameCount = 0;
    vadPreBuffer = [];

    // Get selected voice
    const voiceSelect = $('realtimeVoiceSelect');
    selectedVoice = voiceSelect ? voiceSelect.value : 'ash';

    // Read new model + VAD config fields from dropdowns (audit M3 — no JS-side catalog)
    const modelSelect = $('realtimeModelSelect');
    const vadSelect = $('realtimeVadSelect');
    const eagernessSelect = $('realtimeEagernessSelect');
    const idleTimeoutInput = $('realtimeIdleTimeout');

    const selectedModel = (modelSelect && modelSelect.value) ? modelSelect.value : undefined;
    const vadType = (vadSelect && vadSelect.value) ? vadSelect.value : undefined;
    const vadEagerness = (vadType === 'semantic_vad' && eagernessSelect && eagernessSelect.value)
        ? eagernessSelect.value : undefined;
    const idleTimeoutRaw = (vadType === 'server_vad' && idleTimeoutInput && idleTimeoutInput.value)
        ? parseInt(idleTimeoutInput.value, 10) : NaN;
    const idleTimeoutMs = Number.isFinite(idleTimeoutRaw) ? idleTimeoutRaw : undefined;

    // Capture v2 config into module state so reconnect can replay it
    // (otherwise reconnect rebuilds with only {type, operator, voice} and
    // the backend silently falls back to env defaults — T14 F1).
    currentRealtimeModel = selectedModel || null;
    currentRealtimeVadType = vadType || null;
    currentRealtimeVadEagerness = vadEagerness || null;
    currentRealtimeIdleTimeoutMs = (idleTimeoutMs !== undefined) ? idleTimeoutMs : null;

    sessionId = crypto.randomUUID();
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProtocol}//${window.location.host}/ws/realtime/${sessionId}`;

    console.log('[REALTIME] Connecting to:', wsUrl);
    updateStatus('Connecting...');

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log('[REALTIME] WebSocket connected');
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
        if (vadType) connectMsg.vad_type = vadType;
        if (vadEagerness) connectMsg.vad_eagerness = vadEagerness;
        if (idleTimeoutMs !== undefined) connectMsg.idle_timeout_ms = idleTimeoutMs;
        ws.send(JSON.stringify(connectMsg));
    };

    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            handleMessage(msg);
        } catch (err) {
            console.error('[REALTIME] Failed to parse message:', err);
        }
    };

    ws.onclose = (event) => {
        console.log('[REALTIME] WebSocket closed:', event.code, event.reason);
        stopKeepalive();

        if (event.code === 1000 || intentionalDisconnect) {
            isConnected = false;
            updateStatus('Disconnected');
            updateUI();
            return;
        }

        isConnected = false;
        wasRecordingBeforeDisconnect = isRecording;
        if (isRecording) {
            stopRecordingFully();
        }
        attemptReconnect();
    };

    ws.onerror = (err) => {
        console.error('[REALTIME] WebSocket error:', err);
    };
}

/**
 * Save session conversation to BlackBox
 */
async function saveSessionToBlackBox() {
    if (sessionConversation.length === 0) {
        console.log('[REALTIME] No conversation to save');
        return;
    }

    const operator = getOperator();
    if (!operator) {
        console.warn('[REALTIME] No operator set, cannot save session');
        return;
    }

    // Format conversation as a readable transcript
    const transcript = sessionConversation.map(msg => {
        const role = msg.role === 'user' ? 'User' : 'AI';
        return `[${role}]: ${msg.content}`;
    }).join('\n\n');

    const sessionSummary = `=== GPT-4o Realtime Voice Session ===
Session ID: ${sessionId || 'unknown'}
Voice: ${selectedVoice}
Timestamp: ${new Date().toISOString()}
Messages: ${sessionConversation.length}

--- Transcript ---
${transcript}
--- End Session ---`;

    try {
        // Send to /chat endpoint to be captured in BlackBox
        const response = await fetch('/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                operator: operator,
                messages: [
                    {
                        role: 'user',
                        content: `[Voice Session Transcript]\n${sessionSummary}`
                    }
                ],
                provider: 'google',  // Use Google for session save
                model: 'gemini-2.5-pro',  // Most capable model for session analysis
                streaming: false,
                auto_checkpoint: false  // Don't auto-checkpoint, just record
            })
        });

        if (response.ok) {
            console.log('[REALTIME] Session saved to BlackBox');
            toast('Voice session saved to BlackBox');
        } else {
            console.error('[REALTIME] Failed to save session:', await response.text());
        }
    } catch (err) {
        console.error('[REALTIME] Error saving session:', err);
    }

    // Clear session for next conversation
    sessionConversation = [];
}

/**
 * Disconnect from Realtime API
 */
export function disconnect() {
    // NOTE: Session transcript is saved by the BACKEND when WebSocket closes

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

    stopRecordingFully();
    stopAudio();

    isConnected = false;
    sessionId = null;
    reconnectAttempts = 0;
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
            if (!currentAudioSource && accumulatedSamples.length === 0 && audioBufferChunks.length === 0) {
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
                // Track for BlackBox capture
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

        case 'user_transcript':
            // User's voice input transcribed by Whisper
            if (msg.data) {
                console.log('[REALTIME] User voice transcript:', msg.data);
                // Add to chat and session log (persisted to localStorage)
                addBubble('user', msg.data);
                sessionConversation.push({
                    role: 'user',
                    content: msg.data,
                    timestamp: new Date().toISOString(),
                    source: 'voice'
                });
            }
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
            reconnectAttempts = 0;
            updateStatus('Connected');
            break;

        case 'pong':
            lastPongTime = Date.now();
            break;

        default:
            console.log('[REALTIME] Unknown message type:', type, msg);
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
        console.log('[REALTIME] Not reconnecting (intentional or max attempts reached)');
        updateStatus('Disconnected');
        updateUI();
        if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
            toast('Realtime connection lost');
        }
        return;
    }

    reconnectAttempts++;
    const delay = Math.min(1000 * Math.pow(2, reconnectAttempts - 1), 30000);
    console.log(`[REALTIME] Reconnecting (${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS}) in ${delay}ms...`);
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
        console.log('[REALTIME] No session to reconnect to');
        return;
    }

    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProtocol}//${window.location.host}/ws/realtime/${sessionId}`;

    console.log('[REALTIME] Reconnecting to:', wsUrl);
    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log('[REALTIME] Reconnect WebSocket opened');
        intentionalDisconnect = false;
        // Restore v2 config from module state — prevents silent
        // server-default downgrade on network blip (T14 F1).
        const reconnectMsg = {
            type: 'connect',
            operator: currentOperator,
            voice: selectedVoice
        };
        if (currentRealtimeModel) reconnectMsg.model = currentRealtimeModel;
        if (currentRealtimeVadType) reconnectMsg.vad_type = currentRealtimeVadType;
        if (currentRealtimeVadEagerness) reconnectMsg.vad_eagerness = currentRealtimeVadEagerness;
        if (currentRealtimeIdleTimeoutMs !== null) reconnectMsg.idle_timeout_ms = currentRealtimeIdleTimeoutMs;
        ws.send(JSON.stringify(reconnectMsg));
    };

    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            handleMessage(msg);
            if (msg.type === 'connected' && wasRecordingBeforeDisconnect) {
                wasRecordingBeforeDisconnect = false;
                setTimeout(() => startRecording(), 500);
            }
        } catch (err) {
            console.error('[REALTIME] Failed to parse message:', err);
        }
    };

    ws.onclose = (event) => {
        console.log('[REALTIME] Reconnect WebSocket closed:', event.code);
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
        console.error('[REALTIME] Reconnect WebSocket error:', err);
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

            if (lastPongTime && (Date.now() - lastPongTime > 25000)) {
                console.log('[REALTIME] No pong received in 25s, closing for reconnect');
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
        toast('Not connected to Realtime API');
        return;
    }

    if (!text.trim()) return;

    // Add user message to chat (persisted to localStorage)
    addBubble('user', text);

    // Track for BlackBox capture
    sessionConversation.push({
        role: 'user',
        content: text.trim(),
        timestamp: new Date().toISOString()
    });

    // Send to server
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

    // Stop audio playback
    stopAudio();
    isAISpeaking = false;
    aiStoppedSpeakingAt = Date.now();  // Track when AI stopped for post-speech delay
    responseCompleteReceived = false;  // Reset response state

    // Send interrupt to server
    ws.send(JSON.stringify({
        type: 'interrupt'
    }));

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
    const statusEl = $('realtimeStatus');
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
    const transcriptEl = $('realtimeTranscript');
    if (transcriptEl) {
        transcriptEl.textContent = text;
        transcriptEl.scrollTop = transcriptEl.scrollHeight;
    }
}

/**
 * Clear transcript display
 */
function clearTranscript() {
    const transcriptEl = $('realtimeTranscript');
    if (transcriptEl) {
        transcriptEl.textContent = '';
    }
}

/**
 * Update UI button states
 */
function updateUI() {
    const banner = $('realtimeBanner');
    const connectBtn = $('realtimeConnect');
    const disconnectBtn = $('realtimeDisconnect');
    const micBtn = $('realtimeMic');
    const micText = $('realtimeMicText');

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
    const modelSelect = $('realtimeModelSelect');
    const vadSelect = $('realtimeVadSelect');
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
        if (mediaStream && scriptProcessor) {
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
    const modelSelect = $('realtimeModelSelect');
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
    const vadSelect = $('realtimeVadSelect');
    const eagernessSelect = $('realtimeEagernessSelect');
    const idleTimeoutInput = $('realtimeIdleTimeout');
    if (!vadSelect) return;
    const vadType = vadSelect.value;
    if (eagernessSelect) {
        eagernessSelect.style.display = (vadType === 'semantic_vad') ? '' : 'none';
    }
    if (idleTimeoutInput) {
        idleTimeoutInput.style.display = (vadType === 'server_vad') ? '' : 'none';
    }
}

/**
 * Initialize Realtime UI and event handlers
 */
export function initRealtimeUI() {
    const connectBtn = $('realtimeConnect');
    const disconnectBtn = $('realtimeDisconnect');
    const micBtn = $('realtimeMic');
    const toggleBtn = $('realtimeToggleBtn');
    const transcriptSection = $('realtimeTranscriptSection');
    const vadSelect = $('realtimeVadSelect');

    // Populate model dropdown from /realtime/status (5min sessionStorage cache)
    fetchRealtimeCatalog().then(catalog => {
        if (catalog) populateRealtimeModelDropdown(catalog);
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
