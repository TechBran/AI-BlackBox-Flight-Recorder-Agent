// One bubble audio player (audit P02-voice-web-12). tts-stt.js carried three
// ~100-line copies of the same inline player: attachAudioPlayer (spoken
// replies), attachGeneratedAudioPlayer (generated speech and music) and
// attachThinkingAudioPlayer (spoken reasoning). They now build it with
// createBubbleAudioPlayer (Portal/modules/bubble-audio-player.js) and keep
// only where it goes and which button it lights up.
//
// The attach* tests pin each site's behaviour as it was before the
// extraction, so they hold on both sides of it.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { installDom, loadPurify, loadMarked, PORTAL } from "./helpers/portal-dom.mjs";
import { stripComments, innerHtmlAssignments, isSafeRhs, rawHtmlApiUses } from "./helpers/sink-scan.mjs";

installDom();
loadPurify();
loadMarked();

// jsdom does not implement media playback: record play()/pause() instead.
const calls = [];
window.HTMLMediaElement.prototype.play = function () { calls.push(["play", this]); return Promise.resolve(); };
window.HTMLMediaElement.prototype.pause = function () { calls.push(["pause", this]); };

const tts = await import("../../Portal/modules/tts-stt.js");
const { createBubbleAudioPlayer, formatPlayerTime } = await import("../../Portal/modules/bubble-audio-player.js");

const DATA_URL = "data:audio/mpeg;base64,SUQz";

function fire(el, type) {
    el.dispatchEvent(new window.Event(type));
}

/** Give a detached-from-playback <audio> a duration and a position. */
function setClock(audio, { duration, currentTime }) {
    Object.defineProperty(audio, "duration", { value: duration, configurable: true });
    Object.defineProperty(audio, "currentTime", { value: currentTime, writable: true, configurable: true });
}

function parts(container) {
    return {
        audio: container.querySelector("audio"),
        playBtn: container.querySelector(".audio-play-btn"),
        playIcon: container.querySelector(".play-icon"),
        pauseIcon: container.querySelector(".pause-icon"),
        bar: container.querySelector(".audio-progress-bar"),
        fill: container.querySelector(".audio-progress-fill"),
        time: container.querySelector(".audio-time"),
        download: container.querySelector(".audio-download-btn"),
        label: container.querySelector(".audio-label-icon"),
    };
}

/** Every player shares these controls. */
function checkControls(container) {
    const p = parts(container);
    assert.equal(p.audio.getAttribute("preload"), "metadata");
    assert.equal(p.pauseIcon.style.display, "none");

    // The play button toggles playback.
    calls.length = 0;
    p.playBtn.click();
    assert.deepEqual(calls.map((c) => c[0]), ["play"]);
    Object.defineProperty(p.audio, "paused", { value: false, configurable: true });
    p.playBtn.click();
    assert.deepEqual(calls.map((c) => c[0]), ["play", "pause"]);
    assert.ok(calls.every((c) => c[1] === p.audio));

    // The icons follow the element's own events.
    fire(p.audio, "play");
    assert.equal(p.playIcon.style.display, "none");
    assert.equal(p.pauseIcon.style.display, "block");
    assert.ok(p.playBtn.classList.contains("playing"));
    fire(p.audio, "pause");
    assert.equal(p.playIcon.style.display, "block");
    assert.equal(p.pauseIcon.style.display, "none");
    assert.ok(!p.playBtn.classList.contains("playing"));

    // Progress, time and seeking.
    setClock(p.audio, { duration: 90, currentTime: 45 });
    fire(p.audio, "loadedmetadata");
    assert.equal(p.time.textContent, "1:30");
    fire(p.audio, "timeupdate");
    assert.equal(p.fill.style.width, "50%");
    assert.equal(p.time.textContent, "0:45");
    p.bar.getBoundingClientRect = () => ({ left: 10, width: 200 });
    p.bar.dispatchEvent(new window.MouseEvent("click", { clientX: 60, bubbles: true }));
    assert.equal(p.audio.currentTime, 22.5);
    fire(p.audio, "play");
    fire(p.audio, "ended");
    assert.equal(p.fill.style.width, "0%");
    assert.ok(!p.playBtn.classList.contains("playing"));
    assert.equal(p.playIcon.style.display, "block");
}

function bubbleWithControls() {
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = '<div class="bubble-content">hi</div>'
        + '<div class="bubble-controls"><button class="bubble-btn bubble-btn-loading" title="Listen"></button></div>'
        + '<div class="after-controls"></div>';
    document.getElementById("history").appendChild(bubble);
    return bubble;
}

