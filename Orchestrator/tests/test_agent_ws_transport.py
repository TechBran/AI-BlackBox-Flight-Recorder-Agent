"""The Claude and Gemini agent sockets, end to end against a fake CLI (audit rank 56:
O06-frontier-agent-13, -03, -04, -16, -27).

Each test drives the real WebSocket handler, which spawns a small stand-in for
the `claude` / `gemini` binary (a Python script under tmp_path). The stand-in
records its argv, prints stream-json the way the real CLI does and can be told
to wait for a gate file, flood stderr or start a long-lived tool process.
Hermetic: no network, no real CLI, no ledger writes.

Pinned:
  * a follow-up resumes THIS conversation by id (--resume <id>), never
    --continue / --resume latest, which pick the most recent conversation in
    the shared working directory: another operator's;
  * end_session as a socket's first frame answers session_ended (it used to
    raise UnboundLocalError on `operator`);
  * every attached socket gets the whole stream (one shared cursor used to
    split the lines between them);
  * a CLI that writes a lot to stderr still finishes, and a failed run says why;
  * ending a running session kills the tools the CLI started, not just the CLI;
  * a resume whose conversation the CLI no longer has (an id saved before any
    turn ran, a transcript Claude Code cleaned up) starts a new conversation in
    the same turn instead of failing every turn after it;
  * a client that reconnects without a position gets what was written while it
    was away; a position from an earlier turn reaches the running one;
  * after the reaper evicts a session, the same socket session resumes its
    conversation and a new session of that operator starts fresh;
  * input frames get the same operator check as prompts; a missing working
    directory is reported as that, not as a missing CLI.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import OrderedDict
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import Orchestrator.routes.agent_routes as ar
import Orchestrator.routes.gemini_agent_routes as gr

pytestmark = pytest.mark.timeout(60)


FAKE_CLAUDE = r'''
import json, os, subprocess, sys, time, uuid
d = os.environ["FAKE_CLI_DIR"]
argv = sys.argv[1:]
with open(os.path.join(d, "argv.jsonl"), "a") as fh:
    fh.write(json.dumps(argv) + "\n")
with open(os.path.join(d, "cwd.jsonl"), "a") as fh:
    fh.write(json.dumps(os.getcwd()) + "\n")
mode_path = os.path.join(d, "mode.json")
mode = json.load(open(mode_path)) if os.path.exists(mode_path) else {}
sid = None
for flag in ("--session-id", "--resume"):
    if flag in argv:
        sid = argv[argv.index(flag) + 1]
if sid is None:
    sid = str(uuid.uuid4())
def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
if mode.get("strict"):
    # claude 2.1.296 for `--resume <id>` it has no conversation for (probed on
    # the box): stderr line, a result error event carrying the id, exit 1.
    known_path = os.path.join(d, "known.txt")
    known = open(known_path).read().split() if os.path.exists(known_path) else []
    if "--resume" in argv and sid not in known:
        sys.stderr.write("No conversation found with session ID: %s\n" % sid)
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 0,
              "session_id": sid, "errors": ["No conversation found with session ID: " + sid]})
        sys.exit(1)
    if "--session-id" in argv:
        with open(known_path, "a") as fh:
            fh.write(sid + "\n")
if mode.get("tool_child"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    open(os.path.join(d, "child.pid"), "w").write(str(child.pid))
if mode.get("stderr_bytes"):
    sys.stderr.write("w" * mode["stderr_bytes"] + "\n")
    sys.stderr.flush()
emit({"type": "system", "subtype": "init", "session_id": sid})
gate = mode.get("gate")
if gate:
    deadline = time.time() + 20
    while not os.path.exists(os.path.join(d, gate)) and time.time() < deadline:
        time.sleep(0.02)
for i in range(mode.get("lines", 1)):
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "line %d" % i}]}})
    time.sleep(mode.get("line_delay", 0))
if mode.get("hang"):
    time.sleep(120)
if mode.get("fail"):
    sys.stderr.write(mode["fail"] + "\n")
    sys.exit(3)
emit({"type": "result", "result": "done", "session_id": sid})
'''


FAKE_GEMINI = r'''
import json, os, sys, uuid
d = os.environ["FAKE_CLI_DIR"]
argv = sys.argv[1:]
with open(os.path.join(d, "argv.jsonl"), "a") as fh:
    fh.write(json.dumps(argv) + "\n")
known_path = os.path.join(d, "sessions.txt")
known = open(known_path).read().split() if os.path.exists(known_path) else []
sid = None
if "--resume" in argv:
    ref = argv[argv.index("--resume") + 1]
    if ref == "latest":
        sid = known[-1] if known else None
    elif ref in known:
        sid = ref
    if sid is None:
        sys.stderr.write('Invalid session identifier "%s".\n' % ref)
        sys.exit(42)
else:
    sid = str(uuid.uuid4())
    with open(known_path, "a") as fh:
        fh.write(sid + "\n")
def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
emit({"type": "init", "session_id": sid, "model": "fake-model"})
emit({"type": "message", "role": "assistant", "content": "reply in " + sid})
emit({"type": "result", "status": "success"})
'''


def _write_cli(directory: Path, name: str, body: str) -> str:
    path = directory / name
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def cli_dir(tmp_path, monkeypatch):
    d = tmp_path / "fakecli"
    d.mkdir()
    monkeypatch.setenv("FAKE_CLI_DIR", str(d))
    return d


def _argvs(cli_dir: Path) -> list:
    path = cli_dir / "argv.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _mode(cli_dir: Path, **mode) -> None:
    (cli_dir / "mode.json").write_text(json.dumps(mode))


def _collect(ws, until=("completed",), limit=500) -> list:
    frames = []
    for _ in range(limit):
        frame = ws.receive_json()
        frames.append(frame)
        if frame.get("type") in until:
            return frames
    raise AssertionError(f"no {until} frame in {limit} frames: {frames[-5:]}")


def _texts(frames) -> list:
    return [f["data"] for f in frames if f.get("type") == "assistant_message"]


@pytest.fixture
def claude_ws(cli_dir, monkeypatch):
    fake = _write_cli(cli_dir, "claude", FAKE_CLAUDE)
    monkeypatch.setattr(ar, "_claude_path", lambda: fake)
    monkeypatch.setattr(ar, "retrieve_for_agent", lambda **k: ("", {"recent": []}))
    monkeypatch.setattr(ar, "resolve_operator", lambda raw, prefix: raw)
    monkeypatch.setattr(ar, "save_operator_state", lambda: None)
    monkeypatch.setattr(ar, "notify_in_background", lambda *a, **k: None)
    monkeypatch.setattr(ar, "PERSISTED_AGENT_SESSIONS", {})
    monkeypatch.setattr(ar, "_CLAUDE_REAPED", OrderedDict())
    before = set(ar.AGENT_SESSIONS)
    app = FastAPI()
    app.add_api_websocket_route("/ws/agent/{session_id}", ar.agent_websocket)
    with TestClient(app) as client:
        yield client
    for sid in set(ar.AGENT_SESSIONS) - before:
        ar.AGENT_SESSIONS.pop(sid, None)


def _prompt(ws, text, operator="Alice", **extra):
    ws.send_json({"type": "prompt", "text": text, "operator": operator,
                  "working_dir": None, "permission_mode": "yolo", **extra})


# ── Claude: resume by id, never --continue ───────────────────────────────────────

def test_claude_follow_up_resumes_its_own_conversation_by_id(claude_ws, cli_dir):
    with claude_ws.websocket_connect("/ws/agent/sess-alice") as alice:
        _prompt(alice, "first")
        _collect(alice)
        # Another operator's run is now the most recent conversation in the
        # shared working directory: --continue would resume THAT one.
        with claude_ws.websocket_connect("/ws/agent/sess-bob") as bob:
            _prompt(bob, "bob's turn", operator="Bob")
            _collect(bob)
        alice.send_json({"type": "input", "text": "second"})
        _collect(alice)
        _prompt(alice, "third")
        _collect(alice)

    argvs = _argvs(cli_dir)
    assert len(argvs) == 4
    alice_first, bob_first, alice_input, alice_prompt = argvs
    alice_id = alice_first[alice_first.index("--session-id") + 1]
    bob_id = bob_first[bob_first.index("--session-id") + 1]
    assert alice_id != bob_id
    for argv in (alice_input, alice_prompt):
        assert "--continue" not in argv and "-c" not in argv
        assert argv[argv.index("--resume") + 1] == alice_id
    # The permission argv rides along unchanged.
    assert all("--dangerously-skip-permissions" in a for a in argvs)


def test_claude_resumes_the_id_the_cli_reports(claude_ws, cli_dir, monkeypatch):
    """The id is captured from the stream (the CLI may fork a new one on resume)."""
    fake = _write_cli(cli_dir, "claude", FAKE_CLAUDE.replace(
        'emit({"type": "system", "subtype": "init", "session_id": sid})',
        'sid = "forked-" + sid[-6:] if "--resume" in argv else sid\n'
        'emit({"type": "system", "subtype": "init", "session_id": sid})'))
    monkeypatch.setattr(ar, "_claude_path", lambda: fake)
    with claude_ws.websocket_connect("/ws/agent/sess-fork") as ws:
        _prompt(ws, "one")
        _collect(ws)
        ws.send_json({"type": "input", "text": "two"})
        _collect(ws)
        ws.send_json({"type": "input", "text": "three"})
        _collect(ws)
    first, second, third = _argvs(cli_dir)
    original = first[first.index("--session-id") + 1]
    assert second[second.index("--resume") + 1] == original
    assert third[third.index("--resume") + 1] == "forked-" + original[-6:]


def test_plan_mode_follow_up_keeps_the_read_only_argv(claude_ws, cli_dir):
    with claude_ws.websocket_connect("/ws/agent/sess-plan") as ws:
        _prompt(ws, "look", permission_mode="plan")
        _collect(ws)
        ws.send_json({"type": "input", "text": "and more"})
        _collect(ws)
    for argv in _argvs(cli_dir):
        assert "--dangerously-skip-permissions" not in argv
        assert argv[argv.index("--permission-mode") + 1] == "plan"
        assert "--strict-mcp-config" in argv


# ── end_session as the first frame (O06-frontier-agent-04) ──────────────────────

def test_end_session_as_first_frame_answers_and_clears_the_owner(claude_ws, cli_dir, monkeypatch):
    persisted = {"Alice": {"claude_session_id": "old"}, "Bob": {"claude_session_id": "bobs"}}
    monkeypatch.setattr(ar, "PERSISTED_AGENT_SESSIONS", persisted)
    with claude_ws.websocket_connect("/ws/agent/sess-end") as ws:
        _prompt(ws, "hello")
        _collect(ws)
    # A fresh socket (Android reconnects without a prompt) ends the session.
    with claude_ws.websocket_connect("/ws/agent/sess-end") as ws:
        ws.send_json({"type": "end_session"})
        reply = ws.receive_json()
    assert reply["type"] == "session_ended"
    assert "sess-end" not in ar.AGENT_SESSIONS
    assert "Alice" not in persisted          # the session's own operator
    assert "Bob" in persisted                # nobody else's


def test_end_session_with_no_session_and_no_operator_clears_nothing(claude_ws, monkeypatch):
    persisted = {"Alice": {"claude_session_id": "keep"}}
    monkeypatch.setattr(ar, "PERSISTED_AGENT_SESSIONS", persisted)
    with claude_ws.websocket_connect("/ws/agent/sess-unknown") as ws:
        ws.send_json({"type": "end_session"})
        assert ws.receive_json()["type"] == "session_ended"
    assert persisted == {"Alice": {"claude_session_id": "keep"}}


# ── every attached socket sees the whole stream ──────────────────────────────────

def test_two_sockets_on_one_session_each_get_every_line(claude_ws, cli_dir):
    _mode(cli_dir, gate="go", lines=25, line_delay=0.005)
    with claude_ws.websocket_connect("/ws/agent/sess-two") as first:
        _prompt(first, "stream")
        assert first.receive_json()["type"] == "provenance"
        with claude_ws.websocket_connect("/ws/agent/sess-two") as second:
            second.send_json({"type": "reconnect", "operator": "Alice"})
            info = second.receive_json()
            assert info["type"] == "info", info
            (cli_dir / "go").write_text("")
            got_second = _collect(second)
        got_first = _collect(first)
    expected = [f"line {i}" for i in range(25)]
    assert _texts(got_first) == expected
    assert _texts(got_second) == expected


# ── stderr is drained; a failed run says why (O06-frontier-agent-03) ─────────────

def test_a_cli_that_floods_stderr_still_finishes(claude_ws, cli_dir):
    _mode(cli_dir, stderr_bytes=512 * 1024, lines=3)
    with claude_ws.websocket_connect("/ws/agent/sess-stderr") as ws:
        _prompt(ws, "noisy")
        frames = _collect(ws)
    assert _texts(frames) == ["line 0", "line 1", "line 2"]


def test_a_failed_cli_run_reports_its_stderr(claude_ws, cli_dir):
    _mode(cli_dir, fail="Invalid API key - please run /login", lines=0)
    with claude_ws.websocket_connect("/ws/agent/sess-fail") as ws:
        _prompt(ws, "x")
        frames = _collect(ws)
    errors = [f["data"] for f in frames if f["type"] == "error"]
    assert errors and "please run /login" in errors[-1]


def test_a_cli_that_cannot_start_is_an_error_frame_not_a_dead_socket(claude_ws, monkeypatch):
    monkeypatch.setattr(ar, "_claude_path", lambda: "/nonexistent/claude-binary")
    with claude_ws.websocket_connect("/ws/agent/sess-missing") as ws:
        _prompt(ws, "x")
        frames = _collect(ws)              # the pending reply is closed too
        errors = [f["data"] for f in frames if f["type"] == "error"]
        assert errors and "claude" in errors[0].lower()
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
    assert "sess-missing" not in ar.AGENT_SESSIONS


# ── ending a running session kills the CLI's tools too (O06-frontier-agent-16) ───

def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done:
                return True
        except ChildProcessError:
            pass
        time.sleep(0.05)
    return False


def test_end_session_kills_the_tools_a_running_cli_started(claude_ws, cli_dir):
    _mode(cli_dir, tool_child=True, hang=True, lines=1)
    child_pid = None
    try:
        with claude_ws.websocket_connect("/ws/agent/sess-tree") as ws:
            _prompt(ws, "start a server")
            deadline = time.time() + 15
            while not (cli_dir / "child.pid").exists() and time.time() < deadline:
                time.sleep(0.05)
            child_pid = int((cli_dir / "child.pid").read_text())
            ws.send_json({"type": "end_session"})
            _collect(ws, until=("session_ended",))
        assert _wait_dead(child_pid), "the CLI's tool process survived end_session"
    finally:
        if child_pid:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass


# ── a conversation the CLI no longer has does not wedge the session ─────────────

def _cwds(cli_dir: Path) -> list:
    path = cli_dir / "cwd.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _flag(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


def test_a_persisted_conversation_that_is_gone_starts_a_new_one_in_the_same_turn(
        claude_ws, cli_dir, monkeypatch, tmp_path):
    """The phone bridge saves an id before any turn runs, and Claude Code deletes
    transcripts after cleanupPeriodDays: resuming such an id fails. That used to
    fail every later turn too (each resumed the same id)."""
    _mode(cli_dir, strict=True)
    persisted = {"Alice": {"claude_session_id": "pruned-transcript-id", "message_count": 7,
                           "working_directory": str(tmp_path / "deleted-since")},
                 "Bob": {"claude_session_id": "bobs-own"}}
    monkeypatch.setattr(ar, "PERSISTED_AGENT_SESSIONS", persisted)
    with claude_ws.websocket_connect("/ws/agent/sess-gone") as ws:
        _prompt(ws, "one")
        first = _collect(ws)
        ws.send_json({"type": "input", "text": "two"})
        second = _collect(ws)
        _prompt(ws, "three")
        third = _collect(ws)
    assert [f["type"] for f in first if f["type"] in ("error", "info")] == ["info"]
    assert "no longer on this box" in next(f["data"] for f in first if f["type"] == "info")
    for frames in (first, second, third):
        assert _texts(frames) == ["line 0"], frames
        assert not [f for f in frames if f["type"] == "error"]
    resumed, fresh, follow_up, again = _argvs(cli_dir)
    assert _flag(resumed, "--resume") == "pruned-transcript-id"
    new_id = _flag(fresh, "--session-id")
    assert new_id and new_id != "pruned-transcript-id"
    assert fresh[-1] == "one"
    assert _flag(follow_up, "--resume") == new_id and _flag(again, "--resume") == new_id
    assert "--continue" not in sum(_argvs(cli_dir), [])
    # The gone id is resumed by nobody any more; nobody else's entry is touched.
    assert "Alice" not in persisted and persisted["Bob"] == {"claude_session_id": "bobs-own"}
    # The deleted working directory fell back to the default one.
    assert all(os.path.isdir(c) for c in _cwds(cli_dir))


def test_a_follow_up_whose_conversation_vanished_also_recovers(claude_ws, cli_dir):
    _mode(cli_dir, strict=True)
    with claude_ws.websocket_connect("/ws/agent/sess-vanish") as ws:
        _prompt(ws, "one")
        _collect(ws)
        (cli_dir / "known.txt").write_text("")       # the transcript was cleaned up
        ws.send_json({"type": "input", "text": "two"})
        frames = _collect(ws)
    assert _texts(frames) == ["line 0"]
    assert [f["type"] for f in frames if f["type"] in ("error", "info")] == ["info"]
    first, failed, fresh = _argvs(cli_dir)
    assert _flag(failed, "--resume") == _flag(first, "--session-id")
    assert _flag(fresh, "--session-id") not in (None, _flag(first, "--session-id"))
    assert fresh[-1] == "two"


def test_a_resume_that_fails_for_another_reason_is_not_retried(claude_ws, cli_dir):
    with claude_ws.websocket_connect("/ws/agent/sess-otherfail") as ws:
        _prompt(ws, "one")
        _collect(ws)
        _mode(cli_dir, fail="Invalid API key - please run /login", lines=0)
        ws.send_json({"type": "input", "text": "two"})
        frames = _collect(ws)
    errors = [f["data"] for f in frames if f["type"] == "error"]
    assert errors and "please run /login" in errors[-1]
    assert len(_argvs(cli_dir)) == 2


# ── a reconnecting client gets what it missed ────────────────────────────────────

def _wait_detached(session_id: str, timeout: float = 10.0) -> None:
    """Until the server has let go of every socket of ``session_id``."""
    deadline = time.time() + timeout
    while ar.AGENT_SESSIONS[session_id].websocket_clients and time.time() < deadline:
        time.sleep(0.02)
    assert not ar.AGENT_SESSIONS[session_id].websocket_clients


def test_a_reconnect_without_a_position_gets_the_lines_written_while_away(claude_ws, cli_dir):
    """The Portal's attemptReconnect() sends {type: reconnect, operator} only."""
    _mode(cli_dir, gate="go", lines=40, line_delay=0.05)
    with claude_ws.websocket_connect("/ws/agent/sess-gap") as first:
        _prompt(first, "stream")
        assert first.receive_json()["type"] == "provenance"
    _wait_detached("sess-gap")
    (cli_dir / "go").write_text("")
    time.sleep(0.8)               # lines are written while nobody is attached
    with claude_ws.websocket_connect("/ws/agent/sess-gap") as second:
        second.send_json({"type": "reconnect", "operator": "Alice"})
        frames = _collect(second)
    assert _texts(frames) == [f"line {i}" for i in range(40)]


