# Asterisk Audio Pipeline Fix — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the entire Asterisk ↔ TG200 ↔ AI voice pipeline so inbound and outbound calls have clean bidirectional audio flowing to OpenAI Realtime, Gemini Live, and Grok Live backends.

**Architecture:** Inbound calls route through ARI Stasis for session setup, then `continue_in_dialplan` to AudioSocket for bidirectional 8kHz slin PCM16 streaming. Outbound calls originate via ARI, wait for callee answer (StasisStart), then hand off to AudioSocket. The PhoneAIBridge handles resampling between Asterisk's 8kHz and AI backends' native rates (24kHz for OpenAI/Grok, 16kHz input for Gemini). UUID coordination between ARI and AudioSocket uses ARI channel variables.

**Tech Stack:** Asterisk 20.6.0 (PJSIP, ARI, AudioSocket), Python asyncio, Yeastar TG200 LTE-A (G.711 u-law only), OpenAI Realtime API, Gemini Live API, Grok Live API

---

## Research Summary (from 4 parallel agents)

### Hard Facts
- **G.722 is NOT supported** on ANY TG200 cellular module — confirmed by Yeastar support article. Hardware DSP limitation. No firmware fix possible.
- **AudioSocket protocol** uses 3-byte headers (1 type + 2 uint16 BE length). UUID is 16-byte binary. Our code was using 4-byte headers — already partially fixed.
- **Asterisk 20.6.0 AudioSocket() app forces slin 8kHz** — it calls `ast_set_read_format(chan, ast_format_slin)` regardless of negotiated codec. Multi-rate (slin16) is Asterisk 23+ only.
- **Frame type byte encodes sample rate**: 0x10=8kHz, 0x12=16kHz. Currently we hardcode 0x10.
- **Contact header fix**: `ignore_uri_user_options=yes` in PJSIP `[global]` section.
- **AI backend rates**: OpenAI=24kHz, Gemini=16kHz in/24kHz out, Grok=24kHz.

### Root Causes of Current Failures
1. **AudioSocket header was 4 bytes, should be 3** — partially fixed in audio_bridge.py, not yet deployed/tested
2. **Dialplan routes to AudioSocket directly, bypasses ARI Stasis** — `handle_inbound_call` never fires
3. **Sample rate is 16kHz in config, reality is 8kHz** — would cause double-speed/chipmunk audio
4. **UUID mismatch** — dialplan generates random UUID, code expects ARI channel_id
5. **Outbound originate calls `continue_in_dialplan` before callee answers** — channel drops
6. **Gemini/Grok audio roundtrips through 8kHz ULAW** even when source is already at the right rate
7. **Singleton audio callbacks** — second concurrent call overwrites first call's handlers

---

## Task 1: Fix PJSIP Configuration

**Files:**
- Modify: `Orchestrator/asterisk/configs/pjsip.conf`

**Step 1: Add global section and fix codec list**

```ini
; ADD at the very top of pjsip.conf (before transport):
[global]
type=global
ignore_uri_user_options=yes

; CHANGE the tg200 endpoint codec list:
[tg200]
...
disallow=all
allow=ulaw
allow=alaw
; REMOVED: allow=g722 (TG200 cellular module hardware limitation — Yeastar confirmed)
```

Also remove g722 from the `[softphone]` endpoint unless testing with a softphone that supports it.

**Step 2: Deploy and reload**

```bash
cp Orchestrator/asterisk/configs/pjsip.conf /tmp/pjsip.conf
sudo cp /tmp/pjsip.conf /etc/asterisk/pjsip.conf
sudo asterisk -rx 'pjsip reload'
```

**Step 3: Verify**

```bash
sudo asterisk -rx 'pjsip show endpoint tg200' | grep allow
# Expected: (ulaw|alaw) — no g722
sudo asterisk -rx 'pjsip show registrations'
# Expected: tg200-reg Registered
```

**Step 4: Verify Contact header errors are gone**

Call in, then check:
```bash
sudo tail -20 /var/log/asterisk/messages.log
# Expected: No more "PJSIP syntax error exception when parsing Contact header"
```

---

## Task 2: Fix Sample Rate to 8kHz

**Files:**
- Modify: `Orchestrator/asterisk/config.py:36-37`
- Modify: `Orchestrator/asterisk/audio_bridge.py` (docstrings only)

