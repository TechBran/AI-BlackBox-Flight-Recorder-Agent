/**
 * mic-capture.js
 * Browser microphone capture for the live voice agents and dictation.
 *
 * openMicCapture() opens the mic (getUserMedia, retried while another app or
 * module still holds it) and delivers raw mono Float32 frames at the capture
 * context's native rate to onFrame(). Frames come from an AudioWorklet
 * (pcm-capture-worklet.js) where the browser has one; ScriptProcessorNode
 * (deprecated, main-thread) stays the fallback for engines without
 * AudioWorklet. Each module keeps its own per-frame conditioning (noise gate,
 * client VAD) and conversion (audio-dsp.js).
 *
 * The capture loop, the constraints and the busy-mic retry used to be copied
 * into four modules (audit P02-voice-web-12).
 *
 * Created: 2026-10-29 (audit rank 75)
 */

import { PROCESSOR_NAME } from './pcm-capture-worklet.js';

/** Worklet module URL, resolved next to this module (served under /ui). */
const WORKLET_URL = new URL('./pcm-capture-worklet.js', import.meta.url).href;

/** Mono, with the browser's echo cancellation / noise suppression / AGC on. */
export const MIC_CONSTRAINTS = Object.freeze({
    audio: {
        sampleRate: { ideal: 48000 },
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
    },
});

/** Frame size: smaller on mobile (better compatibility), larger on desktop. */
export function micFrameSize() {
    const ua = (typeof navigator !== 'undefined' && navigator.userAgent) || '';
    return /Android|iPhone|iPad|iPod/i.test(ua) ? 4096 : 8192;
}

/** Client-facing messages for the getUserMedia failures, by DOMException name. */
export const MIC_ERROR_MESSAGES = Object.freeze({
    NotAllowedError: 'Microphone permission denied.',
    NotFoundError: 'No microphone found.',
    NotReadableError: 'Microphone is busy.',
});

/**
 * getUserMedia, retried while the mic is busy (NotReadableError: another app,
 * or a module still letting go of it; Samsung audio HALs are slow to release).
 * @param {object} [opts]
 * @param {object} [opts.constraints=MIC_CONSTRAINTS]
 * @param {number} [opts.retries=5] - Extra attempts after a busy mic
 * @param {string} [opts.logPrefix='[MIC]']
 * @param {object} [opts.messages] - Overrides for MIC_ERROR_MESSAGES
 * @param {(ms: number) => Promise<void>} [opts.sleep]
 * @returns {Promise<MediaStream>}
 */
export async function getMicStream({ constraints = MIC_CONSTRAINTS, retries = 5, logPrefix = '[MIC]',
                                     messages = {}, sleep = defaultSleep } = {}) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        throw new Error('Microphone not supported. Requires HTTPS or localhost.');
    }
    const text = { ...MIC_ERROR_MESSAGES, ...messages };
    for (let attempt = 0; ; attempt++) {
        try {
            const stream = await navigator.mediaDevices.getUserMedia(constraints);
            console.log(`${logPrefix} Got media stream successfully`);
            return stream;
        } catch (micError) {
            console.error(`${logPrefix} Microphone error (attempt ${attempt + 1}):`, micError.name);
            if (micError.name === 'NotReadableError' && attempt < retries) {
                const delay = 500 + (attempt * 500);  // 500ms, 1000ms, ... while the mic frees up
                console.log(`${logPrefix} Mic busy, waiting ${delay}ms...`);
                await sleep(delay);
                continue;
            }
            if (text[micError.name]) throw new Error(text[micError.name]);
            throw new Error(`Microphone error: ${micError.message || micError.name}`);
        }
    }
}

function defaultSleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * A capture AudioContext at the device's native rate, resumed (mobile
 * browsers start it suspended until a user gesture).
 * @param {AudioContext|null} [existing] - Reused while it is still open
 * @returns {Promise<AudioContext>}
 */
export async function ensureCaptureContext(existing = null) {
    let context = existing && existing.state !== 'closed' ? existing : null;
    if (!context) {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextClass) {
            throw new Error('AudioContext not supported on this device');
        }
        context = new AudioContextClass();
    }
    if (context.state === 'suspended') {
        await context.resume();
    }
    return context;
}

/** Contexts whose worklet module is loaded (addModule once per context). */
const _workletReady = new WeakMap();

async function workletNode(context, frameSize) {
    if (!context.audioWorklet || typeof AudioWorkletNode === 'undefined') return null;
    try {
        if (!_workletReady.has(context)) {
            _workletReady.set(context, context.audioWorklet.addModule(WORKLET_URL));
        }
        await _workletReady.get(context);
        return new AudioWorkletNode(context, PROCESSOR_NAME, {
            numberOfInputs: 1,
            numberOfOutputs: 1,
            channelCount: 1,
            processorOptions: { frameSize },
        });
    } catch (e) {
        _workletReady.delete(context);
        console.warn('[MIC] AudioWorklet unavailable, using ScriptProcessorNode:', e);
        return null;
    }
}

/**
 * Open the mic and stream frames to onFrame.
 * @param {object} opts
 * @param {(frame: Float32Array, sampleRate: number) => void} opts.onFrame
 * @param {AudioContext|null} [opts.context] - Capture context to reuse
 * @param {number} [opts.frameSize=micFrameSize()]
 * @param {string} [opts.logPrefix='[MIC]']
 * @param {object} [opts.constraints] - getUserMedia constraints
 * @param {object} [opts.messages] - Error message overrides (getMicStream)
 * @param {number} [opts.retries] - Busy-mic retries (getMicStream)
 * @returns {Promise<{context: AudioContext, stream: MediaStream, sampleRate: number,
 *                    worklet: boolean, close: () => void}>}
 */
export async function openMicCapture({ onFrame, context = null, frameSize = micFrameSize(),
                                       logPrefix = '[MIC]', constraints, messages, retries } = {}) {
    const ctx = await ensureCaptureContext(context);
    const stream = await getMicStream({ constraints, messages, retries, logPrefix });
    const source = ctx.createMediaStreamSource(stream);
    const sampleRate = ctx.sampleRate;

    let node = await workletNode(ctx, frameSize);
    const worklet = !!node;
    if (node) {
        node.port.onmessage = (event) => onFrame(event.data, sampleRate);
    } else {
        node = ctx.createScriptProcessor(frameSize, 1, 1);
        node.onaudioprocess = (e) => onFrame(e.inputBuffer.getChannelData(0), sampleRate);
    }
    source.connect(node);
    // The graph only pulls a node connected to the destination (it outputs silence).
    node.connect(ctx.destination);
    console.log(`${logPrefix} Capturing at ${sampleRate}Hz (${worklet ? 'AudioWorklet' : 'ScriptProcessor'}, `
        + `${frameSize}-sample frames)`);

    let closed = false;
    return {
        context: ctx,
        stream,
        sampleRate,
        worklet,
        close() {
            if (closed) return;
            closed = true;
            try {
                if (worklet) {
                    node.port.onmessage = null;
                    node.port.postMessage('stop');
                } else {
                    node.onaudioprocess = null;
                }
                node.disconnect();
            } catch (e) { /* already disconnected */ }
            try { source.disconnect(); } catch (e) { /* already disconnected */ }
            try { stream.getTracks().forEach(track => track.stop()); } catch (e) { /* already stopped */ }
        },
    };
}
