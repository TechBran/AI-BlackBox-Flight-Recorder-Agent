# SMS System Overhaul — Main Pipeline, Per-Operator Model, Contact Book UI

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Route SMS through the main chat pipeline (with semantic search, snapshots, and checkpoints), add per-operator model/provider selection for SMS, build a Contact Book UI for both Android native and Portal, and integrate the native Composer for SMS replies on Android.

**Architecture:** Inbound SMS currently uses a lightweight `sms_processor.py` with hardcoded Claude Sonnet and no snapshot context. This overhaul replaces that with the real chat pipeline (`POST /chat`), using the operator's preferred SMS model+provider stored in operator preferences. The Android SMS screen drops its custom compose bar and uses the native Composer (with Whisper, provider selector, attachments). A new Contact Book screen is added to both Android and Portal for per-operator CRUD.

**Tech Stack:** Python (FastAPI, SQLite, asyncio), Kotlin (Jetpack Compose, ViewModel, StateFlow), JavaScript (ES modules), existing BlackBoxApi + SSE infrastructure.

---

## Connection Details (from prior validated work)

| Property | Value |
|----------|-------|
| TG200 AMI | `192.168.1.200:5038`, user `blackbox`, pass `<REDACTED-SECRET>` |
| Active GSM Span | `2` (Quectel EC21, AT&T) |
| TG200 Phone | `+14103497272` |
| Operator Prefs | `Manifest/operator_preferences.json` via `state.py` get/set_operator_preference |
| Contacts | `Contacts/contacts.json` via `contacts.py` load/save/upsert/search |
| SMS DB | `Manifest/sms_messages.db` via `sms/message_store.py` |
| Chat Endpoint | `POST /chat` → task → `process_chat_task()` in `tasks.py` |
| SMS Character Limit | 160/segment, ~1500 max for multi-part (10 segments) |

---

## Task 1: Per-Operator SMS Model/Provider Preferences (Backend)

**Files:**
- Modify: `Orchestrator/state.py` (no changes needed — `get/set_operator_preference` already works)
- Create: `Orchestrator/routes/sms_routes.py` — add preferences endpoints
- Modify: `Orchestrator/sms/router.py` — read preferences when processing inbound

**New Preference Keys:**
```json
{
  "Brandon": {
    "tts_voice": "openai:onyx",
    "sms_provider": "anthropic",
    "sms_model": "claude-opus-4-6-20250918"
  }
}
```

**New API Endpoints (add to existing `sms_routes.py`):**

```
GET  /sms/preferences?operator={op}    — Get SMS model/provider prefs
POST /sms/preferences                   — Set SMS model/provider prefs
     Body: {"operator": str, "sms_provider": str, "sms_model": str}
```

**Implementation:**
```python
@router.get("/preferences")
async def get_sms_preferences(operator: str = Query(...)):
    from Orchestrator.state import get_operator_preference
    return {
        "operator": operator,
        "sms_provider": get_operator_preference(operator, "sms_provider", "anthropic"),
        "sms_model": get_operator_preference(operator, "sms_model", "claude-opus-4-6-20250918"),
    }

@router.post("/preferences")
async def set_sms_preferences(req: SMSPreferencesRequest):
    from Orchestrator.state import set_operator_preference
    set_operator_preference(req.operator, "sms_provider", req.sms_provider)
    set_operator_preference(req.operator, "sms_model", req.sms_model)
    return {"success": True}
```

---

## Task 2: Route Inbound SMS Through Main Chat Pipeline

**Files:**
- Modify: `Orchestrator/sms/router.py` — replace `process_incoming_sms` call with `/chat` pipeline
- Modify: `Orchestrator/phone/sms_processor.py` — keep as fallback but not primary path
- Reference: `Orchestrator/routes/chat_routes.py` (POST /chat endpoint, line ~6398)
- Reference: `Orchestrator/tasks.py` (process_chat_task, line ~1094)

**Key Change:** Instead of calling `process_incoming_sms()` directly (which uses hardcoded Sonnet with no context), post to the internal `/chat` endpoint — this gives us:
- Semantic search against 4800+ snapshots
- Checkpoint creation
- Operator context injection
- Per-operator model/provider selection
- Full tool suite

**SMS System Prompt Addition (inject into operator context):**
```
You are responding to an SMS text message from {contact_name} ({sender_phone}).
CRITICAL SMS RULES:
- Keep each response under 160 characters if possible (1 SMS segment)
- Maximum 1500 characters total (multi-part SMS limit)
- NO markdown, NO formatting — plain text only
- Be concise and conversational
- If you need to send a long response, the system will split it into multiple SMS messages
- To reply, simply respond with text. The system will send it as SMS automatically.
- You have access to tools (send_sms, search_contacts, etc.) if needed for the request.
```

