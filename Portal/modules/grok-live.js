/**
 * grok-live.js
 * xAI Grok Voice Agent API WebSocket client with audio handling
 *
 * Provides real-time voice conversations with Grok using semantic memory search.
 * Connects to Orchestrator which bridges to xAI Grok Voice Agent API.
 *
 * Features:
 * - Toggle-to-record audio input (click to start, click to stop)
 * - Real-time audio playback from AI (24kHz)
 * - Audio input at 24kHz (Grok standard)
 * - Voice selection (Ara, Rex, Sal, Eve, Leo)
 * - Text input support alongside voice
 * - AI interrupt capability (barge-in)
 * - Live transcript display
 * - Session snapshot to BlackBox on disconnect
 */

import { $, toast } from './core-utils.js';
import { getOperator, saveHistory } from './state-management.js';
import { addBubble, appendBubble } from './chat-bubbles.js';

// =============================================================================
// Selector configuration
// =============================================================================
//
// Track 4 (2026-05-20): inline banners deprecated. Module reads DOM ids from
// the SEL table below; the Voice Agents modal passes its own ids via
// initGrokLiveUI({selectors: {...}}) so the same code drives the modal.
// Defaults preserved for backward-compat; production uses modal-supplied ids.
const SEL = {
    voiceSelect: 'grokVoiceSelect',
    modelSelect: 'grokModelSelect',
    reasoningSelect: 'grokReasoningSelect',
    connectButton: 'grokConnectBtn',
    disconnectButton: 'grokDisconnectBtn',
    micButton: 'grokMicBtn',
    micText: 'grokMicText',
    statusEl: 'grokLiveStatus',
    transcriptEl: 'grokTranscript',
    transcriptSection: 'grokTranscriptSection',
    toggleButton: 'grokToggleBtn',
    sendTextButton: 'grokSendTextBtn',
    textInput: 'grokTextInput',
    interruptButton: 'grokInterruptBtn',
    speakingIndicator: 'grokSpeakingIndicator',
};

// =============================================================================
// State
// =============================================================================

/** WebSocket connection to Orchestrator */
let ws = null;

/** Session ID */
let sessionId = null;

/** Selected voice */
let selectedVoice = 'Ara';

/**
 * Config captured at connect, replayed on reconnect — prevents silent
 * server-default downgrade on network blip (T14 F1 pattern, gpt-realtime.js).
 */
let currentGrokModel = null;
let currentGrokReasoningEffort = null;

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

/** Whether connected to Grok Voice API */
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
 * Resample audio from source rate to 16kHz for Grok input
 * @param {Float32Array} input - Input samples
 * @param {number} sourceRate - Source sample rate
 * @returns {Float32Array} Resampled audio at 16kHz
 */
function resampleTo24kHz(input, sourceRate) {
    const targetRate = 16000;  // Grok input rate — must match the backend's session.update audio.input.format.rate (16k)

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
    const sourceRate = 24000;  // Grok outputs 24kHz

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
        console.log(`[GROK-LIVE] Playback context created at ${playbackSampleRate}Hz (native)`);
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
        console.log(`[GROK-LIVE] Recording context created at ${recordingContext.sampleRate}Hz`);
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

    // Upsample to native playback rate for smooth playback
    const float32Native = upsampleFrom24kHz(float32_24k, playbackSampleRate);

    // Accumulate samples for continuous playback
    const newAccumulated = new Float32Array(accumulatedSamples.length + float32Native.length);
    newAccumulated.set(accumulatedSamples);
    newAccumulated.set(float32Native, accumulatedSamples.length);
    accumulatedSamples = newAccumulated;

    // Start buffering mode if we have enough samples
    const bufferThreshold = playbackSampleRate * 0.15;  // 150ms buffer
    if (!isBufferingAudio && accumulatedSamples.length >= bufferThreshold) {
        isBufferingAudio = true;
        schedulePlayback();
    }
}

/**
 * Schedule playback of accumulated audio samples
 */
