/**
 * stt-stream.js
 * Shared web streaming speech-to-text client.
 *
 * Streams microphone audio over WS /ws/stt and applies the backend's
 * CUMULATIVE interim transcripts directly into a target input/textarea.
 *
 * Contract (uniform across providers — the backend normalizes everything):
 *   Client -> server:
 *     {type:"stt_start", target:"<id>", provider:"", lang:"en", sample_rate:16000}
 *     {type:"stt_audio", pcm:"<base64 PCM16>"}   (repeated)
 *     {type:"stt_stop"}
 *   Server -> client:
 *     {type:"stt_delta", text:"<CUMULATIVE interim so far>", target:...}
 *     {type:"stt_final", text:"<full final>", target:...}
 *     {type:"stt_error", message:...}
 *     {type:"stt_done"}                    (terminal — always the last frame)
 *
 * KEY: stt_delta.text is CUMULATIVE and already normalized backend-side, so the
 * client simply REPLACES the interim region with `text` on each delta and
 * COMMITS on stt_final. No per-provider logic lives here.
 *
 * Stop handshake (2026-07-09, mirrors the Android client): stop() keys the
 * close on the server's terminal stt_done — which arrives right after the
 * trailing stt_final — instead of a blind grace timer. If the stop window
 * closes with NO final having landed (backstop expiry / dead socket / server
 * close without stt_done), the newest interim is fallback-committed so the
 * words the user watched appear are not dropped. The server's authoritative
 * EMPTY final for hallucination-filtered stops clears the pending interim
 * first, so filtered interims are discarded, not resurrected.
 *
 * Audio capture mechanics (resampling, PCM16 encoding, native Android path) are
 * mirrored from Portal/modules/gemini-live.js — the proven voice-agent capture
 * path — minus the AI-speaking auto-mute logic (an STT mic has no AI playback).
 */

import { $, toast } from './core-utils.js';

// =============================================================================
// Module state
// =============================================================================

let ws = null;                  // WebSocket to /ws/stt
let streaming = false;          // public isStreaming() flag
let wsReady = false;            // true once stt_start has been sent

// Capture state (browser path)
let recordingContext = null;    // AudioContext
let mediaStream = null;         // MediaStream from getUserMedia
let sourceNode = null;          // MediaStreamAudioSourceNode
let scriptProcessor = null;     // ScriptProcessorNode
let nativeRate = 48000;         // actual capture rate, set on start

// Native Android path
let usingNative = false;
let prevNativeAudioChunk = null;   // saved window.onNativeAudioChunk to restore
let prevNativeStreamStart = null;
let prevNativeStreamStop = null;
let prevNativeStreamError = null;

// Target / delta-applier state
let targetId = null;            // id of the input/textarea
let buttonId = null;            // id of the trigger button (visual state)
let baseBefore = '';            // committed text before the insertion point
let baseAfter = '';             // text after the insertion point (untouched)
let interimLen = 0;             // length of the current interim region

// Stop-handshake state (2026-07-09): stop() waits for the server's terminal
// stt_done (arrives right after the trailing stt_final) — closeTimer is now the
// disaster BACKSTOP, not the expected wait (the server bounds its own provider
// drains at 5-8s; see stt_ws_routes.py). lastInterim is the newest cumulative
// interim, fallback-committed if the stop window ends with no final (any real
// final — including the authoritative EMPTY one for filtered stops — clears it).
let closeTimer = null;
let stopping = false;
let lastInterim = '';
const STOP_BACKSTOP_MS = 10000;

// Fast-failure / fallback support.
// onUnavailable() fires exactly once when streaming can't get going (ws fails to
// open within OPEN_TIMEOUT_MS, ws errors before any transcript, or the backend
// reports a provider/connection stt_error before any transcript). It lets a
// caller fall back to a legacy record->/stt path for THAT invocation. Once any
// transcript (delta/final) has been applied, fallback is no longer offered.
let onUnavailable = null;          // one-shot callback, cleared after firing
let gotTranscript = false;         // true once a delta/final landed
let openTimer = null;              // ws-open watchdog
const OPEN_TIMEOUT_MS = 4000;