**Router change in `handle_incoming`:**
```python
async def handle_incoming(self, sender, body, span, recvtime):
    # ... existing operator matching ...

    # Get operator's preferred SMS model/provider
    from Orchestrator.state import get_operator_preference
    sms_provider = get_operator_preference(operator, "sms_provider", "anthropic")
    sms_model = get_operator_preference(operator, "sms_model", "claude-opus-4-6-20250918")

    # Build SMS context prefix
    sms_context = f"[SMS from {contact_name} ({sender})]: {body}"

    # Route through main chat pipeline via internal HTTP
    import aiohttp
    async with aiohttp.ClientSession() as session:
        payload = {
            "messages": [{"role": "user", "content": sms_context}],
            "operator": operator,
            "provider": sms_provider,
            "model": sms_model,
            "sms_mode": True,  # Flag for SMS-specific system prompt
            "sms_sender": sender,
            "sms_contact_name": contact_name,
        }
        async with session.post("http://localhost:9091/chat", json=payload) as resp:
            result = await resp.json()
            task_id = result.get("task_id")

    # Poll for task completion (the chat pipeline is async)
    reply = await self._poll_chat_task(task_id)

    # Send reply as SMS (split into segments if needed)
    if reply:
        segments = self._split_sms(reply, max_len=160)
        for segment in segments:
            result = await self.ami.send_sms(sender, segment, span=int(span))
            self.store.store_message(...)
```

**SMS segment splitting:**
```python
def _split_sms(self, text: str, max_len: int = 160) -> list:
    """Split long text into SMS-sized segments at word boundaries."""
    if len(text) <= max_len:
        return [text]
    segments = []
    while text:
        if len(text) <= max_len:
            segments.append(text)
            break
        # Find last space within limit
        split_at = text.rfind(' ', 0, max_len)
        if split_at == -1:
            split_at = max_len
        segments.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    return segments[:10]  # Max 10 segments
```

**Task polling helper:**
```python
async def _poll_chat_task(self, task_id: str, timeout: float = 30) -> str:
    """Poll a /chat task until completion, return assistant response."""
    import aiohttp
    deadline = time.time() + timeout
    while time.time() < deadline:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://localhost:9091/tasks/{task_id}") as resp:
                data = await resp.json()
                if data.get("status") in ("completed", "failed"):
                    return data.get("result", {}).get("response", "")
        await asyncio.sleep(1)
    return ""
```

**SMS mode in chat pipeline** — small addition to `tasks.py` `process_chat_task`:
- Check for `sms_mode` flag in task data
- If present, prepend SMS-specific system prompt instructions
- Cap response at 1500 characters

---

## Task 3: SMS System Prompt Integration in Chat Pipeline

**Files:**
- Modify: `Orchestrator/tasks.py` — add `sms_mode` handling in `process_chat_task`

**Where to inject (inside process_chat_task, after context retrieval):**
```python
# Check if this is an SMS-originated message
sms_mode = chat_input.get("sms_mode", False)
sms_sender = chat_input.get("sms_sender", "")
sms_contact_name = chat_input.get("sms_contact_name", "")

if sms_mode:
    sms_prompt = f"""
IMPORTANT: You are responding to an SMS text message from {sms_contact_name} ({sms_sender}).
SMS RULES:
- Keep responses under 160 characters when possible (1 SMS segment)
- Maximum 1500 characters (will be split into multiple messages)
- Plain text only — NO markdown, NO formatting, NO emojis unless natural
- Be concise and conversational, like a real text message
- You can send multiple messages if needed — the system handles splitting
- To send SMS to someone else, use the send_sms tool
"""
    # Prepend to system prompt
    system_prompt = sms_prompt + "\n\n" + system_prompt
```

---

## Task 4: Fix AMI Send Timeout Bug

**Files:**
- Modify: `Orchestrator/sms/ami_client.py`

**Root Cause:** After sending an SMS via AMI `SMSCommand`, the TG200 fires `UpdateSMSSend` events. These events use `--END SMS EVENT--` terminators. If the read loop reads a response for a DIFFERENT action (like a pending Ping), it may consume the send response's lines, causing the actual send Future to never resolve.

**Fix:** The `_send_action` response matching needs to handle the case where the AMI interleaves events between the action request and its response. The current read loop already handles this — but there may be a race condition where the `_read_message` timeout (60s in the read loop) doesn't match the `_send_action` timeout (15s).

**Approach:**
1. Increase `_send_action` timeout from 15s to 30s for SMSCommand specifically
2. In `send_sms`, add a retry with fresh ActionID if first attempt times out
3. Ensure `UpdateSMSSend` events don't interfere with response matching (they don't have ActionID, so they shouldn't — but verify the parser handles them cleanly)