def test_a_reconnect_after_the_turn_ended_replays_its_tail(claude_ws, cli_dir):
    _mode(cli_dir, gate="go", lines=5)
    with claude_ws.websocket_connect("/ws/agent/sess-tail") as first:
        _prompt(first, "stream")
        assert first.receive_json()["type"] == "provenance"
    _wait_detached("sess-tail")
    (cli_dir / "go").write_text("")
    deadline = time.time() + 15
    while ar.AGENT_SESSIONS["sess-tail"].transport.running and time.time() < deadline:
        time.sleep(0.05)
    with claude_ws.websocket_connect("/ws/agent/sess-tail") as second:
        second.send_json({"type": "reconnect", "operator": "Alice"})
        frames = _collect(second)
        # The replay is used once: the next reconnect has nothing to replay.
        second.send_json({"type": "reconnect", "operator": "Alice"})
        assert second.receive_json() == {"type": "completed", "data": None}
    assert _texts(frames) == [f"line {i}" for i in range(5)]


def test_a_reconnect_position_from_an_earlier_turn_reaches_the_running_turn(claude_ws, cli_dir):
    with claude_ws.websocket_connect("/ws/agent/sess-turns") as ws:
        _prompt(ws, "one")
        _collect(ws)
        _mode(cli_dir, gate="go", lines=3)
        ws.send_json({"type": "input", "text": "two"})
        with claude_ws.websocket_connect("/ws/agent/sess-turns") as late:
            late.send_json({"type": "reconnect", "operator": "Alice", "since_seq": 0})
            assert late.receive_json()["type"] == "info"
            skipped = late.receive_json()
            assert skipped["type"] == "info" and "earlier turn" in skipped["data"]
            (cli_dir / "go").write_text("")
            frames = _collect(late)
        _collect(ws)
    assert _texts(frames) == ["line 0", "line 1", "line 2"]