function fireUnavailable(reason) {
    const cb = onUnavailable;
    onUnavailable = null;
    if (openTimer) { clearTimeout(openTimer); openTimer = null; }
    console.warn('[STT-STREAM] streaming unavailable:', reason);
    if (typeof cb === 'function') {
        try { cb(reason); } catch (e) { console.error('[STT-STREAM] onUnavailable cb error:', e); }
    }
    return !!cb;  // whether a fallback handler consumed it
}

// =============================================================================
// Native Android detection (same check as tts-stt.js / gemini-live.js)
// =============================================================================

function isNativeAndroid() {
    return typeof AndroidMic !== 'undefined';
}

function hasMic() {
    return isNativeAndroid() || !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
}

// =============================================================================
// Audio encoding helpers (mirrored from gemini-live.js)
// =============================================================================

/**
 * Convert a Float32 sample buffer to base64-encoded PCM16.
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
 * Single-pole low-pass filter to prevent aliasing before downsampling.
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
 * Resample Float32 audio from sourceRate to 24kHz (Catmull-Rom interpolation
 * with anti-alias low-pass). 24kHz is the single uniform STT rate: OpenAI
 * realtime transcription REQUIRES >= 24000 (it rejects 16000 with "format.rate
 * integer below minimum value"); Google Cloud Speech v2 accepts it; and the
 * native Android capture is already 24kHz.
 */
function resampleTo24kHz(input, sourceRate) {
    const targetRate = 24000;
    if (sourceRate === targetRate) return input;

    const cutoff = (targetRate / sourceRate) * 0.9;
    const filtered = lowPassFilter(input, cutoff);

    const ratio = sourceRate / targetRate;
    const outputLength = Math.floor(filtered.length / ratio);
    const output = new Float32Array(outputLength);

    for (let i = 0; i < outputLength; i++) {
        const srcIndex = i * ratio;
        const idx = Math.floor(srcIndex);
        const frac = srcIndex - idx;

        const s0 = filtered[Math.max(0, idx - 1)];
        const s1 = filtered[idx];
        const s2 = filtered[Math.min(filtered.length - 1, idx + 1)];
        const s3 = filtered[Math.min(filtered.length - 1, idx + 2)];

        const a = -0.5 * s0 + 1.5 * s1 - 1.5 * s2 + 0.5 * s3;
        const b = s0 - 2.5 * s1 + 2 * s2 - 0.5 * s3;
        const c = -0.5 * s0 + 0.5 * s2;
        const d = s1;

        output[i] = a * frac * frac * frac + b * frac * frac + c * frac + d;
    }
    return output;
}

// =============================================================================
// AudioContext lifecycle
// =============================================================================

async function initRecordingContext() {
    if (!recordingContext) {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextClass) {
            throw new Error('AudioContext not supported on this device');
        }
        recordingContext = new AudioContextClass();
    }
    if (recordingContext.state === 'suspended') {
        await recordingContext.resume();
    }
}

// =============================================================================
// WebSocket send helper
// =============================================================================

function sendAudio(base64) {
    if (ws && ws.readyState === WebSocket.OPEN && wsReady && base64) {
        ws.send(JSON.stringify({ type: 'stt_audio', pcm: base64 }));
    }
}

// =============================================================================
// Button visual state (mirrors tts-stt.js aria-pressed convention)
// =============================================================================

function setButtonRecording(on) {
    if (!buttonId) return;
    const btn = $(buttonId);
    if (!btn) return;
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.classList.toggle('stt-streaming', on);
}

// =============================================================================
// Delta applier
// =============================================================================

/**
 * Apply a cumulative interim transcript: replace the interim region with `text`.
 */
function applyDelta(text) {
    gotTranscript = true;
    onUnavailable = null;  // streaming is working — no fallback offer anymore
    lastInterim = text || '';  // newest partial — fallback-committed on a lost final
    const el = $(targetId);
    if (!el) return;
    const interim = text || '';
    el.value = baseBefore + interim + baseAfter;
    interimLen = interim.length;
    const caret = baseBefore.length + interim.length;
    try { el.setSelectionRange(caret, caret); } catch (e) { /* non-text input */ }
    el.dispatchEvent(new Event('input', { bubbles: true }));
}

