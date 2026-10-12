# ElevenLabs Reliability — TTS Stream/Idle-Timeout + STT Session-Resume + Mic Isolation

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop ElevenLabs operations from dying on a fixed timer. (A) TTS no longer 60s-times-out on long generations — it streams and only aborts on a true stall. (B1) The STT mic no longer dies at ElevenLabs Scribe's session cap — the backend transparently reconnects and resumes. (B2) Leftover transcript from one mic press no longer bleeds into the next.

**Architecture:** Replace fixed wall-clock timeouts with progress/continuity signals. TTS → ElevenLabs `/stream` endpoint consumed with a per-chunk idle timeout (server-side, since the MCP path returns a saved-file URL), executor cap raised to a backstop. STT → `_elevenlabs_bridge` refactored to a queue-based, reconnectable relay that rotates the upstream Scribe session on `session_time_limit_exceeded` while keeping the client socket and transcript continuous. Mic client → per-session epoch so stale events/teardown can't cross presses.

**Tech Stack:** Python (FastAPI, `requests` streaming, `websockets`, asyncio), Kotlin (OkHttp WS, coroutines). The box uses ElevenLabs for BOTH TTS and STT (`.env: STT_PROVIDER=elevenlabs`).

---

## Context the engineer needs (verified during investigation)

- **TTS call chain:** MCP `text_to_speech` → `ToolVault/tools/text_to_speech/executor.py` (POST `/tts` with `return_json:true`, `aiohttp ClientTimeout(total=60)` ← binding) → `Orchestrator/routes/tts_routes.py::tts_openai` (ElevenLabs branch lines 147-174; `return_json:true` saves a file + returns `{audio_url}`) → `_elevenlabs_synth_chunked` (lines 112-131) → `Orchestrator/elevenlabs/tts.py::synthesize/_post` (buffered `requests.post`, `timeout=120`, returns `resp.content`). No streaming today.
- **STT wire contract:** client→server JSON `{type:stt_start|stt_audio|stt_stop}`; server→client `{type:stt_delta(cumulative)|stt_final|stt_error}`. ElevenLabs bridge: `Orchestrator/routes/stt_ws_routes.py::_elevenlabs_bridge` (lines 276-417). It currently SWALLOWS `session_time_limit_exceeded` silently (lines 348-359) — no error, no reconnect — then closes `/ws/stt`. `InterimAccumulator` (`Orchestrator/stt/streaming.py`) makes `stt_delta.text` cumulative and resets on final.
- **Mic client:** `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/data/voice/SttStreamClient.kt` reuses ONE instance across presses (kept by `remember(api)` in `CliMicButton.kt`); `stop()` keeps the WS open for `STOP_GRACE_MS=1200ms`; a late `stt_final` from press N can reach press N+1's shared events flow.
- **Build/run:** Backend = `sudo systemctl restart blackbox.service` (pre-authorized; 60-90s warm-up). Python tests: the repo's existing pytest under `Orchestrator/tests/`. Android: `cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:compileDebugKotlin` (SDK configured; device = the Fold). Work on `main` (no worktree). Stage only our files (working tree has unrelated changes); never `git add -A`. Commit trailers:
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01EpjDERCGEeW3U8Jo4hNYfK
  ```

---

# PART A — TTS streaming + idle-timeout (backend, Python)

## Task A1: Config constants

**Files:** Modify `Orchestrator/config.py` (near the other ElevenLabs/TTS settings, ~line 543).

**Step 1:** Add:
```python
# ElevenLabs TTS streaming: abort a generation only after this many seconds with
# NO audio bytes received (a true stall) — NOT a total wall-clock cap. As long as
# ElevenLabs keeps streaming audio, generation continues regardless of length.
ELEVENLABS_TTS_STREAM_IDLE_S = int(os.getenv("ELEVENLABS_TTS_STREAM_IDLE_S", "30"))
# Generous backstop on the whole MCP /tts tool call (the server self-bounds via the
# idle timeout above; this only prevents an indefinite client hang). Not the binding
# constraint for legitimate long generations.
TTS_TOOL_BACKSTOP_S = int(os.getenv("TTS_TOOL_BACKSTOP_S", "900"))
```

**Step 2:** Verify import: `python -c "from Orchestrator import config; print(config.ELEVENLABS_TTS_STREAM_IDLE_S, config.TTS_TOOL_BACKSTOP_S)"` → `30 900`.

**Step 3:** Commit (`config.py`): `feat(tts): config for ElevenLabs stream idle-timeout + tool backstop`.

---

## Task A2: Provider streaming synth (TDD)

**Files:** Modify `Orchestrator/elevenlabs/tts.py`; Test `Orchestrator/tests/test_elevenlabs_tts_stream.py` (create).

**Step 1 — Write the failing test.** Create `Orchestrator/tests/test_elevenlabs_tts_stream.py`:
```python
import types
import pytest
from Orchestrator.elevenlabs import tts as el_tts