# ── the reaper keeps a conversation for its own socket session ───────────────────

def test_after_eviction_the_same_session_resumes_and_a_new_one_starts_fresh(claude_ws, cli_dir, monkeypatch):
    persisted = {}
    monkeypatch.setattr(ar, "PERSISTED_AGENT_SESSIONS", persisted)
    with claude_ws.websocket_connect("/ws/agent/sess-evict") as ws:
        _prompt(ws, "one")
        _collect(ws)
    conversation = _flag(_argvs(cli_dir)[0], "--session-id")
    assert ar.AGENT_SESSION_REAPER.sweep(now=time.monotonic() + 31 * 60) == ["sess-evict"]
    # Kept for a restart (the state file's last conversation of the operator).
    assert persisted["Alice"]["claude_session_id"] == conversation
    with claude_ws.websocket_connect("/ws/agent/sess-brand-new") as ws:
        _prompt(ws, "a new chat")
        frames = _collect(ws)
    with claude_ws.websocket_connect("/ws/agent/sess-evict") as ws:
        _prompt(ws, "back again")
        _collect(ws)
    _, brand_new, back = _argvs(cli_dir)
    assert _flag(brand_new, "--session-id") not in (None, conversation)
    assert "--resume" not in brand_new
    assert _flag(back, "--resume") == conversation
    assert _texts(frames) == ["line 0"]