// ── the factory ──────────────────────────────────────────────────────────
test("createBubbleAudioPlayer builds the shared controls and calls back", () => {
    const seen = [];
    const player = createBubbleAudioPlayer({
        src: "/ui/uploads/a.mp3",
        className: "generated-audio-player",
        onDownload: () => seen.push("download"),
        onPlay: () => seen.push("play"),
        onPause: () => seen.push("pause"),
        onEnded: () => seen.push("ended"),
    });
    const { container, audio } = player;
    assert.equal(container.className, "bubble-audio-player generated-audio-player");
    assert.equal(audio, container.querySelector("audio"));
    assert.equal(audio.getAttribute("src"), "/ui/uploads/a.mp3");
    assert.equal(parts(container).label, null, "no label unless asked");
    assert.equal(container.isConnected, false, "the caller places it");
    document.body.appendChild(container);
    checkControls(container);
    parts(container).download.click();
    assert.deepEqual(seen, ["play", "pause", "play", "ended", "download"]);
    container.remove();
});

test("createBubbleAudioPlayer: a label, no callbacks, and play() that tolerates a refusal", async () => {
    const player = createBubbleAudioPlayer({ src: DATA_URL, labelIcon: "🧠" });
    const p = parts(player.container);
    assert.equal(p.label.textContent, "🧠");
    assert.equal(p.label.previousElementSibling, p.audio, "the label sits between the audio and the button");
    assert.equal(p.label.nextElementSibling, p.playBtn);
    p.download.click();  // no onDownload: nothing happens
    fire(p.audio, "play");
    fire(p.audio, "ended");
    const refused = window.HTMLMediaElement.prototype.play;
    window.HTMLMediaElement.prototype.play = () => Promise.reject(new Error("NotAllowedError"));
    try {
        await player.play();  // autoplay blocked: logged, never thrown
    } finally {
        window.HTMLMediaElement.prototype.play = refused;
    }
});

test("an attribute-breaking src stays one src attribute", () => {
    const evil = 'x" onerror="window.__pwned=1';
    const { container, audio } = createBubbleAudioPlayer({ src: evil });
    assert.equal(audio.getAttribute("src"), evil);
    assert.equal(audio.getAttribute("onerror"), null);
    assert.equal(container.querySelectorAll("[onerror]").length, 0);
});

test("the player parses no caller data as HTML: its only innerHTML is static markup", () => {
    const code = stripComments(readFileSync(resolve(PORTAL, "modules/bubble-audio-player.js"), "utf8"));
    const writes = innerHtmlAssignments(code);
    assert.equal(writes.length, 1);
    assert.ok(writes.every((a) => isSafeRhs(a.rhs)), "a template with no interpolation");
    assert.deepEqual(rawHtmlApiUses(code), []);
    const tts = stripComments(readFileSync(resolve(PORTAL, "modules/tts-stt.js"), "utf8"));
    assert.doesNotMatch(tts, /class="audio-play-btn"/, "tts-stt.js keeps no copy of the player");
});

test("formatPlayerTime", () => {
    assert.equal(formatPlayerTime(NaN), "0:00");
    assert.equal(formatPlayerTime(0), "0:00");
    assert.equal(formatPlayerTime(9.9), "0:09");
    assert.equal(formatPlayerTime(61), "1:01");
    assert.equal(formatPlayerTime(3600), "60:00");
});

// ── the three sites (pinned before the extraction) ───────────────────────
test("attachAudioPlayer: after the controls, one per bubble, drives the Listen button", () => {
    const bubble = bubbleWithControls();
    const listen = bubble.querySelector('.bubble-btn[title="Listen"]');
    tts.attachAudioPlayer(bubble, DATA_URL);
    tts.attachAudioPlayer(bubble, DATA_URL);
    const players = bubble.querySelectorAll(".bubble-audio-player");
    assert.equal(players.length, 1, "a second attach replaces the first");
    const container = players[0];
    assert.equal(container.className, "bubble-audio-player");
    assert.equal(container.previousElementSibling, bubble.querySelector(".bubble-controls"));
    assert.equal(parts(container).audio.getAttribute("src"), DATA_URL);
    assert.equal(parts(container).label, null);

    fire(parts(container).audio, "play");
    assert.ok(listen.classList.contains("bubble-btn-playing"));
    assert.ok(!listen.classList.contains("bubble-btn-loading"));
    fire(parts(container).audio, "pause");
    assert.ok(!listen.classList.contains("bubble-btn-playing"), "paused by the user or another player");
    fire(parts(container).audio, "play");
    fire(parts(container).audio, "ended");
    assert.ok(!listen.classList.contains("bubble-btn-playing"));
    checkControls(container);

    parts(container).download.click();
    assert.match(document.getElementById("toast").textContent, /^Downloading tts-response-.*\.mp3\.\.\.$/);
});