class _FakeResp:
    def __init__(self, status_code=200, chunks=(b"AB", b"CD"), body=None):
        self.status_code = status_code
        self._chunks = chunks
        self._body = body or {}
    def iter_content(self, chunk_size=4096):
        for c in self._chunks:
            yield c
    def json(self):
        return self._body
    def close(self):
        pass


def test_synthesize_stream_yields_chunks(monkeypatch):
    calls = {}
    def fake_post(url, **kw):
        calls["url"] = url
        calls["stream"] = kw.get("stream")
        return _FakeResp(200, chunks=(b"AB", b"CD"))
    monkeypatch.setattr(el_tts.requests, "post", fake_post)
    out = b"".join(el_tts.synthesize_stream("hello", "elevenlabs:voice123", model_id="eleven_v3"))
    assert out == b"ABCD"
    assert calls["url"].endswith("/v1/text-to-speech/voice123/stream")  # raw id + /stream
    assert calls["stream"] is True


def test_synthesize_stream_downgrades_format_once_on_gate(monkeypatch):
    seq = [_FakeResp(403, chunks=()), _FakeResp(200, chunks=(b"XY",))]
    def fake_post(url, **kw): return seq.pop(0)
    monkeypatch.setattr(el_tts.requests, "post", fake_post)
    out = b"".join(el_tts.synthesize_stream("hi", "voice123", output_format="mp3_44100_192"))
    assert out == b"XY"
    assert seq == []  # exactly two posts (original + one downgrade)
```

**Step 2 — Run, expect FAIL** (`synthesize_stream` undefined):
`cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && python -m pytest Orchestrator/tests/test_elevenlabs_tts_stream.py -q`

**Step 3 — Implement.** In `Orchestrator/elevenlabs/tts.py`, add (after `_post`):
```python
def _post_stream(text: str, voice_id: str, model_id: str, output_format: str,
                 voice_settings: dict | None) -> requests.Response:
    """One STREAMING TTS POST to the /stream endpoint. The (connect, read) timeout
    makes `read` a per-chunk IDLE timeout: it fires only after that many seconds
    with NO bytes — not a total cap — so long-but-progressing generations succeed."""
    body: dict = {"text": text, "model_id": model_id}
    if voice_settings is not None:
        body["voice_settings"] = voice_settings
    return requests.post(
        f"{client.BASE_URL}/v1/text-to-speech/{voice_id}/stream",
        headers=client.auth_headers(),
        params={"output_format": output_format},
        json=body,
        stream=True,
        timeout=(10, config.ELEVENLABS_TTS_STREAM_IDLE_S),
    )


