// native-bridge.js: the Android WebView bridge (AndroidMic / Android) behind
// one module, its streaming callbacks routed through mic-arbiter.js (audit
// rank 75, P02-voice-web-12, P03-live-agents-web-17).
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom } from "./helpers/portal-dom.mjs";

installDom();

const bridge = await import("../../Portal/modules/native-bridge.js");
const arbiter = await import("../../Portal/modules/mic-arbiter.js");

function fakeMic() {
    const calls = [];
    globalThis.AndroidMic = {
        startAudioStreaming: () => calls.push("start"),
        stopAudioStreaming: () => calls.push("stop"),
        startRecording: () => calls.push("record"),
    };
    return calls;
}

function noMic() {
    delete globalThis.AndroidMic;
}

test("outside the app nothing is native", () => {
    noMic();
    assert.equal(bridge.isNativeAndroid(), false);
    assert.equal(bridge.hasNativeStreaming(), false);
    assert.equal(bridge.hasNativeRecording(), false);
    assert.equal(bridge.nativeDownloadFile("/x.wav", "x.wav"), false);
    assert.equal(bridge.hasNativeDownload(), false);
});

test("inside the app the bridge is detected", () => {
    fakeMic();
    assert.equal(bridge.isNativeAndroid(), true);
    assert.equal(bridge.hasNativeStreaming(), true);
    assert.equal(bridge.hasNativeRecording(), true);
    assert.equal(bridge.hasMic(), true);
    noMic();
});

test("an awaited native stream resolves on the app's start, delivers chunks, and a stop unbinds it", async () => {
    const calls = fakeMic();
    const chunks = [];
    const started = bridge.startNativeStreamAwaited("gemini-live", { onChunk: (c) => chunks.push(c) });
    assert.deepEqual(calls, ["start"]);
    assert.equal(arbiter.nativeStreamOwner(), "gemini-live");
    window.onNativeStreamStart();          // the app confirms
    await started;
    window.onNativeAudioChunk("AAAA");
    bridge.stopNativeStream("gemini-live");
    window.onNativeAudioChunk("BBBB");     // after the stop: nobody bound
    assert.deepEqual(chunks, ["AAAA"]);
    assert.deepEqual(calls, ["start", "stop"]);
    assert.equal(arbiter.nativeStreamOwner(), null);
    noMic();
});

test("a stale stop from another module leaves the current owner's feed alone", () => {
    fakeMic();
    const got = [];
    bridge.startNativeStream("realtime", { onChunk: (c) => got.push(c) });
    bridge.stopNativeStream("stt-stream");  // not the owner
    window.onNativeAudioChunk("C1");
    assert.deepEqual(got, ["C1"]);
    bridge.stopNativeStream("realtime");
    noMic();
});

test("the app's stream error rejects the start (and reaches the module's handler)", async () => {
    fakeMic();
    const errors = [];
    const started = bridge.startNativeStreamAwaited("grok-live", { onError: (e) => errors.push(e) });
    window.onNativeStreamError("mic denied");
    await assert.rejects(started, /mic denied/);
    assert.deepEqual(errors, ["mic denied"]);
    bridge.stopNativeStream("grok-live");
    noMic();
});

test("no start from the app within the timeout rejects", async () => {
    fakeMic();
    await assert.rejects(bridge.startNativeStreamAwaited("grok-live", {}, { timeoutMs: 20 }), /timeout/);
    bridge.stopNativeStream("grok-live");
    noMic();
});

test("downloads go to the app's download bridge when it has one", () => {
    const got = [];
    globalThis.Android = { downloadFile: (url, name) => got.push([url, name]) };
    assert.equal(bridge.hasNativeDownload(), true);
    assert.equal(bridge.nativeDownloadFile("http://box/ui/uploads/a.wav", "a.wav"), true);
    assert.deepEqual(got, [["http://box/ui/uploads/a.wav", "a.wav"]]);
    delete globalThis.Android;
});