test("attachAudioPlayer: an explicit speaker button, no controls bar, autoplay", () => {
    const bubble = document.createElement("div");
    document.getElementById("history").appendChild(bubble);
    const speaker = document.createElement("button");
    calls.length = 0;
    tts.attachAudioPlayer(bubble, DATA_URL, true, speaker);
    const container = bubble.querySelector(".bubble-audio-player");
    assert.equal(container.parentElement, bubble, "appended when there is no controls bar");
    assert.equal(bubble.lastElementChild, container);
    assert.deepEqual(calls.map((c) => [c[0], c[1] === parts(container).audio]), [["play", true]],
        "autoplay starts the new player, once it is in the page");
    fire(parts(container).audio, "play");
    assert.ok(speaker.classList.contains("bubble-btn-playing"));
});

test("attachGeneratedAudioPlayer: inside the audio result, never autoplays, keeps earlier players", () => {
    const bubble = document.createElement("div");
    bubble.innerHTML = '<div class="audio-result"><p>Music ready</p></div>';
    document.getElementById("history").appendChild(bubble);
    calls.length = 0;
    tts.attachGeneratedAudioPlayer(bubble, "/ui/uploads/song.mp3", "generated-music");
    tts.attachGeneratedAudioPlayer(bubble, "/ui/uploads/song2.wav");
    assert.deepEqual(calls, [], "no autoplay");
    const players = bubble.querySelectorAll(".audio-result > .bubble-audio-player.generated-audio-player");
    assert.equal(players.length, 2, "each generated clip keeps its own player");
    assert.equal(players[0].className, "bubble-audio-player generated-audio-player");
    assert.equal(parts(players[0]).audio.getAttribute("src"), "/ui/uploads/song.mp3");
    checkControls(players[0]);
    parts(players[0]).download.click();
    assert.match(document.getElementById("toast").textContent, /^Downloading generated-music-.*\.mp3\.\.\.$/);
    parts(players[1]).download.click();
    assert.match(document.getElementById("toast").textContent, /^Downloading generated-audio-.*\.wav\.\.\.$/);

    const bare = document.createElement("div");
    tts.attachGeneratedAudioPlayer(bare, "/ui/uploads/x.ogg");
    assert.equal(bare.lastElementChild.className, "bubble-audio-player generated-audio-player",
        "appended to the bubble when it has no audio result");
});

test("attachThinkingAudioPlayer: after the panel, one per panel, drives the reasoning button", () => {
    const wrap = document.createElement("div");
    wrap.innerHTML = '<div class="streaming-thinking-panel"></div><div class="after"></div>';
    document.getElementById("history").appendChild(wrap);
    const panel = wrap.querySelector(".streaming-thinking-panel");
    const btn = document.createElement("button");
    btn.className = "thinking-btn-loading";
    calls.length = 0;
    tts.attachThinkingAudioPlayer(panel, DATA_URL, btn, true);
    tts.attachThinkingAudioPlayer(panel, DATA_URL, btn, false);
    const players = wrap.querySelectorAll(".thinking-audio-player");
    assert.equal(players.length, 1, "a second attach replaces the first");
    const container = players[0];
    assert.equal(container.className, "bubble-audio-player thinking-audio-player");
    assert.equal(container.previousElementSibling, panel);
    assert.equal(parts(container).label.textContent, "🧠");
    assert.equal(calls.filter((c) => c[0] === "play").length, 1, "only the autoplay attach played");

    fire(parts(container).audio, "play");
    assert.ok(btn.classList.contains("thinking-btn-playing"));
    assert.ok(!btn.classList.contains("thinking-btn-loading"));
    fire(parts(container).audio, "pause");
    assert.ok(btn.classList.contains("thinking-btn-playing"), "a pause leaves the reasoning button lit");
    fire(parts(container).audio, "ended");
    assert.ok(!btn.classList.contains("thinking-btn-playing"));
    checkControls(container);
    parts(container).download.click();
    assert.match(document.getElementById("toast").textContent, /^Downloading tts-reasoning-.*\.mp3\.\.\.$/);
});