def test_after_a_restart_a_new_session_resumes_the_persisted_conversation(claude_ws, cli_dir, monkeypatch):
    """Nothing was reaped in this run: the persisted entry is the operator's last
    conversation from before the restart, resumed as it always was."""
    monkeypatch.setattr(ar, "PERSISTED_AGENT_SESSIONS",
                        {"Alice": {"claude_session_id": "from-before-restart", "message_count": 3}})
    with claude_ws.websocket_connect("/ws/agent/sess-after-restart") as ws:
        _prompt(ws, "hello again")
        _collect(ws)
    (argv,) = _argvs(cli_dir)
    assert _flag(argv, "--resume") == "from-before-restart"


# ── input frames get the operator check; a missing directory says so ────────────

def test_an_input_frame_cannot_drive_another_operators_session(claude_ws, cli_dir):
    with claude_ws.websocket_connect("/ws/agent/sess-shared") as alice:
        _prompt(alice, "hi")
        _collect(alice)
    with claude_ws.websocket_connect("/ws/agent/sess-shared") as bob:
        _prompt(bob, "hi", operator="Bob")
        assert [f["type"] for f in _collect(bob)] == ["error", "completed"]
        bob.send_json({"type": "input", "text": "now as input", "operator": "Bob"})
        refused = _collect(bob)
    assert [f["type"] for f in refused] == ["error", "completed"]
    assert "another operator" in refused[0]["data"]
    assert len(_argvs(cli_dir)) == 1