**Step 1: Update config.py**

```python
# Line 35-37: Change from:
# Audio format: Asterisk decodes G.722 to slin16 (signed linear 16-bit @ 16kHz)
ASTERISK_SAMPLE_RATE = 16000   # 16kHz — double the old SIM7600 8kHz path
ASTERISK_FRAME_SIZE_MS = 20    # 20ms frames = 640 bytes per frame at 16kHz

# To:
# Audio format: Asterisk AudioSocket() app sends slin 8kHz (forced by app_audiosocket.c)
# TG200 negotiates G.711 u-law — Asterisk transcodes to slin before AudioSocket
ASTERISK_SAMPLE_RATE = 8000    # 8kHz — TG200 only supports G.711 u-law
ASTERISK_FRAME_SIZE_MS = 20    # 20ms frames = 320 bytes per frame at 8kHz
```

**Step 2: Update audio_bridge.py docstring**

Line 8: Change "Bidirectional 16kHz PCM16 (slin16) audio streaming" to "Bidirectional 8kHz PCM16 (slin) audio streaming"

**Step 3: Verify no code references ASTERISK_SAMPLE_RATE for logic**

```bash
grep -rn "ASTERISK_SAMPLE_RATE" Orchestrator/
# Should only appear in config.py and imports — audio_bridge.py imports it but doesn't use it in frame logic
```

---

## Task 3: Fix AudioSocket Frame Type for Dynamic Rate Detection

**Files:**
- Modify: `Orchestrator/asterisk/audio_bridge.py`

The audio type byte tells us the sample rate. Add constants and detect it dynamically so we're future-proof for Asterisk 23+ (16kHz support).

**Step 1: Add rate-aware frame types**

After line 38, add:
```python
FRAME_TYPE_AUDIO_SLIN16 = 0x12  # slin 16kHz (Asterisk 23+)
FRAME_TYPE_AUDIO_SLIN24 = 0x13  # slin 24kHz (Asterisk 23+)

# Map frame type → sample rate
AUDIO_TYPE_RATES = {
    0x10: 8000,   # slin 8kHz
    0x11: 12000,  # slin 12kHz
    0x12: 16000,  # slin 16kHz
    0x13: 24000,  # slin 24kHz
}
```

**Step 2: Track detected rate per channel**

In `__init__`, add:
```python
self._channel_rates: Dict[str, int] = {}  # channel_uuid → detected sample rate
```

In `_handle_connection`, after UUID registration (around line 175), add:
```python
self._channel_rates[channel_id] = 8000  # Default, updated on first audio frame
```

In the audio loop where `frame_type == FRAME_TYPE_AUDIO` (around line 193), change to:
```python
if frame_type in AUDIO_TYPE_RATES:
    # Detect sample rate from frame type
    detected_rate = AUDIO_TYPE_RATES[frame_type]
    if detected_rate != self._channel_rates.get(channel_id, 8000):
        self._channel_rates[channel_id] = detected_rate
        print(f"[AudioSocket] Channel {channel_id} audio rate: {detected_rate}Hz")
    # Forward PCM16 audio to the Orchestrator
    if self.on_audio and payload:
        ...
```

Also accept these types in `send_audio` — use the detected rate to pick the right type byte:
```python
async def send_audio(self, channel_id: str, pcm16_data: bytes):
    ...
    # Use the same frame type the channel is receiving at
    rate = self._channel_rates.get(channel_id, 8000)
    if rate == 16000:
        frame_type = FRAME_TYPE_AUDIO_SLIN16
    elif rate == 24000:
        frame_type = FRAME_TYPE_AUDIO_SLIN24
    else:
        frame_type = FRAME_TYPE_AUDIO

    length = len(pcm16_data)
    header = bytes([frame_type, (length >> 8) & 0xFF, length & 0xFF])
    ...
```

**Step 3: Add `get_channel_rate()` method**

```python
def get_channel_rate(self, channel_id: str) -> int:
    """Get the detected audio sample rate for a channel."""
    return self._channel_rates.get(channel_id, 8000)
```

In cleanup (finally block), add:
```python
self._channel_rates.pop(channel_id, None)
```

---

## Task 4: Fix Per-Channel Callbacks (Concurrent Call Support)

**Files:**
- Modify: `Orchestrator/asterisk/audio_bridge.py`

