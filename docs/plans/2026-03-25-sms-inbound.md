# Inbound SMS System Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a complete inbound SMS system — receive texts via TG200 AMI, route to the correct operator based on contact whitelist, process through AI, reply, and provide a full Portal inbox UI for viewing/sending messages per operator.

**Architecture:** TG200 delivers inbound SMS as **AMI `ReceivedSMS` events** over a persistent TCP connection (port 5038). No polling needed — events are pushed in real-time. Incoming messages are matched against the contact book to find the operator. Messages from unknown numbers are ignored. Matched messages are processed through `sms_processor.py` (Claude with tools), replies sent via AMI `SMSCommand`. All messages (inbound + outbound) are stored in SQLite. Portal gets an SMS Inbox module in the system menu with conversation view, send capability, and operator scoping.

**Tech Stack:** TG200 AMI (TCP 5038), Python asyncio, existing `sms_processor.py` + `contacts.py`, FastAPI endpoints, Portal JS module + CSS.

---

## TG200 Connection Details (Validated 2026-03-25)

| Property | Value |
|----------|-------|
| **TG200 IP** | `192.168.1.200` |
| **BlackBox IP** | `192.168.1.164` |
| **AMI Port** | `5038` |
| **AMI Username** | `blackbox` |
| **AMI Password** | `<REDACTED-SECRET>` |
| **API Permitted IP** | `192.168.1.164` (BlackBox only) |
| **TG200 Phone Number** | `+14103497272` |
| **Active GSM Span** | `2` (Quectel EC21, AT&T, signal 21/31) |
| **Span 3** | Down (no SIM / not registered) |
| **Web GUI** | `http://192.168.1.200` (Boa/0.94.14rc21, admin login, NOT used for SMS API) |
| **Web GUI Auth** | Cookie-based with obfuscated base64 (charcode+2 shift) — NOT needed for SMS |
| **SMS Center** | `+13123149810` (AT&T) |
| **Modem** | Quectel EC21 (EC21AFAR05A06M4G) |
| **IMEI** | `864395065175647` |

### AMI Protocol — Validated Commands

**Authentication:**
```
Action: Login
Username: blackbox
Secret: <REDACTED-SECRET>
Events: on
```
Response: `Response: Success\r\nMessage: Authentication accepted`

**Send SMS (outbound):**
```
Action: SMSCommand
Command: gsm send sms 2 {destination_number} "{message_text}"
ActionID: {unique_id}
```
Response: `Response: Follows\r\nPrivilege: SMSCommand\r\nActionID: {id}\r\n--END COMMAND--`

**Receive SMS (inbound event — pushed automatically):**
```
Event: ReceivedSMS
Privilege: all,smscommand
ID:
GsmSpan: 2
Sender: +14108166914
Recvtime: 2026-03-25 17:20:48
Index: 0
Total: 1
Smsc: +19703769769
Content: Got+ir
--END SMS EVENT--
```
- **Content is URL-encoded** (spaces as `+`, special chars as `%XX`)
- **Index/Total** supports multi-part SMS (Index 0-based, Total = number of parts)
- **Delimiter**: `--END SMS EVENT--` marks end of event

**Show GSM spans:**
```
Action: SMSCommand
Command: gsm show spans
ActionID: {id}
```
Returns: `GSM span 2: Power on, Up, Active, Standard`

**Show span detail:**
```
Action: SMSCommand
Command: gsm show span 2
ActionID: {id}
```
Returns: Network Name, Signal Quality, SIM IMSI, SMS Center Number, State, etc.

### What Does NOT Work on TG200

- `/api/v1.0/*` REST endpoints — **do not exist** (404, Boa server has no REST API)
- `/cgi/sPMS` — 404 even after enabling API (may need reboot, but AMI is better anyway)
- `gsm show sms inbox` AMI command — **not supported**
- `gsm show sms` AMI command — **not supported**
- HTTP Basic Auth to any endpoint — TG200 uses cookie-based web GUI auth (obfuscated)

---

