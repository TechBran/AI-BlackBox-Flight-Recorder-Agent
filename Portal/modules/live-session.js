/**
 * live-session.js
 * The client half of a live voice session's connection: open, keepalive,
 * reconnect with backoff, teardown.
 *
 * gpt-realtime.js, gemini-live.js and grok-live.js each carried this state
 * machine twice (a connect path and a reconnect path with their own message
 * builders), and the copies drifted: a reconnect rebuilt a thinner connect
 * message than the first connect and silently downgraded the session to the
 * server defaults (T14 F1), and a reconnected socket that dropped again left
 * the UI "connected" with the mic streaming into a dead socket (audit
 * P03-live-agents-web-17). LiveSession owns the socket; the module supplies
 * the endpoint, ONE connect-message builder (used for the first connect and
 * every reconnect) and its message vocabulary.
 *
 *   const session = new LiveSession({
 *       path: '/ws/gemini-live/', logPrefix: '[GEMINI-LIVE]',
 *       buildConnectMsg: () => ({type: 'connect', ...}),
 *       onMessage, onStatus, onDropped, onClosed, onGiveUp, onResumed,
 *   });
 *   session.connect(); session.send({type: 'ping'}); session.disconnect();
 *
 * The server's `connected` frame (re)starts the keepalive and resets the
 * reconnect budget, `pong` feeds the keepalive, `reconnected` (the server
 * re-dialed its provider) resets the budget too; every frame then goes to
 * onMessage.
 *
 * Created: 2026-10-29 (audit rank 75)
 */

const OPEN = 1;
const CONNECTING = 0;

export class LiveSession {
    /**
     * @param {object} opts
     * @param {string} opts.path - Endpoint path up to the session id ('/ws/realtime/')
     * @param {string} [opts.logPrefix]
     * @param {() => object} opts.buildConnectMsg - The connect message (first and every reconnect)
     * @param {(msg: object) => void} [opts.onMessage] - Every server frame
     * @param {(text: string) => void} [opts.onStatus] - Status line text
     * @param {() => void} [opts.onDropped] - A live (connected) socket closed unexpectedly; a reconnect follows
     * @param {() => void} [opts.onClosed] - Closed for good by a normal close or disconnect()
     * @param {(exhausted: boolean) => void} [opts.onGiveUp] - No (more) reconnect: budget spent, or intentional
     * @param {() => void} [opts.onResumed] - A reconnect's socket is connected again
     * @param {number} [opts.maxReconnectAttempts=15]
     * @param {number} [opts.keepaliveMs=15000] - Ping interval
     * @param {number} [opts.pongTimeoutMs=25000] - No pong for this long: close and reconnect
     * @param {number} [opts.maxBackoffMs=30000]
     * @param {Function} [opts.WebSocketImpl] - For tests (default window.WebSocket)
     * @param {object} [opts.timers] - For tests: {setTimeout, clearTimeout, setInterval, clearInterval, now}
     * @param {() => string} [opts.newSessionId] - For tests (default crypto.randomUUID)
     */
    constructor(opts) {
        this.opts = {
            logPrefix: '[LIVE-SESSION]',
            maxReconnectAttempts: 15,
            keepaliveMs: 15000,
            pongTimeoutMs: 25000,
            maxBackoffMs: 30000,
            ...opts,
        };
        this.timers = opts.timers || {
            setTimeout: (fn, ms) => setTimeout(fn, ms),
            clearTimeout: (t) => clearTimeout(t),
            setInterval: (fn, ms) => setInterval(fn, ms),
            clearInterval: (t) => clearInterval(t),
            now: () => Date.now(),
        };
        this.ws = null;
        this.sessionId = null;
        this.reconnectAttempts = 0;
        this.reconnectTimer = null;
        this.keepaliveTimer = null;
        this.lastPongTime = 0;
        this.intentionalDisconnect = false;
    }

    get maxReconnectAttempts() {
        return this.opts.maxReconnectAttempts;
    }

    /** @returns {boolean} The socket is open */
    get isOpen() {
        return !!(this.ws && this.ws.readyState === OPEN);
    }

    /** @returns {boolean} The socket is still opening */
    get isConnecting() {
        return !!(this.ws && this.ws.readyState === CONNECTING);
    }

    /** Send a frame (JSON). False when the socket is not open. */
    send(msg) {
        if (!this.isOpen) return false;
        this.ws.send(JSON.stringify(msg));
        return true;
    }

    /** Open a new session (a fresh session id). */
    connect() {
        this.sessionId = this.opts.newSessionId ? this.opts.newSessionId() : crypto.randomUUID();
        this.reconnectAttempts = 0;
        this._open(false);
    }

