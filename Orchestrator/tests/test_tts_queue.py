"""B1 — async on-box TTS queue (Orchestrator/tts_queue.py) + bundled audit fixes.

Covers:
  * submit -> queued -> generating -> done state walk (fake synthesize_batch)
  * queue_position ordering for 3 jobs
  * sub-batch progress ticks (subbatch / subbatches_total)
  * transient-failure auto-retry once, then failed + retryable
  * POST-/retry semantics (module retry() requeues a failed job)
  * cooperative cancel stops between sub-batches
  * route layer: non-qwen submit 400s, stack-off submit 503s, unknown 404s
  * bundled fix 1: script-aware settings.max_new_tokens_for (CJK 4.5 f/char)
  * bundled fix 2: deadline-based 429 retry in Orchestrator/qwen_tts.py

The GPU/member is never touched: qwen_tts.synthesize_batch / synthesize are
monkeypatched; requests.post is faked for the 429 tests. Everything runs with
the on-box stack OFF (dev-box state).
"""
import asyncio
import io
import pathlib
import sys
import threading
import time
import wave

import pytest
from unittest.mock import patch

# settings.py lives in the standalone server package (own lean venv in prod;
# importable from the repo for unit tests — same pattern as test_qwen_tts_server).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "LocalModels"))

from qwen_tts_server import settings  # noqa: E402

from Orchestrator import qwen_tts, tts_queue  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _wav(seconds=0.1, sr=16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x01" * int(sr * seconds))
    return buf.getvalue()


def _long_text(n_sentences=4, sent_chars=250) -> str:
    """n sentences of ~sent_chars so chunk_text_for_tts(max=300) yields one
    chunk per sentence."""
    body = "word " * ((sent_chars - 10) // 5)
    return " ".join(f"Sentence {i} {body.strip()}." for i in range(n_sentences))


async def _poll_until(tid, states, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = tts_queue.get_status(tid)
        if st and st["status"] in states:
            return st
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"task {tid} never reached {states}; last={tts_queue.get_status(tid)}")


@pytest.fixture
def q(monkeypatch, tmp_path):
    tts_queue._reset_for_tests()
    monkeypatch.setattr(tts_queue, "_uploads_dir", lambda: tmp_path)
    monkeypatch.setenv("QWEN_TTS_MAX_BATCH", "2")   # small sub-batches for tests
    yield tts_queue
    tts_queue._reset_for_tests()


# ---------------------------------------------------------------------------
# state walk: queued -> generating -> done
# ---------------------------------------------------------------------------
def test_submit_queued_generating_done_walk(q, tmp_path, monkeypatch):
    release = threading.Event()
    started = threading.Event()
    calls = []

    def fake_batch(voice, texts, response_format="wav"):
        calls.append(list(texts))
        started.set()
        assert release.wait(5)
        return [_wav() for _ in texts]

    monkeypatch.setattr(qwen_tts, "synthesize_batch", fake_batch)

    async def scenario():
        job = q.submit(text="Hello there, this is a short reply.", voice="Vivian",
                       operator="tester")
        tid = job["task_id"]
        assert job["status"] == "queued"
        assert job["queue_position"] == 1
        # worker picks it up -> generating while the fake blocks
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: started.wait(5))
        st = await _poll_until(tid, ("generating",))
        assert st["queue_position"] == 1
        assert st["eta_s"] >= 0
        release.set()
        st = await _poll_until(tid, ("done",))
        assert st["audio_url"].startswith("/ui/uploads/")
        assert st["bytes"] > 0
        assert st["seconds"] > 0
        assert (tmp_path / st["audio_url"].rsplit("/", 1)[1]).is_file()
        assert st["subbatch"] == st["subbatches_total"] == 1
    asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0][0].startswith("Hello there")