The current singleton `self.on_audio` / `self.on_hangup` callbacks get overwritten by the second call. Replace with per-channel dispatch.

**Step 1: Replace singleton callbacks with per-channel dicts**

In `__init__`, change:
```python
# OLD:
self.on_audio: Optional[Callable] = None
self.on_hangup: Optional[Callable] = None
self.on_connect: Optional[Callable] = None

# NEW:
# Per-channel callbacks (support concurrent calls)
self._audio_handlers: Dict[str, Callable[[str, bytes], Awaitable[None]]] = {}
self._hangup_handlers: Dict[str, Callable[[str], Awaitable[None]]] = {}
self._connect_handlers: Dict[str, Callable[[str], Awaitable[None]]] = {}
# Global fallback (for logging/diagnostics)
self.on_connect: Optional[Callable[[str], Awaitable[None]]] = None
```

**Step 2: Add register/unregister methods**

```python
def register_channel(self, channel_id: str,
                     on_audio: Callable[[str, bytes], Awaitable[None]] = None,
                     on_hangup: Callable[[str], Awaitable[None]] = None,
                     on_connect: Callable[[str], Awaitable[None]] = None):
    """Register per-channel audio/hangup handlers."""
    if on_audio:
        self._audio_handlers[channel_id] = on_audio
    if on_hangup:
        self._hangup_handlers[channel_id] = on_hangup
    if on_connect:
        self._connect_handlers[channel_id] = on_connect

def unregister_channel(self, channel_id: str):
    """Remove all handlers for a channel."""
    self._audio_handlers.pop(channel_id, None)
    self._hangup_handlers.pop(channel_id, None)
    self._connect_handlers.pop(channel_id, None)
```

**Step 3: Update _handle_connection to dispatch per-channel**

In the audio loop:
```python
if frame_type in AUDIO_TYPE_RATES:
    handler = self._audio_handlers.get(channel_id)
    if handler and payload:
        try:
            await handler(channel_id, payload)
        except Exception as e:
            print(f"[AudioSocket] on_audio error: {e}")
```

For hangup:
```python
elif frame_type == FRAME_TYPE_HANGUP:
    print(f"[AudioSocket] Hangup frame received: {channel_id}")
    break
```

In the `finally` cleanup:
```python
# Fire per-channel hangup handler
hangup_handler = self._hangup_handlers.get(channel_id)
if hangup_handler:
    asyncio.create_task(hangup_handler(channel_id))
self.unregister_channel(channel_id)
```

For connect (after UUID registration):
```python
# Fire per-channel connect handler
connect_handler = self._connect_handlers.get(channel_id)
if connect_handler:
    asyncio.create_task(connect_handler(channel_id))
elif self.on_connect:
    asyncio.create_task(self.on_connect(channel_id))
```

---

## Task 5: Add `set_variable` to ARI Client

**Files:**
- Modify: `Orchestrator/asterisk/client.py`

**Step 1: Add set_variable method**

After `get_channel_info` (around line 295), add:

```python
async def set_variable(self, channel_id: str, variable: str, value: str) -> bool:
    """Set a channel variable via ARI."""
    result = await self._ari_post(
        f"/ari/channels/{channel_id}/variable",
        params={"variable": variable, "value": value}
    )
    return result is not None
```

**Step 2: Fix originate() to use app_args instead of context**

Replace the originate method (lines 306-345):

```python
async def originate(
    self,
    endpoint: str,
    callerid: str = "",
    timeout: int = 45,
    variables: dict = None,
    app_args: str = "",
) -> Optional[str]:
    """
    Originate an outbound call via ARI Stasis.

    The channel enters Stasis when the callee answers.
    Use app_args to pass data to the StasisStart handler.
    """
    data = {
        "endpoint": endpoint,
        "timeout": timeout,
        "app": self.app_name,
    }
    if app_args:
        data["appArgs"] = app_args
    if callerid:
        data["callerId"] = callerid
    if variables:
        data["variables"] = {"type": "ChannelDialplanVariable", "variables": variables}

    result = await self._ari_post("/ari/channels", json_data=data)
    if result and "id" in result:
        channel_id = result["id"]
        print(f"[ARI] Originated call: {endpoint} → channel={channel_id}")
        return channel_id
    return None
```

---

## Task 6: Fix Dialplan — Stasis-First Inbound + AudioSocket Context

