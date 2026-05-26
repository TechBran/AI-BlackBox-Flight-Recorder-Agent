# Zellij web-client WebSocket protocol (v0.44.3)

T17 spike output for Phase 4 / Track D (Android MVP CLI Agents). Documents
zellij's web-client WebSocket protocol so T18 can implement a Kotlin
client without further reverse engineering.

**Source of truth:** `/tmp/zellij-build/zellij/zellij-client/assets/websockets.js`
(131 lines of well-commented JS, shipped with zellij 0.44.3, served at
`http://<host>:9097/assets/websockets.js` from the running daemon).

## Connection topology

Two WebSockets per session:

1. **Terminal WS** — `ws://host:9097/ws/terminal/{sessionName}?web_client_id={uuid}`
   - Carries the PTY byte stream (TUI rendering data + keystrokes)
   - Bidirectional plain WebSocket — no proprietary framing
2. **Control WS** — `ws://host:9097/ws/control`
   - Carries JSON control messages (resize, theme push, log mirror, session-switched notification)
   - NO session name in URL — control channel is per-client, not per-session

## Authentication

- Cookie-based: `session_token=<uuid>` cookie set via `POST /command/login`
  (orchestrator already does this — see `/cli-agent/zellij/launch` flow:
  mint token via zellij CLI, return URL + token; client posts token to
  `/command/login` to get the cookie, then opens iframe).
- `web_client_id` query param — UUID identifier for THIS client connection.
  Generated client-side; reused for both terminal and control WS.
- HTTPS-enforcement is OFF for localhost per our install.sh config
  (`enforce_https_for_localhost false`), so plain `ws://` works for
  Android dev/test; production would use `wss://` via the Tailscale
  funnel cert.

## Terminal WS (bytes)

**Server → Client**: raw byte stream OR string for ANSI sequences like
title-changes (`\x1b]0;<title>\x07`). Feed directly to xterm.js
`term.write(data)`. **For Android Kotlin client: feed straight to
`TerminalEmulator.append(bytes, length)` on the existing Termux
`TerminalView`** — same pattern as the current `CliAgentWebSocket`
already does.

**Client → Server**: raw byte stream — keystrokes encoded as ANSI key
sequences (e.g., `\x1b[A` for up arrow, `\x03` for Ctrl-C, plain ASCII
for typed characters). Send via `wsTerminal.send(bytes)`. **For Android:
intercept `TerminalView` keystrokes the same way `TerminalScreen.kt`
already does, then forward to `wsTerminal.send()` instead of the current
tmux WebSocket.**

No special framing on either direction. The wire is JUST the bytes the
PTY would carry.

## Control WS (JSON)

All messages are JSON-over-text-WebSocket.

**Client → Server messages:**

```json
{
  "web_client_id": "<uuid>",
  "payload": {
    "type": "TerminalResize",
    "rows": 24,
    "cols": 80
  }
}
```

```json
{
  "web_client_id": "<uuid>",
  "payload": {
    "type": "TerminalMetrics",
    "cell_pixel_width": 9,
    "cell_pixel_height": 18,
    "text_area_pixel_width": 720,
    "text_area_pixel_height": 432
  }
}
```

`TerminalResize` whenever the grid changes (window resize, rotate,
foldable unfold).
`TerminalMetrics` answers host-terminal pixel-dimension queries (CSI
14t / 16t / OSC 11;?). If we don't send TerminalMetrics, those host
queries get default values — likely fine for most TUIs.

**Server → Client messages** (no envelope wrapper, just the payload):

| Type | Fields | What to do |
|---|---|---|
| `SetConfig` | `font`, `theme`, `cursor_blink`, `mac_option_is_meta`, `cursor_style`, `cursor_inactive_style` | Apply to TerminalView (font + theme). Theme `background` is the new pane bg. |
| `QueryTerminalSize` | (none) | Reply with `TerminalResize` + `TerminalMetrics`. |
| `Log` | `lines: string[]` | Log to client console / dev logs (optional). |
| `LogError` | `lines: string[]` | Log as error (optional). |
| `SwitchedSession` | `new_session_name: string` | Navigate to new session — for Android, this means open a new terminal WS to `/ws/terminal/{new_session_name}` and close the old one. |

## Connection lifecycle

1. Client opens `wsTerminal` first.
2. On `wsTerminal.onopen` → `markConnectionEstablished()` (zero protocol bytes; just lifecycle hook).
3. On first `wsTerminal.onmessage` → open `wsControl`.
4. On `wsControl.onopen` → call `sendSizeUpdate(wsControl, ownWebClientId, term, rows, cols)` — initial resize.
5. Steady state: terminal WS streams bytes, control WS handles JSON.

## Close codes

- **4001** = "intentional disconnect by host" — DO NOT reconnect, show "Disconnected by host" modal.
- **Anything else** (network drop, server restart) = reconnect with exponential backoff [1, 2, 4, 8, 16] seconds.

## Kotlin implementation sketch (T18)

```kotlin
class ZellijWebSocketClient(
    private val origin: String,           // "http(s)://host:9097"
    private val sessionName: String,
    private val sessionToken: String,     // from /cli-agent/zellij/launch
    private val webClientId: String = UUID.randomUUID().toString(),
) {
    private val client = OkHttpClient.Builder()
        .cookieJar(/* preload session_token cookie via POST /command/login */)
        .build()
    private lateinit var wsTerminal: WebSocket
    private var wsControl: WebSocket? = null

    fun connect(onBytes: (ByteArray) -> Unit, onSwitchedSession: (String) -> Unit) {
        val termUrl = "$wsBaseUrl/ws/terminal/$sessionName?web_client_id=$webClientId"
        wsTerminal = client.newWebSocket(Request.Builder().url(termUrl).build(),
            object : WebSocketListener() {
                override fun onMessage(ws: WebSocket, bytes: ByteString) {
                    if (wsControl == null) openControl()
                    onBytes(bytes.toByteArray())
                }
                override fun onMessage(ws: WebSocket, text: String) {
                    if (wsControl == null) openControl()
                    onBytes(text.toByteArray(Charsets.UTF_8))
                }
                override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                    if (code == 4001) onDisconnected() else scheduleReconnect()
                }
            })
    }

    fun sendBytes(b: ByteArray) {
        wsTerminal.send(b.toByteString(0, b.size))
    }

    fun sendResize(cols: Int, rows: Int) {
        val msg = """{"web_client_id":"$webClientId","payload":{"type":"TerminalResize","rows":$rows,"cols":$cols}}"""
        wsControl?.send(msg)
    }

    fun close() {
        wsTerminal.close(1000, "client closing")
        wsControl?.close(1000, "client closing")
    }

    private fun openControl() { /* same pattern with /ws/control URL */ }
}
```

**Estimated implementation: 1 day (was 2-3 in original plan).** Protocol
is so simple it's mostly okhttp boilerplate + JSON envelope generation.
Bulk of T18's time is on tests + Termux integration verification, not
protocol implementation.

## Plan timeline update

T18 estimate revised: **2-3 days → 1 day**. Total Phase 4 timeline:
**10-15 days → 8-12 days**. The protocol simplicity is a real win;
remaining risk concentration moves to T20 (switcher dropdown UX
polish) and T23 (device QA edge cases on Z Fold 6).

## Version probe (defensive, T18 requirement)

Endpoint: `GET http://host:9097/info/version` (referenced by zellij
client's `checkConnection()`). Use this on client startup to confirm
remote zellij is 0.44.3; WARN + degrade if version mismatch since the
protocol could shift between minor releases.