def test_queue_position_ordering_three_jobs(q, monkeypatch):
    release = threading.Event()

    def fake_batch(voice, texts, response_format="wav"):
        assert release.wait(5)
        return [_wav() for _ in texts]

    monkeypatch.setattr(qwen_tts, "synthesize_batch", fake_batch)

    async def scenario():
        t1 = q.submit(text="First reply.", voice="Vivian")["task_id"]
        t2 = q.submit(text="Second reply.", voice="Vivian")["task_id"]
        j3 = q.submit(text="Third reply.", voice="Vivian")
        assert j3["queue_position"] == 3
        await asyncio.sleep(0.05)  # let the worker start job 1
        assert tts_queue.get_status(t1)["queue_position"] == 1
        assert tts_queue.get_status(t2)["queue_position"] == 2
        assert tts_queue.get_status(j3["task_id"])["queue_position"] == 3
        summary = tts_queue.queue_status()
        assert summary["queue_length"] == 3
        release.set()
        for tid in (t1, t2, j3["task_id"]):
            await _poll_until(tid, ("done",))
        assert tts_queue.queue_status()["queue_length"] == 0
    asyncio.run(scenario())


def test_subbatch_progress_ticks(q, monkeypatch):
    """QWEN_TTS_MAX_BATCH=2 + 4 chunks -> 2 sub-batches; the job's subbatch
    counter ticks after each sub-batch completes."""
    gate = threading.Semaphore(0)
    entered = threading.Semaphore(0)

    def fake_batch(voice, texts, response_format="wav"):
        entered.release()
        assert gate.acquire(timeout=5)
        return [_wav() for _ in texts]

    monkeypatch.setattr(qwen_tts, "synthesize_batch", fake_batch)

    async def scenario():
        loop = asyncio.get_running_loop()
        tid = q.submit(text=_long_text(4), voice="Serena")["task_id"]
        await loop.run_in_executor(None, lambda: entered.acquire(timeout=5))
        st = await _poll_until(tid, ("generating",))
        assert st["subbatches_total"] == 2
        assert st["subbatch"] == 0
        gate.release()   # finish sub-batch 1
        await loop.run_in_executor(None, lambda: entered.acquire(timeout=5))
        deadline = time.monotonic() + 5
        while tts_queue.get_status(tid)["subbatch"] != 1:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        gate.release()   # finish sub-batch 2
        st = await _poll_until(tid, ("done",))
        assert st["subbatch"] == 2
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# reliability: auto-retry once, failed+retryable, manual retry, cancel
# ---------------------------------------------------------------------------
def test_transient_failure_auto_retries_once_then_succeeds(q, monkeypatch):
    calls = []

    def fake_batch(voice, texts, response_format="wav"):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Qwen TTS batch failed (HTTP 429): busy")
        return [_wav() for _ in texts]

    monkeypatch.setattr(qwen_tts, "synthesize_batch", fake_batch)

    async def scenario():
        tid = q.submit(text="Retry me once.", voice="Vivian")["task_id"]
        st = await _poll_until(tid, ("done", "failed"))
        assert st["status"] == "done"
    asyncio.run(scenario())
    assert len(calls) == 2   # first attempt + exactly one auto-retry


def test_transient_failure_twice_marks_failed_retryable(q, monkeypatch):
    calls = []

    def fake_batch(voice, texts, response_format="wav"):
        calls.append(1)
        raise RuntimeError("Qwen TTS batch failed (HTTP 502): dead")

    monkeypatch.setattr(qwen_tts, "synthesize_batch", fake_batch)

    async def scenario():
        tid = q.submit(text="Doomed job.", voice="Vivian")["task_id"]
        st = await _poll_until(tid, ("failed",))
        assert st["retryable"] is True
        assert "dead" in st["error"]
        assert st["queue_position"] == 0
    asyncio.run(scenario())
    assert len(calls) == 2   # auto-retry fired exactly once, then gave up


def test_retry_requeues_failed_job(q, monkeypatch):
    calls = []

    def fake_batch(voice, texts, response_format="wav"):
        calls.append(1)
        if len(calls) <= 2:   # first attempt + auto-retry both fail
            raise RuntimeError("transient")
        return [_wav() for _ in texts]

    monkeypatch.setattr(qwen_tts, "synthesize_batch", fake_batch)

    async def scenario():
        tid = q.submit(text="Manual retry works.", voice="Vivian")["task_id"]
        await _poll_until(tid, ("failed",))
        out = tts_queue.retry(tid)
        assert out["status"] == "queued"
        st = await _poll_until(tid, ("done",))
        assert st["audio_url"]
    asyncio.run(scenario())