def test_a_missing_working_directory_is_not_reported_as_a_missing_cli(claude_ws, cli_dir, tmp_path):
    missing = str(tmp_path / "no-such-folder")
    with claude_ws.websocket_connect("/ws/agent/sess-nodir") as ws:
        _prompt(ws, "x", working_dir=missing)
        frames = _collect(ws)
    errors = [f["data"] for f in frames if f["type"] == "error"]
    assert errors and "working directory" in errors[0] and missing in errors[0]
    assert "Install" not in errors[0]
    assert _argvs(cli_dir) == []


# ── Gemini: resume by the captured session id, never "latest" ────────────────────

@pytest.fixture
def gemini_ws(cli_dir, monkeypatch):
    fake = _write_cli(cli_dir, "gemini", FAKE_GEMINI)
    monkeypatch.setattr(gr, "get_gemini_path", lambda: fake)
    monkeypatch.setattr(gr, "retrieve_for_agent", lambda **k: ("", {"recent": []}))
    monkeypatch.setattr(gr, "resolve_operator", lambda raw, prefix: raw)
    monkeypatch.setattr(gr, "_GEMINI_RESUME_IDS", OrderedDict())
    before = set(gr.GEMINI_AGENT_SESSIONS)
    app = FastAPI()
    app.add_api_websocket_route("/ws/gemini-agent/{session_id}", gr.gemini_agent_websocket)
    with TestClient(app) as client:
        yield client
    for sid in set(gr.GEMINI_AGENT_SESSIONS) - before:
        gr.GEMINI_AGENT_SESSIONS.pop(sid, None)


