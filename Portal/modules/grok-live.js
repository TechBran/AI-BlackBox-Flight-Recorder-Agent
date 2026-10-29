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
 * - Audio input at 16kHz (GROK_LIVE_INPUT_SAMPLE_RATE, the bridge's session rate)
 * - Voice selection (Ara, Rex, Sal, Eve, Leo)
 * - Text input support alongside voice
 * - AI interrupt capability (barge-in)
 * - Live transcript display
 * - Session snapshot to BlackBox on disconnect
 */

import { $, toast, escapeHtml } from './core-utils.js';
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
const MIC_OWNER = 'grok-live';

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
    presetSelect: 'grokPresetSelect',
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

/** Selected voice */
let selectedVoice = 'Ara';

/**
 * Config captured at connect, replayed on reconnect — prevents silent
 * server-default downgrade on network blip (T14 F1 pattern, gpt-realtime.js).
 */
let currentGrokModel = null;
let currentGrokReasoningEffort = null;
let currentGrokPresetId = null;

/** Audio context for playback (native rate for best quality) */
let playbackContext = null;

/** Native playback sample rate */
let playbackSampleRate = 48000;

/** Audio context for recording (native rate), kept across captures */
let recordingContext = null;

/** The open browser mic capture (mic-capture.js), while recording */
let micCapture = null;

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

/** Accumulated audio samples for continuous playback */
let accumulatedSamples = new Float32Array(0);

/** Scheduled playback time for seamless audio */
let nextPlaybackTime = 0;

/** Whether we're currently buffering audio */
let isBufferingAudio = false;

/** Whether the response is complete (API sent response_complete) but audio may still be playing */
let responseCompleteReceived = false;

// =============================================================================
// Connection (live-session.js: keepalive, reconnect with backoff, teardown)
// =============================================================================

/** Whether mic was recording before disconnect (to resume after reconnect) */
let wasRecordingBeforeDisconnect = false;

/** Stored operator for reconnection */
let currentOperator = '';

const liveSession = new LiveSession({
    path: '/ws/grok-live/',
    logPrefix: '[GROK-LIVE]',
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
            toast('Grok Live connection lost');
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
 * server defaults, T14 F1). Undefined fields are omitted.
 */
function buildConnectMsg() {
    const connectMsg = {
        type: 'connect',
        operator: currentOperator,
        voice: selectedVoice
    };
    if (currentGrokModel) connectMsg.model = currentGrokModel;
    if (currentGrokReasoningEffort) connectMsg.reasoning_effort = currentGrokReasoningEffort;
    if (currentGrokPresetId) connectMsg.agent = currentGrokPresetId;
    return connectMsg;
}

// =============================================================================
// Audio rates
// =============================================================================

/**
 * Grok input rate — must match the backend's session.update
 * audio.input.format.rate (config.py GROK_LIVE_INPUT_SAMPLE_RATE, 16k).
 */
const GROK_INPUT_SAMPLE_RATE = 16000;

/** The model's output rate */
const GROK_OUTPUT_SAMPLE_RATE = 24000;

/**
 * Resample one native-mic chunk (base64 PCM16 at NATIVE_MIC_SAMPLE_RATE) to
 * the 16 kHz the bridge configures Grok for. Sent verbatim, 24 kHz audio was
 * heard at 2/3 speed and a lower pitch, and transcription on the Android
 * WebView came out garbled (audit P03-live-agents-web-03).
 * @param {string} base64Audio - Base64 PCM16 from AndroidMic
 * @param {number} [sourceRate] - The chunk's sample rate
 * @returns {string} Base64 PCM16 at 16 kHz
 */
export function resampleNativeChunkForGrok(base64Audio, sourceRate = NATIVE_MIC_SAMPLE_RATE) {
    return resamplePCM16Base64(base64Audio, sourceRate, GROK_INPUT_SAMPLE_RATE);
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
 * Play base64-encoded PCM16 audio with seamless scheduling
 * @param {string} base64Audio - Base64 encoded PCM16 at 24kHz
 */
async function playAudio(base64Audio) {
    await initPlaybackContext();

    // Decode PCM16 to float32 (at 24kHz), upsample to the native playback rate
    const float32Native = upsampleAudio(pcm16Base64ToFloat32(base64Audio), GROK_OUTPUT_SAMPLE_RATE,
                                        playbackSampleRate);

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
 * Stop other mic users (dictation, the Gemini recorder, the other voice
 * agents) before starting recording, through the shared mic arbiter.
 */
async function stopOtherMicUsers() {
    await claimMic(MIC_OWNER, stopRecordingAndSend);
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

        // If native Android, use native streaming
        if (hasNativeStreaming()) {
            console.log('[GROK-LIVE] Using native Android audio streaming');
            await startNativeStreaming();
            return;
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
            logPrefix: '[GROK-LIVE]',
            onFrame: (inputData, nativeRate) => {
                if (!isRecording || !liveSession.isOpen) return;

                // Auto-mute while AI is speaking AND for POST_SPEECH_DELAY_MS after
                const timeSinceAIStopped = Date.now() - aiStoppedSpeakingAt;
                const inPostSpeechDelay = timeSinceAIStopped < POST_SPEECH_DELAY_MS;
                if (isAISpeaking || inPostSpeechDelay) return;

                // Noise gate, high-pass, capped-gain normalize, resample to 16kHz for Grok.
                const base64 = conditionMicFrame(inputData, {
                    sourceRate: nativeRate, targetRate: GROK_INPUT_SAMPLE_RATE, highPass,
                });
                if (base64) {
                    liveSession.send({ type: 'audio_input', data: base64 });
                }
            },
        });
        recordingContext = micCapture.context;

        isRecording = true;
        updateUI();
        console.log('[GROK-LIVE] Recording started');

    } catch (err) {
        console.error('[GROK-LIVE] Failed to start recording:', err);
        toast('Microphone access failed: ' + err.message);
    }
}

/**
 * Start native Android audio streaming (24kHz PCM16, resampled to 16kHz)
 */
async function startNativeStreaming() {
    await startNativeStreamAwaited(MIC_OWNER, {
        onStart: () => {
            console.log('[GROK-LIVE] Native streaming started');
            isRecording = true;
            usingNativeRecording = true;
            updateUI();
        },
        onError: (error) => {
            console.error('[GROK-LIVE] Native streaming error:', error);
            usingNativeRecording = false;
            isRecording = false;
        },
        onChunk: (base64Audio) => {
            if (!isRecording || !liveSession.isOpen) return;

            // Auto-mute while AI is speaking
            const timeSinceAIStopped = Date.now() - aiStoppedSpeakingAt;
            const inPostSpeechDelay = timeSinceAIStopped < POST_SPEECH_DELAY_MS;
            if (isAISpeaking || inPostSpeechDelay) return;

            liveSession.send({
                type: 'audio_input',
                data: resampleNativeChunkForGrok(base64Audio)
            });
        },
        onStop: () => {
            console.log('[GROK-LIVE] Native streaming stopped');
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
    console.log('[GROK-LIVE] Stopping native audio streaming');
    stopNativeStream(MIC_OWNER);

    usingNativeRecording = false;
    isRecording = false;
    updateUI();
}

/**
 * Stop recording and send audio to AI
 */
function stopRecordingAndSend() {
    releaseMic(MIC_OWNER);
    if (!isRecording) return;

    console.log('[GROK-LIVE] Stopping recording...');

    // If using native Android streaming, use native stop
    if (usingNativeRecording) {
        stopNativeStreaming();
        // Send commit message to trigger AI response
        liveSession.send({ type: 'audio_commit' });
        updateUI();
        console.log('[GROK-LIVE] Native recording stopped, audio committed');
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
    if (isConnected || liveSession.isConnecting) {
        console.log('[GROK-LIVE] Already connected or connecting');
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
    selectedVoice = voiceSelect ? voiceSelect.value : 'Ara';

    // Read model from dropdown (hydrated from /grok-live/status models[] — P2)
    const modelSelect = SEL.modelSelect ? $(SEL.modelSelect) : null;
    const selectedModel = (modelSelect && modelSelect.value) ? modelSelect.value : undefined;
    currentGrokModel = selectedModel || null;

    // reasoning.effort (high|none) — grok-voice-think-fast background reasoning
    const reasoningSelect = SEL.reasoningSelect ? $(SEL.reasoningSelect) : null;
    const reasoningEffort = (reasoningSelect && reasoningSelect.value) ? reasoningSelect.value : undefined;
    currentGrokReasoningEffort = reasoningEffort || null;

    // Voice Agent preset (P4 registry) — sent as `agent`; backend precedence
    // is explicit params > preset > defaults, so sending both is safe.
    const presetSelect = SEL.presetSelect ? $(SEL.presetSelect) : null;
    const presetId = (presetSelect && presetSelect.value) ? presetSelect.value : undefined;
    currentGrokPresetId = presetId || null;

    // Reset session state
    accumulatedSamples = new Float32Array(0);
    isBufferingAudio = false;
    nextPlaybackTime = 0;

    currentOperator = operator;
    liveSession.connect();
}

/**
 * Disconnect from Grok Voice API
 */
export function disconnect() {
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


                // Show in transcript widget
                const transcriptEl = SEL.transcriptEl ? $(SEL.transcriptEl) : null;
                if (transcriptEl) {
                    const userEl = document.createElement('div');
                    userEl.className = 'user-turn';
                    userEl.innerHTML = `<strong>You:</strong> ${escapeHtml(userText)}`;
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
            updateStatus('Connected');
            break;

        case 'pong':
            break;  // keepalive (live-session.js)

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

    // Preset dropdown from GET /voice-agents (P4 registry; row hidden pre-P4)
    fetchVoicePresets().then(presets => {
        const presetSelect = SEL.presetSelect ? $(SEL.presetSelect) : null;
        populatePresetDropdown(presetSelect, filterPresetsByProvider(presets, ['grok', 'grok-live', 'xai']));
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
            if (text && liveSession.isOpen) {
                liveSession.send({
                    type: 'text_input',
                    text: text
                });
                textInput.value = '';
                // The user's turn in the chat, as the other live agents show it.
                addBubble('user', text);


                // Show in transcript
                const transcriptEl = SEL.transcriptEl ? $(SEL.transcriptEl) : null;
                if (transcriptEl) {
                    const userEl = document.createElement('div');
                    userEl.className = 'user-turn';
                    userEl.innerHTML = `<strong>You:</strong> ${escapeHtml(text)}`;
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
            if (liveSession.send({ type: 'interrupt' })) {
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