def test_retry_rejects_non_failed_and_unknown(q, monkeypatch):
    monkeypatch.setattr(qwen_tts, "synthesize_batch",
                        lambda v, t, response_format="wav": [_wav() for _ in t])

    async def scenario():
        tid = q.submit(text="Fine job.", voice="Vivian")["task_id"]
        await _poll_until(tid, ("done",))
        assert tts_queue.retry(tid) is None        # not failed -> refused
        assert tts_queue.retry("ttsq-nope") is None
    asyncio.run(scenario())


def test_cancel_stops_between_subbatches(q, monkeypatch):
    """2 sub-batches; cancel lands while sub-batch 1 runs -> the worker stops
    at the boundary: exactly ONE member call, terminal status cancelled."""
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def fake_batch(voice, texts, response_format="wav"):
        calls.append(list(texts))
        entered.set()
        assert release.wait(5)
        return [_wav() for _ in texts]

    monkeypatch.setattr(qwen_tts, "synthesize_batch", fake_batch)

    async def scenario():
        loop = asyncio.get_running_loop()
        tid = q.submit(text=_long_text(4), voice="Serena")["task_id"]
        await loop.run_in_executor(None, lambda: entered.wait(5))
        out = tts_queue.cancel(tid)
        assert out["cancelled"] is True
        release.set()
        st = await _poll_until(tid, ("cancelled",))
        assert st["status"] == "cancelled"
    asyncio.run(scenario())
    assert len(calls) == 1   # sub-batch 2 never dispatched


def test_cancel_queued_job_immediate_and_terminal_idempotent(q, monkeypatch):
    release = threading.Event()

    def fake_batch(voice, texts, response_format="wav"):
        assert release.wait(5)
        return [_wav() for _ in texts]

    monkeypatch.setattr(qwen_tts, "synthesize_batch", fake_batch)

    async def scenario():
        t1 = q.submit(text="Occupies the worker.", voice="Vivian")["task_id"]
        t2 = q.submit(text="Cancelled while queued.", voice="Vivian")["task_id"]
        await asyncio.sleep(0.05)
        out = tts_queue.cancel(t2)
        assert out["cancelled"] is True
        assert tts_queue.get_status(t2)["status"] == "cancelled"
        release.set()
        st1 = await _poll_until(t1, ("done",))
        assert st1["status"] == "done"
        # cancelling a terminal job is a no-op report, never an exception
        again = tts_queue.cancel(t2)
        assert again["cancelled"] is False and again["already_terminal"] is True
        assert tts_queue.cancel("ttsq-nope") is None
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# route layer (full app) — validation + inert-when-stack-off
# ---------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    tts_queue._reset_for_tests()
    monkeypatch.setattr(tts_queue, "_uploads_dir", lambda: tmp_path)
    with patch("Orchestrator.toolvault.embeddings.sync_embeddings") as m_emb:
        m_emb.return_value = {"x": {"vector": [0.1]}}
        from Orchestrator.app import app
        with TestClient(app) as c:
            yield c
    tts_queue._reset_for_tests()


def test_queue_submit_non_qwen_400(client):
    resp = client.post("/tts/queue", json={"text": "Hi.", "voice": "onyx",
                                           "provider": "openai"})
    assert resp.status_code == 400
    # cloud providers stay synchronous — the queue must say so
    assert "qwen" in resp.json()["detail"].lower()


def test_queue_submit_stack_off_503(client):
    """Inert when the on-box stack is off (the dev-box state): clean 503, no job."""
    with patch("Orchestrator.qwen_tts._tts_available", return_value=False):
        resp = client.post("/tts/queue", json={"text": "Hi.", "voice": "qwen:Vivian"})
    assert resp.status_code == 503


