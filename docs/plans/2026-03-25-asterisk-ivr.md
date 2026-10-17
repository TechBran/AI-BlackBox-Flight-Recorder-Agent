# Asterisk IVR System Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a professional IVR system for Asterisk inbound calls with PIN verification, dynamic operator selection, and AI backend selection (OpenAI/Gemini/Grok) — reusing the proven prompts from `ivr_prompts.py` and connecting to AI via `AsteriskVoiceBridge`.

**Architecture:** IVR runs while the channel is in ARI Stasis (DTMF via ARI events, TTS via pre-generated WAV files played with ARI playback). After IVR completes, channel continues to AudioSocket for the AI voice bridge. TTS prompts are generated at startup via OpenAI TTS-1-HD and cached as Asterisk-compatible WAV files. Operators loaded dynamically from `/operators` API.

**Tech Stack:** Asterisk ARI (DTMF + playback), OpenAI TTS-1-HD (prompt generation), Python asyncio, existing `ivr_prompts.py` text, `AsteriskVoiceBridge` for AI connection.

---

## Design Decisions

### Why ARI Playback (not AudioSocket for IVR)?
- ARI DTMF events only work while channel is in Stasis
- ARI has native `play_sound` for WAV files — reliable, no frame pacing needed
- Once IVR completes, channel moves to AudioSocket for bidirectional AI audio
- Clean separation: IVR = ARI, AI conversation = AudioSocket

### Why Pre-Generated TTS (not live TTS)?
- IVR prompts are static text (greeting, PIN prompt, menu options)
- Pre-generating at startup eliminates TTS latency during calls
- Cached as WAV files in Asterisk's sound directory — played via `ARI play_sound`
- Dynamic prompts (operator names) generated on first use and cached

### IVR Flow
```
Call arrives → Stasis(blackbox,inbound)
  → PIN verification (PHONE_PIN_CODE digits, 3 attempts)
  → Operator selection (dynamic from /operators API)
  → Backend selection (1=Claude, 2=Gemini, 3=GPT, 4=Grok)
  → Confirmation ("Welcome, Brandon. Connecting you to GPT.")
  → continue_in_dialplan → AudioSocket → AsteriskVoiceBridge
```

### DTMF During Stasis
- ARI fires `ChannelDtmfReceived` events while channel is in Stasis
- We register a per-call DTMF handler on the ARI client
- Barge-in: DTMF during playback cancels the playback and processes the digit

---

## Task 1: TTS Prompt Generator + WAV Cache

**Files:**
- Create: `Orchestrator/asterisk/ivr_audio.py`

This module generates TTS for all IVR prompts and saves them as WAV files that Asterisk can play via ARI.

```python
# ivr_audio.py — Pre-generates IVR TTS prompts as Asterisk-playable WAV files
#
# OpenAI TTS-1-HD → 24kHz PCM16 → downsample to 8kHz → save as WAV
# Files stored in: Orchestrator/asterisk/ivr_cache/
# Asterisk plays them via: ARI channels/{id}/play with media=sound:custom/ivr/{name}
#
# Prompt names match ivr_prompts.py constants:
#   greeting.wav, pin_prompt.wav, pin_accepted.wav, pin_retry.wav, pin_failed.wav,
#   backend_menu.wav, backend_retry.wav, backend_invalid.wav, backend_timeout.wav,
#   confirm_claude.wav, confirm_gemini.wav, confirm_gpt.wav, confirm_grok.wav,
#   goodbye.wav, connecting.wav, operator_{name}.wav (dynamic)
```

**Key implementation:**
- `generate_ivr_prompt(name, text, voice="onyx")` → generates TTS, resamples 24k→8k, saves as 8kHz 16-bit mono WAV
- `ensure_all_prompts()` → generates all static prompts if not cached
- `get_prompt_uri(name)` → returns `sound:custom/ivr/{name}` for ARI playback
- `generate_operator_prompt(operators)` → generates dynamic "Press 1 for Brandon..." prompt
- WAV files saved to Asterisk custom sounds: `/usr/share/asterisk/sounds/custom/ivr/`
- Uses streaming Butterworth downsampler (same proven technique from voice_bridge.py)
- Cached: only regenerates if file doesn't exist

