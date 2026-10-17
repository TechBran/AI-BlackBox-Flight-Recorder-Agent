# Outbound Phone Calls + Tool Schema Updates Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ensure outbound phone calls through the TG200/Asterisk pipeline work end-to-end with the same audio quality as inbound calls, update all tool schemas for `send_sms`, `make_phone_call`, and `make_voice_call` to reflect current architecture, and validate the complete voice agent delegation flow.

**Architecture:** Outbound calls use ARI `originate()` to dial via TG200 PJSIP trunk → callee answers → Asterisk routes to AudioSocket (port 9092) → audio subprocess handles 8kHz PCM16 with 20ms frame pacing → IPC to main process → PhoneAIBridge connects to voice backend (OpenAI Realtime, Gemini Live, or Grok Live) via internal WebSocket. The tool registry (`tool_registry.py`) is the single source of truth for schemas — all route files import from it via converter functions. Tool execution handlers in each route file dispatch to the appropriate provider.

**Tech Stack:** Python (FastAPI, asyncio, ARI HTTP/WS, AudioSocket TCP), Asterisk PJSIP/dialplan, TG200 GSM gateway, existing tool_registry.py system.

---

## System Reference

| Component | File | Notes |
|-----------|------|-------|
| **ARI Client** | `Orchestrator/asterisk/client.py` | `originate()` at line 330, singleton via `get_ari_client()` |
| **Audio Subprocess** | `Orchestrator/asterisk/audio_subprocess.py` | Separate process, 20ms frame pacing, 24k→8k resampling |
| **Audio IPC** | `Orchestrator/asterisk/audio_ipc.py` | Unix socketpair, binary framed, `send_ai_audio()` |
| **Voice Bridge** | `Orchestrator/asterisk/voice_bridge.py` | Bridges AudioSocket↔WS for inbound calls |
| **Phone AI Bridge** | `Orchestrator/phone/bridge.py` | Used for outbound calls, connects to voice backends |
| **Outbound Endpoint** | `Orchestrator/routes/asterisk_routes.py:130` | `POST /asterisk/call` → `_handle_outbound_call()` |
| **Inbound Handler** | `Orchestrator/routes/asterisk_routes.py:326` | `handle_inbound_call()` → IVR → AudioSocket → voice bridge |
| **Tool Registry** | `Orchestrator/tools/tool_registry.py` | Single source of truth, group-based |
| **Tool Executor** | `Orchestrator/tools/blackbox_tools.py` | `_execute_send_sms`, `_execute_make_phone_call`, `_execute_make_voice_call` |
| **Dialplan** | `Orchestrator/asterisk/configs/extensions.conf` | `from-tg200` (inbound), `blackbox-audiosocket` (bridge), `to-tg200` (outbound) |
| **TG200 Trunk** | PJSIP `tg200` | `192.168.1.200:5060`, registered, 0.776ms RTT |

### Audio Pipeline (Proven for Inbound)
```
Phone (8kHz u-law) ↔ TG200 (SIP) ↔ Asterisk (slin 8kHz) ↔ AudioSocket TCP:9092
    ↔ Audio Subprocess (20ms pacing, Butterworth resampling)
    ↔ IPC (Unix socketpair)
    ↔ Main Process
    ↔ Voice Backend WS (OpenAI/Gemini/Grok @ 24kHz)
```

### Tool Schema Locations (All Generated from tool_registry.py)

| Consumer | File | Import |
|----------|------|--------|
| Anthropic REST | `chat_routes.py:52` | `get_anthropic_tools("chat")` |
| Anthropic CU | `chat_routes.py:53` | `get_anthropic_tools("chat_cu")` |
| OpenAI REST | `chat_routes.py:54` | `get_openai_rest_tools("chat")` |
| Gemini REST | `chat_routes.py:55` | `get_gemini_rest_tools("chat")` |
| OpenAI Realtime | `realtime_routes.py:78` | `get_openai_realtime_tools("realtime")` |
| Gemini Live | `gemini_live_routes.py:85` | `get_gemini_live_tools("gemini_live")` |
| Grok Live | `grok_live_routes.py:81` | `get_openai_realtime_tools("grok_live")` |
| Phone Bridge | `blackbox_tools.py:34` | `get_anthropic_tools("phone")` |
| MCP | `MCP/blackbox_mcp_server.py` | Not for phone tools (excluded by group) |