/**
 * Commit a final transcript: fold it into baseBefore so subsequent utterances
 * append after it. Adds a single trailing space so successive dictations don't
 * run together.
 */
function applyFinal(text) {
    gotTranscript = true;
    onUnavailable = null;  // streaming is working — no fallback offer anymore
    lastInterim = '';      // committed — nothing pending for the stop fallback
    const el = $(targetId);
    if (!el) return;
    let committed = text || '';
    // Single trailing space so successive dictations don't run together.
    if (committed && !/\s$/.test(committed)) {
        committed += ' ';
    }
    el.value = baseBefore + committed + baseAfter;
    baseBefore = baseBefore + committed;
    interimLen = 0;
    const caret = baseBefore.length;
    try { el.setSelectionRange(caret, caret); } catch (e) { /* non-text input */ }
    el.dispatchEvent(new Event('input', { bubbles: true }));
}

// =============================================================================
// WebSocket message handling
// =============================================================================

function handleMessage(msg) {
    switch (msg.type) {
        case 'stt_delta':
            applyDelta(msg.text);
            break;
        case 'stt_final':
            applyFinal(msg.text);
            break;
        case 'stt_error':
            console.error('[STT-STREAM] stt_error:', msg.message);
            // If the error arrived before any transcript landed, this invocation
            // never really started — offer the caller a fallback (e.g. legacy
            // record->/stt) instead of surfacing a hard error. Tear down quietly.
            if (!gotTranscript && onUnavailable) {
                cleanup({ silent: true });
                fireUnavailable('stt_error: ' + (msg.message || 'unknown'));
            } else {
                toast('STT error: ' + (msg.message || 'unknown'));
                // Stop capture and tear down; do not leave the mic open.
                cleanup();
            }
            break;
        case 'stt_done':
            // Terminal marker (2026-07-09): ALWAYS the server's last frame —
            // finalize the stop the moment the session is truly over instead of
            // waiting out the backstop. Outside a stop (server-initiated end),
            // the socket close that follows drives the existing onclose path.
            if (stopping) finalizeStop();
            break;
        default:
            // Ignore unknown message types (forward-compatible).
            break;
    }
}

// =============================================================================
// Capture: browser ScriptProcessor path
// =============================================================================

async function startBrowserCapture() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        throw new Error('Microphone not supported. Requires HTTPS or localhost.');
    }

    await initRecordingContext();

    const isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
    const audioConstraints = {
        audio: {
            sampleRate: { ideal: 48000 },
            channelCount: 1,
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true
        }
    };

    try {
        mediaStream = await navigator.mediaDevices.getUserMedia(audioConstraints);
    } catch (micError) {
        if (micError.name === 'NotAllowedError') {
            throw new Error('Microphone permission denied.');
        } else if (micError.name === 'NotFoundError') {
            throw new Error('No microphone found.');
        } else if (micError.name === 'NotReadableError') {
            throw new Error('Microphone is busy.');
        }
        throw new Error(`Microphone error: ${micError.message || micError.name}`);
    }

    sourceNode = recordingContext.createMediaStreamSource(mediaStream);
    nativeRate = recordingContext.sampleRate;

    const bufferSize = isMobile ? 4096 : 8192;
    scriptProcessor = recordingContext.createScriptProcessor(bufferSize, 1, 1);

    scriptProcessor.onaudioprocess = (e) => {
        if (!streaming || !ws || ws.readyState !== WebSocket.OPEN || !wsReady) return;

        const inputData = e.inputBuffer.getChannelData(0);

        // Noise gate: skip frames below the noise floor.
        let maxLevel = 0;
        for (let i = 0; i < inputData.length; i++) {
            const abs = Math.abs(inputData[i]);
            if (abs > maxLevel) maxLevel = abs;
        }
        if (maxLevel < 0.015) return;

        // High-pass to cut sub-100Hz rumble (single-pole).
        const hpAlpha = 0.98;
        let hpPrev = 0;
        const filtered = new Float32Array(inputData.length);
        for (let i = 0; i < inputData.length; i++) {
            filtered[i] = hpAlpha * (hpPrev + inputData[i] - (i > 0 ? inputData[i - 1] : 0));
            hpPrev = filtered[i];
        }

        // Capped-gain normalize.
        const normalizedData = new Float32Array(filtered.length);
        const targetLevel = 0.4;
        const gain = maxLevel > 0.03 ? Math.min(targetLevel / maxLevel, 1.5) : 1.0;
        for (let i = 0; i < filtered.length; i++) {
            normalizedData[i] = Math.max(-1, Math.min(1, filtered[i] * gain));
        }

        // Resample native rate -> 24kHz, encode PCM16 -> base64.
        const resampled = resampleTo24kHz(normalizedData, nativeRate);
        const base64 = float32ToPCM16Base64(resampled);
        sendAudio(base64);
    };

    sourceNode.connect(scriptProcessor);
    scriptProcessor.connect(recordingContext.destination);
}