def test_queue_submit_missing_text_400(client):
    with patch("Orchestrator.qwen_tts._tts_available", return_value=True):
        resp = client.post("/tts/queue", json={"voice": "qwen:Vivian"})
    assert resp.status_code == 400


def test_queue_routes_full_walk(client):
    with patch("Orchestrator.qwen_tts._tts_available", return_value=True), \
         patch("Orchestrator.qwen_tts.synthesize_batch",
               side_effect=lambda v, t, response_format="wav": [_wav() for _ in t]) as mb:
        resp = client.post("/tts/queue", json={"text": "Route walk test.",
                                               "voice": "qwen:Vivian",
                                               "operator": "tester"})
        assert resp.status_code == 200
        body = resp.json()
        tid = body["task_id"]
        assert body["status"] == "queued"
        assert body["queue_position"] == 1
        deadline = time.monotonic() + 5
        st = None
        while time.monotonic() < deadline:
            st = client.get(f"/tts/task/{tid}").json()
            if st["status"] in ("done", "failed"):
                break
            time.sleep(0.02)
        assert st["status"] == "done"
        assert st["audio_url"].startswith("/ui/uploads/")
        assert mb.call_args[0][0] == "Vivian"   # qwen: prefix stripped
    qs = client.get("/tts/queue/status").json()
    assert qs["queue_length"] == 0
    assert any(j["task_id"] == tid for j in qs["jobs"])
    # unknown ids 404 on every task route
    assert client.get("/tts/task/ttsq-nope").status_code == 404
    assert client.post("/tts/task/ttsq-nope/retry").status_code == 404
    assert client.post("/tts/task/ttsq-nope/cancel").status_code == 404
    # retrying a non-failed job is a 409
    assert client.post(f"/tts/task/{tid}/retry").status_code == 409


# ---------------------------------------------------------------------------
# bundled fix 1 — script-aware max_new_tokens_for (CJK 4.5 frames/char)
# ---------------------------------------------------------------------------
def test_max_new_tokens_ascii_rate_unchanged(monkeypatch):
    monkeypatch.delenv("QWEN_TTS_FRAMES_PER_CHAR", raising=False)
    monkeypatch.delenv("QWEN_TTS_FRAMES_PER_CHAR_CJK", raising=False)
    text = "a" * 400
    assert settings.max_new_tokens_for(text) == 400 * 2 + 256


def test_max_new_tokens_cjk_counts_at_4_5(monkeypatch):
    monkeypatch.delenv("QWEN_TTS_FRAMES_PER_CHAR_CJK", raising=False)
    # 200 CJK Unified chars: int(200*4.5)+256 = 1156 (vs 656 at the ASCII rate,
    # which starved real Chinese chunks of frame budget)
    text = "你" * 200
    assert settings.max_new_tokens_for(text) == int(200 * 4.5) + 256


def test_max_new_tokens_hiragana_katakana_hangul_are_cjk():
    hira, kata, hangul = "あ" * 100, "ア" * 100, "가" * 100
    for text in (hira, kata, hangul):
        assert settings.max_new_tokens_for(text) == int(100 * 4.5) + 256


def test_max_new_tokens_mixed_script(monkeypatch):
    monkeypatch.delenv("QWEN_TTS_FRAMES_PER_CHAR", raising=False)
    monkeypatch.delenv("QWEN_TTS_FRAMES_PER_CHAR_CJK", raising=False)
    text = "中" * 100 + "x" * 100
    assert settings.max_new_tokens_for(text) == int(100 * 4.5 + 100 * 2.0) + 256


def test_max_new_tokens_env_overrides_stay(monkeypatch):
    monkeypatch.setenv("QWEN_TTS_FRAMES_PER_CHAR", "3.0")
    monkeypatch.setenv("QWEN_TTS_FRAMES_PER_CHAR_CJK", "6.0")
    text = "中" * 10 + "x" * 10
    assert settings.max_new_tokens_for(text) == int(10 * 6.0 + 10 * 3.0) + 256


