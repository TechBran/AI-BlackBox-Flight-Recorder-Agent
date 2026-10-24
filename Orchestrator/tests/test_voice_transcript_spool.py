"""Failed voice-transcript saves are spooled and retried (audit rank 51,
O09-live-voice-providers-13).

save_session_to_blackbox kept a failed transcript "for a later retry" that
nothing ever ran: the reaper evicted the session without saving, so a service
restart, a 5xx, the 30 s client timeout while perform_mint embedded, or the
phone bridge's 10 s save timeout lost the whole call. Pins:

  * the transcript is spooled under the ledger root BEFORE the POST (write-
    ahead), keyed by session id, and leaves the spool on a 200;
  * a failed, timed-out or cancelled save leaves it spooled;
  * the reaper's retry posts it again, drops it when the ledger already holds
    it (a save whose 200 never arrived), parks a permanent refusal and backs off;
  * the first retry pass runs at startup;
  * reap() saves a session that still holds turns before evicting it.

Hermetic: the spool and the ledger live in tmp_path; /chat/save is a fake.
"""
import asyncio
import json
import time
from datetime import datetime, timezone, timedelta

import pytest

import Orchestrator.live_session_reaper as reaper
import Orchestrator.routes.gemini_live_routes as glr
import Orchestrator.routes.grok_live_routes as gk
import Orchestrator.routes.realtime_routes as rt
import Orchestrator.routes.voice_ws_shared as shared
from Orchestrator import voice_transcript_spool as spool
from Orchestrator.models import GeminiLiveSession, GrokLiveSession, RealtimeSession


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(spool, "SPOOL_DIR", tmp_path / "Volumes" / "pending_transcripts")
    ledger = tmp_path / "Volumes" / "SNAPSHOT_VOLUME.txt"
    ledger.parent.mkdir(parents=True)
    ledger.write_bytes(b"=== SNAP-1 ===\nolder snapshots\n")
    monkeypatch.setattr(spool, "LEDGER_PATH", ledger)
    return ledger


class _ChatSave:
    """Fake POST /chat/save: answers with `status`; records bodies."""

    def __init__(self, monkeypatch, status=200, ledger=None, hang=False):
        self.status = status
        self.bodies = []
        self.ledger = ledger
        self.hang = hang
        monkeypatch.setattr(shared, "_post_chat_save", self)

    async def __call__(self, body, log_prefix):
        self.bodies.append(body)
        if self.ledger is not None:   # the server minted it, whatever the client sees
            with open(self.ledger, "ab") as f:  # noqa: ASYNC230 - a test double appending a few bytes
                f.write(("\n" + body["assistant_response"] + "\n").encode())
        if self.hang:
            await asyncio.sleep(3600)
        return self.status


ROUTES = [
    pytest.param(glr.save_session_to_blackbox, GeminiLiveSession, "gemini-live-voice", id="gemini"),
    pytest.param(gk.save_grok_session_to_blackbox, GrokLiveSession, "grok-live-voice", id="grok"),
    pytest.param(rt.save_session_to_blackbox, RealtimeSession, "openai-realtime-voice", id="openai"),
]


def _session(cls, sid="voice-sid-1"):
    s = cls(session_id=sid, operator="system")
    s.conversation = [
        {"role": "user", "content": "remind me at noon", "timestamp": "2026-10-23T11:00:00Z"},
        {"role": "assistant", "content": "Done.", "timestamp": "2026-10-23T11:00:02Z"},
    ]
    return s


# ---------------------------------------------------------------------------
@pytest.mark.parametrize("save,cls,label", ROUTES)
def test_failed_save_is_spooled_under_the_ledger_root(box, monkeypatch, save, cls, label):
    _ChatSave(monkeypatch, status=503)
    session = _session(cls)

    asyncio.run(save(session))

    records = list(spool.pending())
    assert len(records) == 1, "a failed save must leave the transcript in the spool"
    record = records[0]
    assert record["session_id"] == "voice-sid-1"
    assert record["operator"] == "system"
    assert record["model"] == label
    assert "remind me at noon" in record["assistant_response"]
    path = spool.record_path(record)
    assert path.parent == box.parent / "pending_transcripts"
    assert path.name.startswith("voice-sid-1--")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert session.conversation == [], "the spool owns the turns now; no double save"