def _gemini_prompt(ws, text, operator, **extra):
    ws.send_json({"type": "prompt", "text": text, "operator": operator,
                  "working_dir": None, "yolo_mode": True, **extra})


def _contents(frames) -> list:
    return [f["data"] for f in frames if f.get("type") == "content"]


def test_gemini_follow_up_resumes_its_own_session_not_the_latest(gemini_ws, cli_dir):
    with gemini_ws.websocket_connect("/ws/gemini-agent/g-alice") as alice:
        _gemini_prompt(alice, "first", "Alice")
        first = _collect(alice)
        alice_sid = _contents(first)[0].split()[-1]
        with gemini_ws.websocket_connect("/ws/gemini-agent/g-bob") as bob:
            _gemini_prompt(bob, "bob", "Bob")
            _collect(bob)
        alice.send_json({"type": "input", "text": "second"})
        second = _collect(alice)
        _gemini_prompt(alice, "third", "Alice")
        third = _collect(alice)
    assert _contents(second) == [f"reply in {alice_sid}"]
    assert _contents(third) == [f"reply in {alice_sid}"]
    for argv in _argvs(cli_dir):
        assert "latest" not in argv
    assert _argvs(cli_dir)[2][_argvs(cli_dir)[2].index("--resume") + 1] == alice_sid