function schedulePlayback() {
    if (!playbackContext || accumulatedSamples.length === 0) {
        isBufferingAudio = false;
        return;
    }

    isAISpeaking = true;

    // Create buffer from accumulated samples
    const buffer = playbackContext.createBuffer(1, accumulatedSamples.length, playbackSampleRate);
    buffer.getChannelData(0).set(accumulatedSamples);

    // Create source node
    const source = playbackContext.createBufferSource();
    source.buffer = buffer;
    source.connect(playbackContext.destination);

    // Schedule playback
    const currentTime = playbackContext.currentTime;
    if (nextPlaybackTime < currentTime) {
        nextPlaybackTime = currentTime + 0.01;
    }

    source.start(nextPlaybackTime);
    currentAudioSource = source;

    // Calculate when this chunk ends
    const chunkDuration = accumulatedSamples.length / playbackSampleRate;
    nextPlaybackTime += chunkDuration;

    // Clear accumulated samples
    accumulatedSamples = new Float32Array(0);

    source.onended = () => {
        isAISpeaking = false;
        aiStoppedSpeakingAt = Date.now();
        currentAudioSource = null;

        // If response is complete and no more audio, we're done
        if (responseCompleteReceived && accumulatedSamples.length === 0) {
            isBufferingAudio = false;
            updateUI();
        } else if (accumulatedSamples.length > 0) {
            // Schedule more if we have accumulated audio
            schedulePlayback();
        } else {
            isBufferingAudio = false;
        }
    };

    updateUI();
}

/**
 * Stop audio playback
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
    accumulatedSamples = new Float32Array(0);
    nextPlaybackTime = 0;
    isAISpeaking = false;
    isBufferingAudio = false;
}

// =============================================================================
// Native Android Audio Support
// =============================================================================

/**
 * Check if running in native Android WebView
 */
function isNativeAndroid() {
    return typeof AndroidMic !== 'undefined';
}

/**
 * Stop other mic users before starting recording
 */
async function stopOtherMicUsers() {
    // Stop GPT Realtime if it's using mic
    if (typeof window.realtimeStopRecording === 'function') {
        try {
            window.realtimeStopRecording();
            await new Promise(resolve => setTimeout(resolve, 200));
        } catch (e) {}
    }

    // Stop Gemini Live if it's using mic
    if (typeof window.geminiLiveStopRecording === 'function') {
        try {
            window.geminiLiveStopRecording();
            await new Promise(resolve => setTimeout(resolve, 200));
        } catch (e) {}
    }
}

// =============================================================================
// Audio Recording
// =============================================================================

/**
 * Start audio recording - captures at native rate, resamples to 24kHz
 */
