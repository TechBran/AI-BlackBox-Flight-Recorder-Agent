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
  * ending a running session kills the tools the CLI started, not just the CLI.
"""
from __future__ import annotations

import json
import os
import sys
import time
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


# ── Gemini: resume by the captured session id, never "latest" ────────────────────

@pytest.fixture
def gemini_ws(cli_dir, monkeypatch):
    fake = _write_cli(cli_dir, "gemini", FAKE_GEMINI)
    monkeypatch.setattr(gr, "get_gemini_path", lambda: fake)
    monkeypatch.setattr(gr, "retrieve_for_agent", lambda **k: ("", {"recent": []}))
    monkeypatch.setattr(gr, "resolve_operator", lambda raw, prefix: raw)
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
