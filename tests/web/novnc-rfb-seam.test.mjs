// Contract test for the CU viewer's use of the VENDORED noVNC (audit
// P07-html-css-14/-16, upgrade roadmap item 12: "noVNC 1.5 to 1.7 behind an
// rfb._sendMouse adapter").
//
// The viewer (Portal/cu-view/cu-view.js) constructs RFB from
// Portal/vendor/novnc/core/rfb.js, sets its public options, sends keys with
// the public sendKey, and sends pointer events through the PRIVATE
// RFB#_sendMouse, wrapped by Portal/cu-view/rfb-input.js. This test runs the
// real vendored module end to end: an RFB 3.8 handshake (no auth) over a fake
// WebSocket, then the bytes the adapter and sendKey put on the wire. A
// re-vendor that renames or changes the seam fails here, not on a phone.
//
// jsdom has no 2D canvas or ResizeObserver; those two are stubbed. The
// WebSocket is a fake the test drives as the VNC server.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";
import { installDom, VENDOR } from "./helpers/portal-dom.mjs";

const dom = installDom();
const w = dom.window;

// A 2D context whose every method is a no-op (getImageData returns pixels).
function fakeContext(canvas) {
    const imageData = (width, height) => ({ width, height, data: new Uint8ClampedArray(Math.max(0, width * height * 4)) });
    return new Proxy({ canvas }, {
        get(target, prop) {
            if (prop in target) return target[prop];
            if (prop === "getImageData" || prop === "createImageData") return (...a) => imageData(a.at(-2) | 0, a.at(-1) | 0);
            return () => {};
        },
        set(target, prop, value) { target[prop] = value; return true; },
    });
}
w.HTMLCanvasElement.prototype.getContext = function () { return fakeContext(this); };
globalThis.ResizeObserver = w.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
globalThis.CustomEvent = w.CustomEvent;

/** The VNC server side of the socket, driven by the test. */
const sockets = [];
class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    constructor(url, protocols) {
        this.url = url;
        this.protocols = protocols;
        this.protocol = "";
        this.readyState = FakeWebSocket.CONNECTING;
        this.binaryType = "blob";
        this.onopen = null;
        this.onmessage = null;
        this.onclose = null;
        this.onerror = null;
        this.sent = [];
        sockets.push(this);
    }
    send(data) { this.sent.push(Uint8Array.from(data)); }   // Websock reuses its buffer
    close() { this.readyState = FakeWebSocket.CLOSED; }
    serverOpen() {
        this.readyState = FakeWebSocket.OPEN;
        this.onopen();
    }
    serverSend(bytes) {
        const buf = Uint8Array.from(bytes).buffer;
        this.onmessage({ data: buf });
    }
    /** Everything the client sent since `from`, as one byte array. */
    bytesSince(from) {
        return Array.from(this.sent.slice(from).flatMap((u) => Array.from(u)));
    }
}
// websock.js reads WebSocket.CONNECTING etc. at import time.
globalThis.WebSocket = w.WebSocket = FakeWebSocket;

const { default: RFB } = await import(pathToFileURL(resolve(VENDOR, "novnc", "core", "rfb.js")).href);
const { sendPointerEvent, hasPointerSeam } = await import("../../Portal/cu-view/rfb-input.js");

const settle = (ms = 10) => new Promise((r) => setTimeout(r, ms));
const u16 = (n) => [(n >> 8) & 0xff, n & 0xff];
const u32 = (n) => [(n >>> 24) & 0xff, (n >>> 16) & 0xff, (n >>> 8) & 0xff, n & 0xff];
const ascii = (s) => Array.from(s, (c) => c.charCodeAt(0));

