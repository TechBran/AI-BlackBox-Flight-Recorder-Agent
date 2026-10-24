"""CliAgentTransport, the shared CLI-agent plumbing (audit rank 56: O06-frontier-agent-03,
-15, -16, -27, O10-telephony-twilio-06).

Hermetic: every "CLI" is a short Python child of this test process.

Pinned:
  * a JSON line split across pipe writes comes out whole (the phone path used to
    drop both halves);
  * stderr is drained, so a chatty CLI cannot block on a full pipe, and a failed
    run's stderr tail becomes an ERROR entry (secrets masked);
  * the buffer is bounded by entries and bytes, keeps absolute sequence numbers,
    and tells a reader that fell behind how much it lost;
  * every reader has its own cursor;
  * stop() kills the CLI's whole process tree, including a tool that ignores
    SIGTERM or left the process group; a tool holding the CLI's stdout open
    does not keep a finished turn "running";
  * the child gets exactly the spec's env;
  * the reaper evicts only finished, unattached, idle sessions;
  * on_failure replaces a failed attempt within the same turn (once), after an
    INFO entry, and stop() still ends a turn that is switching attempts;
  * a reader whose cursor is in an earlier turn reaches the running one; a
    disconnected reader's position is kept for its reconnect (resume_position);
  * a missing working directory is not reported as a missing CLI.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from types import SimpleNamespace

import pytest

from Orchestrator.cli_agent import transport as tr

pytestmark = pytest.mark.timeout(60)


def _spec(code: str, tmp_path, env=None) -> tr.SpawnSpec:
    return tr.SpawnSpec(argv=[sys.executable, "-c", code], cwd=str(tmp_path),
                        env=env if env is not None else {"PATH": os.environ.get("PATH", "")})


async def _run_turn(transport: tr.CliAgentTransport, spec: tr.SpawnSpec):
    start = await transport.start(spec)
    entries = []
    async for kind, payload in transport.follow(start):
        entries.append((kind, payload))
    return entries


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split()[2] != "Z"
    except OSError:
        return False


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


# ── whole lines ───────────────────────────────────────────────────────────────

def test_a_json_line_written_in_two_parts_comes_out_whole(tmp_path):
    code = (
        "import json, sys, time\n"
        "line = json.dumps({'type': 'result', 'result': 'x' * 70000}) + '\\n'\n"
        "sys.stdout.write(line[:100]); sys.stdout.flush(); time.sleep(0.3)\n"
        "sys.stdout.write(line[100:]); sys.stdout.flush()\n"
    )
    entries = asyncio.run(_run_turn(tr.CliAgentTransport("split"), _spec(code, tmp_path)))
    outs = [p for k, p in entries if k == tr.OUT]
    assert len(outs) == 1
    assert json.loads(outs[0])["result"] == "x" * 70000
    assert entries[-1] == (tr.EXIT, 0)


@pytest.mark.parametrize("code", [
    "print('a' * 5000); print('after')",                     # arrives with its newline
    "import sys\nsys.stdout.write('a' * 300000); sys.stdout.flush(); print(); print('after')",
])
def test_an_oversized_line_is_dropped_not_held(code):
    async def lines():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE)
        got = [line async for line in tr.iter_lines(proc.stdout, max_line_bytes=1000)]
        await proc.wait()
        return got

    assert asyncio.run(lines()) == ["after"]


# ── stderr ────────────────────────────────────────────────────────────────────

def test_a_cli_that_floods_stderr_finishes(tmp_path):
    code = ("import sys\n"
            "sys.stderr.write('w' * (1024 * 1024)); sys.stderr.flush()\n"
            "print('{\"type\": \"done\"}')\n")

    async def run():
        return await asyncio.wait_for(_run_turn(tr.CliAgentTransport("noisy"), _spec(code, tmp_path)), 20)

    entries = asyncio.run(run())
    assert (tr.OUT, '{"type": "done"}') in entries
    assert entries[-1] == (tr.EXIT, 0)
    assert not [e for e in entries if e[0] == tr.ERROR], "a clean exit is not an error"


def test_a_failed_run_reports_its_stderr_tail_with_secrets_masked(tmp_path):
    secret = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
    code = (f"import sys\nsys.stderr.write('Error: invalid credentials {secret}\\n')\n"
            "sys.exit(2)\n")
    entries = asyncio.run(_run_turn(tr.CliAgentTransport("fails"), _spec(code, tmp_path)))
    errors = [p for k, p in entries if k == tr.ERROR]
    assert len(errors) == 1
    assert "exited with status 2" in errors[0]
    assert "invalid credentials" in errors[0]
    assert secret not in errors[0]
    assert entries[-1] == (tr.EXIT, 2)


def test_a_cli_that_cannot_start_raises_oserror(tmp_path):
    spec = tr.SpawnSpec(argv=[str(tmp_path / "no-such-cli")], cwd=str(tmp_path), env={})

    async def run():
        t = tr.CliAgentTransport("missing")
        with pytest.raises(OSError):
            await t.start(spec)
        assert not t.running

    asyncio.run(run())


def test_the_child_gets_exactly_the_spec_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOULD_NOT_LEAK", "server-side-value")
    code = "import json, os; print(json.dumps(dict(os.environ)))"
    env = {"PATH": os.environ.get("PATH", ""), "ONLY_THIS": "1"}
    entries = asyncio.run(_run_turn(tr.CliAgentTransport("env"), _spec(code, tmp_path, env)))
    child_env = json.loads(next(p for k, p in entries if k == tr.OUT))
    assert child_env.get("ONLY_THIS") == "1"
    assert "SHOULD_NOT_LEAK" not in child_env


# ── bounded buffer, own cursors ──────────────────────────────────────────────────

def test_the_buffer_is_bounded_by_entries_and_keeps_absolute_seqs():
    buf = tr.OutputBuffer(max_entries=3, max_bytes=10_000)
    for i in range(5):
        buf.append(tr.OUT, f"l{i}")
    assert (buf.base_seq, buf.next_seq) == (2, 5)
    assert buf.lines() == ["l2", "l3", "l4"]
    entries, cursor, dropped = buf.read_from(0)
    assert dropped == 2 and cursor == 5
    assert [p for _, p in entries] == ["l2", "l3", "l4"]
    assert buf.read_from(4)[0] == [(tr.OUT, "l4")]


def test_the_buffer_is_bounded_by_bytes_but_keeps_the_newest_entry():
    buf = tr.OutputBuffer(max_entries=100, max_bytes=10)
    buf.append(tr.OUT, "aaaa")
    buf.append(tr.OUT, "bbbb")
    buf.append(tr.OUT, "c" * 50)
    assert buf.lines() == ["c" * 50]


def test_every_reader_follows_with_its_own_cursor(tmp_path):
    code = "import time\nfor i in range(30):\n    print(f'line {i}', flush=True); time.sleep(0.002)\n"

    async def run():
        t = tr.CliAgentTransport("fanout")
        start = await t.start(_spec(code, tmp_path))

        async def read():
            return [p async for k, p in t.follow(start) if k == tr.OUT]

        return await asyncio.gather(read(), read(), read())

    results = asyncio.run(run())
    expected = [f"line {i}" for i in range(30)]
    assert results == [expected, expected, expected]


def test_relay_tells_a_reader_that_fell_behind(tmp_path):
    async def run():
        t = tr.CliAgentTransport("slow", buffer=tr.OutputBuffer(max_entries=4))
        for i in range(6):
            t.buffer.append(tr.OUT, json.dumps({"n": i}))
        t.buffer.append(tr.EXIT, 0)
        sent = []

        async def send(frame):
            sent.append(frame)
            return True

        await tr.relay(t, send, lambda line: [{"type": "content", "data": line}], 0)
        return sent

    sent = asyncio.run(run())
    assert sent[0]["type"] == "info" and "dropped" in sent[0]["data"]
    assert sent[-1] == {"type": "completed", "data": None}


def test_relay_stops_when_the_client_is_gone(tmp_path):
    async def run():
        t = tr.CliAgentTransport("gone")
        for i in range(5):
            t.buffer.append(tr.OUT, str(i))
        t.buffer.append(tr.EXIT, 0)
        calls = []

        async def send(frame):
            calls.append(frame)
            return False

        await tr.relay(t, send, lambda line: [{"type": "raw", "data": line}], 0)
        return calls

    assert len(asyncio.run(run())) == 1


# ── process tree ──────────────────────────────────────────────────────────────

_TREE_CLI = r'''
import os, signal, subprocess, sys, time
d = sys.argv[1]
stubborn = subprocess.Popen([sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"])
escaped = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                           start_new_session=True)
open(os.path.join(d, "pids"), "w").write("%d %d" % (stubborn.pid, escaped.pid))
print("started", flush=True)
time.sleep(120)
'''


def test_stop_kills_the_whole_tree_even_a_stubborn_or_escaped_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "KILL_GRACE_SECONDS", 1.0)
    script = tmp_path / "tree.py"
    script.write_text(_TREE_CLI)
    spec = tr.SpawnSpec(argv=[sys.executable, str(script), str(tmp_path)], cwd=str(tmp_path),
                        env={"PATH": os.environ.get("PATH", "")})
    pids = []

    async def run():
        t = tr.CliAgentTransport("tree")
        start = await t.start(spec)
        async for kind, payload in t.follow(start):
            if kind == tr.OUT:
                break
        pids.extend(int(p) for p in (tmp_path / "pids").read_text().split())
        leader = t.pid
        await t.stop(grace=1.0)
        assert not t.running
        return leader

    leader = None
    try:
        leader = asyncio.run(run())
        for pid in [leader, *pids]:
            assert _wait_dead(pid), f"pid {pid} survived stop()"
    finally:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_a_tool_holding_stdout_open_does_not_keep_the_turn_running(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "PIPE_DRAIN_GRACE_SECONDS", 0.5)
    code = ("import subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "print(p.pid, flush=True)\n")
    child = []

    async def run():
        t = tr.CliAgentTransport("held")
        entries = await asyncio.wait_for(_run_turn(t, _spec(code, tmp_path)), 15)
        child.append(int(next(p for k, p in entries if k == tr.OUT)))
        return entries, t

    try:
        entries, t = asyncio.run(run())
        assert entries[-1] == (tr.EXIT, 0)
        assert not t.running
    finally:
        for pid in child:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_stop_leaves_a_finished_turn_and_its_server_alone(tmp_path):
    code = ("import subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
            "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "print(p.pid, flush=True)\n")
    child = []

    async def run():
        t = tr.CliAgentTransport("server")
        entries = await _run_turn(t, _spec(code, tmp_path))
        child.append(int(next(p for k, p in entries if k == tr.OUT)))
        await t.stop()

    try:
        asyncio.run(run())
        assert _alive(child[0]), "a server the agent started on purpose was killed"
    finally:
        for pid in child:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_on_line_and_on_exit_hooks_run_without_a_reader(tmp_path):
    seen = {"lines": [], "exit": None}

    async def run():
        t = tr.CliAgentTransport("hooks", on_line=seen["lines"].append,
                                 on_exit=lambda rc: seen.__setitem__("exit", rc))
        await t.start(_spec("print('a'); print('b')", tmp_path))
        assert await t.wait_finished(timeout=10)

    asyncio.run(run())
    assert seen == {"lines": ["a", "b"], "exit": 0}


# ── reaper ────────────────────────────────────────────────────────────────────

class _T:
    def __init__(self, running=False, idle=0.0):
        self.running = running
        self._idle = idle

    def idle_seconds(self, now=None):
        return self._idle


def test_the_reaper_evicts_only_finished_unattached_idle_sessions():
    import threading
    registry = {
        "done-idle": SimpleNamespace(transport=_T(idle=4000), websocket_clients=[]),
        "done-recent": SimpleNamespace(transport=_T(idle=10), websocket_clients=[]),
        "running": SimpleNamespace(transport=_T(running=True, idle=9999), websocket_clients=[]),
        "attached": SimpleNamespace(transport=_T(idle=9999), websocket_clients=[object()]),
        "no-transport": SimpleNamespace(transport=None, websocket_clients=[]),
    }
    evicted = []
    reaper = tr.SessionReaper(registry, threading.Lock(), label="t", idle_seconds=1800,
                              on_evict=evicted.append)
    assert reaper.sweep() == ["done-idle"]
    assert set(registry) == {"done-recent", "running", "attached", "no-transport"}
    assert len(evicted) == 1


def test_a_second_start_while_a_turn_runs_is_refused(tmp_path):
    async def run():
        t = tr.CliAgentTransport("busy")
        first = asyncio.ensure_future(t.start(_spec("import time; time.sleep(0.5)", tmp_path)))
        await asyncio.sleep(0)            # the first start is now spawning
        assert t.running
        with pytest.raises(RuntimeError):
            await t.start(_spec("print('second')", tmp_path))
        await first
        assert await t.wait_finished(timeout=10)
        return [k for k, _ in t.buffer.read_from(0)[0]]

    assert asyncio.run(run()) == [tr.EXIT]



# ── on_failure: a known failure is replaced within the same turn ─────────────────

def _failing(marker: str) -> str:
    return f"import sys\nprint('{{\"type\": \"result\", \"is_error\": true}}', flush=True)\n" \
           f"sys.stderr.write('{marker}\\n'); sys.exit(1)\n"


def test_on_failure_runs_the_replacement_in_the_same_turn(tmp_path):
    asked = []

    async def on_failure(returncode, diagnostics):
        asked.append((returncode, diagnostics))
        return _spec("print('second attempt')", tmp_path), "starting over"

    async def run():
        t = tr.CliAgentTransport("retry")
        start = await t.start(_spec(_failing("conversation gone"), tmp_path), on_failure=on_failure)
        return [e async for e in t.follow(start)], t

    entries, t = asyncio.run(run())
    assert (asked[0][0], "conversation gone" in asked[0][1], '"is_error"' in asked[0][1]) == (1, True, True)
    kinds = [k for k, _ in entries]
    assert tr.ERROR not in kinds
    assert entries[-3:] == [(tr.INFO, "starting over"), (tr.OUT, "second attempt"), (tr.EXIT, 0)]
    assert t.returncode == 0 and not t.running


def test_on_failure_is_asked_once_and_never_after_success(tmp_path):
    asked = []

    async def on_failure(returncode, diagnostics):
        asked.append(returncode)
        return _spec(_failing("still failing"), tmp_path), "again"

    async def run():
        t = tr.CliAgentTransport("once")
        ok = await _run_turn_with(t, _spec("print('fine')", tmp_path), on_failure)
        bad = await _run_turn_with(t, _spec(_failing("first"), tmp_path), on_failure)
        return ok, bad

    ok, bad = asyncio.run(run())
    assert ok[-1] == (tr.EXIT, 0)
    assert asked == [1]                     # not for the success, once for the failure
    assert bad[-1] == (tr.EXIT, 1)
    assert [p for k, p in bad if k == tr.ERROR] and "still failing" in [p for k, p in bad if k == tr.ERROR][0]


async def _run_turn_with(transport, spec, on_failure):
    start = await transport.start(spec, on_failure=on_failure)
    return [e async for e in transport.follow(start)]


def test_on_failure_that_declines_leaves_the_failure_as_it_was(tmp_path):
    async def on_failure(returncode, diagnostics):
        return None

    entries = asyncio.run(_run_turn_with(tr.CliAgentTransport("decline"),
                                         _spec(_failing("bad key"), tmp_path), on_failure))
    errors = [p for k, p in entries if k == tr.ERROR]
    assert errors and "bad key" in errors[0] and entries[-1] == (tr.EXIT, 1)
    assert tr.INFO not in [k for k, _ in entries]


def test_stop_while_a_replacement_runs_ends_the_turn(tmp_path):
    async def on_failure(returncode, diagnostics):
        return _spec("import time; print('replacement', flush=True); time.sleep(60)", tmp_path), "over"

    async def run():
        t = tr.CliAgentTransport("stop-retry")
        start = await t.start(_spec(_failing("gone"), tmp_path), on_failure=on_failure)
        async for kind, payload in t.follow(start):
            if payload == "replacement":
                break
        pid = t.pid
        await t.stop(grace=1.0)
        return t, pid

    t, pid = asyncio.run(run())
    assert not t.running and t.buffer.read_from(0)[0][-1][0] == tr.EXIT
    assert _wait_dead(pid)


def test_a_replacement_that_cannot_start_fails_the_turn(tmp_path):
    async def on_failure(returncode, diagnostics):
        return tr.SpawnSpec(argv=[str(tmp_path / "no-such-cli")], cwd=str(tmp_path), env={}), "over"

    entries = asyncio.run(_run_turn_with(tr.CliAgentTransport("nostart"),
                                         _spec(_failing("gone"), tmp_path), on_failure))
    errors = [p for k, p in entries if k == tr.ERROR]
    assert errors and "no-such-cli" in errors[0]
    assert entries[-1][0] == tr.EXIT and entries[-1][1] != 0


# ── readers across turns, and reconnect positions ────────────────────────────────

def test_a_cursor_from_an_earlier_turn_reaches_the_running_turn(tmp_path):
    async def run():
        t = tr.CliAgentTransport("turns")
        first = await _run_turn(t, _spec("print('turn one')", tmp_path))
        stale = 0                                    # a position inside turn one
        await t.start(_spec("import time; time.sleep(0.3); print('turn two')", tmp_path))
        got = [e async for e in t.follow(stale)]
        return first, got

    first, got = asyncio.run(run())
    assert first[-1] == (tr.EXIT, 0)
    # Turn one's EXIT is passed over; the reader ends with turn two's.
    assert [e for e in got if e[0] == tr.EXIT] == [(tr.EXIT, 0)]
    assert (tr.OUT, "turn two") in got


def test_resume_position_replays_a_detached_readers_gap_once():
    t = tr.CliAgentTransport("pos")
    t.buffer.append(tr.OUT, "a")
    t.buffer.append(tr.EXIT, 0)
    assert t.resume_position(None) == (None, 0)          # nothing running, nothing missed
    t.detach(0)
    assert t.resume_position(None) == (0, 0)             # replay the finished turn's tail
    assert t.resume_position(None) == (None, 0)          # used once
    t._turn_open = True
    t.turn_start_seq = t.buffer.next_seq
    t.buffer.append(tr.OUT, "b")
    assert t.resume_position(None) == (3, 0)             # live tail
    t.detach(2)
    t.detach(3)
    assert t.resume_position(None) == (2, 0)             # the earliest detached position
    assert t.resume_position(0) == (2, 2)                # an earlier turn: skipped, counted
    assert t.resume_position(99) == (3, 0)               # clamped to the tail


def test_relay_keeps_the_cursor_at_the_first_unsent_entry(tmp_path):
    async def run():
        t = tr.CliAgentTransport("cursor")
        await _run_turn(t, _spec("print('one'); print('two'); print('three')", tmp_path))
        sent = []

        async def send(frame):
            if frame.get("data") == "two":
                return False                     # the socket went away here
            sent.append(frame)
            return True

        cursor = tr.Cursor(0)
        await tr.relay(t, send, lambda line: [{"type": "content", "data": line}], 0, cursor)
        return sent, cursor.seq

    sent, seq = asyncio.run(run())
    assert [f["data"] for f in sent] == ["one"]
    assert seq == 1                              # "two" (seq 1) is replayed on reconnect


def test_a_missing_working_directory_is_not_called_a_missing_cli(tmp_path):
    missing = str(tmp_path / "gone")
    spec = tr.SpawnSpec(argv=[sys.executable, "-c", "pass"], cwd=missing, env={})

    async def run():
        try:
            await tr.CliAgentTransport("nodir").start(spec)
        except OSError as e:
            return e
        raise AssertionError("start() did not fail")

    error = asyncio.run(run())
    text = tr.spawn_error_text(error, spec, "Claude Code", "Install it.")
    assert missing in text and "working directory" in text and "Install" not in text
    no_cli = tr.SpawnSpec(argv=[str(tmp_path / "claude")], cwd=str(tmp_path), env={})
    text = tr.spawn_error_text(FileNotFoundError(2, "No such file or directory", no_cli.argv[0]),
                               no_cli, "Claude Code", "Install it.")
    assert text.startswith("Could not start the Claude Code CLI (claude)") and "Install it." in text