**Files:**
- Modify: `Orchestrator/asterisk/configs/extensions.conf`

**Step 1: Rewrite extensions.conf**

```ini
; =============================================================================
; extensions.conf — AI BlackBox Asterisk Dialplan
; =============================================================================

[general]
static=yes
writeprotect=yes

[globals]
AUDIOSOCKET_ADDR=127.0.0.1:9092

; =============================================================================
; Inbound from TG200 — route to ARI Stasis for session setup
; =============================================================================
[from-tg200]
exten => _X.,1,NoOp(=== Inbound call from TG200 ===)
 same => n,NoOp(Caller: ${CALLERID(num)} -> ${EXTEN})
 same => n,Stasis(blackbox,inbound,${CALLERID(num)},${EXTEN})
 same => n,Hangup()

exten => _+X.,1,Goto(from-tg200,${EXTEN:1},1)
exten => s,1,Stasis(blackbox,inbound,${CALLERID(num)},unknown)
 same => n,Hangup()
exten => unknown,1,Stasis(blackbox,inbound,${CALLERID(num)},unknown)
 same => n,Hangup()

; =============================================================================
; AudioSocket bridge context — entered via ARI continue_in_dialplan
; CALL_UUID must be set as a channel variable by ARI before continuing here
; =============================================================================
[blackbox-audiosocket]
exten => s,1,NoOp(=== AudioSocket bridge ===)
 same => n,NoOp(UUID: ${CALL_UUID})
 same => n,AudioSocket(${CALL_UUID},${AUDIOSOCKET_ADDR})
 same => n,Hangup()

; =============================================================================
; Outbound via TG200 (direct dialplan, no ARI — for future use)
; =============================================================================
[to-tg200]
exten => _+1NXXXXXXXXX,1,NoOp(=== Outbound call via TG200 ===)
 same => n,Dial(PJSIP/${EXTEN}@tg200,45,g)
 same => n,Hangup()

exten => _1NXXXXXXXXX,1,Goto(to-tg200,+${EXTEN},1)
exten => _NXXXXXXXXX,1,Goto(to-tg200,+1${EXTEN},1)
```

**Step 2: Deploy**

```bash
cp Orchestrator/asterisk/configs/extensions.conf /tmp/ext.conf
sudo cp /tmp/ext.conf /etc/asterisk/extensions.conf
sudo asterisk -rx 'dialplan reload'
```

---

## Task 7: Rewrite Inbound Call Handler

**Files:**
- Modify: `Orchestrator/routes/asterisk_routes.py:293-458`

The inbound handler must:
1. Receive StasisStart event (with args: "inbound", caller, callee)
2. Answer the channel via ARI
3. Generate a UUID, set it as channel variable via ARI
4. Register the UUID with AudioSocket bridge (expect_channel)
5. `continue_in_dialplan` to `blackbox-audiosocket`
6. Wait for AudioSocket to connect
7. Create PhoneSession and PhoneAIBridge
8. Wire audio callbacks (per-channel, not singleton)
9. Start bridge, wait for hangup

**Step 1: Rewrite handle_inbound_call**