def synthesize_stream(
    text: str,
    voice_id: str,
    *,
    model_id: str | None = None,
    output_format: str | None = None,
    voice_settings: dict | None = None,
):
    """Yield audio chunks from ElevenLabs' streaming TTS endpoint.

    Same quality-first defaults + one-time plan-tier format downgrade as
    ``synthesize``, but streamed so each chunk is proof of progress and the only
    failure is a true stall (idle timeout in ``_post_stream``)."""
    model_id = model_id or config.ELEVENLABS_TTS_MODEL_DEFAULT
    output_format = output_format or config.ELEVENLABS_TTS_FORMAT_DEFAULT
    raw_voice_id = voice_id.split("elevenlabs:")[-1]

    resp = _post_stream(text, raw_voice_id, model_id, output_format, voice_settings)
    if not (200 <= resp.status_code < 300):
        if resp.status_code in _FORMAT_GATE_STATUSES and output_format != _FALLBACK_FORMAT:
            print(f"[ELEVENLABS] TTS(stream) output format downgraded to {_FALLBACK_FORMAT} (plan tier)")
            try: resp.close()
            except Exception: pass
            resp = _post_stream(text, raw_voice_id, model_id, _FALLBACK_FORMAT, voice_settings)
        if not (200 <= resp.status_code < 300):
            err = client.map_error(resp.status_code, _parse_body(resp))
            try: resp.close()
            except Exception: pass
            raise RuntimeError(err)
    try:
        for chunk in resp.iter_content(chunk_size=4096):
            if chunk:
                yield chunk
    finally:
        try: resp.close()
        except Exception: pass
```

**Step 4 — Run, expect PASS** (2 tests). **Step 5 — Commit** (`tts.py` + test): `feat(tts): streaming ElevenLabs synth with per-chunk idle timeout (tested)`.

---

## Task A3: Route streams instead of buffering

**Files:** Modify `Orchestrator/routes/tts_routes.py` (ElevenLabs branch, ~112-174).

**Step 1:** Add a streaming chunker beside `_elevenlabs_synth_chunked`:
```python
def _elevenlabs_stream_chunked(text: str, voice: str, model_id: str | None,
                               voice_settings: dict | None):
    """Yield ElevenLabs audio for `text`, splitting by the model's live char cap
    and streaming each chunk's audio (each chunk has its own idle timeout)."""
    from Orchestrator.config import ELEVENLABS_TTS_MODEL_DEFAULT
    from Orchestrator.elevenlabs import tts as el_tts
    cap = el_tts.max_chars_for(model_id or ELEVENLABS_TTS_MODEL_DEFAULT)
    pieces = [text] if len(text) <= cap else chunk_text_for_tts(text, max_chars=cap)
    for piece in pieces:
        yield from el_tts.synthesize_stream(piece, voice, model_id=model_id, voice_settings=voice_settings)
```

**Step 2:** Replace the ElevenLabs branch body (lines 154-174) so both paths stream. Replace:
```python
        try:
            audio = _elevenlabs_synth_chunked(text, voice, model_id, voice_settings)
        except Exception as e:
            print(f"[ELEVENLABS] /tts synthesis failed: {e}")
            return {"status": "fallback", "detail": str(e)}

        if not body.get("return_json"):
            return StreamingResponse(iter([audio]), media_type="audio/mpeg")

        filename = f"{uuid.uuid4()}_tts.mp3"
        save_path = UPLOADS_DIR / filename
        with open(save_path, "wb") as f:
            f.write(audio)
        return {
            "status": "success",
            "audio_url": f"/ui/uploads/{filename}",
            "voice": voice,
            "model": model_id or "eleven_v3",
            "format": "mp3",
            "size_bytes": len(audio),
        }
```
with:
```python
        # Browser path: true passthrough stream (audio starts as ElevenLabs generates).
        if not body.get("return_json"):
            return StreamingResponse(
                _elevenlabs_stream_chunked(text, voice, model_id, voice_settings),
                media_type="audio/mpeg",
            )

        # MCP/API path: write chunks to the file AS THEY ARRIVE, then return the URL.
        # The provider's per-chunk idle timeout (Task A2) bounds this — no total cap,
        # so a long-but-progressing generation completes instead of being cut at 60s.
        filename = f"{uuid.uuid4()}_tts.mp3"
        save_path = UPLOADS_DIR / filename
        size = 0
        try:
            with open(save_path, "wb") as f:
                for chunk in _elevenlabs_stream_chunked(text, voice, model_id, voice_settings):
                    f.write(chunk)
                    size += len(chunk)
        except Exception as e:
            print(f"[ELEVENLABS] /tts synthesis failed: {e}")
            try: save_path.unlink(missing_ok=True)
            except Exception: pass
            return {"status": "fallback", "detail": str(e)}
        return {
            "status": "success",
            "audio_url": f"/ui/uploads/{filename}",
            "voice": voice,
            "model": model_id or "eleven_v3",
            "format": "mp3",
            "size_bytes": size,
        }