**Step 1:** Create `ivr_audio.py` with the TTS generation and WAV caching logic.

**Step 2:** Add `ensure_all_prompts()` call to Asterisk startup in `startup.py` (after ARI connects).

**Step 3:** Verify WAV files are created:
```bash
ls -la /usr/share/asterisk/sounds/custom/ivr/
sudo asterisk -rx 'core show sounds like custom/ivr'
```

---

## Task 2: Asterisk IVR Engine

**Files:**
- Create: `Orchestrator/asterisk/ivr.py`

The IVR engine manages the call flow while the channel is in ARI Stasis. Uses ARI for DTMF and playback.

```python
# ivr.py — Asterisk IVR Engine
#
# Runs the IVR flow while channel is in ARI Stasis:
#   1. PIN verification (PHONE_PIN_CODE, 3 attempts)
#   2. Operator selection (dynamic from USERS_LIST)
#   3. Backend selection (1=Claude, 2=Gemini, 3=GPT, 4=Grok)
#
# DTMF: Received via ARI ChannelDtmfReceived events
# Audio: Played via ARI channels/{id}/play (pre-generated WAV files)
# Barge-in: ARI playback can be stopped with DELETE /playbacks/{id}
```

**Class: `AsteriskIVR`**

Constructor:
- `ari_client`: ARI client instance
- `channel_id`: Asterisk channel ID (in Stasis)
- `caller_id`: Caller's phone number

Key methods:
- `async run() -> Optional[dict]` — runs full IVR, returns `{"operator": str, "backend": str, "voice": str}` or None
- `async _play_and_wait(prompt_name, num_digits, timeout_s)` — play WAV + wait for DTMF
- `async _play(prompt_name)` — play WAV via ARI (stoppable for barge-in)
- `async _wait_dtmf(num_digits, timeout_s)` — wait for DTMF digits via ARI events
- `async _run_pin()` — PIN stage (returns True/False)
- `async _run_operator_select()` — operator stage (returns operator name)
- `async _run_backend_select()` — backend stage (returns backend ID)

DTMF handling:
- Register per-channel DTMF handler on ARI client during IVR
- Store in asyncio.Event + buffer (same pattern as CellularIVR)
- Barge-in: stop current playback via `DELETE /ari/playbacks/{id}`
- Deregister handler after IVR completes

**Step 1:** Create `ivr.py` with the AsteriskIVR class.

**Step 2:** Add `stop_playback` method to `client.py` if not present (already exists at line 381-383).

**Step 3:** Add `play_sound` to play custom IVR sounds:
```python
# In client.py, modify play_sound to support custom sound URIs
async def play_sound(self, channel_id, media):
    # media format: "sound:custom/ivr/greeting" (no .wav extension)
```

---

## Task 3: Wire IVR into Inbound Call Handler

**Files:**
- Modify: `Orchestrator/routes/asterisk_routes.py` (handle_inbound_call function)

Replace the hardcoded `operator="phone-caller"` and `backend="openai_realtime"` with the IVR flow.

**Flow:**
```python
async def handle_inbound_call(channel_id, caller_id, callee_id, args):
    # ... existing: answer, set UUID, etc.

    # Run IVR while channel is in Stasis
    ivr = AsteriskIVR(client, channel_id, caller_id)
    ivr_result = await ivr.run()

    if not ivr_result:
        # IVR failed (PIN wrong, hangup, etc.)
        await client.hangup_channel(channel_id)
        session.status = PhoneStatus.FAILED
        return

    operator = ivr_result["operator"]
    backend = ivr_result["backend"]
    voice = ivr_result.get("voice", "ash")

    # NOW continue to AudioSocket for AI bridge
    await client.set_variable(channel_id, "CALL_UUID", call_uuid)
    audio_bridge.expect_channel(call_uuid)
    await client.continue_in_dialplan(channel_id, context="blackbox-audiosocket", extension="s")

    # ... existing: wait for AudioSocket, create VoiceBridge with selected backend/operator
```