function stopBrowserCapture() {
    if (scriptProcessor) {
        try { scriptProcessor.disconnect(); } catch (e) {}
        scriptProcessor.onaudioprocess = null;
        scriptProcessor = null;
    }
    if (sourceNode) {
        try { sourceNode.disconnect(); } catch (e) {}
        sourceNode = null;
    }
    if (mediaStream) {
        try { mediaStream.getTracks().forEach(t => t.stop()); } catch (e) {}
        mediaStream = null;
    }
    if (recordingContext) {
        try { recordingContext.close(); } catch (e) {}
        recordingContext = null;
    }
}

// =============================================================================
// Capture: native Android path (24kHz PCM16 frames via window.onNativeAudioChunk)
// =============================================================================

function startNativeCapture() {
    // Save the previous handler so we can restore it on stop (mirrors gemini-live).
    prevNativeAudioChunk = window.onNativeAudioChunk;
    prevNativeStreamStart = window.onNativeStreamStart;
    prevNativeStreamStop = window.onNativeStreamStop;
    prevNativeStreamError = window.onNativeStreamError;

    window.onNativeStreamStart = () => {
        console.log('[STT-STREAM] Native streaming started');
    };
    window.onNativeStreamStop = () => {
        console.log('[STT-STREAM] Native streaming stopped');
    };
    window.onNativeStreamError = (error) => {
        console.error('[STT-STREAM] Native streaming error:', error);
        toast('Mic error: ' + error);
        cleanup();
    };
    window.onNativeAudioChunk = (base64Data) => {
        // Native frames are already 24kHz PCM16 base64 — send as-is (stt_start
        // declared sample_rate:24000 for the native path).
        sendAudio(base64Data);
    };

    AndroidMic.startAudioStreaming();
    usingNative = true;
}

function stopNativeCapture() {
    if (typeof AndroidMic !== 'undefined' && typeof AndroidMic.stopAudioStreaming === 'function') {
        try { AndroidMic.stopAudioStreaming(); } catch (e) {}
    }
    // Restore previous native handlers.
    window.onNativeAudioChunk = prevNativeAudioChunk || null;
    window.onNativeStreamStart = prevNativeStreamStart || null;
    window.onNativeStreamStop = prevNativeStreamStop || null;
    window.onNativeStreamError = prevNativeStreamError || null;
    prevNativeAudioChunk = null;
    prevNativeStreamStart = null;
    prevNativeStreamStop = null;
    prevNativeStreamError = null;
    usingNative = false;
}

// =============================================================================
// Teardown
// =============================================================================

/**
 * Finalize a user stop: idempotent terminal step reached via the server's
 * stt_done, the backstop timer, a dead socket, or an onclose during the stop.
 * If no stt_final absorbed the interim (lastInterim is only cleared by a real
 * final — including the authoritative EMPTY final of a hallucination-filtered
 * stop), fallback-commit the newest partial so the utterance isn't dropped —
 * then close the socket and end the session. Mirrors the Android client.
 */