**Key insight:** Since all consumers import from `tool_registry.py`, updating the canonical definition automatically propagates to all providers after service restart. The execution handlers are separate and need individual updates.

---

## Task 1: Validate Outbound Call Audio Pipeline

**Files:**
- Reference: `Orchestrator/routes/asterisk_routes.py:130-320` (outbound handler)
- Reference: `Orchestrator/asterisk/client.py:330` (ARI originate)
- Reference: `Orchestrator/asterisk/configs/extensions.conf` (dialplan)

**Goal:** Test the outbound call flow end-to-end to identify any issues before writing code.

**Test procedure:**
```bash
# 1. Check Asterisk ARI is connected
curl -s http://localhost:9091/asterisk/status | python3 -m json.tool

# 2. Check TG200 trunk is registered
sudo asterisk -rx "pjsip show endpoint tg200"

# 3. Check AudioSocket subprocess is running
curl -s http://localhost:9091/asterisk/status | grep -i audio

# 4. Initiate a test outbound call
curl -X POST http://localhost:9091/asterisk/call \
  -H "Content-Type: application/json" \
  -d '{
    "to": "+15555550112",
    "backend": "openai_realtime",
    "operator": "Brandon",
    "greeting": "Hello Brandon, this is a test call from the AI BlackBox.",
    "role": "You are a friendly AI assistant making a test call."
  }'

# 5. Check phone session was created
curl -s http://localhost:9091/asterisk/channels

# 6. Verify AudioSocket connected (check logs)
journalctl -u blackbox.service --since "30 sec ago" | grep -i "audio\|socket\|channel\|bridge"
```

**Expected issues to look for:**
- ARI originate may fail if trunk name doesn't match
- AudioSocket may not connect if dialplan `blackbox-audiosocket` context is wrong
- PhoneAIBridge may fail to connect to voice backend WS
- Audio may not flow if subprocess IPC isn't wired for outbound

**Document any failures** — they become the fix list for Task 2.

---

## Task 2: Fix Outbound Audio Pipeline Issues

**Files:**
- Modify: `Orchestrator/routes/asterisk_routes.py` — fix any issues found in Task 1
- Modify: `Orchestrator/asterisk/configs/extensions.conf` — fix dialplan if needed
- Modify: `Orchestrator/asterisk/client.py` — fix originate params if needed

**Common issues to fix (based on inbound experience documented in MEMORY.md):**

### A. Trunk name for originate
The outbound handler uses `session.trunk or "tg200"` to build the PJSIP endpoint. Verify this matches the registered trunk:
```python
endpoint = f"PJSIP/{to_number}@tg200"
```

### B. Dialplan context for AudioSocket
After ARI answers and sets CALL_UUID, it continues to `blackbox-audiosocket` context. Verify this context exists and routes correctly:
```
[blackbox-audiosocket]
exten => s,1,NoOp(=== AudioSocket bridge ===)
 same => n,Set(AUDIOSOCKET_ADDR=127.0.0.1:9092)
 same => n,AudioSocket(${CALL_UUID},${AUDIOSOCKET_ADDR})
 same => n,Hangup()
```

### C. PhoneAIBridge vs AsteriskVoiceBridge
Outbound currently uses `PhoneAIBridge` (line 258 of asterisk_routes.py). Inbound uses `AsteriskVoiceBridge`.

**Key difference:**
- `AsteriskVoiceBridge` (voice_bridge.py): Connects to WS endpoint, handles resampling internally, proven working for inbound
- `PhoneAIBridge` (phone/bridge.py): Legacy bridge for Twilio/cellular, may not handle Asterisk 8kHz properly

**Decision:** If outbound audio doesn't work with PhoneAIBridge, switch to `AsteriskVoiceBridge` (same as inbound). The bridge type should be consistent for the same audio source.

### D. Channel UUID format
The AudioSocket `app_audiosocket.c` sends UUID as raw 16 bytes, but the subprocess expects a 36-char string (with dashes). Verify the UUID format matches between ARI `set_variable(CALL_UUID)` and what AudioSocket sends.

---

## Task 3: Ensure Consistent Bridge for Inbound + Outbound