/** Connect an RFB the way cu-view.js does and complete the handshake. */
async function connectedRfb({ viewOnly = false, width = 1280, height = 720 } = {}) {
    const screen = document.createElement("div");
    document.body.appendChild(screen);
    const rfb = new RFB(screen, "ws://localhost:9091/cu/view/s1/ws", {});
    rfb.viewOnly = viewOnly;
    rfb.scaleViewport = false;
    rfb.clipViewport = false;
    rfb.resizeSession = false;
    rfb.showDotCursor = false;
    rfb.background = "#0b0b0d";
    const events = [];
    for (const name of ["connect", "disconnect", "desktopname"]) {
        rfb.addEventListener(name, (ev) => events.push([name, ev.detail]));
    }
    await settle();
    const sock = sockets.at(-1);
    sock.serverOpen();
    sock.serverSend(ascii("RFB 003.008\n"));
    await settle();
    assert.deepEqual(sock.bytesSince(0), ascii("RFB 003.008\n"), "client protocol version");
    sock.serverSend([1, 1]);                 // one security type: None
    await settle();
    sock.serverSend(u32(0));                 // SecurityResult OK
    await settle();
    const name = "CU desktop";
    sock.serverSend([
        ...u16(width), ...u16(height),
        32, 24, 0, 1, ...u16(255), ...u16(255), ...u16(255), 16, 8, 0, 0, 0, 0,
        ...u32(name.length), ...ascii(name),
    ]);
    await settle();
    return { rfb, sock, events, screen };
}

test("the vendored RFB has the private pointer seam the viewer uses", () => {
    assert.equal(typeof RFB.prototype._sendMouse, "function");
    assert.equal(RFB.prototype._sendMouse.length, 3, "_sendMouse(x, y, mask)");
    assert.equal(typeof RFB.prototype.sendKey, "function");
    assert.equal(typeof RFB.messages.pointerEvent, "function");
});

test("a handshake over the fake socket reaches 'connected' with the viewer's options", async () => {
    const { rfb, events } = await connectedRfb();
    assert.ok(events.some(([n]) => n === "connect"), JSON.stringify(events));
    assert.ok(events.some(([n, d]) => n === "desktopname" && d.name === "CU desktop"));
    assert.equal(rfb.viewOnly, false);
    assert.equal(rfb.scaleViewport, false);
    assert.equal(rfb.resizeSession, false);
    assert.ok(hasPointerSeam(rfb));
    rfb.disconnect();
});

test("the adapter puts a PointerEvent with display coords and the mask on the wire", async () => {
    const { rfb, sock } = await connectedRfb();
    const from = sock.sent.length;
    assert.equal(sendPointerEvent(rfb, 100.4, 200.6, 1), true);
    assert.equal(sendPointerEvent(rfb, 1279, 719, 0), true);
    assert.equal(sendPointerEvent(rfb, 640, 360, 64), true);       // button 7 (horizontal wheel)
    assert.deepEqual(sock.bytesSince(from), [
        5, 1, ...u16(100), ...u16(201),
        5, 0, ...u16(1279), ...u16(719),
        5, 64, ...u16(640), ...u16(360),
    ]);
    rfb.disconnect();
});

test("view-only and not-yet-connected sessions send nothing", async () => {
    const { rfb, sock } = await connectedRfb({ viewOnly: true });
    const from = sock.sent.length;
    sendPointerEvent(rfb, 10, 10, 1);
    rfb.sendKey(0xff0d, null, true);
    assert.deepEqual(sock.bytesSince(from), []);
    rfb.disconnect();

    const early = new RFB(document.createElement("div"), "ws://localhost:9091/cu/view/s2/ws", {});
    await settle();
    const s2 = sockets.at(-1);
    s2.serverOpen();
    const before = s2.sent.length;
    sendPointerEvent(early, 10, 10, 1);
    assert.deepEqual(s2.bytesSince(before), [], "no pointer bytes before the handshake");
    early.disconnect();
});

test("sendKey (public) still emits KeyEvent messages", async () => {
    const { rfb, sock } = await connectedRfb();
    const from = sock.sent.length;
    rfb.sendKey(0xff0d, null, true);
    rfb.sendKey(0xff0d, null, false);
    assert.deepEqual(sock.bytesSince(from), [4, 1, 0, 0, ...u32(0xff0d), 4, 0, 0, 0, ...u32(0xff0d)]);
    rfb.disconnect();
});