**Alternatively — fire-and-forget send:**
Since we know from raw testing that `gsm send sms` works (we validated it), and the response is just `Response: Follows` with no useful data, we can make `send_sms` fire-and-forget:
```python
async def send_sms(self, destination, message, span=2):
    # Send the action but don't wait for response
    safe_msg = message.replace('"', '\\"')
    command = f'gsm send sms {span} {destination} "{safe_msg}"'
    try:
        await self._send_raw(
            f"Action: SMSCommand\r\nCommand: {command}\r\n\r\n"
        )
        return {"success": True, "error": None}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
```
This avoids the ActionID matching entirely. The `UpdateSMSSend` events will be consumed by the read loop as regular events.

---

## Task 5: Contacts REST API Endpoints

**Files:**
- Create: `Orchestrator/routes/contacts_routes.py`
- Modify: `Orchestrator/app.py` — register router

**Endpoints:**
```
GET    /contacts?operator={op}                    — List all contacts for operator
GET    /contacts/search?operator={op}&query={q}   — Search contacts
POST   /contacts                                   — Create/update contact
DELETE /contacts/{contact_id}?operator={op}        — Delete contact
```

**Implementation:**
```python
router = APIRouter(prefix="/contacts", tags=["contacts"])

@router.get("")
async def list_contacts(operator: str = Query(...)):
    from Orchestrator.contacts import load_contacts, ensure_operator_book
    data = load_contacts()
    ensure_operator_book(data, operator)
    contacts = list(data.get(operator, {}).values())
    contacts.sort(key=lambda c: c.get("name", "").lower())
    return {"contacts": contacts, "operator": operator}

@router.get("/search")
async def search(operator: str = Query(...), query: str = Query(...)):
    from Orchestrator.contacts import search_contacts
    results = search_contacts(query, operator)
    return {"results": results, "operator": operator, "query": query}

@router.post("")
async def upsert(req: ContactUpsertRequest):
    from Orchestrator.contacts import upsert_contact
    contact = upsert_contact(
        name=req.name, notes=req.notes or "", tags=req.tags or [],
        operator=req.operator, created_by=req.operator,
        phone=req.phone, email=req.email, relationship=req.relationship
    )
    return {"success": True, "contact": contact}

@router.delete("/{contact_id}")
async def delete_contact(contact_id: str, operator: str = Query(...)):
    from Orchestrator.contacts import load_contacts, save_contacts
    data = load_contacts()
    book = data.get(operator, {})
    if contact_id in book:
        del book[contact_id]
        save_contacts(data)
        return {"success": True}
    raise HTTPException(404, "Contact not found")
```

---

## Task 6: Portal Contact Book Module (WebView)

**Files:**
- Create: `Portal/modules/contacts-manager.js`
- Create: `Portal/styles/features/_contacts.css`
- Modify: `Portal/index.html` — add Contacts button + modal HTML
- Modify: `Portal/styles/main.css` — import `_contacts.css`
- Modify: `Portal/modules/app-init.js` — import + init

**UI Design:**
```
┌─── Contacts (Brandon) ────────────────────────────────┐
│ [Search...]                            [+ Add Contact] │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │ ★ Brandon                        +14108166914    │   │
│  │   Owner | brandon@email.com                      │   │
│  │   Tags: owner, system         [Edit] [Delete]    │   │
│  ├─────────────────────────────────────────────────┤   │
│  │   Anna                           +14108165293    │   │
│  │   Sister | anna@email.com                        │   │
│  │   Tags: family                [Edit] [Delete]    │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
│  ┌── Add/Edit Contact ──────────────────────────────┐  │
│  │ Name:    [________________]                       │  │
│  │ Phone:   [________________]                       │  │
│  │ Email:   [________________]                       │  │
│  │ Relation:[________________]                       │  │
│  │ Notes:   [________________]                       │  │
│  │ Tags:    [________________]                       │  │
│  │                              [Cancel]  [Save]     │  │
│  └──────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

**Features:**
- Per-operator scoped (uses `getOperator()`)
- Search bar with live filtering
- Add/Edit form (inline, not separate page)
- Delete with confirmation
- Fields: name, phone, email, relationship, notes, tags (comma-separated)
- Follow the same patterns as `telephony-manager.js` and `cron-manager.js`
- Import: `toast`, `toastSuccess`, `toastError` from `core-utils.js`

---

## Task 7: Android Contact Book Screen (Native)

**Files:**
- Create: `ui/contacts/ContactsViewModel.kt`
- Create: `ui/contacts/ContactsScreen.kt`
- Modify: `navigation/NavGraph.kt` — add route
- Modify: `ui/settings/SettingsSheet.kt` — add menu button

**Follow exact same pattern as CronManagerScreen:** ViewModel with StateFlow, GlassCard list, search bar, add/edit dialog.

**Features:**
- Thread list with GlassCard per contact
- Search bar at top
- FAB or button for "Add Contact"
- Edit dialog (AlertDialog or BottomSheet) with name, phone, email, relationship, notes, tags
- Delete with confirmation dialog
- Per-operator scoped (operator passed from NavGraph)

---

## Task 8: Android SMS Screen — Use Native Composer

**Files:**
- Modify: `ui/sms/SmsInboxScreen.kt` — remove custom compose bar from SmsConversationView, add flag for parent to show Composer
- Modify: `NativeMainActivity.kt` — detect when SMS conversation is active, route Composer sends to SMS
- Modify: `ui/sms/SmsViewModel.kt` — add provider/model preference loading and saving

**Key Changes:**

**A. Remove custom compose bar from SmsConversationView:**
The current `SmsConversationView` has an `OutlinedTextField` + send button at the bottom. Remove it. Instead, expose a state flag that tells `NativeMainActivity` to route Composer input to SMS.

**B. SmsViewModel additions:**
```kotlin
// SMS-specific provider/model
private val _smsProvider = MutableStateFlow("anthropic")
val smsProvider: StateFlow<String> = _smsProvider.asStateFlow()

