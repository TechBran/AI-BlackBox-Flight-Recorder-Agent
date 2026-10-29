// audio-dsp.js: the PCM16 codecs, resamplers and mic-frame conditioning the
// live voice agents and dictation share (audit rank 75, P02-voice-web-12).
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";

const dsp = await import("../../Portal/modules/audio-dsp.js");

function sine(n, rate, freq = 440, amp = 0.5) {
    const out = new Float32Array(n);
    for (let i = 0; i < n; i++) out[i] = amp * Math.sin(2 * Math.PI * freq * i / rate);
    return out;
}

test("PCM16 base64 round-trips Float32 within one quantisation step, clamping out-of-range samples", () => {
    const input = Float32Array.from([0, 0.25, -0.25, 0.999, -1, 1.7, -3]);
    const back = dsp.pcm16Base64ToFloat32(dsp.float32ToPCM16Base64(input));
    assert.equal(back.length, input.length);
    const want = [0, 0.25, -0.25, 0.999, -1, 1, -1];
    for (let i = 0; i < want.length; i++) assert.ok(Math.abs(back[i] - want[i]) < 1 / 16384, `sample ${i}: ${back[i]}`);
});

test("a base64 chunk with an odd byte length decodes instead of throwing", () => {
    const odd = btoa(String.fromCharCode(1, 0, 2));  // 3 bytes: one full sample
    assert.equal(dsp.pcm16Base64ToFloat32(odd).length, 1);
});

test("downsampling keeps the duration (48 kHz -> 16 kHz is a third of the samples) and passes DC through", () => {
    const dc = new Float32Array(4800).fill(0.5);
    const out = dsp.resampleAudio(dc, 48000, 16000);
    assert.equal(out.length, 1600);
    assert.ok(out.slice(100).every(v => Math.abs(v - 0.5) < 1e-3), "steady DC level survives the anti-alias filter");
    assert.equal(dsp.resampleAudio(dc, 16000, 16000), dc, "same rate: the input itself");
});

test("downsampling low-passes first (a Nyquist-rate tone comes out attenuated, not aliased at full level)", () => {
    const nyquist = Float32Array.from({ length: 4800 }, (_, i) => (i % 2 ? -0.5 : 0.5));
    const out = dsp.resampleAudio(nyquist, 48000, 16000);
    assert.ok(dsp.peakLevel(out.slice(10)) < 0.25, `peak ${dsp.peakLevel(out.slice(10))}`);
});

test("upsampling 24 kHz to 48 kHz takes the exact 2x midpoint path; other rates interpolate", () => {
    const input = Float32Array.from([0, 1, 0, -1]);
    assert.deepEqual(Array.from(dsp.upsampleAudio(input, 24000, 48000)), [0, 0.5, 1, 0.5, 0, -0.5, -1, -1]);
    assert.equal(dsp.upsampleAudio(sine(2400, 24000), 24000, 44100).length, Math.floor(2400 * 44100 / 24000));
    assert.equal(dsp.upsampleAudio(input, 24000, 24000), input);
});

test("a native 24 kHz chunk resamples to 16 kHz (two thirds of the samples)", () => {
    const chunk = dsp.float32ToPCM16Base64(sine(2400, 24000, 440, 0.3));
    const out = dsp.pcm16Base64ToFloat32(dsp.resamplePCM16Base64(chunk, 24000, 16000));
    assert.equal(out.length, 1600);
    assert.equal(dsp.resamplePCM16Base64(chunk, 24000, 24000), chunk);
});

test("the high-pass filter carries its state across frames (no click at frame boundaries)", () => {
    // The old copies reset the filter at every frame: a step split over two
    // frames came out different from the same step in one frame.
    const signal = new Float32Array(1000).fill(0.5);
    const whole = dsp.createHighPassFilter().process(signal);
    const hp = dsp.createHighPassFilter();
    const split = new Float32Array(1000);
    split.set(hp.process(signal.subarray(0, 400)), 0);
    split.set(hp.process(signal.subarray(400)), 400);
    for (let i = 0; i < 1000; i++) assert.ok(Math.abs(whole[i] - split[i]) < 1e-12, `sample ${i}`);
    assert.ok(Math.abs(split[999]) < 0.01, "DC is removed");
    hp.reset();
    assert.ok(Math.abs(hp.process(Float32Array.from([0.5]))[0] - 0.49) < 1e-6, "reset starts over");
});

test("capped-gain normalisation lifts toward 0.4 at most 1.5x and leaves near-silence alone", () => {
    const quiet = Float32Array.from([0.02, -0.02]);
    assert.deepEqual(Array.from(dsp.normalizePeak(quiet, 0.02)), Array.from(quiet));
    const soft = Float32Array.from([0.1, -0.1]);
    assert.ok(Math.abs(dsp.normalizePeak(soft, 0.1)[0] - 0.15) < 1e-7, "gain capped at 1.5");
    const loud = Float32Array.from([0.8, -0.8]);
    assert.ok(Math.abs(dsp.normalizePeak(loud, 0.8)[0] - 0.4) < 1e-7);
});

test("a mic frame under the noise gate is not sent; one above is conditioned and resampled", () => {
    const hp = dsp.createHighPassFilter();
    assert.equal(dsp.conditionMicFrame(new Float32Array(4800).fill(0.01), { sourceRate: 48000, targetRate: 16000, highPass: hp }), null);
    const out = dsp.conditionMicFrame(sine(4800, 48000, 440, 0.3), { sourceRate: 48000, targetRate: 16000, highPass: hp });
    assert.equal(dsp.pcm16Base64ToFloat32(out).length, 1600);
});

test("silence and concatenation helpers", () => {
    assert.deepEqual(Array.from(dsp.pcm16Base64ToFloat32(dsp.silencePCM16Base64(480))), new Array(480).fill(0));
    const a = dsp.float32ToPCM16Base64(Float32Array.from([0.5]));
    const b = dsp.float32ToPCM16Base64(Float32Array.from([-0.5, 0.25]));
    assert.equal(dsp.pcm16Base64ToFloat32(dsp.concatPCM16Base64([a, b])).length, 3);
    assert.equal(dsp.concatPCM16Base64([a]), a);
    assert.equal(dsp.concatPCM16Base64([]), "");
    assert.ok(Math.abs(dsp.rmsLevelPCM16Base64(b) - Math.sqrt((0.25 + 0.0625) / 2)) < 1e-4);
    assert.equal(dsp.peakLevel(Float32Array.from([0.1, -0.7, 0.3])), 0.699999988079071);
});