```
(Leave `_elevenlabs_synth_chunked` in place — harmless if still referenced elsewhere; grep to confirm no other caller breaks.)

**Step 3:** `python -c "import Orchestrator.routes.tts_routes"` (imports clean). **Step 4 — Commit:** `feat(tts): stream ElevenLabs audio through /tts (file + browser paths)`.

---

## Task A4: Raise the executor cap to a backstop

**Files:** Modify `ToolVault/tools/text_to_speech/executor.py`.

**Step 1:** Replace `timeout=aiohttp.ClientTimeout(total=60)` (line 26) with:
```python
                timeout=aiohttp.ClientTimeout(total=__import__("Orchestrator.config", fromlist=["TTS_TOOL_BACKSTOP_S"]).TTS_TOOL_BACKSTOP_S, sock_connect=15)
```
(Prefer a clean import at the top of the function instead of the inline `__import__`: add `from Orchestrator import config` and use `aiohttp.ClientTimeout(total=config.TTS_TOOL_BACKSTOP_S, sock_connect=15)`. Use the clean import.)

**Step 2:** Note in a comment that the server self-bounds via the streaming idle timeout (Task A2), so this `total` is a generous backstop, not the binding constraint.

**Step 3:** `python -m Orchestrator.toolvault.validate` (executor still valid). **Step 4 — Commit:** `fix(tts): raise text_to_speech tool timeout to a backstop (server self-bounds via stream idle)`.

---

## Task A5: Live smoke — long TTS no longer cuts at 60s

**Step 1:** `sudo systemctl restart blackbox.service` (wait ~90s; `curl -s localhost:9091/health` or equivalent).

**Step 2:** Generate a long ElevenLabs TTS (text long enough to exceed the model char cap → multi-chunk, and long enough to historically exceed 60s). Use an `elevenlabs:` voice from `/tts/catalog`:
```bash
curl -s -X POST http://localhost:9091/tts -H "Content-Type: application/json" \
  -d '{"voice":"elevenlabs:<VOICE_ID>","return_json":true,"text":"<~3000+ chars of prose>"}' | python3 -m json.tool
```
Expected: `{"status":"success","audio_url":"/ui/uploads/...mp3","size_bytes":>0}` returned AFTER >60s if needed, with NO timeout. Confirm in `journalctl -u blackbox.service` there is NO `[ELEVENLABS] /tts synthesis failed` and the file plays.

**Step 3:** Report timing + size to the user. (No commit — validation only.)

---

# PART B1 — STT server-side Scribe reconnect-resume (backend, Python)

## Task B1a: Cross-rotation text-join helper (TDD)

**Files:** Modify `Orchestrator/stt/streaming.py`; Test `Orchestrator/tests/test_stt_join.py` (create).

**Step 1 — Failing test** `Orchestrator/tests/test_stt_join.py`:
```python
from Orchestrator.stt.streaming import join_transcript_segments as j

def test_empty_prefix_returns_text():
    assert j("", "hello world") == "hello world"

def test_joins_with_single_space():
    assert j("first part", "second part") == "first part second part"

def test_no_double_space_when_prefix_ends_with_space():
    assert j("first ", "second") == "first second"

def test_empty_text_returns_prefix():
    assert j("first", "") == "first"
```

**Step 2 — Run, expect FAIL.** `python -m pytest Orchestrator/tests/test_stt_join.py -q`

**Step 3 — Implement** in `Orchestrator/stt/streaming.py` (top-level):
```python
def join_transcript_segments(prefix: str, text: str) -> str:
    """Concatenate a carried-over transcript prefix with the current cumulative
    text, with exactly one separating space. Used to keep the transcript
    continuous across an STT provider session rotation (reconnect-and-resume)."""
    if not prefix:
        return text
    if not text:
        return prefix
    sep = "" if prefix.endswith((" ", "\n")) else " "
    return f"{prefix}{sep}{text}"
