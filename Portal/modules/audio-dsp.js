/**
 * audio-dsp.js
 * PCM16 codecs, resampling and the mic-frame conditioning chain shared by the
 * live voice agents (gpt-realtime.js, gemini-live.js, grok-live.js) and the
 * streaming dictation (stt-stream.js).
 *
 * Each of those modules carried its own copy of these functions, and the
 * copies had drifted: the high-pass filter reset its state at every frame in
 * three of them (a click at every 4096-sample boundary) while gpt-realtime had
 * fixed it, and Gemini resampled the native Android stream linearly while
 * Grok anti-aliased it (audit P02-voice-web-12, P03-live-agents-web-17).
 *
 * Pure: no DOM, no Web Audio, so it runs under node --test.
 *
 * Created: 2026-10-29 (audit rank 75)
 */

/**
 * Float32 samples (-1..1) to base64 PCM16 (little-endian).
 * @param {Float32Array} float32Array
 * @returns {string}
 */
export function float32ToPCM16Base64(float32Array) {
    const pcm16 = new Int16Array(float32Array.length);
    for (let i = 0; i < float32Array.length; i++) {
        const s = Math.max(-1, Math.min(1, float32Array[i]));
        pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return bytesToBase64(new Uint8Array(pcm16.buffer));
}

/**
 * Base64 PCM16 (little-endian) to Float32 samples (-1..1).
 * @param {string} base64
 * @returns {Float32Array}
 */
export function pcm16Base64ToFloat32(base64) {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
        bytes[i] = binary.charCodeAt(i);
    }
    const pcm16 = new Int16Array(bytes.buffer, 0, bytes.length >> 1);
    const float32 = new Float32Array(pcm16.length);
    for (let i = 0; i < pcm16.length; i++) {
        float32[i] = pcm16[i] / 32768.0;
    }
    return float32;
}

function bytesToBase64(bytes) {
    let binary = '';
    for (let i = 0; i < bytes.length; i++) {
        binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary);
}

/**
 * Base64 PCM16 of `samples` zero samples (silence).
 * @param {number} samples
 * @returns {string}
 */
export function silencePCM16Base64(samples) {
    return bytesToBase64(new Uint8Array(new Int16Array(samples).buffer));
}

/**
 * Single-pole low-pass filter (anti-aliasing before downsampling).
 * @param {Float32Array} input
 * @param {number} [cutoffRatio=0.4] - Cutoff as a ratio of the sample rate
 * @returns {Float32Array}
 */