private val _smsModel = MutableStateFlow("claude-opus-4-6-20250918")
val smsModel: StateFlow<String> = _smsModel.asStateFlow()

fun loadPreferences() {
    api?.let { api ->
        viewModelScope.launch {
            val resp = api.get("/sms/preferences?operator=$operator")
            val obj = json.parseToJsonElement(resp).jsonObject
            _smsProvider.value = obj["sms_provider"]?.jsonPrimitive?.content ?: "anthropic"
            _smsModel.value = obj["sms_model"]?.jsonPrimitive?.content ?: "claude-opus-4-6-20250918"
        }
    }
}

fun savePreferences(provider: String, model: String) {
    _smsProvider.value = provider
    _smsModel.value = model
    api?.let { api ->
        viewModelScope.launch {
            val body = """{"operator":"$operator","sms_provider":"$provider","sms_model":"$model"}"""
            api.post("/sms/preferences", body)
        }
    }
}
```

**C. NativeMainActivity routing:**
When `SmsViewModel.selectedPhone != null`, the Composer's `onSend` callback should call `smsViewModel.sendSms(inputText)` instead of `chatViewModel.sendMessage()`. This enables Whisper transcription → SMS send.

**D. Provider/Model selector in SMS area:**
Add a small provider+model dropdown in the SMS conversation header (or as a settings row at the top of the SMS thread list). This calls `savePreferences()` and persists to the backend.

---

## Task 9: Portal SMS Inbox — Model/Provider Selector

**Files:**
- Modify: `Portal/modules/sms-inbox.js` — add provider/model selector UI
- Add API calls to `GET/POST /sms/preferences`

**UI Addition (in the thread list header area):**
```html
<div class="sms-prefs-bar">
    <select id="smsProvider">...</select>
    <select id="smsModel">...</select>
</div>
```

Load available models from existing `/chat/models?provider={p}` endpoint (same as main chat). Persist selection via `POST /sms/preferences`.

---

## Task 10: End-to-End Testing

**Test Matrix:**

| Test | Description |
|------|-------------|
| **Inbound → AI Reply** | Text TG200, verify AI processes through main pipeline with snapshots/context, reply arrives on phone |
| **Model Selection** | Change operator's SMS model to Gemini, text again, verify Gemini responds |
| **Multi-segment Reply** | Ask a question that requires a long answer, verify it splits into multiple SMS |
| **Portal Send** | Send SMS from Portal inbox, verify received on phone |
| **Android Send** | Send SMS from Android native Composer, verify received on phone |
| **Whisper → SMS** | Use Whisper on Android to dictate an SMS reply |
| **Contact CRUD** | Add, edit, delete contacts from both Portal and Android |
| **Unknown Number** | Text from unregistered number, verify ignored (whitelist) |
| **Operator Routing** | Add Anna's contact, text from her number, verify routed to Anna's model prefs |

---

## Dependency Graph

```
Task 1 (SMS preferences API) ──────────────────┐
Task 2 (Route SMS through /chat) ── dep 1 ─────┤
Task 3 (SMS system prompt in tasks.py) ── dep 2┤
Task 4 (Fix AMI send timeout) ─────────────────┤
Task 5 (Contacts REST API) ────────────────────┤
Task 6 (Portal Contact Book) ── dep 5 ─────────┤── Task 10 (E2E Test)
Task 7 (Android Contact Book) ── dep 5 ────────┤
Task 8 (Android SMS + Composer) ── dep 1,4 ─────┤
Task 9 (Portal SMS prefs) ── dep 1 ────────────┘
```

**Parallelizable:**
- Tasks 1, 4, 5 (no deps)
- Tasks 2+3 (after 1)
- Tasks 6, 7 (after 5)
- Tasks 8, 9 (after 1, 4)