```python
async def handle_inbound_call(channel_id: str, caller_id: str, callee_id: str, args: list = None):
    """Handle inbound call from TG200 via ARI Stasis."""
    from Orchestrator.asterisk.audio_bridge import get_audio_bridge
    from Orchestrator.asterisk.client import get_ari_client
    from Orchestrator.asterisk.config import TG200_PHONE_NUMBER, ASTERISK_SAMPLE_RATE

    client = get_ari_client()
    audio_bridge = get_audio_bridge()
    if not client or not audio_bridge:
        return

    bridge = None
    session_id = f"ast-in-{uuid.uuid4().hex[:12]}"
    call_uuid = str(uuid.uuid4())

    session = PhoneSession(
        session_id=session_id,
        caller_id=caller_id,
        callee_id=callee_id or TG200_PHONE_NUMBER,
        direction=CallDirection.INBOUND,
        status=PhoneStatus.RINGING,
        created_at=now_utc_iso(),
        last_activity=now_utc_iso(),
    )
    session.asterisk_channel_id = channel_id
    PHONE_SESSIONS[session_id] = session

    call_ended = asyncio.Event()

    try:
        print(f"[ASTERISK-ROUTE] Inbound: {caller_id} → {callee_id} (channel={channel_id})")

        # Answer via ARI
        await client.answer_channel(channel_id)
        session.status = PhoneStatus.BRIDGED
        session.call_start = now_utc_iso()

        # Set UUID as channel variable for dialplan AudioSocket
        await client.set_variable(channel_id, "CALL_UUID", call_uuid)

        # Expect AudioSocket connection with this UUID
        audio_bridge.expect_channel(call_uuid)

        # Send channel to AudioSocket context
        await client.continue_in_dialplan(channel_id, context="blackbox-audiosocket", extension="s")

        # Wait for AudioSocket TCP connection
        connected = await audio_bridge.wait_for_channel(call_uuid, timeout=10.0)
        if not connected:
            print("[ASTERISK-ROUTE] AudioSocket timeout for inbound call")
            try:
                await client.hangup_channel(channel_id)
            except Exception:
                pass
            session.status = PhoneStatus.FAILED
            return

        print(f"[ASTERISK-ROUTE] AudioSocket connected: {call_uuid}")

        # Detect actual sample rate from AudioSocket
        actual_rate = audio_bridge.get_channel_rate(call_uuid)
        print(f"[ASTERISK-ROUTE] Audio rate: {actual_rate}Hz")

        # Skip IVR for now — go straight to AI bridge
        # Default to OpenAI Realtime, operator from session or "phone-caller"
        session.operator = "phone-caller"
        session.ai_backend = AIBackend.OPENAI_REALTIME

        bridge = PhoneAIBridge(session)
        _active_bridges[session.session_id] = bridge

        # Wire AI audio → Asterisk (resample from AI rate to Asterisk rate)
        async def on_ai_pcm16(pcm16_data: bytes, source_rate: int):
            target_rate = actual_rate
            if source_rate != target_rate:
                from Orchestrator.phone.audio_converter import AudioConverter
                import numpy as np
                samples = np.frombuffer(pcm16_data, dtype=np.int16)
                num_out = int(len(samples) * target_rate / source_rate)
                from scipy.signal import resample_poly
                # Use resample_poly for better quality on small chunks
                gcd = math.gcd(target_rate, source_rate)
                up = target_rate // gcd
                down = source_rate // gcd
                resampled = resample_poly(samples.astype(np.float64), up, down)
                pcm16_data = np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()
            await audio_bridge.send_audio(call_uuid, pcm16_data)

        async def on_ai_transcript(text: str):
            session.add_message("assistant", text, "voice")

        bridge.on_ai_pcm16 = on_ai_pcm16
        bridge.on_ai_transcript = on_ai_transcript

        # Wire Asterisk audio → AI bridge (per-channel handler)
        async def on_asterisk_audio(ch_id: str, pcm16_data: bytes):
            if bridge.is_running:
                await bridge.send_pcm16(pcm16_data, source_rate=actual_rate)

        async def on_asterisk_hangup(ch_id: str):
            call_ended.set()

        audio_bridge.register_channel(
            call_uuid,
            on_audio=on_asterisk_audio,
            on_hangup=on_asterisk_hangup,
        )

        # Start AI bridge
        success = await bridge.start(session.operator)
        if not success:
            print("[ASTERISK-ROUTE] Bridge failed to start")
            try:
                await client.hangup_channel(channel_id)
            except Exception:
                pass
            session.status = PhoneStatus.FAILED
            return

        print(f"[ASTERISK-ROUTE] Inbound bridged: {caller_id} <-> {session.ai_backend.value}")
        await call_ended.wait()

    except Exception as e:
        print(f"[ASTERISK-ROUTE] Inbound error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if bridge:
            await bridge.stop()
        _active_bridges.pop(session.session_id, None)
        audio_bridge.unregister_channel(call_uuid)
        if client and client.is_connected:
            try:
                await client.hangup_channel(channel_id)
            except Exception:
                pass
        session.status = PhoneStatus.COMPLETED
        session.call_end = now_utc_iso()
        print(f"[ASTERISK-ROUTE] Inbound ended: {session.session_id}")
```

**Step 2: Add `import math` at top of file**

---

## Task 8: Rewrite Outbound Call Handler

**Files:**
- Modify: `Orchestrator/routes/asterisk_routes.py:128-287`