```

**Step 4 — Run, expect PASS (4).** **Step 5 — Commit:** `feat(stt): transcript-segment join helper for session-resume (tested)`.

---

## Task B1b: Refactor `_elevenlabs_bridge` to queue-based reconnect-resume

**Files:** Modify `Orchestrator/routes/stt_ws_routes.py` (replace `_elevenlabs_bridge`, lines 276-417). Test `Orchestrator/tests/test_stt_ws_reconnect.py` (create).

This is the highest-risk change. Use TDD: write the reconnect integration test with a fake upstream FIRST, then refactor until it passes.

**Step 1 — Failing test** `Orchestrator/tests/test_stt_ws_reconnect.py`. Build fakes: a fake client `WebSocket` that yields a scripted sequence of `receive_json` messages (audio frames then `stt_stop`) and records `send_json` calls; a fake `websockets.connect` returning fake upstream sockets — the FIRST emits a `partial_transcript` then a `session_time_limit_exceeded` error frame then closes; the SECOND emits a `partial_transcript` then (after the client commit) a `committed_transcript`. Assert: (a) `websockets.connect` was called TWICE (reconnect happened); (b) NO `stt_error` was sent to the client for the time-limit; (c) the final `stt_final` text contains BOTH the first session's words AND the second session's words (continuity via the prefix); (d) the client socket was NOT closed between sessions. (Model the fakes on the existing `Orchestrator/tests/test_stt_ws.py` patterns; reuse its helpers if present.)

**Step 2 — Run, expect FAIL.**

**Step 3 — Implement.** Replace the entire `_elevenlabs_bridge` function with the queue-based, reconnectable version:
```python
async def _elevenlabs_bridge(websocket: WebSocket, *, target, lang, sample_rate):
    """Bridge client PCM -> ElevenLabs Scribe realtime -> client deltas/finals,
    with TRANSPARENT reconnect-and-resume on Scribe's session_time_limit_exceeded.

    A single client_pump feeds an asyncio.Queue (audio survives a reconnect). An
    upstream-manager loop opens a Scribe session and relays it; when Scribe hits
    its session cap it closes, and the manager opens a FRESH session and resumes
    WITHOUT telling the client (no stt_final, socket stays open). A carried-over
    `prefix` keeps the cumulative transcript continuous across the rotation. A
    normal push-to-talk stop (stt_stop) commits + drains the single final.
    """
    key = resolve_api_key()
    if not key:
        await websocket.send_json({"type": "stt_error", "message": "ELEVENLABS_API_KEY not configured"})
        return

    url = (
        f"{WS_BASE_URL}/v1/speech-to-text/realtime"
        f"?model_id={config.ELEVENLABS_STT_STREAM_MODEL}"
        f"&sample_rate={sample_rate}&commit_strategy=manual"
    )
    if lang and lang != "auto":
        url += f"&language_code={lang}"

    audio_q: asyncio.Queue = asyncio.Queue()
    stop_evt = asyncio.Event()
    prefix = ""  # transcript delivered before any rotation (continuity across reconnects)

    async def client_pump():
        """Runs ONCE for the whole logical session: client -> queue."""
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            if mtype == "stt_audio":
                pcm = msg.get("pcm", "")
                if pcm:
                    await audio_q.put(("audio", pcm))
            elif mtype == "stt_stop":
                await audio_q.put(("stop", None))
                stop_evt.set()
                return

    async def run_epoch(el_ws) -> str:
        """Relay ONE Scribe connection. Returns 'rotate' (cap hit -> reconnect),
        'done' (stop final delivered / fatal error already surfaced)."""
        nonlocal prefix
        acc = InterimAccumulator()
        last_interim = {"text": ""}
        rotate = {"v": False}

        async def feeder():
            while True:
                kind, pcm = await audio_q.get()
                if kind == "audio":
                    await el_ws.send(_el_audio_msg(pcm, sample_rate, commit=False))
                else:  # stop
                    await el_ws.send(_el_audio_msg("", sample_rate, commit=True))
                    return

        async def reader():
            async for raw in el_ws:
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                err_code = classify_realtime_frame(event)
                if err_code:
                    print(f"[STT/WS] elevenlabs ERROR message: {json.dumps(event)[:500]}")
                    if err_code == "session_time_limit_exceeded":
                        rotate["v"] = True
                        return
                    await websocket.send_json({"type": "stt_error", "message": map_error(0, event)})
                    return
                m = acc.elevenlabs(event)
                if not m:
                    continue
                if m["type"] == "stt_final" and is_whisper_hallucination(m.get("text", "")):
                    continue
                if m["type"] == "stt_delta":
                    last_interim["text"] = m["text"]
                    m = {"type": "stt_delta", "text": join_transcript_segments(prefix, m["text"])}
                else:
                    m = {"type": "stt_final", "text": join_transcript_segments(prefix, m["text"])}
                m["target"] = target
                await websocket.send_json(m)
                if m["type"] == "stt_final" and stop_evt.is_set():
                    return

        feeder_task = asyncio.ensure_future(feeder())
        reader_task = asyncio.ensure_future(reader())
        try:
            done, _ = await asyncio.wait({feeder_task, reader_task}, return_when=asyncio.FIRST_COMPLETED)
            if feeder_task in done and reader_task not in done and not rotate["v"]:
                # client committed (stop): drain for the single final, 5s backstop.
                try:
                    await asyncio.wait_for(reader_task, timeout=5.0)
                except asyncio.TimeoutError:
                    reader_task.cancel()
                    try: await reader_task
                    except (asyncio.CancelledError, WebSocketDisconnect): pass
                    except Exception: pass
            else:
                feeder_task.cancel()
                try: await feeder_task
                except (asyncio.CancelledError, WebSocketDisconnect): pass
                except Exception: pass
            for t in (feeder_task, reader_task):
                if t.done() and not t.cancelled():
                    exc = t.exception()
                    if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                        raise exc
        finally:
            for t in (feeder_task, reader_task):
                if not t.done():
                    t.cancel()

        if rotate["v"] and not stop_evt.is_set():
            prefix = join_transcript_segments(prefix, last_interim["text"])
            return "rotate"
        return "done"

    pump_task = asyncio.ensure_future(client_pump())
    try:
        while not stop_evt.is_set():
            el_ws = await websockets.connect(
                url, additional_headers=auth_headers(key),
                open_timeout=10, ping_interval=20, ping_timeout=30, close_timeout=10,
            )
            print(f"[STT/WS] elevenlabs connected model={config.ELEVENLABS_STT_STREAM_MODEL} "
                  f"rate={sample_rate} commit_strategy=manual")
            try:
                result = await run_epoch(el_ws)
            finally:
                try: await el_ws.close()
                except Exception: pass
            if result == "rotate" and not stop_evt.is_set():
                print("[STT/WS] elevenlabs session_time_limit_exceeded — reconnecting & resuming")
                continue
            break
    finally:
        if not pump_task.done():
            pump_task.cancel()
            try: await pump_task
            except (asyncio.CancelledError, WebSocketDisconnect): pass
            except Exception: pass