async function startRecording() {
    if (isRecording) return;

    const isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);

    try {
        await stopOtherMicUsers();

        // If native Android, use native streaming
        if (isNativeAndroid() && typeof AndroidMic !== 'undefined' && typeof AndroidMic.startAudioStreaming === 'function') {
            console.log('[GROK-LIVE] Using native Android audio streaming');
            await startNativeStreaming();
            return;
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

        const audioConstraints = {
            audio: {
                sampleRate: { ideal: 48000 },
                channelCount: 1,
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true
            }
        };

        console.log(`[GROK-LIVE] Requesting microphone (mobile: ${isMobile})`);

        let retryCount = 0;
        const maxRetries = 5;

        while (retryCount <= maxRetries) {
            try {
                mediaStream = await navigator.mediaDevices.getUserMedia(audioConstraints);
                console.log('[GROK-LIVE] Got media stream successfully');
                break;
            } catch (micError) {
                console.error(`[GROK-LIVE] Microphone error (attempt ${retryCount + 1}):`, micError.name);

                if (micError.name === 'NotReadableError' && retryCount < maxRetries) {
                    const delay = 500 + (retryCount * 500);
                    console.log(`[GROK-LIVE] Mic busy, waiting ${delay}ms...`);
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
        console.log(`[GROK-LIVE] Recording at native rate: ${nativeRate}Hz, resampling to 24kHz`);

        const bufferSize = isMobile ? 4096 : 8192;
        scriptProcessor = recordingContext.createScriptProcessor(bufferSize, 1, 1);

        scriptProcessor.onaudioprocess = (e) => {
            if (!isRecording || !ws || ws.readyState !== WebSocket.OPEN) return;

            // Auto-mute while AI is speaking AND for POST_SPEECH_DELAY_MS after
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

            // Resample to 24kHz for Grok
            const resampled = resampleTo24kHz(normalizedData, nativeRate);
            const base64 = float32ToPCM16Base64(resampled);

            ws.send(JSON.stringify({
                type: 'audio_input',
                data: base64
            }));
        };

        sourceNode.connect(scriptProcessor);
        scriptProcessor.connect(recordingContext.destination);

        isRecording = true;
        updateUI();
        console.log('[GROK-LIVE] Recording started');

    } catch (err) {
        console.error('[GROK-LIVE] Failed to start recording:', err);
        toast('Microphone access failed: ' + err.message);
    }
}

/**
 * Start native Android audio streaming
 */
async function startNativeStreaming() {
    return new Promise((resolve, reject) => {
        // Set up handlers for native streaming
        window.onGrokNativeStreamStart = () => {
            console.log('[GROK-LIVE] Native streaming started');
            isRecording = true;
            usingNativeRecording = true;
            updateUI();
            resolve();
        };

        window.onGrokNativeStreamError = (error) => {
            console.error('[GROK-LIVE] Native streaming error:', error);
            usingNativeRecording = false;
            isRecording = false;
            reject(new Error(error));
        };

        window.onGrokNativeAudioChunk = (base64Audio) => {
            if (!isRecording || !ws || ws.readyState !== WebSocket.OPEN) return;

            // Auto-mute while AI is speaking
            const timeSinceAIStopped = Date.now() - aiStoppedSpeakingAt;
            const inPostSpeechDelay = timeSinceAIStopped < POST_SPEECH_DELAY_MS;
            if (isAISpeaking || inPostSpeechDelay) return;

            ws.send(JSON.stringify({
                type: 'audio_input',
                data: base64Audio
            }));
        };

        window.onGrokNativeStreamStop = () => {
            console.log('[GROK-LIVE] Native streaming stopped');
            isRecording = false;
            usingNativeRecording = false;
            updateUI();
        };

        try {
            // Redirect callbacks to Grok handlers
            window.onNativeStreamStart = window.onGrokNativeStreamStart;
            window.onNativeStreamError = window.onGrokNativeStreamError;
            window.onNativeAudioChunk = window.onGrokNativeAudioChunk;
            window.onNativeStreamStop = window.onGrokNativeStreamStop;

            AndroidMic.startAudioStreaming();
        } catch (e) {
            console.error('[GROK-LIVE] Failed to start native streaming:', e);
            reject(e);
        }

        setTimeout(() => {
            if (!isRecording) {
                console.error('[GROK-LIVE] Native streaming timeout');
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
        console.log('[GROK-LIVE] Stopping native audio streaming');
        AndroidMic.stopAudioStreaming();
    }

    usingNativeRecording = false;
    isRecording = false;
    updateUI();
}

/**
 * Stop recording and send audio to AI
 */
function stopRecordingAndSend() {
    if (!isRecording) return;

    console.log('[GROK-LIVE] Stopping recording...');

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
        console.log('[GROK-LIVE] Native recording stopped, audio committed');
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
    console.log('[GROK-LIVE] Recording stopped, audio committed');
}

// =============================================================================
// WebSocket Connection
// =============================================================================

/**
 * Connect to Grok Voice API via Orchestrator
 * @param {string} operator - Operator name
 */
export async function connect(operator) {
    if (isConnected || (ws && ws.readyState === WebSocket.CONNECTING)) {
        console.log('[GROK-LIVE] Already connected or connecting');
        return;
    }

    operator = operator || getOperator();
    if (!operator) {
        toast('Select an operator first');
        return;
    }

    // Get selected voice
    const voiceSelect = SEL.voiceSelect ? $(SEL.voiceSelect) : null;
    selectedVoice = voiceSelect ? voiceSelect.value : 'Ara';

    // Read model from dropdown (hydrated from /grok-live/status models[] — P2)
    const modelSelect = SEL.modelSelect ? $(SEL.modelSelect) : null;
    const selectedModel = (modelSelect && modelSelect.value) ? modelSelect.value : undefined;
    currentGrokModel = selectedModel || null;

    // reasoning.effort (high|none) — grok-voice-think-fast background reasoning
    const reasoningSelect = SEL.reasoningSelect ? $(SEL.reasoningSelect) : null;
    const reasoningEffort = (reasoningSelect && reasoningSelect.value) ? reasoningSelect.value : undefined;
    currentGrokReasoningEffort = reasoningEffort || null;

    // Reset session state
    sessionConversation = [];
    accumulatedSamples = new Float32Array(0);
    isBufferingAudio = false;
    nextPlaybackTime = 0;

    sessionId = crypto.randomUUID();
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProtocol}//${window.location.host}/ws/grok-live/${sessionId}`;

    console.log('[GROK-LIVE] Connecting to:', wsUrl);
    updateStatus('Connecting...');

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log('[GROK-LIVE] WebSocket connected');
        updateStatus('Initializing...');
        currentOperator = operator;
        intentionalDisconnect = false;

        // Build connect message; omit undefined fields to keep the wire clean
        const connectMsg = {
            type: 'connect',
            operator: operator,
            voice: selectedVoice
        };
        if (selectedModel) connectMsg.model = selectedModel;
        if (reasoningEffort) connectMsg.reasoning_effort = reasoningEffort;
        ws.send(JSON.stringify(connectMsg));
    };

    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            handleMessage(msg);
        } catch (err) {
            console.error('[GROK-LIVE] Failed to parse message:', err);
        }
    };

    ws.onclose = (event) => {
        console.log('[GROK-LIVE] WebSocket closed:', event.code, event.reason);
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
            stopRecordingAndSend();
        }
        attemptReconnect();
    };

    ws.onerror = (err) => {
        console.error('[GROK-LIVE] WebSocket error:', err);
    };
}

/**
 * Disconnect from Grok Voice API
 */
export function disconnect() {
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
    sessionConversation = [];
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
            toast(`Grok Live connected (${msg.data.voice})`);
            console.log('[GROK-LIVE] Connected:', msg.data);
            break;

        case 'status':
            updateStatus(msg.data);
            break;

        case 'audio_delta':
            responseCompleteReceived = false;
            playAudio(msg.data);
            break;

        case 'transcript_delta':
            transcriptBuffer += msg.data;
            updateTranscript(transcriptBuffer);
            break;

        case 'text_delta':
            transcriptBuffer += msg.data;
            updateTranscript(transcriptBuffer);
            break;

        case 'response_complete':
            responseCompleteReceived = true;
            const transcript = msg.data?.transcript || transcriptBuffer;

            if (transcript.trim()) {
                // Add to main chat UI
                addBubble('assistant', transcript.trim());

                // Save to session conversation
                sessionConversation.push({
                    role: 'assistant',
                    content: transcript.trim(),
                    timestamp: new Date().toISOString()
                });
            }

            transcriptBuffer = '';
            console.log('[GROK-LIVE] Response complete');
            break;

        case 'user_transcript_delta':
            // Incremental (interim) user transcription chunk — live word-by-word.
            // Mirrors gpt-realtime.js: accumulate into a buffer and render into a
            // transient (non-persisted) user bubble that updates as chunks arrive.
            // The final user_transcript commits it. Grok may not emit deltas — if
            // it doesn't, this case never fires and behavior is unchanged.
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
            // User's voice input was transcribed (authoritative final)
            const userText = msg.data;
            if (userText && userText.trim()) {
                if (liveUserBubble) {
                    // Drop the transient live bubble, then persist via addBubble
                    // so there's exactly one final bubble (no duplicate).
                    liveUserBubble.remove();
                    liveUserBubble = null;
                }
                // Add to main chat UI
                addBubble('user', userText.trim());

                // Save to session conversation
                sessionConversation.push({
                    role: 'user',
                    content: userText.trim(),
                    timestamp: new Date().toISOString(),
                    source: 'voice'
                });

                // Show in transcript widget
                const transcriptEl = SEL.transcriptEl ? $(SEL.transcriptEl) : null;
                if (transcriptEl) {
                    const userEl = document.createElement('div');
                    userEl.className = 'user-turn';
                    userEl.innerHTML = `<strong>You:</strong> ${userText}`;
                    transcriptEl.appendChild(userEl);
                    transcriptEl.scrollTop = transcriptEl.scrollHeight;
                }
            }
            // Reset for the next utterance (covers delta-less providers too).
            userTranscriptBuffer = '';
            break;

        case 'speech_started':
            console.log('[GROK-LIVE] User speech detected');
            break;

        case 'speech_stopped':
            console.log('[GROK-LIVE] User speech stopped');
            break;

        case 'tool_call':
            console.log('[GROK-LIVE] Tool called:', msg.data);
            updateTranscript(`[Searching memory: "${msg.data.arguments?.query || ''}"]`);
            break;

        case 'tool_result':
            console.log('[GROK-LIVE] Tool result received');
            break;

        case 'image_task':
            console.log('[GROK-LIVE] Image generation task:', msg.data);
            // Track with task manager
            import('./task-manager.js').then(({ taskManager }) => {
                const { task_id, prompt, count } = msg.data;
                taskManager.addTask(task_id, 'image_generation', prompt, null, count || 1);
                console.log(`[GROK-LIVE] Tracking image generation: ${task_id}`);
            });
            break;

        case 'video_task':
            console.log('[GROK-LIVE] Video generation task:', msg.data);
            // Track with task manager
            import('./task-manager.js').then(({ taskManager }) => {
                const { task_id, prompt, duration, resolution } = msg.data;
                taskManager.addTask(task_id, 'video_generation', prompt, { duration, resolution });
                console.log(`[GROK-LIVE] Tracking video generation: ${task_id}`);
            });
            break;

        case 'music_task':
            console.log('[GROK-LIVE] Music generation task:', msg.data);
            // Track with task manager - count is 5th param for lyria_music
            import('./task-manager.js').then(({ taskManager }) => {
                const { task_id, prompt, sample_count } = msg.data;
                taskManager.addTask(task_id, 'lyria_music', prompt, null, sample_count || 1);
                console.log(`[GROK-LIVE] Tracking music generation: ${task_id}`);
            });
            break;

        case 'error':
            console.error('[GROK-LIVE] Error:', msg.data);
            toast(`Grok error: ${msg.data}`);
            break;

        case 'reconnecting':
            console.log('[GROK-LIVE] Backend reconnecting:', msg.data);
            updateStatus(`Reconnecting (${msg.data.attempt}/${msg.data.max})...`);
            break;

        case 'reconnected':
            console.log('[GROK-LIVE] Backend reconnected on attempt', msg.data.attempt);
            reconnectAttempts = 0;
            updateStatus('Connected');
            break;

        case 'pong':
            lastPongTime = Date.now();
            break;

        case 'disconnected':
            console.log('[GROK-LIVE] Disconnected:', msg.data);
            isConnected = false;
            updateStatus('Disconnected');
            updateUI();
            break;

        default:
            console.log('[GROK-LIVE] Unknown message type:', type, msg);
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
        console.log('[GROK-LIVE] Not reconnecting (intentional or max attempts reached)');
        updateStatus('Disconnected');
        updateUI();
        if (reconnectAttempts >= MAX_RECONNECT_ATTEMPTS) {
            toast('Grok Live connection lost');
        }
        return;
    }

    reconnectAttempts++;
    const delay = Math.min(1000 * Math.pow(2, reconnectAttempts - 1), 30000);
    console.log(`[GROK-LIVE] Reconnecting (${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS}) in ${delay}ms...`);
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
        console.log('[GROK-LIVE] No session to reconnect to');
        return;
    }

    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProtocol}//${window.location.host}/ws/grok-live/${sessionId}`;

    console.log('[GROK-LIVE] Reconnecting to:', wsUrl);
    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log('[GROK-LIVE] Reconnect WebSocket opened');
        intentionalDisconnect = false;
        // Restore config from module state — prevents silent server-default
        // downgrade on network blip (T14 F1 pattern from gpt-realtime.js).
        const reconnectMsg = {
            type: 'connect',
            operator: currentOperator,
            voice: selectedVoice
        };
        if (currentGrokModel) reconnectMsg.model = currentGrokModel;
        if (currentGrokReasoningEffort) reconnectMsg.reasoning_effort = currentGrokReasoningEffort;
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
            console.error('[GROK-LIVE] Failed to parse message:', err);
        }
    };

    ws.onclose = (event) => {
        console.log('[GROK-LIVE] Reconnect WebSocket closed:', event.code);
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
        console.error('[GROK-LIVE] Reconnect WebSocket error:', err);
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
                console.log('[GROK-LIVE] No pong received in 25s, closing for reconnect');
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
// UI Updates
// =============================================================================

/**
 * Update connection status display
 * @param {string} status - Status text
 */
function updateStatus(status) {
    const statusEl = SEL.statusEl ? $(SEL.statusEl) : null;
    if (statusEl) {
        statusEl.textContent = status;
        statusEl.className = `status ${isConnected ? 'connected' : 'disconnected'}`;
    }
}

/**
 * Update transcript display
 * @param {string} text - Transcript text
 */
function updateTranscript(text) {
    const transcriptEl = SEL.transcriptEl ? $(SEL.transcriptEl) : null;
    if (transcriptEl) {
        // Find or create current AI turn element
        let aiTurn = transcriptEl.querySelector('.ai-turn.current');
        if (!aiTurn) {
            aiTurn = document.createElement('div');
            aiTurn.className = 'ai-turn current';
            aiTurn.innerHTML = '<strong>Grok:</strong> <span class="content"></span>';
            transcriptEl.appendChild(aiTurn);
        }
        const contentEl = aiTurn.querySelector('.content');
        if (contentEl) {
            contentEl.textContent = text;
        }
        transcriptEl.scrollTop = transcriptEl.scrollHeight;

        // Mark as complete when response is done
        if (responseCompleteReceived) {
            aiTurn.classList.remove('current');
        }
    }
}

/**
 * Update UI state (buttons, indicators)
 */
function updateUI() {
    const micBtn = SEL.micButton ? $(SEL.micButton) : null;
    const connectBtn = SEL.connectButton ? $(SEL.connectButton) : null;
    const disconnectBtn = SEL.disconnectButton ? $(SEL.disconnectButton) : null;
    const voiceSelect = SEL.voiceSelect ? $(SEL.voiceSelect) : null;

    if (micBtn) {
        micBtn.disabled = !isConnected;
        micBtn.classList.toggle('recording', isRecording);
        // Update mic button text
        const micText = SEL.micText ? $(SEL.micText) : null;
        if (micText) {
            micText.textContent = isRecording ? 'Stop' : 'Mic';
        }
    }

    // Connect button: never repurpose to "Disconnect" when a dedicated
    // disconnect button exists (modal layout). Preserve legacy
    // single-button label-swap behavior only when disconnectBtn absent.
    if (connectBtn) {
        if (!disconnectBtn) {
            const btnLabel = connectBtn.querySelector('.btn-label');
            if (btnLabel) {
                btnLabel.textContent = isConnected ? 'Disconnect' : 'Connect';
            }
        }
        connectBtn.classList.toggle('connected', isConnected);
        connectBtn.disabled = isConnected;
    }

    if (disconnectBtn) {
        disconnectBtn.disabled = !isConnected;
    }

    if (voiceSelect) {
        voiceSelect.disabled = isConnected;
    }

    // Update speaking indicator
    const speakingIndicator = SEL.speakingIndicator ? $(SEL.speakingIndicator) : null;
    if (speakingIndicator) {
        speakingIndicator.classList.toggle('active', isAISpeaking);
    }
}

// =============================================================================
// Check Availability
// =============================================================================

/**
 * Check if Grok Live API is available
 * @returns {Promise<boolean>} Whether Grok Live is available
 */
export async function checkGrokLiveAvailable() {
    try {
        const response = await fetch('/grok-live/status');
        if (response.ok) {
            const data = await response.json();
            return data.available === true;
        }
        return false;
    } catch (err) {
        console.error('[GROK-LIVE] Failed to check availability:', err);
        return false;
    }
}

/**
 * Fetch Grok Live catalog from /grok-live/status with 5min sessionStorage
 * cache. Mirrors gpt-realtime.js fetchRealtimeCatalog() (audit M3 — no
 * JS-side catalog).
 * @returns {Promise<Object|null>} status/catalog object, or null on failure
 */
async function fetchGrokLiveCatalog() {
    const CACHE_TTL_MS = 5 * 60 * 1000;  // 5 minutes
    const cacheKey = 'bb_grok_live_catalog';

    try {
        const cached = JSON.parse(sessionStorage.getItem(cacheKey) || 'null');
        if (cached && Date.now() - cached.ts < CACHE_TTL_MS && cached.data) {
            console.log(`[GROK-LIVE] Catalog cache hit (age ${Math.round((Date.now() - cached.ts) / 1000)}s)`);
            return cached.data;
        }
    } catch (_) { /* corrupted cache — fall through */ }

    try {
        const res = await fetch('/grok-live/status');
        if (res.ok) {
            const data = await res.json();
            try {
                sessionStorage.setItem(cacheKey, JSON.stringify({ ts: Date.now(), data }));
            } catch (_) { /* sessionStorage full or disabled */ }
            return data;
        }
    } catch (err) {
        console.error('[GROK-LIVE] Failed to fetch catalog:', err);
    }
    return null;
}

/**
 * Populate the Grok model dropdown from catalog models[] (P2 contract:
 * models: [{id, name}], model_default). No-op when fields absent (pre-P2
 * backend): connect() then omits `model` and the backend default applies.
 */
function populateGrokModelDropdown(catalog) {
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
    console.log(`[GROK-LIVE] Model dropdown populated with ${catalog.models.length} entries, default=${catalog.model_default}`);
}

// =============================================================================
// UI Initialization
// =============================================================================

/**
 * Initialize Grok Live UI components.
 *
 * @param {Object} [config] - Optional configuration.
 * @param {Object} [config.selectors] - Override DOM ids (Track 4 modal hook).
 *   Any omitted key falls back to the historical inline-banner default in SEL.
 */
export function initGrokLiveUI(config) {
    if (config && config.selectors && typeof config.selectors === 'object') {
        Object.assign(SEL, config.selectors);
    }
    console.log('[GROK-LIVE] Initializing UI...');

    // Populate model dropdown from /grok-live/status (5min sessionStorage cache)
    fetchGrokLiveCatalog().then(catalog => {
        if (catalog) populateGrokModelDropdown(catalog);
    });

    const micBtn = SEL.micButton ? $(SEL.micButton) : null;
    const connectBtn = SEL.connectButton ? $(SEL.connectButton) : null;
    const sendTextBtn = SEL.sendTextButton ? $(SEL.sendTextButton) : null;
    const textInput = SEL.textInput ? $(SEL.textInput) : null;
    const interruptBtn = SEL.interruptButton ? $(SEL.interruptButton) : null;
    const disconnectBtn = SEL.disconnectButton ? $(SEL.disconnectButton) : null;

    // Connect button — if a dedicated disconnect button is wired (modal
    // layout), this only initiates connect. Otherwise, fall back to legacy
    // single-button toggle.
    if (connectBtn) {
        connectBtn.addEventListener('click', () => {
            if (disconnectBtn) {
                if (!isConnected) connect();
            } else if (isConnected) {
                disconnect();
            } else {
                connect();
            }
        });
    }

    // Microphone button - toggle to talk
    if (micBtn) {
        micBtn.addEventListener('click', () => {
            if (!isConnected) {
                toast('Connect to Grok first');
                return;
            }

            if (isRecording) {
                stopRecordingAndSend();
            } else {
                startRecording();
            }
        });
    }

    // Send text button
    if (sendTextBtn && textInput) {
        sendTextBtn.addEventListener('click', () => {
            const text = textInput.value.trim();
            if (text && ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({
                    type: 'text_input',
                    text: text
                }));
                textInput.value = '';

                // Add to conversation
                sessionConversation.push({
                    role: 'user',
                    content: text
                });

                // Show in transcript
                const transcriptEl = SEL.transcriptEl ? $(SEL.transcriptEl) : null;
                if (transcriptEl) {
                    const userEl = document.createElement('div');
                    userEl.className = 'user-turn';
                    userEl.innerHTML = `<strong>You:</strong> ${text}`;
                    transcriptEl.appendChild(userEl);
                    transcriptEl.scrollTop = transcriptEl.scrollHeight;
                }
            }
        });

        textInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendTextBtn.click();
            }
        });
    }

    // Interrupt button
    if (interruptBtn) {
        interruptBtn.addEventListener('click', () => {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'interrupt' }));
                stopAudio();
            }
        });
    }

    // Disconnect button (separate from connect — already resolved above
    // as `disconnectBtn`; just wire its click handler here).
    if (disconnectBtn) {
        disconnectBtn.addEventListener('click', () => {
            disconnect();
        });
    }

    // Toggle transcript section
    const toggleBtn = SEL.toggleButton ? $(SEL.toggleButton) : null;
    const transcriptSection = SEL.transcriptSection ? $(SEL.transcriptSection) : null;
    if (toggleBtn && transcriptSection) {
        toggleBtn.addEventListener('click', () => {
            const isCollapsed = transcriptSection.classList.toggle('collapsed');
            toggleBtn.textContent = isCollapsed ? '▼' : '▲';
            // Trigger banner height recalculation
            if (typeof window.updateClaudeCodeBanner === 'function') {
                window.updateClaudeCodeBanner('grok-live');
            }
        });
    }

    // Expose stop recording function globally for mic coordination
    window.grokLiveStopRecording = stopRecordingAndSend;

    console.log('[GROK-LIVE] UI initialized');
}

// Export for external use
export { isConnected, isRecording, selectedVoice };