The outbound handler must:
1. Originate via ARI with `app_args="outbound,{call_uuid}"`
2. Wait for StasisStart (callee answers)
3. Set UUID channel variable, continue to AudioSocket
4. Wire audio bridge with per-channel handlers

**Step 1: Rewrite _handle_outbound_call**

```python
async def _handle_outbound_call(session: PhoneSession, to_number: str, trunk: str):
    """Background task: originate call, wait for answer, bridge to AI."""
    from Orchestrator.asterisk.client import get_ari_client
    from Orchestrator.asterisk.audio_bridge import get_audio_bridge
    from Orchestrator.asterisk.config import ASTERISK_SAMPLE_RATE

    client = get_ari_client()
    audio_bridge = get_audio_bridge()
    if not client or not audio_bridge:
        session.status = PhoneStatus.FAILED
        return

    bridge = None
    channel_id = None
    call_uuid = str(uuid.uuid4())
    stasis_event = asyncio.Event()
    stasis_channel_id_holder = [None]  # mutable container for closure

    # Intercept StasisStart for our outbound call
    original_on_stasis = client.on_stasis_start

    async def on_outbound_stasis(ch_id, caller, callee, args):
        if args and len(args) >= 2 and args[0] == "outbound" and args[1] == call_uuid:
            stasis_channel_id_holder[0] = ch_id
            stasis_event.set()
        elif original_on_stasis:
            await original_on_stasis(ch_id, caller, callee, args)

    client.on_stasis_start = on_outbound_stasis

    try:
        endpoint = f"PJSIP/{to_number}@{trunk}"
        print(f"[ASTERISK-ROUTE] Originating: {endpoint} (uuid={call_uuid})")

        channel_id = await client.originate(
            endpoint=endpoint,
            callerid=session.caller_id,
            timeout=45,
            app_args=f"outbound,{call_uuid}",
        )

        if not channel_id:
            print("[ASTERISK-ROUTE] Originate failed")
            session.status = PhoneStatus.FAILED
            return

        session.asterisk_channel_id = channel_id

        # Wait for callee to answer (StasisStart fires)
        print(f"[ASTERISK-ROUTE] Waiting for answer: {channel_id}")
        try:
            await asyncio.wait_for(stasis_event.wait(), timeout=50.0)
        except asyncio.TimeoutError:
            print("[ASTERISK-ROUTE] No answer timeout")
            await client.hangup_channel(channel_id)
            session.status = PhoneStatus.FAILED
            return

        answered_channel = stasis_channel_id_holder[0] or channel_id
        print(f"[ASTERISK-ROUTE] Callee answered: {answered_channel}")

        # Set UUID and continue to AudioSocket
        await client.set_variable(answered_channel, "CALL_UUID", call_uuid)
        audio_bridge.expect_channel(call_uuid)
        await client.continue_in_dialplan(answered_channel, context="blackbox-audiosocket", extension="s")

        # Wait for AudioSocket connection
        connected = await audio_bridge.wait_for_channel(call_uuid, timeout=15.0)
        if not connected:
            print("[ASTERISK-ROUTE] AudioSocket timeout")
            await client.hangup_channel(answered_channel)
            session.status = PhoneStatus.FAILED
            return

        session.status = PhoneStatus.BRIDGED
        session.call_start = now_utc_iso()

        actual_rate = audio_bridge.get_channel_rate(call_uuid)
        print(f"[ASTERISK-ROUTE] AudioSocket connected, rate={actual_rate}Hz")

        # Create AI bridge
        bridge = PhoneAIBridge(session)
        _active_bridges[session.session_id] = bridge

        # Wire AI audio → Asterisk
        async def on_ai_pcm16(pcm16_data: bytes, source_rate: int):
            target_rate = actual_rate
            if source_rate != target_rate:
                import numpy as np
                from scipy.signal import resample_poly
                samples = np.frombuffer(pcm16_data, dtype=np.int16)
                gcd = math.gcd(target_rate, source_rate)
                up = target_rate // gcd
                down = source_rate // gcd
                resampled = resample_poly(samples.astype(np.float64), up, down)
                pcm16_data = np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()
            await audio_bridge.send_audio(call_uuid, pcm16_data)

        async def on_ai_transcript(text: str):
            session.add_message("assistant", text, "voice")

        bridge.on_ai_pcm16 = on_ai_pcm16
        bridge.on_ai_transcript = on_ai_transcript

        # Wire Asterisk audio → AI bridge (per-channel)
        call_ended = asyncio.Event()

        async def on_asterisk_audio(ch_id: str, pcm16_data: bytes):
            if bridge.is_running:
                await bridge.send_pcm16(pcm16_data, source_rate=actual_rate)

        async def on_asterisk_hangup(ch_id: str):
            call_ended.set()

        audio_bridge.register_channel(
            call_uuid,
            on_audio=on_asterisk_audio,
            on_hangup=on_asterisk_hangup,
        )

        # Start AI bridge
        success = await bridge.start(session.operator or "phone-caller")
        if not success:
            print("[ASTERISK-ROUTE] Bridge failed to start")
            await client.hangup_channel(answered_channel)
            session.status = PhoneStatus.FAILED
            return

        print(f"[ASTERISK-ROUTE] Outbound bridged: {to_number} <-> {session.ai_backend.value}")

        # Send greeting if configured
        if session.outbound_greeting and bridge.is_running:
            await bridge.inject_text(session.outbound_greeting)

        await call_ended.wait()

    except Exception as e:
        print(f"[ASTERISK-ROUTE] Outbound error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        client.on_stasis_start = original_on_stasis
        if bridge:
            await bridge.stop()
        _active_bridges.pop(session.session_id, None)
        audio_bridge.unregister_channel(call_uuid)
        if channel_id and client and client.is_connected:
            try:
                await client.hangup_channel(channel_id)
            except Exception:
                pass
        session.status = PhoneStatus.COMPLETED
        session.call_end = now_utc_iso()
        print(f"[ASTERISK-ROUTE] Outbound ended: {session.session_id}")
```