```

Add `join_transcript_segments` to the existing import from `Orchestrator.stt.streaming` at the top of `stt_ws_routes.py` (find the `from Orchestrator.stt.streaming import ...` line and add it; if `InterimAccumulator` is imported there, add alongside).

**Step 4 — Run the reconnect test, expect PASS.** Also run the existing STT WS suite to confirm no regression: `python -m pytest Orchestrator/tests/test_stt_ws.py Orchestrator/tests/test_stt_ws_reconnect.py -q`.

**Step 5 — Commit:** `feat(stt): transparent reconnect-and-resume on ElevenLabs Scribe session cap`.

---

## Task B1c: Live STT validation (manual, on device/web)

**Step 1:** `sudo systemctl restart blackbox.service`. **Step 2:** With ElevenLabs as the STT provider, hold a mic session (web mic is easiest for this server-only check) past Scribe's session cap (the duration that previously cut you). CONFIRM: the session keeps transcribing across the cap (watch `journalctl -u blackbox.service` for `session_time_limit_exceeded — reconnecting & resuming`), no error, transcript stays continuous. (No commit — validation.)

---

# PART B2 — Android mic session isolation (Kotlin)

## Task B2a: Per-session epoch (drop stale events + preempt grace)

**Files:** Modify `.../data/voice/SttStreamClient.kt`.

**Step 1:** Add session-epoch + grace-job fields (after `private var captureJob: Job? = null`, ~line 71):
```kotlin
    // Monotonic id per start(); used to drop late events and skip teardown from a
    // previous session once a new one has begun (prevents cross-press bleed).
    @Volatile private var sessionEpoch = 0
    private var graceJob: Job? = null
