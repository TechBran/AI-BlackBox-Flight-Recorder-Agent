/**
 * pcm-capture-worklet.js
 * AudioWorklet processor of mic-capture.js: collects the mic's mono input into
 * frames of `frameSize` samples and posts each frame (a Float32Array, its
 * buffer transferred) to the main thread.
 *
 * Replaces ScriptProcessorNode (deprecated, and run on the main thread, where a
 * busy page glitched the capture). The frame size keeps the old 4096 / 8192
 * sample frames, so the per-frame conditioning on the main thread (noise gate,
 * normalisation, client VAD) sees what it always saw.
 *
 * Loaded with audioWorklet.addModule(): runs in the AudioWorkletGlobalScope,
 * where AudioWorkletProcessor and registerProcessor are globals.
 *
 * Created: 2026-10-29 (audit rank 75)
 */

export const PROCESSOR_NAME = 'pcm-capture';

export function createProcessorClass(Base) {
    return class PcmCaptureProcessor extends Base {
        constructor(options) {
            super(options);
            const size = (options && options.processorOptions && options.processorOptions.frameSize) || 4096;
            this.frameSize = size;
            this.frame = new Float32Array(size);
            this.filled = 0;
            this.stopped = false;
            this.port.onmessage = (event) => {
                if (event.data === 'stop') this.stopped = true;
            };
        }

        process(inputs) {
            if (this.stopped) return false;
            const channel = inputs[0] && inputs[0][0];
            if (!channel) return true;  // no input connected yet
            let offset = 0;
            while (offset < channel.length) {
                const n = Math.min(channel.length - offset, this.frameSize - this.filled);
                this.frame.set(channel.subarray(offset, offset + n), this.filled);
                this.filled += n;
                offset += n;
                if (this.filled === this.frameSize) {
                    const full = this.frame;
                    this.port.postMessage(full, [full.buffer]);
                    this.frame = new Float32Array(this.frameSize);
                    this.filled = 0;
                }
            }
            return true;
        }
    };
}

if (typeof AudioWorkletProcessor !== 'undefined' && typeof registerProcessor === 'function') {
    registerProcessor(PROCESSOR_NAME, createProcessorClass(AudioWorkletProcessor));  // eslint-disable-line no-undef
}