---

## Task 9: Fix Gemini/Grok Direct PCM16 Path in PhoneAIBridge

**Files:**
- Modify: `Orchestrator/phone/bridge.py:646-663`

Currently Gemini/Grok roundtrip through 8kHz ULAW even when source is 8kHz or 16kHz. For Gemini specifically, it wants 16kHz — upsampling from 8kHz is unavoidable, but the ULAW encoding step is pure waste.

**Step 1: Add direct PCM16 path for Gemini**

Replace lines 646-663 in `send_pcm16`:

```python
else:
    # Gemini/Grok need client-side VAD — but avoid unnecessary ULAW roundtrip
    import numpy as np
    backend = self._ai_session.ai_backend

    if backend == AIBackend.GEMINI_LIVE:
        # Gemini wants 16kHz PCM16 — resample directly, skip ULAW
        target_rate = 16000
        if source_rate != target_rate:
            from scipy.signal import resample_poly
            samples = np.frombuffer(pcm16_data, dtype=np.int16)
            gcd = math.gcd(target_rate, source_rate)
            up = target_rate // gcd
            down = source_rate // gcd
            resampled = resample_poly(samples.astype(np.float64), up, down)
            pcm16_data = np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()
        # Send to Gemini VAD as PCM16 at 16kHz
        await self._send_gemini_pcm16(pcm16_data)

    elif backend == AIBackend.GROK_LIVE:
        # Grok wants 24kHz — resample from source rate
        target_rate = 24000
        if source_rate != target_rate:
            from scipy.signal import resample_poly
            samples = np.frombuffer(pcm16_data, dtype=np.int16)
            gcd = math.gcd(target_rate, source_rate)
            up = target_rate // gcd
            down = source_rate // gcd
            resampled = resample_poly(samples.astype(np.float64), up, down)
            pcm16_data = np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()
        # Send to Grok VAD as PCM16 at 24kHz
        await self._send_grok_pcm16(pcm16_data)

    else:
        # Fallback: downsample to 8kHz ULAW for unknown backends
        samples = np.frombuffer(pcm16_data, dtype=np.int16)
        if source_rate == 16000:
            if len(samples) >= 2:
                pcm_8k = ((samples[0::2].astype(np.int32) + samples[1::2].astype(np.int32)) // 2).astype(np.int16).tobytes()
            else:
                pcm_8k = pcm16_data
        elif source_rate != 8000:
            pcm_8k = AudioConverter.downsample(pcm16_data, source_rate // 8000)
        else:
            pcm_8k = pcm16_data
        ulaw_data = AudioConverter.pcm16_to_ulaw_bytes(pcm_8k)
        await self.send_audio(ulaw_data)
```