@pytest.mark.parametrize("save,cls,label", ROUTES)
def test_successful_save_leaves_nothing_spooled(box, monkeypatch, save, cls, label):
    fake = _ChatSave(monkeypatch, status=200)
    session = _session(cls)
    asyncio.run(save(session))
    assert list(spool.pending()) == []
    assert session.conversation == []
    body = fake.bodies[0]
    assert spool.MARKER_PREFIX in body["assistant_response"]


def test_cancelled_save_is_still_spooled(box, monkeypatch):
    """The phone bridge wraps the save in a 10 s wait_for: the cancel landed
    mid-POST and the session was then popped, so the call was lost."""
    _ChatSave(monkeypatch, hang=True)
    session = _session(RealtimeSession, "phone-call-7")

    async def run():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(rt.save_session_to_blackbox(session), timeout=0.2)

    asyncio.run(run())
    assert [r["session_id"] for r in spool.pending()] == ["phone-call-7"]


@pytest.mark.parametrize("save,cls,label", ROUTES)
def test_a_cancelled_save_releases_the_turns_it_spooled(box, monkeypatch, save, cls, label):
    """Once spooled the turns are safe, so memory drops them even if the POST is
    then cancelled: kept, the next save (reaper, teardown) posted them twice."""
    _ChatSave(monkeypatch, hang=True)
    session = _session(cls, "phone-call-8")

    async def run():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(save(session), timeout=0.2)

    asyncio.run(run())
    assert session.conversation == []
    assert len(list(spool.pending())) == 1


@pytest.mark.parametrize("save,cls,label", ROUTES)
def test_a_turn_added_while_the_save_runs_is_kept(box, monkeypatch, save, cls, label):
    session = _session(cls)

    async def post(body, log_prefix):
        session.conversation.append({"role": "user", "content": "one more thing",
                                     "timestamp": "2026-10-23T11:00:09Z"})
        return 200

    monkeypatch.setattr(shared, "_post_chat_save", post)
    asyncio.run(save(session))
    assert [m["content"] for m in session.conversation] == ["one more thing"]


def test_retry_posts_the_spooled_transcript_and_clears_it(box, monkeypatch):
    _ChatSave(monkeypatch, status=503)
    asyncio.run(rt.save_session_to_blackbox(_session(RealtimeSession)))
    [record] = list(spool.pending())

    fake = _ChatSave(monkeypatch, status=200)
    counts = asyncio.run(shared.retry_spooled_transcripts(now=time.time() + 3600))

    assert counts["saved"] == 1
    assert list(spool.pending()) == []
    assert fake.bodies == [spool.chat_save_body(record)]


def test_fresh_entry_waits_for_its_own_post_to_settle(box, monkeypatch):
    _ChatSave(monkeypatch, status=503)
    asyncio.run(rt.save_session_to_blackbox(_session(RealtimeSession)))
    fake = _ChatSave(monkeypatch, status=200)
    asyncio.run(shared.retry_spooled_transcripts())
    assert fake.bodies == [], "retried before its own POST could have finished minting"
    assert len(list(spool.pending())) == 1


def test_retry_does_not_duplicate_a_transcript_the_server_already_minted(box, monkeypatch):
    """The client timed out (or was cancelled) but the server minted anyway."""
    _ChatSave(monkeypatch, status=None, ledger=box)   # no response, but minted
    asyncio.run(gk.save_grok_session_to_blackbox(_session(GrokLiveSession)))
    assert len(list(spool.pending())) == 1

    fake = _ChatSave(monkeypatch, status=200)
    counts = asyncio.run(shared.retry_spooled_transcripts(now=time.time() + 3600))

    assert counts["already_saved"] == 1
    assert fake.bodies == [], "re-posting a minted transcript duplicates the snapshot"
    assert list(spool.pending()) == []