**Files:**
- Modify: `Orchestrator/routes/asterisk_routes.py` — unify bridge type

**Goal:** Both inbound and outbound should use the same proven audio bridge (`AsteriskVoiceBridge`), since both go through the same AudioSocket → subprocess → IPC path.

If outbound currently uses `PhoneAIBridge` and it works, leave it. If it doesn't work (audio issues), switch to `AsteriskVoiceBridge`:

```python
# In _handle_outbound_call(), replace PhoneAIBridge with AsteriskVoiceBridge:
from Orchestrator.asterisk.voice_bridge import AsteriskVoiceBridge

bridge = AsteriskVoiceBridge(
    channel_uuid=call_uuid,
    backend=session.ai_backend,
    operator=session.operator,
    greeting=session.greeting,
    role=session.role,
)
await bridge.start()
```

---

## Task 4: Update `send_sms` Tool Schema + Execution

**Files:**
- Modify: `Orchestrator/tools/tool_registry.py:366-384` — update schema
- Modify: `Orchestrator/tools/blackbox_tools.py:93-140` — already updated (AMI path)
- Verify: All route execution handlers still work

**Schema updates in tool_registry.py:**
```python
{
    "name": "send_sms",
    "description": "Send an SMS text message via the TG200 gateway. The message will be delivered as a real cellular SMS. For long messages, the system automatically splits into multiple SMS segments (160 chars each, up to 10 segments).",
    "parameters": {
        "type": "object",
        "properties": {
            "phone_number": {
                "type": "string",
                "description": "Phone number in E.164 format (e.g., +15551234567) or 10-digit US format"
            },
            "message": {
                "type": "string",
                "description": "The text message to send. Plain text only, no markdown. Max ~1500 characters (auto-split into segments)."
            }
        },
        "required": ["phone_number", "message"]
    },
    "groups": _ALL_NO_MCP,
},
```

**Execution verification:**
The `_execute_send_sms()` in `blackbox_tools.py` was already updated to use AMI (Task 6 from previous plan). Verify each route's execution handler delegates to the tool executor correctly:

- `chat_routes.py`: Check `if tool_name == "send_sms"` blocks in all 4 streaming handlers
- `realtime_routes.py`: Check the tool execution dispatch
- `gemini_live_routes.py`: Same
- `grok_live_routes.py`: Same

All should call `BlackBoxToolExecutor._execute_send_sms()` or route to `/sms/send`.

---

## Task 5: Update `make_phone_call` Tool Schema + Execution

**Files:**
- Modify: `Orchestrator/tools/tool_registry.py:385-412` — update schema
- Verify: `Orchestrator/tools/blackbox_tools.py:728-778` — execution path

**Schema updates:**
```python
{
    "name": "make_phone_call",
    "description": "DELEGATE a phone call to a separate AI voice agent via the TG200 cellular gateway. The agent will have a real-time voice conversation. Use 'role' to define WHO the agent IS and 'greeting' for WHAT task to accomplish. The call goes through Asterisk + TG200 as a real cellular call.",
    "parameters": {
        "type": "object",
        "properties": {
            "phone_number": {
                "type": "string",
                "description": "Phone number in E.164 format (e.g., +15551234567) or 10-digit US format"
            },
            "role": {
                "type": "string",
                "description": "The PERSONA/CHARACTER for the AI voice agent — define WHO they are. This becomes the agent's system prompt."
            },
            "greeting": {
                "type": "string",
                "description": "The TASK INSTRUCTIONS — WHAT to do on this call. The voice agent will speak this greeting first, then converse."
            },
            "backend": {
                "type": "string",
                "description": "AI voice backend for the call",
                "enum": ["openai_realtime", "gemini_live", "grok_live"],
                "default": "openai_realtime"
            }
        },
        "required": ["phone_number"]
    },
    "groups": _ALL_NO_MCP,
},
```

**Changes from current:**
- Removed `claude_code` from backend enum (not a voice backend)
- Updated description to mention TG200 cellular gateway
- Added `default` for backend

**Execution verification:**
`_execute_make_phone_call()` already routes to `/asterisk/call` when `TELEPHONY_PROVIDER == "asterisk"`. Verify this works.

---

