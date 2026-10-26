// Smoke test for the CLI Agents terminal on the vendored xterm.js 6 + fit
// addon 0.11 (audit rank 73: P07-html-css-14). The tmux-backend modal is
// driven through its real wiring (initCLIAgentsModal, the index.html markup,
// the Launch button): it must create a Terminal, fit it to its box, send the
// fitted size in the WebSocket handshake, write PTY bytes with ANSI escapes,
// send keystrokes as binary frames, and refit + report a new size on resize.
//
// The libraries are the files index.html loads from /ui/vendor/; jsdom has no
// layout, so the one thing stubbed is the browser's measurement of xterm's
// character-measure element (8 x 17 px per cell) and the container's size.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { JSDOM } from "jsdom";
import { installDom, loadPurify, loadXterm, PORTAL } from "./helpers/portal-dom.mjs";

const dom = installDom();
const w = dom.window;
loadPurify();
const { Terminal: RealTerminal } = loadXterm();

// Character cell metrics a browser would measure for xterm's measure element.
const CELL_W = 8;
const CELL_H = 17;
for (const [prop, value] of [["offsetWidth", CELL_W * 32], ["offsetHeight", CELL_H]]) {
    const original = Object.getOwnPropertyDescriptor(w.HTMLElement.prototype, prop);
    Object.defineProperty(w.HTMLElement.prototype, prop, {
        configurable: true,
        get() {
            if (this.classList && this.classList.contains("xterm-char-measure-element")) return value;
            return original.get.call(this);
        },
    });
}

// Every Terminal the modal creates, so the test can read its buffer.
const terminals = [];
w.Terminal = class extends RealTerminal {
    constructor(options) {
        super(options);
        terminals.push(this);
    }
};

// The real modal markup from Portal/index.html.
const page = new JSDOM(readFileSync(resolve(PORTAL, "index.html"), "utf8"));
document.body.appendChild(document.importNode(page.window.document.getElementById("cliAgentsModal"), true));
const openBtn = document.createElement("button");
openBtn.id = "btnCLIAgents";
document.body.appendChild(openBtn);
const host = document.getElementById("cliAgentsTerminal");
host.setAttribute("style", "width: 800px; height: 408px; padding: 0px;");
const style = document.createElement("style");
style.textContent = ".xterm { padding: 0px; }";
document.head.appendChild(style);

/** A WebSocket the test drives by hand. */
class FakeSocket extends w.EventTarget {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    constructor(url) {
        super();
        this.url = url;
        this.readyState = FakeSocket.CONNECTING;
        this.sent = [];
        sockets.push(this);
    }
    send(data) { this.sent.push(data); }
    close() { this.readyState = FakeSocket.CLOSED; }
    serverOpen() {
        this.readyState = FakeSocket.OPEN;
        this.dispatchEvent(new w.Event("open"));
    }
    serverSend(data) {
        const ev = new w.Event("message");
        ev.data = data;
        this.dispatchEvent(ev);
    }
}
const sockets = [];
globalThis.WebSocket = w.WebSocket = FakeSocket;

const json = (obj) => new Response(JSON.stringify(obj), { status: 200, headers: { "Content-Type": "application/json" } });
globalThis.fetch = w.fetch = async (url) => {
    if (String(url).startsWith("/cli-agent/zellij/backend-status")) return json({ effective_backend: "tmux" });
    if (String(url).startsWith("/agent/apps")) return json({ apps: [] });
    return json({});
};
localStorage.setItem("bbx_operator", "tester");

const errors = [];
const realError = console.error;
console.error = (...a) => { errors.push(a.map(String).join(" ")); };
process.on("exit", () => { console.error = realError; });

const settle = (ms = 80) => new Promise((r) => setTimeout(r, ms));
/** Resolve once xterm has parsed everything written so far. */
const flushed = (term) => new Promise((r) => term.write("", r));
const lineText = (term) => {
    const buf = term.buffer.active;
    const out = [];
    for (let y = 0; y < buf.length; y++) out.push(buf.getLine(y).translateToString(true));
    return out;
};
const resizeFrames = (sock) => sock.sent
    .filter((d) => typeof d === "string")
    .map((d) => JSON.parse(d))
    .filter((m) => m.type === "resize");

const { initCLIAgentsModal } = await import("../../Portal/modules/cli-agents-modal.js");
initCLIAgentsModal();