**Important:** The IVR runs BEFORE `continue_in_dialplan`. The channel stays in Stasis during the entire IVR. Only after IVR completes does the channel move to AudioSocket.

**Step 1:** Import `AsteriskIVR` and integrate into `handle_inbound_call`.

**Step 2:** Pass `operator`, `backend`, `voice` from IVR result to `AsteriskVoiceBridge`.

**Step 3:** Handle the "claude_code" backend case — Claude uses a different mechanism (not WS streaming). For now, map it to openai_realtime with a log warning, or add Claude support later.

---

## Task 4: Operator Voice Preferences

**Files:**
- Modify: `Orchestrator/asterisk/ivr.py` (backend selection stage)

After operator is selected, look up their preferred voice from operator preferences:

```python
from Orchestrator.state import get_operator_preference

voice = get_operator_preference(operator, "voice", "ash")
```

This way each operator gets their preferred AI voice automatically.

Also: the backend selection can check operator preferences for a default backend:
```python
default_backend = get_operator_preference(operator, "default_backend", "openai_realtime")
```

---

## Task 5: Startup TTS Pre-Generation

**Files:**
- Modify: `Orchestrator/startup.py` (Asterisk initialization section)

After Asterisk ARI connects, pre-generate all static IVR prompts:

```python
# In startup_asterisk(), after ARI connects:
from Orchestrator.asterisk.ivr_audio import ensure_all_prompts
await ensure_all_prompts()
print("IVR prompts pre-generated")
```

This runs once at startup. Cached files persist across restarts. New operator names trigger generation on first call.

---

## Task 6: Deploy and End-to-End Test

**Steps:**

1. Restart BlackBox service
2. Verify IVR WAV files exist:
   ```bash
   ls /usr/share/asterisk/sounds/custom/ivr/
   ```
3. Call in from cell phone
4. Verify flow:
   - Hear "Thank you for calling AI BlackBox"
   - Hear "Please enter your 4-digit access code"
   - Enter the PIN (PHONE_PIN_CODE in .env)
   - Hear "Access granted"
   - Hear operator menu (if multiple operators)
   - Select operator
   - Hear backend menu: "Press 1 for Claude, 2 for Gemini, 3 for GPT, 4 for Grok"
   - Press 3
   - Hear "Connecting you to GPT. One moment."
   - AI voice conversation starts
5. Test wrong PIN (should get 3 attempts then hangup)
6. Test timeout (should default to GPT after timeout)

---

## Task 7: Gemini and Grok Backend Testing

**Files:**
- No code changes needed — `AsteriskVoiceBridge` already supports all three backends via ENDPOINTS dict

**Steps:**
1. Call in, select "Press 2" for Gemini
2. Verify Gemini voice connects and responds
3. Call in, select "Press 4" for Grok
4. Verify Grok voice connects and responds
5. Check logs for correct sample rates (Gemini input=16kHz, Grok input=24kHz)

**Note:** Claude (Press 1) won't work through the WS bridge — it uses a different mechanism. Map to a friendly error message for now.

---

## Dependency Graph

```
Task 1 (TTS generator) ────────────────┐
Task 2 (IVR engine) ─── depends on 1 ──┤
Task 3 (Wire into handler) ── deps 2 ──┤── Task 6 (Test)
Task 4 (Operator prefs) ── deps 2 ─────┤
Task 5 (Startup pre-gen) ── deps 1 ────┘
Task 7 (Gemini/Grok test) ── after 6
```

**Parallelizable:** Tasks 1 and 2 can be developed in parallel (IVR engine uses prompt URIs, not the generator directly).
