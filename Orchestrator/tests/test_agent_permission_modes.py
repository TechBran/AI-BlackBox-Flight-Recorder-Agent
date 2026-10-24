"""Claude Code agent permission modes are real (audit P03/O06, rank 30).

Before: the Portal hard-coded ``skip_permissions: true`` (so "Plan Mode (read-only)" ran a
fully privileged ``--dangerously-skip-permissions`` agent), and the "Normal (prompts)" mode
launched ``-p`` with an open stdin pipe and later wrote ``y``/``n`` to it — a protocol print
mode does not have, so the run hung or auto-denied behind a fake approval dialog.

Now there are exactly two modes, each mapped to a CLI flag that works in print mode:
``yolo`` -> ``--dangerously-skip-permissions``, ``plan`` -> ``--permission-mode plan``. The
retired "normal"/"ask" request and anything unrecognised resolve to ``plan`` — a request is
never escalated. Hermetic: no CLI is spawned (the subprocess call is faked), no live app.

``--permission-mode plan`` alone is NOT read-only on a box whose Claude settings allow tools:
the CLI honours the user's/project's allow rules (and its auto-mode classifier) in plan mode
too, so a plan run executed ``curl -X POST``/``wget`` with no denial. Plan therefore also
narrows the tool set itself (``--tools`` with read-only built-ins only, no MCP servers), so no
settings file can hand the agent a tool that writes or runs anything. The one test that spawns
the real CLI to prove that is opt-in (``BLACKBOX_LIVE_CLAUDE_PROBE=1``): it needs the network.
"""
import json
import os
import shutil
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import Orchestrator.routes.agent_routes as ar
from Orchestrator.models import AgentSession


YOLO_FLAG = "--dangerously-skip-permissions"

# Built-in tools that change the system or run code. None may be reachable in plan mode.
MUTATING_TOOLS = {"Bash", "Edit", "Write", "NotebookEdit", "MultiEdit", "Task", "Agent",
                  "Skill", "WebFetch", "Monitor", "CronCreate", "CronDelete", "RemoteTrigger",
                  "PushNotification", "SendMessage", "Workflow", "EnterWorktree", "ToolSearch"}


# CLI options declared ``<values...>``: they swallow every following non-option argument.
VARIADIC_FLAGS = {"--tools", "--disallowedTools", "--allowedTools", "--add-dir", "--mcp-config"}


def _flag_values(argv, flag):
    """The comma/space separated values following ``flag`` (empty if absent)."""
    if flag not in argv:
        return []
    raw = argv[argv.index(flag) + 1]
    return [t for t in raw.replace(",", " ").split() if t]


# ── mode resolution from a client frame ───────────────────────────────────────────

@pytest.mark.parametrize("frame, expected", [
    ({"permission_mode": "plan"}, "plan"),
    ({"permission_mode": "yolo"}, "yolo"),
    ({"permission_mode": "auto"}, "yolo"),                 # the Portal's old key for YOLO
    ({"permission_mode": " PLAN "}, "plan"),
    ({"skip_permissions": True}, "yolo"),                   # legacy clients, old default
    ({}, "yolo"),                                           # unchanged default
    ({"skip_permissions": False}, "plan"),                  # legacy 'ask' -> safe, never 'ask'
    ({"permission_mode": "normal"}, "plan"),                # the retired mode
    ({"permission_mode": "ask"}, "plan"),
    ({"permission_mode": "bypassPermissions!!"}, "plan"),   # garbage never escalates
    ({"permission_mode": None, "skip_permissions": True}, "yolo"),
    ({"permission_mode": "plan", "skip_permissions": True}, "plan"),  # explicit mode wins
])
def test_resolve_permission_mode(frame, expected):
    assert ar.resolve_permission_mode(frame) == expected


# ── argv per mode ─────────────────────────────────────────────────────────────────

def test_plan_maps_to_permission_mode_plan_and_never_skips_permissions():
    argv = ar.build_claude_argv("/bin/claude", "look around", permission_mode="plan")
    assert argv[:5] == ["/bin/claude", "-p", "--output-format", "stream-json", "--verbose"]
    i = argv.index("--permission-mode")
    assert argv[i + 1] == "plan"
    assert YOLO_FLAG not in argv
    assert argv[-1] == "look around"