def test_gemini_resume_request_without_a_known_id_never_resumes_latest(gemini_ws, cli_dir):
    # Someone else's session is the latest one in the shared directory.
    (cli_dir / "sessions.txt").write_text("someone-elses-session\n")
    with gemini_ws.websocket_connect("/ws/gemini-agent/g-restart") as ws:
        _gemini_prompt(ws, "after a restart", "Alice", resume_session=True)
        frames = _collect(ws)
    assert "reply in someone-elses-session" not in _contents(frames)
    assert all("latest" not in argv for argv in _argvs(cli_dir))


def test_gemini_end_session_as_first_frame(gemini_ws):
    with gemini_ws.websocket_connect("/ws/gemini-agent/g-end") as ws:
        ws.send_json({"type": "end_session"})
        assert ws.receive_json()["type"] == "session_ended"


def test_gemini_follow_up_after_eviction_resumes_the_conversation(gemini_ws, cli_dir):
    """The Portal's Gemini handler reopens a closed socket and sends a plain
    prompt (no resume_session): after the reaper evicted the idle session, that
    follow-up still continues the same conversation."""
    with gemini_ws.websocket_connect("/ws/gemini-agent/g-evict") as ws:
        _gemini_prompt(ws, "first", "Alice")
        sid = _contents(_collect(ws))[0].split()[-1]
    assert gr.GEMINI_SESSION_REAPER.sweep(now=time.monotonic() + 31 * 60) == ["g-evict"]
    with gemini_ws.websocket_connect("/ws/gemini-agent/g-evict") as ws:
        ws.send_json({"type": "prompt", "text": "second", "operator": "Alice", "yolo_mode": True})
        second = _collect(ws)
    assert _argvs(cli_dir)[-1][_argvs(cli_dir)[-1].index("--resume") + 1] == sid
    assert _contents(second) == [f"reply in {sid}"]
    assert not [f for f in second if f["type"] == "info" and "not known" in f["data"]]