def test_ledger_scan_finds_a_marker_split_across_chunks(box, monkeypatch):
    record = spool.new_record(session_id="s", operator="system", user_message="u",
                              session_summary="x", model_label="m", log_prefix="[T]")
    marker = f"{spool.MARKER_PREFIX}{record['transcript_id']}".encode()
    monkeypatch.setattr(spool, "_SCAN_CHUNK", 16)
    with open(box, "ab") as f:
        f.write(b"y" * 37 + marker + b"\n")
    assert spool.ledger_has(record) is True
    other = dict(record, transcript_id="0" * 32)
    assert spool.ledger_has(other) is False


def test_failed_retry_backs_off(box, monkeypatch):
    _ChatSave(monkeypatch, status=503)
    asyncio.run(rt.save_session_to_blackbox(_session(RealtimeSession)))
    later = time.time() + 3600

    fake = _ChatSave(monkeypatch, status=502)
    counts = asyncio.run(shared.retry_spooled_transcripts(now=later))
    assert counts["failed"] == 1
    [record] = list(spool.pending())
    assert record["attempts"] == 1
    assert record["next_attempt_at"] > later

    asyncio.run(shared.retry_spooled_transcripts(now=later + 1))
    assert len(fake.bodies) == 1, "retried again before its backoff ran out"


def test_permanent_refusal_is_parked_not_retried_forever(box, monkeypatch):
    _ChatSave(monkeypatch, status=503)
    asyncio.run(rt.save_session_to_blackbox(_session(RealtimeSession)))
    fake = _ChatSave(monkeypatch, status=400)
    counts = asyncio.run(shared.retry_spooled_transcripts(now=time.time() + 3600))
    assert counts["rejected"] == 1
    assert list(spool.pending()) == []
    parked = list((box.parent / "pending_transcripts").glob("*.rejected.json"))
    assert len(parked) == 1, "a refused transcript stays on disk for recovery"
    asyncio.run(shared.retry_spooled_transcripts(now=time.time() + 7200))
    assert len(fake.bodies) == 1


def test_a_server_that_keeps_failing_gets_the_transcript_parked(box, monkeypatch):
    """/chat/save appends the turns to the operator's log BEFORE it mints and
    answers 500 when the mint raises, so every re-post of such a transcript
    appended it again: it was retried hourly with no end (review of rank 51)."""
    fake = _ChatSave(monkeypatch, status=500)
    asyncio.run(rt.save_session_to_blackbox(_session(RealtimeSession)))

    async def sweeps():
        now = time.time()
        for _ in range(40):
            now += 4000                      # every sweep is past the 1 h backoff cap
            await shared.retry_spooled_transcripts(now)

    asyncio.run(sweeps())
    assert len(fake.bodies) == spool.MAX_SERVER_ERRORS, \
        "the first save and its retries stop once the server has failed it MAX_SERVER_ERRORS times"
    assert list(spool.pending()) == []
    parked = list((box.parent / "pending_transcripts").glob("*" + spool.GAVE_UP_SUFFIX))
    assert len(parked) == 1, "a transcript given up on stays on disk for recovery"
    assert "remind me at noon" in parked[0].read_text()


def test_retries_without_any_answer_end_too(box, monkeypatch):
    fake = _ChatSave(monkeypatch, status=None)
    asyncio.run(rt.save_session_to_blackbox(_session(RealtimeSession)))

    async def sweeps():
        now = time.time()
        for _ in range(spool.MAX_ATTEMPTS + 10):
            now += 4000
            await shared.retry_spooled_transcripts(now)

    asyncio.run(sweeps())
    assert len(fake.bodies) == spool.MAX_ATTEMPTS + 1   # the first save + its retries
    assert list(spool.pending()) == []
    assert len(list((box.parent / "pending_transcripts").glob("*" + spool.GAVE_UP_SUFFIX))) == 1


def test_a_transient_server_error_is_still_retried(box, monkeypatch):
    _ChatSave(monkeypatch, status=503)
    asyncio.run(rt.save_session_to_blackbox(_session(RealtimeSession)))
    [record] = list(spool.pending())
    assert record["server_errors"] == 1, "the first save's 5xx counts"
    fake = _ChatSave(monkeypatch, status=200)
    counts = asyncio.run(shared.retry_spooled_transcripts(now=time.time() + 3600))
    assert counts["saved"] == 1 and len(fake.bodies) == 1
    assert list(spool.pending()) == []