test("launching a session creates ONE fitted terminal and sends its size in the handshake", async () => {
    openBtn.click();
    await settle();
    document.getElementById("cliAgentsLaunch").click();
    await settle();

    assert.equal(terminals.length, 1, "the modal created a Terminal from window.Terminal");
    const term = terminals[0];
    assert.ok(host.querySelector(".xterm"), "the terminal opened inside #cliAgentsTerminal");
    // 800 px wide minus the scrollbar gutter, 408 px tall, at 8 x 17 px cells.
    assert.ok(term.cols > 80 && term.cols <= Math.floor(800 / CELL_W), `fitted cols ${term.cols}`);
    assert.equal(term.rows, Math.floor(408 / CELL_H));

    assert.equal(sockets.length, 1);
    const url = new URL(sockets[0].url);
    assert.match(url.pathname, /^\/cli-agent\/ws\/cli-agent-tester__claude___root$/);
    assert.equal(url.searchParams.get("cols"), String(term.cols));
    assert.equal(url.searchParams.get("rows"), String(term.rows));
    assert.deepEqual(errors, []);
});

test("PTY bytes with ANSI escapes are rendered with their attributes", async () => {
    const term = terminals[0];
    const sock = sockets[0];
    sock.serverOpen();
    const pty = new TextEncoder().encode("\x1b[1;32mhello\x1b[0m world\r\n\x1b[2Kbox ─│┌\r\n");
    sock.serverSend(pty.buffer);
    await flushed(term);

    const lines = lineText(term);
    const at = lines.findIndex((l) => l.startsWith("hello world"));
    assert.ok(at >= 0, lines.filter(Boolean).join("\n"));
    assert.ok(lines.includes("box ─│┌"), "box-drawing glyphs survive");
    const line = term.buffer.active.getLine(at);
    const h = line.getCell(0);
    assert.equal(h.getChars(), "h");
    assert.ok(h.isBold(), "bold");
    assert.ok(h.isFgPalette() && h.getFgColor() === 2, "green (palette 2)");
    const space = line.getCell(5);
    assert.equal(space.isBold(), 0, "attributes reset after ESC[0m");

    // The connect banner and the authoritative post-open resize both went out.
    assert.ok(lines.some((l) => l.includes("[connected]")), lines.filter(Boolean).join("\n"));
    const sizes = resizeFrames(sock);
    assert.ok(sizes.length >= 1);
    assert.deepEqual(sizes.at(-1), { type: "resize", cols: term.cols, rows: term.rows });
});

test("keystrokes go to the PTY as binary frames", () => {
    const sock = sockets[0];
    const before = sock.sent.length;
    terminals[0].input("ls -la\r");
    const frames = sock.sent.slice(before).filter((d) => d instanceof Uint8Array);
    assert.equal(new TextDecoder().decode(frames.at(-1)), "ls -la\r");
});

test("a window resize refits the terminal and reports the new size", async () => {
    const term = terminals[0];
    const sock = sockets[0];
    const oldCols = term.cols;
    host.setAttribute("style", "width: 400px; height: 204px; padding: 0px;");
    window.dispatchEvent(new w.Event("resize"));
    await settle();
    assert.ok(term.cols < oldCols && term.cols <= Math.floor(400 / CELL_W), `cols ${oldCols} -> ${term.cols}`);
    assert.equal(term.rows, Math.floor(204 / CELL_H));
    assert.deepEqual(resizeFrames(sock).at(-1), { type: "resize", cols: term.cols, rows: term.rows });
    // The scrollback survives the reflow.
    assert.ok(lineText(term).some((l) => l.startsWith("hello")));
});

test("the DOM renderer paints the buffer into the page", async () => {
    await settle(120);   // a render frame
    const rows = host.querySelector(".xterm-rows");
    assert.ok(rows, host.innerHTML.slice(0, 400));
    assert.ok(rows.textContent.includes("hello"), rows.textContent.slice(0, 200));
});

test("a relaunch reuses the terminal and clears it", async () => {
    document.getElementById("cliAgentsDisconnect").click();
    document.getElementById("cliAgentsLaunch").click();
    await settle();
    assert.equal(terminals.length, 1, "no second Terminal");
    assert.equal(sockets.length, 2);
    assert.deepEqual(errors, []);
});