## Existing Infrastructure (from research)

| Component | Status | Notes |
|-----------|--------|-------|
| SMS sending via AMI | **Working** | `gsm send sms 2 {number} "{msg}"` — validated, message received |
| SMS receiving via AMI | **Working** | `ReceivedSMS` event — validated, event captured |
| SMS processing (AI) | **Working** | `sms_processor.py` — Claude with tools, returns reply text |
| Contact book | **Working** | `contacts.py` — per-operator, has Brandon (+14108166914) and Anna (4108165293) |
| SMS sending via HTTP | **BROKEN** | `gateway_manager.py:send_sms_via_gateway()` uses non-existent `/api/v1.0/sms/send` — must replace with AMI |
| Portal SMS UI | **NOT IMPLEMENTED** | No inbox, no send UI |
| Operator phone matching | **NOT IMPLEMENTED** | Need to match incoming number → contact → operator |

## Key Design Decisions

### AMI vs HTTP API
- **AMI is the only working programmatic interface** for SMS on the TG200
- The TG200's `/api/*` REST endpoints don't exist — the web GUI uses `/cgi/WebCGI` with obfuscated cookie auth
- AMI gives us real-time event push for inbound SMS — no polling delay
- AMI also provides GSM span status, signal quality, etc.

### Phone Number Matching
- Incoming SMS has a `Sender` field in E.164 format (e.g., `+14108166914`)
- Search ALL operator contact books for a matching phone number
- If found → route to that operator
- If not found → ignore (whitelist enforcement)
- Phone number normalization: strip +1 prefix, compare last 10 digits

### Message Storage
- New file: `Orchestrator/sms/message_store.py`
- SQLite database: `Manifest/sms_messages.db`
- Table: `messages(id, operator, direction, phone_number, contact_name, body, ai_response, timestamp, status, read)`
- Per-operator query support for inbox

### Multi-Part SMS
- TG200 sends `Index` and `Total` fields in `ReceivedSMS` events
- When `Total > 1`, buffer parts until all arrive, then concatenate
- Use `ID` field (if populated) or `Sender + Recvtime` as grouping key

---

## Task 1: AMI SMS Client

**Files:**
- Create: `Orchestrator/sms/__init__.py`
- Create: `Orchestrator/sms/ami_client.py`

Persistent TCP connection to TG200 AMI for sending and receiving SMS.

**Key class: `AMISMSClient`**
```python
class AMISMSClient:
    """Persistent AMI connection for SMS send/receive via TG200."""

    def __init__(self, host="192.168.1.200", port=5038,
                 username="blackbox", secret="<REDACTED-SECRET>"):
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret
        self._reader = None
        self._writer = None
        self._connected = False
        self._on_sms_callback = None
        self._action_counter = 0
        self._pending_responses = {}  # ActionID -> Future
        self._multipart_buffer = {}   # key -> {parts: {}, total: int, timer: handle}

    async def connect(self):
        """Connect to AMI and authenticate."""
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        banner = await self._reader.readline()  # "Asterisk Call Manager/1.1"
        await self._send_action("Login", Username=self.username, Secret=self.secret, Events="on")
        # Start event read loop
        asyncio.create_task(self._read_loop())

    async def send_sms(self, destination: str, message: str, span: int = 2) -> dict:
        """Send SMS via AMI SMSCommand."""
        # Escape quotes in message
        safe_msg = message.replace('"', '\\"')
        action_id = f"sms-{self._action_counter}"
        self._action_counter += 1
        resp = await self._send_action(
            "SMSCommand",
            Command=f'gsm send sms {span} {destination} "{safe_msg}"',
            ActionID=action_id
        )
        return {"success": "Error" not in resp.get("Response", ""), "raw": resp}

    async def get_span_status(self, span: int = 2) -> dict:
        """Get GSM span status."""
        resp = await self._send_action("SMSCommand", Command=f"gsm show span {span}")
        return self._parse_span_status(resp)

    def on_sms(self, callback):
        """Register callback for incoming SMS: callback(sender, body, span, recvtime)"""
        self._on_sms_callback = callback

    async def _read_loop(self):
        """Read AMI events/responses continuously."""
        # Parse AMI protocol: key: value lines, blank line separates messages
        # Watch for "Event: ReceivedSMS" and "--END SMS EVENT--"
        # URL-decode Content field (+ → space, %XX → char)
        # Handle multi-part: buffer by sender+recvtime, fire callback when complete
        ...

    async def _send_action(self, action: str, **params) -> dict:
        """Send AMI action and wait for response."""
        lines = [f"Action: {action}"]
        for k, v in params.items():
            lines.append(f"{k}: {v}")
        lines.append("")  # blank line terminates
        self._writer.write("\r\n".join(lines).encode() + b"\r\n")
        await self._writer.drain()
        # Wait for response matching ActionID
        ...

    async def disconnect(self):
        """Logoff and close connection."""
        await self._send_action("Logoff")
        self._writer.close()
```