function finalizeStop() {
    if (!stopping) return;   // idempotence: stt_done + backstop + onclose race
    stopping = false;
    if (closeTimer) {
        clearTimeout(closeTimer);
        closeTimer = null;
    }
    if (lastInterim) {
        console.warn('[STT-STREAM] stop ended without a final — committing newest interim');
        applyFinal(lastInterim);
        lastInterim = '';
    }
    if (ws) {
        try { ws.close(); } catch (e) {}
        ws = null;
    }
    wsReady = false;
    streaming = false;
}

/**
 * Full teardown: stop capture, close ws, clear button state. Idempotent.
 * Used by both the error path and any non-stop termination.
 */
function cleanup(opts = {}) {
    stopping = false;
    lastInterim = '';
    if (closeTimer) {
        clearTimeout(closeTimer);
        closeTimer = null;
    }
    if (openTimer) {
        clearTimeout(openTimer);
        openTimer = null;
    }
    if (usingNative) {
        stopNativeCapture();
    } else {
        stopBrowserCapture();
    }
    if (ws) {
        try { ws.close(); } catch (e) {}
        ws = null;
    }
    wsReady = false;
    streaming = false;
    // When tearing down silently for a fallback handoff, leave the button visual
    // for the legacy path to manage so the mic never appears momentarily "off".
    if (!opts.silent) {
        setButtonRecording(false);
    }
}

// =============================================================================
// Public API
// =============================================================================

/**
 * Begin streaming dictation into the element #targetId.
 * @param {string} targetIdArg - id of the target input/textarea
 * @param {string} [buttonIdArg] - id of the trigger button (visual state)
 * @param {Object} [opts] - { lang, onUnavailable } — onUnavailable(reason) fires
 *   once if streaming can't get going, so the caller can fall back to a legacy
 *   record->/stt path for this invocation.
 */