```

**Step 2:** In `start()`, at the very top (before/replacing the `if (_isStreaming.value) return` early-return), preempt any lingering prior session and bump the epoch:
```kotlin
    fun start(provider: String = "", lang: String = "en", target: String = "prompt") {
        // Preempt any pending stop()-grace and fully tear down a prior session so
        // its socket/scope/late-events can't bleed into this one.
        graceJob?.cancel(); graceJob = null
        if (_isStreaming.value || connectionJob != null) {
            shutdown()
        }
        val epoch = ++sessionEpoch
        _isStreaming.value = true
        _amplitude.value = 0f
        val newScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        scope = newScope
        ...
```
Thread `epoch` into the message handler: change `is WsMessage.Text -> parseMessage(msg.text)` to `is WsMessage.Text -> parseMessage(msg.text, epoch)`, and guard the error emits in `WsMessage.Error`/the catch with `if (epoch == sessionEpoch)` before `_events.emit(...)`.

**Step 3:** Change `parseMessage` to take the epoch and drop stale events:
```kotlin
    private suspend fun parseMessage(raw: String, epoch: Int) {
        if (epoch != sessionEpoch) return   // a newer session started — drop late events
        ...
    }
```

**Step 4:** In `stop()`, capture the epoch and only tear down if still current; track the grace job:
```kotlin
        val s = scope
        if (s != null) {
            val epoch = sessionEpoch
            graceJob = s.launch {
                try {
                    wsClient.send(buildJsonObject { put("type", "stt_stop") }.toString())
                } catch (_: Exception) {}
                delay(STOP_GRACE_MS)
                if (epoch == sessionEpoch) shutdown()   // skip if a new session began
            }
        } else {
            shutdown()
        }
```

**Step 5:** Compile: `cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:compileDebugKotlin` → BUILD SUCCESSFUL.

**Step 6 — Commit** (`SttStreamClient.kt`): `fix(android): isolate STT sessions by epoch so leftover transcript can't bleed into the next press`.

---

## Task B2b: Build + on-device validation + snapshot

**Step 1:** `./gradlew :app:installDebug` on the Fold (BUILD SUCCESSFUL, Installed).

**Step 2 — On device:**
1. **No bleed:** record + stop, immediately re-press and record again → the new chip/paste contains ONLY the new utterance; nothing from the previous press leaks in. Repeat with long-press-cancel → re-press.
2. **Cap survival (with Part B1 live):** dictate past the old cutoff duration → the mic keeps going, transcript stays continuous, no mid-word death.
3. **Regression:** live partials still stream in the chip; final still pastes once on stop.

**Step 3:** Mint a dev snapshot (`/snapshot-dev`, operator resolved dynamically) covering all three fixes + plan path; update cross-session memory.

---

## Definition of done
- [ ] TTS: long generation completes (no 60s cut); only a true stall aborts. `test_elevenlabs_tts_stream.py` green; live smoke passed (A5).
- [ ] STT cap: `session_time_limit_exceeded` triggers transparent reconnect-resume; transcript continuous; no client error. `test_stt_ws_reconnect.py` green + existing STT WS suite green; live (B1c).
- [ ] Mic bleed: epoch-gating ships; re-press shows no leftover. Compile green; device-validated (B2b).
- [ ] Snapshot minted; memory updated. Pushed to origin/main after device validation (per workflow).