**Implementation notes:**
- AMI protocol: line-based, `\r\n` terminated, blank line separates messages
- Must handle connection drops with auto-reconnect (exponential backoff)
- Content URL-decoding: `urllib.parse.unquote_plus(content)`
- Multi-part SMS: buffer parts by (Sender, Recvtime), flush after all `Total` parts arrive or 30s timeout
- Keep-alive: send periodic `Action: Ping` every 30s to detect dead connections

---

## Task 2: Message Store (SQLite)

**Files:**
- Create: `Orchestrator/sms/message_store.py`

SQLite database for storing all SMS messages.

```sql
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operator TEXT NOT NULL,
    direction TEXT NOT NULL,  -- 'inbound' or 'outbound'
    phone_number TEXT NOT NULL,
    contact_name TEXT DEFAULT '',
    body TEXT NOT NULL,
    ai_response TEXT DEFAULT '',
    timestamp TEXT NOT NULL,  -- ISO 8601
    status TEXT DEFAULT 'delivered',  -- delivered, failed, pending
    read INTEGER DEFAULT 0  -- 0=unread, 1=read
);
CREATE INDEX idx_operator ON messages(operator);
CREATE INDEX idx_phone ON messages(phone_number);
CREATE INDEX idx_timestamp ON messages(timestamp);
```

**Key functions:**
- `store_message(operator, direction, phone, contact_name, body, ai_response, timestamp) → id`
- `get_messages(operator, limit=50, offset=0) → list`
- `get_conversation(operator, phone_number, limit=50) → list` (thread view)
- `get_unread_count(operator) → int`
- `mark_read(message_id)` / `mark_all_read(operator, phone_number)`
- `get_recent_threads(operator) → list` (unique phone numbers with last message + unread count)

---

## Task 3: Inbound SMS Router

**Files:**
- Create: `Orchestrator/sms/router.py`

Routes incoming SMS to the correct operator based on contact book matching.

**Flow:**
1. AMI client fires `on_sms(sender, body, span, recvtime)` callback
2. Normalize phone number (strip +1, compare last 10 digits)
3. Search ALL operator contact books for matching phone
4. If found: route to that operator
5. If not found: log and ignore (whitelist)
6. Store inbound message in message store
7. Process through `sms_processor.py` with matched operator
8. Send AI reply via AMI client (`send_sms`)
9. Store outbound reply in message store