    /**
     * End the session: no reconnect, tell the server, close the socket.
     */
    disconnect() {
        this.intentionalDisconnect = true;
        this.reconnectAttempts = this.opts.maxReconnectAttempts;
        if (this.reconnectTimer) {
            this.timers.clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
        this._stopKeepalive();
        if (this.ws) {
            try {
                this.ws.send(JSON.stringify({ type: 'disconnect' }));
            } catch (e) { /* not open */ }
            this.ws.close();
            this.ws = null;
        }
        this.sessionId = null;
        this.reconnectAttempts = 0;
    }

    _url() {
        const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        return `${wsProtocol}//${window.location.host}${this.opts.path}${this.sessionId}`;
    }

    _open(resuming) {
        const { logPrefix } = this.opts;
        const url = this._url();
        console.log(`${logPrefix} ${resuming ? 'Reconnecting' : 'Connecting'} to:`, url);
        if (!resuming && this.opts.onStatus) this.opts.onStatus('Connecting...');

        const Impl = this.opts.WebSocketImpl || window.WebSocket;
        const ws = new Impl(url);
        this.ws = ws;
        let live = false;

        ws.onopen = () => {
            console.log(`${logPrefix} WebSocket ${resuming ? 'reconnected' : 'connected'}`);
            if (!resuming && this.opts.onStatus) this.opts.onStatus('Initializing...');
            this.intentionalDisconnect = false;
            ws.send(JSON.stringify(this.opts.buildConnectMsg()));
        };

        ws.onmessage = (event) => {
            let msg;
            try {
                msg = JSON.parse(event.data);
            } catch (err) {
                console.error(`${logPrefix} Failed to parse message:`, err);
                return;
            }
            if (msg.type === 'connected') {
                live = true;
                this.reconnectAttempts = 0;
                this._startKeepalive();
            } else if (msg.type === 'pong') {
                this.lastPongTime = this.timers.now();
            } else if (msg.type === 'reconnected') {
                this.reconnectAttempts = 0;
            }
            if (this.opts.onMessage) this.opts.onMessage(msg);
            if (msg.type === 'connected' && resuming && this.opts.onResumed) this.opts.onResumed();
        };

        ws.onclose = (event) => {
            console.log(`${logPrefix} WebSocket closed:`, event.code, event.reason);
            if (this.ws === ws) this._stopKeepalive();
            if (this.ws !== ws && this.ws !== null) return;  // an older socket; a newer one owns the session
            if (event.code === 1000 || this.intentionalDisconnect) {
                if (this.opts.onClosed) this.opts.onClosed();
                return;
            }
            // Unexpected close: a live session dropped, or a reconnect failed.
            if (live && this.opts.onDropped) this.opts.onDropped();
            this._attemptReconnect();
        };

        ws.onerror = (err) => {
            console.error(`${logPrefix} WebSocket error:`, err);
        };
    }

    _attemptReconnect() {
        const { logPrefix, maxReconnectAttempts, maxBackoffMs } = this.opts;
        if (this.intentionalDisconnect || this.reconnectAttempts >= maxReconnectAttempts) {
            console.log(`${logPrefix} Not reconnecting (intentional or max attempts reached)`);
            if (this.opts.onGiveUp) this.opts.onGiveUp(this.reconnectAttempts >= maxReconnectAttempts);
            return;
        }
        if (!this.sessionId) {
            console.log(`${logPrefix} No session to reconnect to`);
            return;
        }

        this.reconnectAttempts++;
        // Exponential backoff: 1s, 2s, 4s, 8s, ... capped
        const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts - 1), maxBackoffMs);
        console.log(`${logPrefix} Reconnecting (${this.reconnectAttempts}/${maxReconnectAttempts}) in ${delay}ms...`);
        if (this.opts.onStatus) this.opts.onStatus(`Reconnecting (${this.reconnectAttempts}/${maxReconnectAttempts})...`);

        this.reconnectTimer = this.timers.setTimeout(() => {
            this.reconnectTimer = null;
            if (this.sessionId && !this.intentionalDisconnect) this._open(true);
        }, delay);
    }

    _startKeepalive() {
        this._stopKeepalive();
        this.lastPongTime = this.timers.now();
        const ws = this.ws;
        this.keepaliveTimer = this.timers.setInterval(() => {
            if (ws && ws === this.ws && ws.readyState === OPEN) {
                ws.send(JSON.stringify({ type: 'ping' }));
                // No pong recently: the socket is dead without knowing it.
                if (this.lastPongTime && (this.timers.now() - this.lastPongTime > this.opts.pongTimeoutMs)) {
                    console.log(`${this.opts.logPrefix} No pong received in ${this.opts.pongTimeoutMs / 1000}s, closing for reconnect`);
                    ws.close();
                }
            }
        }, this.opts.keepaliveMs);
    }

    _stopKeepalive() {
        if (this.keepaliveTimer) {
            this.timers.clearInterval(this.keepaliveTimer);
            this.keepaliveTimer = null;
        }
    }
}