def test_startup_pass_retries_without_waiting_for_the_schedule(box, monkeypatch):
    _ChatSave(monkeypatch, status=503)
    asyncio.run(rt.save_session_to_blackbox(_session(RealtimeSession)))
    fake = _ChatSave(monkeypatch, status=200)

    async def run():
        task = asyncio.create_task(reaper._reaper_loop(interval=3600, startup_delay=0))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if fake.bodies:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert len(fake.bodies) == 1, "the startup pass must retry what a restart left spooled"
    assert list(spool.pending()) == []


def test_spool_dir_defaults_under_the_ledger_root(monkeypatch):
    from pathlib import Path
    from Orchestrator.config import VOL_PATH
    monkeypatch.setattr(spool, "SPOOL_DIR", None)
    assert spool.spool_dir() == Path(VOL_PATH).parent / "pending_transcripts"


# ---------------------------------------------------------------------------
# reap(): save before evicting
# ---------------------------------------------------------------------------
NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)


def _stale(cls, sid, turns=True, operator="system", age=reaper.DISCONNECTED_GRACE_SEC + 60):
    s = cls(session_id=sid, operator=operator, status="disconnected")
    s.last_activity = (NOW - timedelta(seconds=age)).isoformat()
    if turns:
        s.conversation = [{"role": "user", "content": "hi", "timestamp": "t"}]
    return s


def test_reap_saves_a_session_with_turns_before_evicting_it():
    saved = []

    async def save(session):
        saved.append(session.session_id)
        session.conversation = []

    sessions = {"a": _stale(GeminiLiveSession, "a"), "b": _stale(GeminiLiveSession, "b", turns=False)}
    removed = asyncio.run(reaper.reap(sessions, NOW.timestamp(), save=save))
    assert saved == ["a"]
    assert sorted(removed) == ["a", "b"]
    assert sessions == {}


def test_reap_keeps_a_session_whose_turns_could_not_be_saved():
    async def save(session):
        return None    # neither saved nor spooled: the conversation stays

    sessions = {"a": _stale(RealtimeSession, "a")}
    removed = asyncio.run(reaper.reap(sessions, NOW.timestamp(), save=save))
    assert removed == []
    assert "a" in sessions, "evicting would drop the only copy of those turns"

    # ...but not forever.
    sessions["a"].last_activity = (NOW - timedelta(seconds=reaper.UNSAVED_MAX_AGE_SEC + 1)).isoformat()
    assert asyncio.run(reaper.reap(sessions, NOW.timestamp(), save=save)) == ["a"]


def test_reap_evicts_an_unsavable_operatorless_session():
    async def save(session):
        raise AssertionError("a session with no operator cannot be saved")

    sessions = {"a": _stale(GrokLiveSession, "a", operator="")}
    assert asyncio.run(reaper.reap(sessions, NOW.timestamp(), save=save)) == ["a"]


def test_reap_all_saves_through_the_route_and_retries_the_spool(box, monkeypatch):
    from Orchestrator.models import REALTIME_SESSIONS
    fake = _ChatSave(monkeypatch, status=200)
    session = _stale(RealtimeSession, "reap-all-1")
    session.last_activity = (datetime.now(timezone.utc)
                             - timedelta(seconds=reaper.DISCONNECTED_GRACE_SEC + 60)).isoformat()
    REALTIME_SESSIONS["reap-all-1"] = session
    retried = []

    async def fake_retry(*, ignore_schedule=False):
        retried.append(ignore_schedule)
        return {}

    monkeypatch.setattr(reaper, "retry_transcripts", fake_retry)
    try:
        counts = asyncio.run(reaper.reap_all())
    finally:
        REALTIME_SESSIONS.pop("reap-all-1", None)
    assert counts.get("realtime") == 1
    assert len(fake.bodies) == 1 and "hi" in fake.bodies[0]["assistant_response"]
    assert retried == [False]