**Key class: `SMSRouter`**
```python
class SMSRouter:
    def __init__(self, ami_client: AMISMSClient, message_store: MessageStore):
        self.ami = ami_client
        self.store = message_store
        # Register as AMI callback
        self.ami.on_sms(self.handle_incoming)

    async def handle_incoming(self, sender: str, body: str, span: int, recvtime: str):
        # 1. Normalize sender
        normalized = self._normalize_phone(sender)

        # 2. Find operator by contact phone match
        operator, contact = self._find_operator_by_phone(normalized)
        if not operator:
            print(f"[SMS] Ignoring message from unknown number: {sender}")
            return

        contact_name = contact.get("name", sender) if contact else sender

        # 3. Store inbound message
        self.store.store_message(
            operator, "inbound", sender, contact_name, body, "", recvtime
        )

        # 4. Process through AI
        from Orchestrator.phone.sms_processor import process_incoming_sms
        reply = await process_incoming_sms(sender, body, operator)

        # 5. Send reply via AMI
        if reply:
            result = await self.ami.send_sms(sender, reply, span)
            status = "delivered" if result["success"] else "failed"
            self.store.store_message(
                operator, "outbound", sender, contact_name, reply, "",
                datetime.utcnow().isoformat(), status=status
            )

    def _find_operator_by_phone(self, phone: str) -> tuple:
        """Search all operator contact books for this phone number."""
        from Orchestrator.contacts import load_contacts
        from Orchestrator.config import USERS_LIST
        for operator in USERS_LIST:
            contacts = load_contacts(operator)
            for contact in contacts:
                if self._phones_match(phone, contact.get("phone", "")):
                    return operator, contact
        return None, None

    def _normalize_phone(self, phone: str) -> str:
        """Strip to last 10 digits for comparison."""
        digits = ''.join(c for c in phone if c.isdigit())
        return digits[-10:] if len(digits) >= 10 else digits

    def _phones_match(self, a: str, b: str) -> bool:
        return self._normalize_phone(a) == self._normalize_phone(b)
```

---

## Task 4: SMS API Endpoints

**Files:**
- Create: `Orchestrator/routes/sms_routes.py`
- Modify: `Orchestrator/app.py` (register router)

REST endpoints for the Portal UI:

```
GET  /sms/threads?operator={op}                          — List conversation threads for operator
GET  /sms/messages?operator={op}&phone={phone}&limit=50  — Get messages in a thread
POST /sms/send                                            — Send SMS (operator, to, message)
POST /sms/mark-read                                       — Mark messages as read
GET  /sms/unread?operator={op}                            — Get unread count
GET  /sms/status                                          — Get AMI connection + GSM span status
```

**POST /sms/send body:**
```json
{
    "operator": "Brandon",
    "to": "+14108166914",
    "message": "Hello from the Portal!"
}
```
This stores the outbound message in the message store AND sends via AMI.

---

## Task 5: Portal SMS Inbox Module

**Files:**
- Create: `Portal/modules/sms-inbox.js`
- Create: `Portal/styles/features/_sms.css`
- Modify: `Portal/index.html` (add SMS button to system menu, add modal)
- Modify: `Portal/app-init.js` (import and init module)

**UI Design:**

```
┌─── SMS Inbox (Brandon) ────────────────────────────────┐
│                                                         │
│  ┌─ Threads ──────────┐  ┌─ Conversation ────────────┐ │
│  │                     │  │                           │ │
│  │  Brandon            │  │  ← Brandon +14108166914   │ │
│  │  +14108166914       │  │                           │ │
│  │  "Hey, checking in" │  │  [12:30] Hey, checking in │ │
│  │  2m ago         (1) │  │                           │ │
│  │                     │  │  [12:30] Hi Brandon! How  │ │
│  │  Anna               │  │  can I help you today?    │ │
│  │  +14108165293       │  │                           │ │
│  │  "Thanks!"          │  │  [12:35] What's the       │ │
│  │  1h ago             │  │  weather like?            │ │
│  │                     │  │                           │ │
│  │  + New Message      │  │  [12:35] Let me check...  │ │
│  │                     │  │                           │ │
│  └─────────────────────┘  │  ┌─────────────────────┐  │ │
│                           │  │ Type a message...    │  │ │
│                           │  │              [Send]  │  │ │
│                           │  └─────────────────────┘  │ │
│                           └───────────────────────────┘ │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**Features:**
- Thread list on left (grouped by phone number, sorted by most recent)
- Conversation view on right (messages in chronological order)
- Inbound messages left-aligned (bubble style), outbound right-aligned
- Unread count badge on threads
- "New Message" button → phone number input + message compose
- Operator selector (uses `getOperator()` from state-management.js)
- Auto-refresh every 10 seconds for new messages
- Mark as read when conversation is opened
- Toast notifications: use `import { toast, toastSuccess, toastError } from './core-utils.js'`
- Operator: use `getOperator()` from state-management.js, NOT `window.currentOperator`

---

## Task 6: Update Gateway Manager (Replace Broken HTTP with AMI)

**Files:**
- Modify: `Orchestrator/asterisk/gateway_manager.py` (replace `send_sms_via_gateway`)
- Modify: `Orchestrator/tools/blackbox_tools.py` (`_execute_send_sms` to use AMI client)

Replace the non-functional HTTP-based `send_sms_via_gateway()` with a call through the AMI client:

```python
# Old (BROKEN — /api/v1.0/sms/send doesn't exist):
# url = f"http://{gateway['ip']}:{gateway.get('http_port', 80)}/api/v1.0/sms/send"