async function start(targetIdArg, buttonIdArg, opts = {}) {
    if (streaming) {
        // Already streaming — ignore (caller should stop() first to retarget).
        return;
    }
    // Reset fast-failure + stop-handshake state for this invocation.
    onUnavailable = typeof opts.onUnavailable === 'function' ? opts.onUnavailable : null;
    gotTranscript = false;
    stopping = false;
    lastInterim = '';

    if (!hasMic()) {
        // No mic at all — let the caller decide (fallback shares the same gate).
        if (!fireUnavailable('no microphone')) toast('Microphone not supported');
        return;
    }

    const el = $(targetIdArg);
    if (!el) {
        if (!fireUnavailable('target not found')) toast('STT target not found');
        return;
    }

    // Capture insertion point + surrounding text for the delta applier.
    targetId = targetIdArg;
    buttonId = buttonIdArg || null;
    const insertPos = (typeof el.selectionStart === 'number') ? el.selectionStart : el.value.length;
    baseBefore = el.value.slice(0, insertPos);
    baseAfter = el.value.slice(insertPos);
    interimLen = 0;

    const native = isNativeAndroid();
    const sampleRate = 24000;  // uniform 24kHz (OpenAI requires >=24k; native already 24k)
    const lang = opts.lang || 'en';

    // Open the WebSocket.
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${location.host}/ws/stt`;

    try {
        ws = new WebSocket(url);
    } catch (e) {
        console.error('[STT-STREAM] WS open failed:', e);
        resetTargetState();
        if (!fireUnavailable('ws open threw: ' + (e && e.message))) {
            toast('STT connection failed');
        }
        return;
    }

    streaming = true;
    setButtonRecording(true);

    // Watchdog: if the socket never opens within the timeout, treat streaming as
    // unavailable and hand off to the caller's fallback (before any transcript).
    openTimer = setTimeout(() => {
        openTimer = null;
        if (!wsReady && !gotTranscript && onUnavailable) {
            cleanup({ silent: true });
            fireUnavailable('ws open timeout');
        }
    }, OPEN_TIMEOUT_MS);

    ws.onopen = async () => {
        if (openTimer) { clearTimeout(openTimer); openTimer = null; }
        wsReady = true;
        ws.send(JSON.stringify({
            type: 'stt_start',
            target: targetId,
            provider: '',
            lang: lang,
            sample_rate: sampleRate
        }));

        // Start mic capture only after the socket is open + stt_start sent.
        try {
            if (native) {
                startNativeCapture();
            } else {
                await startBrowserCapture();
            }
        } catch (err) {
            console.error('[STT-STREAM] Capture failed:', err);
            toast('Microphone access failed: ' + err.message);
            cleanup();
        }
    };

    ws.onmessage = (event) => {
        try {
            handleMessage(JSON.parse(event.data));
        } catch (err) {
            console.error('[STT-STREAM] Failed to parse message:', err);
        }
    };

    ws.onerror = (event) => {
        console.error('[STT-STREAM] WebSocket error:', event);
        // A ws error before any transcript means streaming never got going —
        // hand off to the caller's fallback (one-shot).
        if (!gotTranscript && onUnavailable) {
            cleanup({ silent: true });
            fireUnavailable('ws error');
        }
    };

    ws.onclose = () => {
        // If the socket closes for any reason, ensure capture is torn down so
        // the mic never outlives the connection.
        wsReady = false;
        if (stopping) {
            // Socket ended during a stop BEFORE stt_done arrived (server died /
            // hard-closed after a stalled flush): nothing further can arrive —
            // finalize now (fallback-commits the interim) instead of waiting
            // out the backstop.
            finalizeStop();
            return;
        }
        if (streaming) {
            // Closed before any transcript with a pending fallback offer → fall back.
            if (!gotTranscript && onUnavailable) {
                cleanup({ silent: true });
                fireUnavailable('ws closed early');
            } else {
                cleanup();
            }
        }
    };
}

/**
 * Stop streaming and finalize. Sends stt_stop, stops the mic immediately, then
 * keys the close on the server's terminal stt_done (which arrives right after
 * the trailing stt_final). The backstop timer only fires if the server wedges;
 * on expiry — or on a dead socket — the newest interim is fallback-committed
 * so the utterance isn't dropped (mirrors the Android client's stop handshake).
 */
function stop() {
    if (!streaming || stopping) return;
    stopping = true;

    // Send stop signal while the socket is still open.
    let sentStop = false;
    if (ws && ws.readyState === WebSocket.OPEN) {
        try {
            ws.send(JSON.stringify({ type: 'stt_stop' }));
            sentStop = true;
        } catch (e) {}
    }

    // Stop the mic immediately — no more audio after the user releases.
    if (usingNative) {
        stopNativeCapture();
    } else {
        stopBrowserCapture();
    }

    // Clear the recording visual now; transcription may still trickle in.
    setButtonRecording(false);

    if (!sentStop) {
        // Socket already dead — no stt_final/stt_done can arrive on it; commit
        // the newest partial and tear down immediately.
        finalizeStop();
        return;
    }

    // Disaster backstop: the server bounds its own provider drains at 5-8s, so
    // stt_done normally lands well before this (and finalizes immediately).
    if (closeTimer) clearTimeout(closeTimer);
    closeTimer = setTimeout(() => {
        closeTimer = null;
        console.warn('[STT-STREAM] stt_done backstop expired — finalizing stop');
        finalizeStop();
    }, STOP_BACKSTOP_MS);
}

/**
 * @returns {boolean} whether a streaming session is active.
 */
function isStreaming() {
    return streaming;
}

/**
 * Reset only the target/delta state (used when start aborts before capture).
 */
function resetTargetState() {
    streaming = false;
    wsReady = false;
    if (openTimer) { clearTimeout(openTimer); openTimer = null; }
    setButtonRecording(false);
    ws = null;
}

export const sttStream = { start, stop, isStreaming };

// Named exports too, matching the export style used across Portal modules
// (e.g. tts-stt.js exports startSTT/stopSTT as named functions).
export { start as startSTTStream, stop as stopSTTStream, isStreaming as isSTTStreaming };