def test_max_new_tokens_floor_and_ceiling_hold(monkeypatch):
    monkeypatch.delenv("QWEN_TTS_MAX_NEW_TOKENS", raising=False)
    assert settings.max_new_tokens_for("") == 256              # floor
    assert settings.max_new_tokens_for("一" * 5000) == 3072  # ceiling backstop


# ---------------------------------------------------------------------------
# bundled fix 2 — deadline-based 429 retry (Orchestrator/qwen_tts.py)
# ---------------------------------------------------------------------------
class _FakeClock:
    """monotonic() advances only when sleep() is called."""
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def _resp(status):
    class R:
        status_code = status
        content = b"x"
        text = "too many requests" if status == 429 else ""
    return R()


def test_429_retry_is_deadline_based_not_count_based(monkeypatch):
    """Default 60s deadline outlasts the old 5-attempt/13.5s cap: with a member
    busy for a real synth, we keep retrying well past 5 attempts."""
    clock = _FakeClock()
    monkeypatch.setattr(qwen_tts, "_monotonic", clock.monotonic)
    monkeypatch.setattr(qwen_tts, "_sleep", clock.sleep)
    monkeypatch.delenv("QWEN_TTS_429_DEADLINE_S", raising=False)
    posts = []
    with patch("Orchestrator.qwen_tts.requests.post",
               side_effect=lambda *a, **k: (posts.append(1), _resp(429))[1]), \
         patch("Orchestrator.qwen_tts._base_url", return_value="http://x/v1"):
        r = qwen_tts.synthesize("Vivian", "hello")
    assert r.status_code == 429           # eventually surfaced, never raised
    assert len(posts) > 6                 # old fixed cap was 1+5 = 6 posts
    assert clock.now >= 60.0              # retried until the deadline elapsed


def test_429_deadline_env_override_and_recovery(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(qwen_tts, "_monotonic", clock.monotonic)
    monkeypatch.setattr(qwen_tts, "_sleep", clock.sleep)
    monkeypatch.setenv("QWEN_TTS_429_DEADLINE_S", "10")
    responses = [_resp(429), _resp(429), _resp(200)]
    with patch("Orchestrator.qwen_tts.requests.post",
               side_effect=lambda *a, **k: responses.pop(0)), \
         patch("Orchestrator.qwen_tts._base_url", return_value="http://x/v1"):
        r = qwen_tts.synthesize("Vivian", "hello")
    assert r.status_code == 200           # recovers as soon as the member frees
    assert clock.slept == [0.5, 1.0]      # capped exponential backoff unchanged

    clock2 = _FakeClock()
    monkeypatch.setattr(qwen_tts, "_monotonic", clock2.monotonic)
    monkeypatch.setattr(qwen_tts, "_sleep", clock2.sleep)
    posts = []
    with patch("Orchestrator.qwen_tts.requests.post",
               side_effect=lambda *a, **k: (posts.append(1), _resp(429))[1]), \
         patch("Orchestrator.qwen_tts._base_url", return_value="http://x/v1"):
        r = qwen_tts.synthesize("Vivian", "hello")
    assert r.status_code == 429
    # deadline 10s: sleeps 0.5+1+2+4 = 7.5 (<10, retry) then +6 = 13.5 (>=10, stop)
    assert clock2.slept == [0.5, 1.0, 2.0, 4.0, 6.0]
    assert len(posts) == 6


def test_429_deadline_applies_to_batch_too(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(qwen_tts, "_monotonic", clock.monotonic)
    monkeypatch.setattr(qwen_tts, "_sleep", clock.sleep)
    monkeypatch.setenv("QWEN_TTS_429_DEADLINE_S", "3")
    with patch("Orchestrator.qwen_tts.requests.post", side_effect=lambda *a, **k: _resp(429)), \
         patch("Orchestrator.qwen_tts._base_url", return_value="http://x/v1"):
        with pytest.raises(RuntimeError):   # non-200 after deadline -> RuntimeError
            qwen_tts.synthesize_batch("Vivian", ["a", "b"])
    assert clock.now >= 3.0
