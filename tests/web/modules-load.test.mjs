// Every Portal module touched by the sanitizer work must still LINK and load:
// a missing named export or a duplicate escapeHtml declaration is a module
// link error that would blank the whole Portal (app-modular.js imports them all).
import test from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

const MODULES = [
    "core-utils", "sanitize", "markdown-renderer", "chat-bubbles", "chat-send",
    "agent-handler", "gemini-agent-handler", "timeline-browser", "todo-tracker",
    "contacts-manager", "slash-commands", "grok-live", "cellular-manager",
    "media-manager", "task-manager", "tts-stt", "file-upload", "generation-modals",
    "sms-inbox", "telephony-manager", "telephony-wizard", "device-manager", "cron-manager",
    // The live voice agents and the modules they share (audit rank 75).
    "gemini-live", "gpt-realtime", "stt-stream", "gemini-recorder", "audio-dsp",
    "native-bridge", "mic-capture", "pcm-capture-worklet", "live-session",
];

for (const name of MODULES) {
    test(`Portal/modules/${name}.js imports cleanly`, async () => {
        const mod = await import(`../../Portal/modules/${name}.js`);
        assert.ok(mod && typeof mod === "object");
    });
}