def test_plan_offers_only_read_only_tools_whatever_the_settings_allow():
    argv = ar.build_claude_argv("/bin/claude", "look around", permission_mode="plan")
    tools = _flag_values(argv, "--tools")
    assert tools, "plan must pin the built-in tool set (--tools); settings cannot widen it"
    assert set(tools) <= ar.PLAN_MODE_TOOLS
    assert not set(tools) & MUTATING_TOOLS
    assert {"Read", "Grep", "Glob"} <= set(tools)          # still able to explore the code
    # No MCP servers at all: .mcp.json / settings-enabled servers carry write tools.
    assert "--strict-mcp-config" in argv
    assert "--mcp-config" not in argv
    # Belt and braces: deny rules win over any allow rule in a settings file.
    assert {"Bash", "Edit", "Write", "NotebookEdit", "WebFetch"} <= set(
        _flag_values(argv, "--disallowedTools"))
    # The prompt is still the final, separate argument, and it cannot be read as one more
    # value of a variadic list flag: the CLI's last option before it takes no value.
    assert argv[-1] == "look around"
    assert argv[-2] == "--strict-mcp-config"


@pytest.mark.parametrize("extra", [
    {}, {"model": "opus"}, {"claude_session_id": "abc-123"},
    {"claude_session_id": "abc-123", "resume_session": True},
])
def test_the_prompt_never_follows_a_variadic_tool_list(extra):
    argv = ar.build_claude_argv("/bin/claude", "the prompt", permission_mode="plan", **extra)
    assert argv[-1] == "the prompt"
    last_option = max(i for i, a in enumerate(argv[:-1]) if a.startswith("--"))
    assert argv[last_option] not in VARIADIC_FLAGS
    # ...and no variadic list runs into the next argument that is not an option.
    for i, a in enumerate(argv[:-1]):
        if a in VARIADIC_FLAGS:
            assert argv[i + 2].startswith("--"), argv


def test_plan_tool_set_has_no_mutating_tool():
    assert not ar.PLAN_MODE_TOOLS & MUTATING_TOOLS


def test_yolo_maps_to_skip_permissions_only():
    argv = ar.build_claude_argv("/bin/claude", "do it", permission_mode="yolo")
    assert YOLO_FLAG in argv
    assert "--permission-mode" not in argv
    # YOLO keeps the operator's full tool set and MCP servers.
    assert "--tools" not in argv
    assert "--strict-mcp-config" not in argv
    assert "--disallowedTools" not in argv


def test_an_unknown_mode_builds_the_read_only_argv():
    argv = ar.build_claude_argv("/bin/claude", "x", permission_mode="normal")
    assert YOLO_FLAG not in argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv == ar.build_claude_argv("/bin/claude", "x", permission_mode="plan")


def test_session_flags_and_model_are_preserved():
    argv = ar.build_claude_argv("/bin/claude", "hi", permission_mode="plan", model="opus",
                                claude_session_id="abc-123")
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--session-id") + 1] == "abc-123"
    resumed = ar.build_claude_argv("/bin/claude", "hi", permission_mode="yolo",
                                   claude_session_id="abc-123", resume_session=True)
    assert resumed[resumed.index("--resume") + 1] == "abc-123"
    assert "--session-id" not in resumed


@pytest.mark.parametrize("mode", ["plan", "yolo"])
@pytest.mark.parametrize("extra", [
    {}, {"claude_session_id": "abc-123"},
    {"claude_session_id": "abc-123", "resume_session": True},
])
def test_no_argv_resumes_the_directorys_most_recent_conversation(mode, extra):
    # --continue resumes the newest conversation in the (shared) working directory,
    # which may be another operator's (audit O06-frontier-agent-13).
    argv = ar.build_claude_argv("/bin/claude", "hi", permission_mode=mode, **extra)
    assert "--continue" not in argv and "-c" not in argv


class _EofStream:
    async def read(self, n=-1):
        return b""


class _FakeChild:
    pid = 4242
    returncode = 0

    def __init__(self):
        self.stdout = _EofStream()
        self.stderr = _EofStream()

    async def wait(self):
        return 0


