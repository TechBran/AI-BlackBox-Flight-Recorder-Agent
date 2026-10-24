// Generation modals (audit rank 61 Portal half + rank 66: P04-media-tasks-fx-web-07,
// -10, A11-settings-generation-04 parity, P07-html-css-03 mic buttons).
//
//  * The Google SSML voice picker fetched /tts/voices, which never existed, so
//    every generation used one hardcoded fallback voice; task failures read
//    task.error although the field is error_message.
//  * Android sends picks over 5 MB through onNativeFileUploaded, which the
//    modals never routed: a reference / start photo landed in the chat composer.
//
// Runs against the REAL index.html markup (scripts never execute under
// innerHTML), so the modals wire up exactly as on a box.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { installDom, loadPurify, loadMarked, PORTAL } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

const indexHtml = readFileSync(resolve(PORTAL, "index.html"), "utf8");
document.body.innerHTML = /<body[^>]*>([\s\S]*)<\/body>/.exec(indexHtml)[1];
localStorage.setItem("bbx_operator", "tester");

const json = (obj, status = 200) => new Response(JSON.stringify(obj), {
    status, headers: { "Content-Type": "application/json" },
});
let routes = [];
const calls = [];
globalThis.fetch = window.fetch = async (url, opts = {}) => {
    calls.push({ url: String(url), opts });
    for (const [prefix, handler] of routes) if (String(url).startsWith(prefix)) return handler(url, opts);
    return json({}, 404);
};

const fileUpload = await import("../../Portal/modules/file-upload.js");
fileUpload.setupNativeFileCallback();          // app-init order: file-upload first, then the modals
const gen = await import("../../Portal/modules/generation-modals.js");
gen.initGenerationModals();

const settle = (ms = 30) => new Promise((r) => setTimeout(r, ms));
const options = (id) => [...document.getElementById(id).options].map((o) => o.value);

test("an unavailable Google voice list says so -- no invented fallback voice", async () => {
    routes = [["/tts/google/voices", () => json({ detail: "GOOGLE_API_KEY not configured." }, 400)]];
    calls.length = 0;
    document.getElementById("btnGenGoogleSSML").click();
    await settle();
    const voiceCall = calls.find((c) => c.url.startsWith("/tts/google/voices"));
    assert.ok(voiceCall, "the box's Google voices route is the one asked");
    assert.equal(voiceCall.url, "/tts/google/voices?operator=tester");
    assert.ok(!calls.some((c) => c.url === "/tts/voices"), "the route that does not exist is never called");
    assert.ok(!options("google-voice-name-select").includes("en-US-Neural2-D"));
    assert.match(document.getElementById("google-voice-name-select").textContent, /unavailable/i);
});

test("the Google voice list populates the language and voice pickers", async () => {
    routes = [["/tts/google/voices", () => json({ voices: [
        { name: "en-US-Neural2-A", languageCodes: ["en-US"], ssmlGender: "MALE" },
        { name: "en-US-Neural2-F", languageCodes: ["en-US"], ssmlGender: "FEMALE" },
        { name: "es-ES-Neural2-B", languageCodes: ["es-ES"], ssmlGender: "MALE" },
    ] })]];
    document.getElementById("btnGenGoogleSSML").click();
    await settle();
    assert.deepEqual(options("google-voice-language-select").sort(), ["en-US", "es-ES"]);
    assert.deepEqual(options("google-voice-name-select"), ["en-US-Neural2-A", "en-US-Neural2-F"]);
    const lang = document.getElementById("google-voice-language-select");
    lang.value = "es-ES";
    lang.dispatchEvent(new Event("change"));
    assert.deepEqual(options("google-voice-name-select"), ["es-ES-Neural2-B"]);
    // Re-opening does not stack another change listener (one voice list per change).
    document.getElementById("btnGenGoogleSSML").click();
    await settle();
});

test("a failed SSML task shows the task's error_message", async () => {
    routes = [
        ["/generate/google_ssml", () => json({ task_id: "ssml-1" })],
        ["/tasks/status/ssml-1", () => json({ status: "failed", error_message: "Google TTS quota exceeded" })],
    ];
    document.getElementById("google-ssml-input").value = "Hello there";
    const voice = document.getElementById("google-voice-name-select");
    if (!voice.value) voice.value = voice.options[0].value;
    document.getElementById("google-ssml-generate-btn").click();
    await settle(80);
    assert.match(document.getElementById("google-ssml-status").textContent, /Google TTS quota exceeded/);
});

test("a large native pick for a reference image goes to the modal, not the chat composer", async () => {
    routes = [["/ui/uploads/big.jpg", () => new Response(new Uint8Array([0xff, 0xd8, 0xff, 0xe0]), { status: 200 })]];
    const got = [];
    window.referenceImageSelectionActive = true;
    window.referenceImageSelectionCallback = (b64, mime, name) => got.push({ b64, mime, name });
    const before = fileUpload.getAttachedFiles().length;
    window.onNativeFileUploaded("/ui/uploads/big.jpg", "image/jpeg", "big.jpg");
    await settle(80);
    assert.deepEqual(got, [{ b64: "/9j/4A==", mime: "image/jpeg", name: "big.jpg" }]);
    assert.equal(fileUpload.getAttachedFiles().length, before, "nothing was attached to the chat");
    window.referenceImageSelectionActive = false;
});

test("with no modal waiting, a large native pick still attaches to the chat", async () => {
    routes = [["/ui/uploads/doc.pdf", () => new Response(new Uint8Array([0x25, 0x50]), { status: 200 })]];
    window.referenceImageSelectionActive = false;
    window.videoStartImageSelectionActive = false;
    const before = fileUpload.getAttachedFiles().length;
    window.onNativeFileUploaded("/ui/uploads/doc.pdf", "application/pdf", "doc.pdf");
    await settle(80);
    const files = fileUpload.getAttachedFiles();
    assert.equal(files.length, before + 1);
    assert.equal(files[files.length - 1]._preUploadedUrl, "/ui/uploads/doc.pdf");
});

test("the extension, SSML and Gemini Pro mic buttons start dictation into their own prompt", async () => {
    const src = readFileSync(resolve(PORTAL, "modules", "generation-modals.js"), "utf8");
    for (const [mic, input] of [["micVideoExtensionPrompt", "videoExtensionPrompt"],
        ["micGoogleSSML", "google-ssml-input"], ["micGeminiPro", "gemini-pro-text-input"]]) {
        assert.ok(src.includes(`['${mic}', '${input}']`), mic);
        assert.ok(document.getElementById(input), `${input} exists next to ${mic}`);
    }
});