## Task 6: Update `make_voice_call` Tool Schema + Execution

**Files:**
- Modify: `Orchestrator/tools/tool_registry.py:413-436` — update schema
- Verify: `Orchestrator/tools/blackbox_tools.py:176` — TTS + call execution

**Schema updates:**
```python
{
    "name": "make_voice_call",
    "description": "Call a phone number and deliver a pre-recorded voice message using TTS. The system generates the audio first (via OpenAI TTS), then calls the number and plays it. For interactive calls, use make_phone_call instead.",
    "parameters": {
        "type": "object",
        "properties": {
            "phone_number": {
                "type": "string",
                "description": "Phone number in E.164 format (e.g., +15551234567) or 10-digit US format"
            },
            "message": {
                "type": "string",
                "description": "The message to speak. Will be converted to speech via TTS before calling."
            },
            "voice": {
                "type": "string",
                "description": "TTS voice to use",
                "enum": ["alloy", "echo", "fable", "onyx", "nova", "shimmer"],
                "default": "onyx"
            }
        },
        "required": ["phone_number", "message"]
    },
    "groups": _ALL_NO_MCP,
},
```

---

## Task 7: Verify Tool Execution Handlers Across All Routes

**Files to check (read-only verification, fix if broken):**
- `Orchestrator/routes/chat_routes.py` — all `if tool_name == "send_sms"` / `make_phone_call` / `make_voice_call` blocks
- `Orchestrator/routes/realtime_routes.py` — tool dispatch
- `Orchestrator/routes/gemini_live_routes.py` — tool dispatch
- `Orchestrator/routes/grok_live_routes.py` — tool dispatch
- `Orchestrator/phone/bridge.py` — `_execute_tool()` / `unified_tool_map`

**What to check:**
1. Each handler calls the correct executor method
2. `operator` parameter is passed through (needed for SMS message store)
3. Error handling is consistent
4. No stale references to old function names or endpoints

**Phone bridge special case:**
The phone bridge (`bridge.py`) has its own `unified_tool_map` in `_execute_tool()`. Verify `send_sms` and `make_phone_call` entries exist and route correctly.

---

## Task 8: Live E2E Test — Outbound Call

**Test matrix:**

| Test | Command | Expected |
|------|---------|----------|
| **Outbound call via API** | `POST /asterisk/call` with greeting | Phone rings, AI speaks greeting, conversation flows |
| **Outbound call via tool** | Ask AI "Call Brandon and tell him the SMS system is working" | AI uses `make_phone_call`, call initiates |
| **Voice message via tool** | Ask AI "Leave Brandon a voice message saying hello" | AI uses `make_voice_call`, TTS generated, call + playback |
| **SMS via tool** | Ask AI "Text Brandon that the outbound calls are working" | AI uses `send_sms`, SMS delivered |
| **Tool from voice session** | During a call, ask voice AI to "text Brandon" | Voice AI calls `send_sms` tool mid-conversation |

---

## Task 9: Android + Portal — Outbound Call UI

**Files:**
- Reference: Portal modules for any call UI
- Reference: Android telephony screen

**Assessment:** Determine if any UI changes are needed for outbound calling. The tool-based approach (AI decides to call) may be sufficient without dedicated UI. If a "Call" button is wanted in the SMS conversation view or Contact Book, plan it here.

**Possible additions:**
- Contact card "Call" button → triggers `POST /asterisk/call`
- SMS conversation header "Call" button → calls the current thread's number
- Android: Add call button to ContactsScreen and SmsInboxScreen

---

## Dependency Graph

```
Task 1 (Validate pipeline) ──────────────────┐
Task 2 (Fix issues from 1) ── dep 1 ─────────┤
Task 3 (Consistent bridge) ── dep 2 ──────────┤
Task 4 (send_sms schema) ────────────────────┤
Task 5 (make_phone_call schema) ─────────────┤── Task 8 (E2E Test)
Task 6 (make_voice_call schema) ─────────────┤
Task 7 (Verify execution handlers) ── dep 4-6┤
Task 9 (UI assessment) ── dep 8 ─────────────┘
```

**Parallelizable:**
- Tasks 4, 5, 6 (schema updates, independent)
- Tasks 1-3 (pipeline, sequential)
- Task 7 (after 4-6)