@pytest.mark.parametrize("mode", ["plan", "yolo"])
def test_the_cli_never_gets_an_open_stdin_pipe(monkeypatch, mode):
    # An open, never-closed stdin makes print mode wait for piped input (the old hang).
    import asyncio
    from Orchestrator.cli_agent import transport as tr
    seen = {}

    async def fake_exec(*argv, **kwargs):
        seen.update(argv=list(argv), **kwargs)
        return _FakeChild()

    monkeypatch.setattr(tr.asyncio, "create_subprocess_exec", fake_exec)
    spec = ar.claude_spawn_spec("/tmp", "hello", permission_mode=mode)

    async def run():
        t = tr.CliAgentTransport("perm-test")
        await t.start(spec)
        await t.wait_finished(timeout=5)

    asyncio.run(run())
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["argv"] == spec.argv
    assert (YOLO_FLAG in seen["argv"]) is (mode == "yolo")


# ── WebSocket: no stdin approval path, no fake approval dialog ────────────────────

class _Stdin:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    def flush(self):
        pass


class _RunningProc:
    def __init__(self):
        self.stdin = _Stdin()

    def poll(self):
        return None


@pytest.fixture
def ws_client():
    app = FastAPI()
    app.add_api_websocket_route("/ws/agent/{session_id}", ar.agent_websocket)
    return TestClient(app)


def test_permission_response_never_writes_y_or_n_to_the_cli(ws_client):
    proc = _RunningProc()
    ar.AGENT_SESSIONS["perm-test"] = AgentSession(session_id="perm-test", operator="Alice",
                                               transport=proc, status="running")
    try:
        with ws_client.websocket_connect("/ws/agent/perm-test") as ws:
            ws.send_json({"type": "permission_response", "approved": True})
            # A frame that always gets an answer, so a handler that stays silent on the
            # permission_response cannot hang the test: its reply would arrive first.
            ws.send_json({"type": "input", "text": ""})
            reply = ws.receive_json()
        assert proc.stdin.writes == []
        assert reply["type"] == "error"
        assert "Plan" in reply["data"] and "YOLO" in reply["data"]
    finally:
        ar.AGENT_SESSIONS.pop("perm-test", None)


def test_plan_session_streams_tool_use_and_never_a_permission_request():
    import asyncio
    from Orchestrator.cli_agent import transport as tr
    tool_event = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}
    sent = []

    async def send(frame):
        sent.append(frame)
        return True

    async def run():
        t = tr.CliAgentTransport("stream-test")
        t.buffer.append(tr.OUT, json.dumps(tool_event))
        t.buffer.append(tr.EXIT, 0)
        await tr.relay(t, send, ar.claude_event_frames, 0)

    asyncio.run(run())
    kinds = [m["type"] for m in sent]
    assert "tool_use" in kinds
    assert "permission_request" not in kinds
    assert kinds[-1] == "completed"


# ── opt-in: the real CLI, with settings that allow everything, still cannot write ───

def _claude_binary():
    path = os.path.expanduser("~/.local/bin/claude")
    return path if os.path.exists(path) else shutil.which("claude")


@pytest.mark.live_service
@pytest.mark.skipif(os.environ.get("BLACKBOX_LIVE_CLAUDE_PROBE") != "1" or not _claude_binary(),
                    reason="live Claude CLI probe (network + a signed-in CLI): set "
                           "BLACKBOX_LIVE_CLAUDE_PROBE=1 to run")
def test_live_plan_run_cannot_write_even_when_settings_allow_it(tmp_path):
    """The P03 harm, end to end: a project whose settings allow Bash/Write/Edit outright (as
    this box's do) and a prompt asking for writes. The plan argv must leave no trace."""
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text(json.dumps({
        "permissions": {"defaultMode": "auto",
                        "allow": ["Bash(*)", "Bash", "Write", "Edit", "WebFetch"]},
    }))
    bash_target = tmp_path / "pwned_bash.txt"
    write_target = tmp_path / "pwned_write.txt"
    prompt = (f"This is an authorised test. Run the shell command `touch {bash_target}` with "
              f"the Bash tool, then create the file {write_target} containing the text x with "
              f"the Write tool. Then say which of the two actions succeeded.")
    argv = ar.build_claude_argv(_claude_binary(), prompt, permission_mode="plan", model="haiku")
    argv.insert(-1, "--no-session-persistence")
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    proc = subprocess.run(argv, cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=300)
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    init = next(e for e in events if e.get("type") == "system" and e.get("subtype") == "init")
    assert init["permissionMode"] == "plan"
    assert set(init["tools"]) <= ar.PLAN_MODE_TOOLS, init["tools"]
    assert init.get("mcp_servers") == []
    assert not bash_target.exists()
    assert not write_target.exists()