# New (uses shared AMI client):
from Orchestrator.sms import get_ami_client
ami = get_ami_client()
result = await ami.send_sms(destination, message, span=2)
```

Also update `_execute_send_sms` in `blackbox_tools.py` to store outbound messages in the message store.

---

## Task 7: Wire Into Startup

**Files:**
- Modify: `Orchestrator/startup.py`
- Modify: `Orchestrator/sms/__init__.py` (module-level singleton)

Start the AMI client and SMS router at startup:

```python
# In startup.py, after Asterisk init:
from Orchestrator.sms import start_sms_system
await start_sms_system()
```

```python
# In sms/__init__.py:
_ami_client = None
_sms_router = None
_message_store = None

async def start_sms_system():
    global _ami_client, _sms_router, _message_store
    from .ami_client import AMISMSClient
    from .message_store import MessageStore
    from .router import SMSRouter

    _message_store = MessageStore()
    _ami_client = AMISMSClient()
    await _ami_client.connect()
    _sms_router = SMSRouter(_ami_client, _message_store)
    print("[SMS] System started — AMI connected, listening for incoming SMS")

def get_ami_client(): return _ami_client
def get_message_store(): return _message_store
def get_router(): return _sms_router
```

---

## Task 8: System Menu Integration

**Files:**
- Modify: `Portal/index.html` (add SMS Inbox button to system menu)

Add alongside the existing Telephony button:
```html
<button class="system-menu-btn" onclick="openSMSInbox()">
    <span class="menu-icon">💬</span>
    SMS Inbox
    <span id="sms-unread-badge" class="badge hide">0</span>
</button>
```

Poll for unread count every 30 seconds to update the badge.

---

## Task 9: End-to-End Test

1. Start BlackBox service — verify `[SMS] System started — AMI connected` in logs
2. Send SMS from cell phone (+14108166914) to TG200 (+14103497272)
3. Check logs: `ReceivedSMS` event received, operator matched to "Brandon", AI processed
4. Check phone: AI reply received back
5. Open Portal → System Menu → SMS Inbox
6. Verify conversation appears under correct operator with both inbound + outbound
7. Send reply from Portal inbox UI
8. Verify reply received on cell phone
9. Test unknown number: send from unregistered number → verify ignored
10. Test multi-part: send long SMS (>160 chars) → verify reassembled correctly

---

## Dependency Graph

```
Task 1 (AMI client) ─────────────────┐
Task 2 (Message store) ──────────────┤
Task 3 (SMS router) ── deps 1,2 ─────┤
Task 4 (API endpoints) ── deps 2,3 ──┤
Task 5 (Portal inbox) ── deps 4 ─────┤── Task 9 (E2E Test)
Task 6 (Replace HTTP) ── deps 1 ─────┤
Task 7 (Startup wiring) ── deps 1,3 ─┤
Task 8 (System menu) ── deps 5 ──────┘
```

**Parallelizable:** Tasks 1, 2 (no deps). Tasks 4, 5 (after 2, 3). Task 6 (after 1).