def test_gemini_resume_of_a_session_the_cli_lost_starts_a_new_one(gemini_ws, cli_dir):
    with gemini_ws.websocket_connect("/ws/gemini-agent/g-lost") as ws:
        _gemini_prompt(ws, "first", "Alice")
        first_sid = _contents(_collect(ws))[0].split()[-1]
        (cli_dir / "sessions.txt").write_text("")      # the CLI no longer has it
        ws.send_json({"type": "input", "text": "second"})
        second = _collect(ws)
        ws.send_json({"type": "input", "text": "third"})
        third = _collect(ws)
    new_sid = _contents(second)[0].split()[-1]
    assert new_sid != first_sid
    assert [f["type"] for f in second if f["type"] in ("info", "error")][-1:] == ["info"]
    assert not [f for f in second if f["type"] == "error"]
    assert _contents(third) == [f"reply in {new_sid}"]


def test_gemini_input_frame_cannot_drive_another_operators_session(gemini_ws, cli_dir):
    with gemini_ws.websocket_connect("/ws/gemini-agent/g-shared") as alice:
        _gemini_prompt(alice, "hi", "Alice")
        _collect(alice)
    with gemini_ws.websocket_connect("/ws/gemini-agent/g-shared") as bob:
        bob.send_json({"type": "input", "text": "as Bob", "operator": "Bob"})
        refused = _collect(bob)
    assert [f["type"] for f in refused] == ["error", "completed"]
    assert len(_argvs(cli_dir)) == 1


def test_gemini_session_ended_after_eviction_is_not_resumed(gemini_ws, cli_dir):
    with gemini_ws.websocket_connect("/ws/gemini-agent/g-ended") as ws:
        _gemini_prompt(ws, "first", "Alice")
        _collect(ws)
    gr.GEMINI_SESSION_REAPER.sweep(now=time.monotonic() + 31 * 60)
    with gemini_ws.websocket_connect("/ws/gemini-agent/g-ended") as ws:
        ws.send_json({"type": "end_session", "operator": "Alice"})
        assert ws.receive_json()["type"] == "session_ended"
        _gemini_prompt(ws, "fresh start", "Alice")
        _collect(ws)
    assert "--resume" not in _argvs(cli_dir)[-1]