export function lowPassFilter(input, cutoffRatio = 0.4) {
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

/** Catmull-Rom spline through s0..s3, evaluated at frac between s1 and s2. */
function catmullRom(s0, s1, s2, s3, frac) {
    const a = -0.5 * s0 + 1.5 * s1 - 1.5 * s2 + 0.5 * s3;
    const b = s0 - 2.5 * s1 + 2 * s2 - 0.5 * s3;
    const c = -0.5 * s0 + 0.5 * s2;
    const d = s1;
    return a * frac * frac * frac + b * frac * frac + c * frac + d;
}

/**
 * Downsample with an anti-aliasing low-pass and Catmull-Rom interpolation
 * (the mic path: native capture rate to the provider's input rate).
 * @param {Float32Array} input
 * @param {number} sourceRate
 * @param {number} targetRate
 * @returns {Float32Array}
 */
export function resampleAudio(input, sourceRate, targetRate) {
    if (sourceRate === targetRate) {
        return input;
    }

    // Low-pass slightly below the target's Nyquist to prevent aliasing.
    const cutoff = targetRate / sourceRate * 0.9;
    const filtered = lowPassFilter(input, cutoff);

    const ratio = sourceRate / targetRate;
    const outputLength = Math.floor(filtered.length / ratio);
    const output = new Float32Array(outputLength);

    for (let i = 0; i < outputLength; i++) {
        const srcIndex = i * ratio;
        const idx = Math.floor(srcIndex);
        output[i] = catmullRom(
            filtered[Math.max(0, idx - 1)],
            filtered[idx],
            filtered[Math.min(filtered.length - 1, idx + 1)],
            filtered[Math.min(filtered.length - 1, idx + 2)],
            srcIndex - idx);
    }
    return output;
}

/**
 * Exact 2x upsampling (24 kHz to 48 kHz): linear midpoints.
 * @param {Float32Array} input
 * @returns {Float32Array}
 */
export function upsample2x(input) {
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

/**
 * Upsample the model's audio to the playback context's rate (Catmull-Rom; the
 * exact 2x case takes the cheap midpoint path).
 * @param {Float32Array} input
 * @param {number} sourceRate - The model's output rate (24000 for all three)
 * @param {number} targetRate - The playback AudioContext's rate
 * @returns {Float32Array}
 */
export function upsampleAudio(input, sourceRate, targetRate) {
    if (targetRate === sourceRate) {
        return input;
    }
    if (targetRate === sourceRate * 2) {
        return upsample2x(input);
    }

    const ratio = targetRate / sourceRate;
    const outputLength = Math.floor(input.length * ratio);
    const output = new Float32Array(outputLength);

    for (let i = 0; i < outputLength; i++) {
        const srcPos = i / ratio;
        const srcIndex = Math.floor(srcPos);
        output[i] = catmullRom(
            input[Math.max(0, srcIndex - 1)],
            input[srcIndex],
            input[Math.min(input.length - 1, srcIndex + 1)],
            input[Math.min(input.length - 1, srcIndex + 2)],
            srcPos - srcIndex);
    }
    return output;
}

/**
 * Resample one chunk of base64 PCM16 (the native Android mic stream is 24 kHz;
 * Gemini and Grok take 16 kHz). Sent unconverted, 24 kHz audio is heard at 2/3
 * speed and a lower pitch (audit P03-live-agents-web-03).
 * @param {string} base64Audio
 * @param {number} sourceRate
 * @param {number} targetRate
 * @returns {string}
 */
export function resamplePCM16Base64(base64Audio, sourceRate, targetRate) {
    if (sourceRate === targetRate) return base64Audio;
    return float32ToPCM16Base64(resampleAudio(pcm16Base64ToFloat32(base64Audio), sourceRate, targetRate));
}

/**
 * Concatenate base64 PCM16 chunks into one.
 * @param {string[]} chunks
 * @returns {string}
 */
export function concatPCM16Base64(chunks) {
    if (chunks.length === 0) return '';
    if (chunks.length === 1) return chunks[0];
    const floatArrays = chunks.map(chunk => pcm16Base64ToFloat32(chunk));
    const combined = new Float32Array(floatArrays.reduce((sum, arr) => sum + arr.length, 0));
    let offset = 0;
    for (const arr of floatArrays) {
        combined.set(arr, offset);
        offset += arr.length;
    }
    return float32ToPCM16Base64(combined);
}

/**
 * Peak absolute sample value.
 * @param {Float32Array} samples
 * @returns {number}
 */
export function peakLevel(samples) {
    let peak = 0;
    for (let i = 0; i < samples.length; i++) {
        const abs = Math.abs(samples[i]);
        if (abs > peak) peak = abs;
    }
    return peak;
}

/**
 * Root-mean-square level.
 * @param {Float32Array} samples
 * @returns {number}
 */
export function rmsLevel(samples) {
    if (samples.length === 0) return 0;
    let sumSquares = 0;
    for (let i = 0; i < samples.length; i++) {
        sumSquares += samples[i] * samples[i];
    }
    return Math.sqrt(sumSquares / samples.length);
}

/**
 * RMS level of base64 PCM16 (the native stream arrives encoded).
 * @param {string} base64Audio
 * @returns {number}
 */
export function rmsLevelPCM16Base64(base64Audio) {
    return rmsLevel(pcm16Base64ToFloat32(base64Audio));
}

/**
 * A single-pole high-pass filter (cuts sub-100 Hz rumble) whose state carries
 * across frames, so frame boundaries do not click. One per capture.
 * @param {number} [alpha=0.98]
 * @returns {{process: (input: Float32Array) => Float32Array, reset: () => void}}
 */
export function createHighPassFilter(alpha = 0.98) {
    let prevOutput = 0;
    let prevInput = 0;
    return {
        process(input) {
            const output = new Float32Array(input.length);
            for (let i = 0; i < input.length; i++) {
                const x = input[i];
                output[i] = alpha * (prevOutput + x - prevInput);
                prevOutput = output[i];
                prevInput = x;
            }
            return output;
        },
        reset() {
            prevOutput = 0;
            prevInput = 0;
        },
    };
}

/**
 * Capped-gain normalisation toward `target` (never amplifies a near-silent
 * frame: below `minPeak` the gain is 1).
 * @param {Float32Array} input
 * @param {number} peak - The frame's peak level (before filtering)
 * @param {{target?: number, maxGain?: number, minPeak?: number}} [opts]
 * @returns {Float32Array}
 */
export function normalizePeak(input, peak, { target = 0.4, maxGain = 1.5, minPeak = 0.03 } = {}) {
    const gain = peak > minPeak ? Math.min(target / peak, maxGain) : 1.0;
    const output = new Float32Array(input.length);
    for (let i = 0; i < input.length; i++) {
        output[i] = Math.max(-1, Math.min(1, input[i] * gain));
    }
    return output;
}

/** Frames whose peak is below this are background noise (the noise gate). */
export const NOISE_GATE_PEAK = 0.015;

/**
 * Condition one mic frame for a provider: high-pass, capped-gain normalise,
 * resample to `targetRate`, encode as base64 PCM16. Null for a frame under
 * the noise gate (not sent).
 * @param {Float32Array} input - Raw samples at `sourceRate`
 * @param {{sourceRate: number, targetRate: number, highPass: {process: Function},
 *          gate?: number}} opts
 * @returns {string|null}
 */
export function conditionMicFrame(input, { sourceRate, targetRate, highPass, gate = NOISE_GATE_PEAK }) {
    const peak = peakLevel(input);
    if (peak < gate) return null;
    const normalized = normalizePeak(highPass.process(input), peak);
    return float32ToPCM16Base64(resampleAudio(normalized, sourceRate, targetRate));
}