**Step 2: Add _send_gemini_pcm16 and _send_grok_pcm16 helper methods**

These are new methods that bypass the ULAW encoding and feed PCM16 directly to the VAD processor and then to the AI backend. The exact implementation depends on the Gemini/Grok VAD processor interface — this may require modifying the VAD processors to accept PCM16 directly instead of ULAW.

**NOTE:** This task is a quality improvement and can be deferred if the pipeline is working with the ULAW path. The ULAW roundtrip adds ~3dB of noise but doesn't break functionality.

---

## Task 10: Deploy, Restart, and End-to-End Test

**Files:**
- Script: `/tmp/deploy-asterisk.sh`

**Step 1: Create deployment script**

```bash
#!/bin/bash
set -e

BASE="/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"

echo "=== Deploying Asterisk configs ==="
cp "$BASE/Orchestrator/asterisk/configs/pjsip.conf" /tmp/pjsip.conf
cp "$BASE/Orchestrator/asterisk/configs/extensions.conf" /tmp/ext.conf
sudo cp /tmp/pjsip.conf /etc/asterisk/pjsip.conf
sudo cp /tmp/ext.conf /etc/asterisk/extensions.conf
sudo asterisk -rx 'core reload'
echo "Asterisk configs deployed"

echo "=== Restarting BlackBox ==="
sudo systemctl restart blackbox.service
echo "Waiting for BlackBox to start (60-90s)..."
sleep 60

echo "=== Verifying ==="
sudo asterisk -rx 'pjsip show registrations'
sudo asterisk -rx 'pjsip show endpoint tg200' | grep allow
curl -s http://localhost:9091/asterisk/status | python3 -m json.tool
echo "=== Done ==="
```

**Step 2: Test inbound call**

1. Call from cell phone to TG200 number
2. Check Asterisk console: should see `Stasis(blackbox,inbound,...)`
3. Check BlackBox logs: should see `[ASTERISK-ROUTE] Inbound:`, `AudioSocket connected`, `Inbound bridged`
4. Speak into phone — should hear AI respond

```bash
sudo journalctl -u blackbox.service -f | grep -iE "ASTERISK|AudioSocket|bridge"
```

**Step 3: Test outbound call**

```bash
curl -X POST http://localhost:9091/asterisk/call \
  -H "Content-Type: application/json" \
  -d '{"to": "+15555550112", "backend": "openai_realtime", "operator": "Brandon"}'
```

Phone should ring. Pick up. Should hear AI greeting. Speak back — AI should respond.

**Step 4: Verify audio quality**

- No chipmunk/sped-up audio (sample rate correct)
- No static/noise (frame format correct)
- Bidirectional (you hear AI, AI hears you)
- No immediate hangup (AudioSocket stable)

---

## Task 11: Update Memory and Snapshot

**Step 1: Update MEMORY.md** with confirmed findings:
- G.722 confirmed NOT supported (Yeastar article link)
- AudioSocket protocol: 3-byte headers, 16-byte binary UUID
- Asterisk 20 forces 8kHz slin in AudioSocket() app
- Contact header fix: `ignore_uri_user_options=yes`

**Step 2: Create development snapshot**

Use `/snapshot-dev` to capture all changes.

---

## Dependency Graph

```
Task 1 (PJSIP config) ──────────────────────────────────┐
Task 2 (Sample rate) ───────────────────────────────────┤
Task 3 (Dynamic rate detection) ─── depends on Task 2 ──┤
Task 4 (Per-channel callbacks) ─────────────────────────┤
Task 5 (ARI set_variable + originate fix) ──────────────┤
Task 6 (Dialplan rewrite) ──── depends on Task 5 ──────┤── Task 10 (Deploy + Test)
Task 7 (Inbound handler) ──── depends on 4, 5, 6 ──────┤
Task 8 (Outbound handler) ─── depends on 4, 5, 6 ──────┤
Task 9 (Gemini/Grok PCM16) ── independent, can defer ──┘
Task 11 (Memory + Snapshot) ── after Task 10
```

**Parallelizable groups:**
- Group A (no deps): Tasks 1, 2, 4, 5
- Group B (after A): Tasks 3, 6
- Group C (after B): Tasks 7, 8
- Group D (after C): Task 10
- Deferrable: Task 9
