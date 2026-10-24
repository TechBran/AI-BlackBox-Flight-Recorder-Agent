"""The one transport for every CLI agent that streams JSON lines.

Audit rank 56 (O06-frontier-agent-27, -03, -15, -16, O10-telephony-twilio-06).
The Portal/Android agent sockets (/ws/agent, /ws/gemini-agent) and the phone
Claude backend each spawned the CLI with subprocess.Popen, flipped its stdout
to O_NONBLOCK and polled it, three copies with different bugs:

  * stderr was a pipe nobody read: once ~64 KB of warnings piled up the CLI
    blocked on write and the turn never ended;
  * one cursor per session was shared by every socket, so a second client
    (Portal + phone, or a reconnect while the old socket's reader still ran)
    stole lines from the first;
  * every line of every turn stayed in an unbounded list, and finished
    sessions were never evicted;
  * terminate() signalled only the CLI, orphaning the tools it ran;
  * the phone path read the non-blocking pipe with select()+readline(), which
    hands back half a JSON line when a write straddles the pipe buffer (the
    halves both failed json.loads and were dropped) and stalled the event loop.

CliAgentTransport runs one CLI turn at a time as an asyncio subprocess in its
own process group. A pump task reads stdout as whole lines into a bounded,
sequence-numbered OutputBuffer and drains stderr; when the CLI exits it
appends an ERROR entry (non-zero exit: the stderr tail) and an EXIT entry. A
consumer never shares a cursor: follow(since_seq) yields entries from its own
position, and relay() forwards them to one socket, so cancelling that socket's
task on disconnect touches nobody else. stop() kills the whole process tree.
SessionReaper evicts finished sessions nobody watches.

Everything here runs on the event loop that started the turn; nothing blocks it.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

from starlette.websockets import WebSocketState

from Orchestrator.secret_redaction import redact

# Entry kinds in an OutputBuffer.
OUT = "out"          # one stdout line (str)
ERROR = "error"      # why a turn failed (str): the stderr tail of a non-zero exit
EXIT = "exit"        # the turn ended (int return code); always the turn's last entry
DROPPED = "dropped"  # yielded by follow() only: entries evicted before a slow reader got them

# Bounds. A session keeps at most this much output for late readers and
# /agent/session; older entries are evicted first (the newest is always kept).
MAX_BUFFER_ENTRIES = 5000
MAX_BUFFER_BYTES = 8 * 1024 * 1024
# A single stdout line longer than this is dropped (logged), never held: a
# stream-json event is far smaller (Claude's Read tool caps a file at ~256 KB).
MAX_LINE_BYTES = 4 * 1024 * 1024
_READ_CHUNK = 64 * 1024

# stderr: the last lines are kept for the error entry; a few are logged per turn.
STDERR_TAIL_LINES = 40
STDERR_TAIL_CHARS = 2000
STDERR_LOG_LINES = 20

# After the CLI exits its output is already in the pipe. A tool it backgrounded
# can inherit the pipe and hold it open forever, so the readers get this long
# to finish, not until EOF.
PIPE_DRAIN_GRACE_SECONDS = 2.0
# stop(): SIGTERM the tree, wait this long, then SIGKILL what is left.
KILL_GRACE_SECONDS = 5.0

# SessionReaper: a finished session nobody is attached to is evicted after
# this much idle time (its resume id survives where the caller keeps it).
SESSION_IDLE_SECONDS = 30 * 60
REAP_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True)
class SpawnSpec:
    """The exact command a turn runs: argv, working directory and child env."""
    argv: List[str]
    cwd: str
    env: Dict[str, str] = field(repr=False)


class OutputBuffer:
    """A bounded log of (kind, payload) entries with absolute sequence numbers.

    Entry n has seq n for the life of the session. ``base_seq`` is the seq of
    the oldest entry still held and ``next_seq`` the seq the next append gets.
    Readers keep their own cursor (a seq); a cursor below ``base_seq`` lost the
    evicted entries and read_from() reports how many.
    """

    def __init__(self, max_entries: int = MAX_BUFFER_ENTRIES, max_bytes: int = MAX_BUFFER_BYTES):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries: deque = deque()   # (kind, payload, size)
        self._bytes = 0
        self.base_seq = 0
        self._changed = asyncio.Event()

    @property
    def next_seq(self) -> int:
        return self.base_seq + len(self._entries)

    def append(self, kind: str, payload: Any) -> int:
        """Add an entry, evict the oldest past the bounds, wake every waiting reader."""
        seq = self.next_seq
        size = len(payload) if isinstance(payload, str) else 16
        self._entries.append((kind, payload, size))
        self._bytes += size
        while len(self._entries) > 1 and (len(self._entries) > self.max_entries
                                          or self._bytes > self.max_bytes):
            _, _, old_size = self._entries.popleft()
            self._bytes -= old_size
            self.base_seq += 1
        woken, self._changed = self._changed, asyncio.Event()
        woken.set()
        return seq

    def read_from(self, cursor: int) -> Tuple[List[Tuple[str, Any]], int, int]:
        """``(entries, new_cursor, dropped)`` for everything at or after ``cursor``."""
        cursor = min(max(cursor, 0), self.next_seq)
        dropped = 0
        if cursor < self.base_seq:
            dropped = self.base_seq - cursor
            cursor = self.base_seq
        start = cursor - self.base_seq
        entries = [(kind, payload) for kind, payload, _ in list(self._entries)[start:]]
        return entries, cursor + len(entries), dropped

    async def wait_beyond(self, cursor: int) -> None:
        """Return once an entry with seq >= ``cursor`` exists."""
        while cursor >= self.next_seq:
            await self._changed.wait()

    def lines(self) -> List[str]:
        """The stdout lines still held, oldest first (what /agent/session serves)."""
        return [payload for kind, payload, _ in self._entries if kind == OUT]


async def iter_lines(stream: asyncio.StreamReader, max_line_bytes: int = MAX_LINE_BYTES,
                     label: str = "") -> AsyncIterator[str]:
    """Whole, stripped, non-empty lines from ``stream`` until EOF.

    A line only comes out once its newline (or EOF) arrived, however the writer
    split it across pipe writes. A line over ``max_line_bytes`` is dropped up
    to its newline instead of being held in memory.
    """
    pending = bytearray()
    skipping = False
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        pending += chunk
        while True:
            nl = pending.find(b"\n")
            if nl < 0:
                break
            raw = bytes(pending[:nl])
            del pending[:nl + 1]
            if skipping:
                skipping = False
                continue
            if len(raw) > max_line_bytes:
                print(f"[CLI-TRANSPORT] {label}: dropped an output line over {max_line_bytes} bytes")
                continue
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                yield text
        if len(pending) > max_line_bytes:
            if not skipping:
                print(f"[CLI-TRANSPORT] {label}: dropped an output line over {max_line_bytes} bytes")
            pending.clear()
            skipping = True
    if pending and not skipping:
        text = bytes(pending).decode("utf-8", errors="replace").strip()
        if text:
            yield text


def _signal_group(pgid: int, sig: int) -> bool:
    """Signal a process group; False when it no longer exists."""
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError as e:
        print(f"[CLI-TRANSPORT] cannot signal process group {pgid}: {e}")
        return False


def _group_alive(pgid: int) -> bool:
    return _signal_group(pgid, 0)


async def wait_reaped(proc, timeout: Optional[float] = None) -> bool:
    """Wait until the child itself has exited; False on timeout.

    asyncio's Process.wait() also waits for every pipe to close, and a tool the
    CLI started can inherit its stdout and hold it open for as long as it runs.
    ``returncode`` is set as soon as the child is reaped, so poll that instead.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while proc.returncode is None:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


def _group_is_ours(proc) -> bool:
    """Whether ``proc.pid`` still names the process group this turn started.

    The CLI is spawned with start_new_session, so its pid is its group id. While
    asyncio has not reaped it the pid cannot be reused. Once reaped, the kernel
    keeps the number reserved while any member of the group lives; if a live
    process now HAS that pid, the number was handed out again and the group
    with that id is somebody else's.
    """
    if proc.returncode is None:
        return True
    try:
        os.kill(proc.pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _escaped_descendants(pid: int, pgid: int) -> list:
    """Live descendants of ``pid`` that moved to another process group (a tool
    that called setsid), which a group signal would miss."""
    try:
        import psutil
        children = psutil.Process(pid).children(recursive=True)
    except Exception:  # noqa: BLE001 — psutil missing, or the process just exited
        return []
    escaped = []
    for child in children:
        try:
            if os.getpgid(child.pid) != pgid:
                escaped.append(child)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return escaped


async def kill_process_tree(proc, grace: float = KILL_GRACE_SECONDS, label: str = "") -> None:
    """SIGTERM a CLI's whole process tree, wait up to ``grace``, then SIGKILL.

    The CLI leads its own process group (start_new_session), so the group
    signal reaches every tool it ran; a descendant that left the group is
    signalled by pid. Never blocks the event loop.
    """
    pgid = proc.pid
    if not _group_is_ours(proc):
        return
    escaped = _escaped_descendants(proc.pid, pgid) if proc.returncode is None else []
    _signal_group(pgid, signal.SIGTERM)
    for child in escaped:
        try:
            child.terminate()
        except Exception:  # noqa: BLE001 — it exited on its own
            pass
    deadline = time.monotonic() + grace
    await wait_reaped(proc, timeout=grace)
    while time.monotonic() < deadline and _group_alive(pgid):
        await asyncio.sleep(0.05)
    if _group_alive(pgid):
        print(f"[CLI-TRANSPORT] {label}: process group {pgid} ignored SIGTERM, sending SIGKILL")
        _signal_group(pgid, signal.SIGKILL)
    for child in escaped:
        try:
            if child.is_running():
                child.kill()
        except Exception:  # noqa: BLE001
            pass


class CliAgentTransport:
    """One agent session's CLI plumbing: one turn (process) at a time.

    ``on_line(line)`` runs for every stdout line whether or not anyone is
    attached (session-id capture, activity stamps); ``on_exit(returncode)``
    runs once per turn right after its EXIT entry. Both are synchronous and
    must not raise (a raise is logged and ignored).
    """

    def __init__(self, label: str, *, buffer: Optional[OutputBuffer] = None,
                 on_line: Optional[Callable[[str], None]] = None,
                 on_exit: Optional[Callable[[int], None]] = None):
        self.label = label
        self.buffer = buffer if buffer is not None else OutputBuffer()
        self.on_line = on_line
        self.on_exit = on_exit
        self.returncode: Optional[int] = None
        self.last_activity = time.monotonic()
        self._proc = None
        self._pump: Optional[asyncio.Task] = None
        self._turn_open = False
        self._starting = False
        self._stopping = False
        self._stderr_tail: deque = deque(maxlen=STDERR_TAIL_LINES)
        self._cli_name = ""

    @property
    def running(self) -> bool:
        """True from start() until the turn's EXIT entry is appended."""
        return self._turn_open or self._starting

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    def idle_seconds(self, now: Optional[float] = None) -> float:
        return (time.monotonic() if now is None else now) - self.last_activity

    def stdout_lines(self) -> List[str]:
        return self.buffer.lines()

    async def start(self, spec: SpawnSpec) -> int:
        """Spawn one turn. Returns the seq of its first entry (where its reader
        starts). Raises OSError when the CLI cannot be started (not installed)."""
        if self._turn_open or self._starting:
            raise RuntimeError(f"{self.label}: a turn is already running")
        start_seq = self.buffer.next_seq
        self._stopping = False
        self.returncode = None
        self._stderr_tail.clear()
        self._cli_name = os.path.basename(spec.argv[0]) if spec.argv else "CLI"
        # stdin is never a pipe: print mode reads nothing from it, and an open,
        # never-closed pipe makes a CLI wait for piped input.
        self._starting = True
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *spec.argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=spec.cwd,
                env=spec.env,
                start_new_session=True,
            )
        finally:
            self._starting = False
        self._turn_open = True
        self.last_activity = time.monotonic()
        self._pump = asyncio.create_task(self._run(self._proc), name=f"cli-pump-{self.label}")
        print(f"[CLI-TRANSPORT] {self.label}: started {self._cli_name} "
              f"PID={self._proc.pid} in {spec.cwd}")
        return start_seq

    async def _read_stdout(self, stream) -> None:
        async for line in iter_lines(stream, label=self.label):
            self.last_activity = time.monotonic()
            self.buffer.append(OUT, line)
            if self.on_line is not None:
                try:
                    self.on_line(line)
                except Exception as e:  # noqa: BLE001 — a hook never breaks the pump
                    print(f"[CLI-TRANSPORT] {self.label}: on_line hook failed: {e}")

    async def _drain_stderr(self, stream) -> None:
        logged = 0
        async for line in iter_lines(stream, max_line_bytes=64 * 1024, label=self.label):
            self._stderr_tail.append(line)
            if logged < STDERR_LOG_LINES:
                print(f"[CLI-TRANSPORT] {self.label} stderr: {redact(line[:300])}")
                logged += 1

    def _failure_text(self, rc: int) -> str:
        name = self._cli_name or "CLI"
        how = f"was killed by signal {-rc}" if rc < 0 else f"exited with status {rc}"
        tail = "\n".join(self._stderr_tail).strip()
        if len(tail) > STDERR_TAIL_CHARS:
            tail = "..." + tail[-STDERR_TAIL_CHARS:]
        return redact(f"{name} {how}" + (f": {tail}" if tail else "."))

    async def _run(self, proc) -> None:
        rc: Optional[int] = None
        readers = [asyncio.create_task(self._read_stdout(proc.stdout)),
                   asyncio.create_task(self._drain_stderr(proc.stderr))]
        try:
            # The turn ends when the CLI does: both pipes at EOF, or the CLI gone
            # and its pipes given PIPE_DRAIN_GRACE_SECONDS to empty (a tool it
            # backgrounded may hold them open indefinitely).
            pending = set(readers)
            while pending and proc.returncode is None:
                _, pending = await asyncio.wait(pending, timeout=0.1)
            if pending:
                _, pending = await asyncio.wait(pending, timeout=PIPE_DRAIN_GRACE_SECONDS)
            for task in pending:
                task.cancel()
            if pending:
                print(f"[CLI-TRANSPORT] {self.label}: a process the CLI started still holds its output open; "
                      f"ending the turn without it")
                # Release this side of the pipes now (Process exposes no close).
                pipe_transport = getattr(proc, "_transport", None)
                if pipe_transport is not None:
                    pipe_transport.close()
            for task in readers:
                if task.done() and not task.cancelled() and task.exception() is not None:
                    print(f"[CLI-TRANSPORT] {self.label}: reader failed: {task.exception()}")
            await wait_reaped(proc)
            rc = proc.returncode
        except asyncio.CancelledError:
            # The loop is shutting down under a running turn: do not leave the
            # CLI and its tools behind.
            for task in readers:
                task.cancel()
            if proc.returncode is None:
                _signal_group(proc.pid, signal.SIGKILL)
            raise
        finally:
            self.returncode = proc.returncode if rc is None else rc
            if self.returncode not in (0, None) and not self._stopping:
                self.buffer.append(ERROR, self._failure_text(self.returncode))
            # running flips with the EXIT append, in the same step, so a caller
            # that sees running=True can always wait for this EXIT entry.
            self._turn_open = False
            self.last_activity = time.monotonic()
            self.buffer.append(EXIT, self.returncode if self.returncode is not None else -1)
            print(f"[CLI-TRANSPORT] {self.label}: turn ended (rc={self.returncode}"
                  f"{', stopped' if self._stopping else ''})")
            if self.on_exit is not None:
                try:
                    self.on_exit(self.returncode if self.returncode is not None else -1)
                except Exception as e:  # noqa: BLE001
                    print(f"[CLI-TRANSPORT] {self.label}: on_exit hook failed: {e}")

    async def wait_finished(self, timeout: Optional[float] = None) -> bool:
        """Wait for the current turn to end; False if it is still running after ``timeout``."""
        pump = self._pump
        if pump is None or pump.done():
            return True
        try:
            await asyncio.wait_for(asyncio.shield(pump), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            if pump.done():
                return True
            raise
        except Exception:  # noqa: BLE001 — the pump's own error is already logged
            return True
        return True

    async def stop(self, grace: float = KILL_GRACE_SECONDS) -> None:
        """End the running turn: kill the CLI's whole process tree, then wait
        for its EXIT entry. A finished turn is left alone (a server the agent
        started on purpose keeps running)."""
        proc = self._proc
        if proc is None or not self._turn_open:
            return
        self._stopping = True
        await kill_process_tree(proc, grace=grace, label=self.label)
        await self.wait_finished(timeout=PIPE_DRAIN_GRACE_SECONDS + 1.0)

    async def finish_or_stop(self, wait: float) -> None:
        """Give a running turn ``wait`` seconds to end on its own, then stop it."""
        if self._turn_open and not await self.wait_finished(timeout=wait):
            print(f"[CLI-TRANSPORT] {self.label}: previous turn still running after {wait:.0f}s, stopping it")
            await self.stop()

    async def follow(self, since_seq: int) -> AsyncIterator[Tuple[str, Any]]:
        """Yield entries from ``since_seq`` on, through the next EXIT entry.

        A reader that fell behind the buffer's bounds gets one (DROPPED, n)
        first. Every caller has its own cursor; nobody else's reads move it.
        """
        cursor = since_seq
        while True:
            entries, cursor, dropped = self.buffer.read_from(cursor)
            if dropped:
                yield DROPPED, dropped
            for kind, payload in entries:
                yield kind, payload
                if kind == EXIT:
                    return
            if not entries:
                await self.buffer.wait_beyond(cursor)


Send = Callable[[dict], Awaitable[bool]]
Translate = Callable[[str], List[dict]]


async def safe_ws_send(websocket, data: dict) -> bool:
    """Send JSON to a WebSocket; False (never a raise) when it is gone."""
    try:
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.send_json(data)
            return True
    except Exception:  # noqa: BLE001 — a dead socket is an answer, not an error
        pass
    return False


def dropped_notice(count: int) -> dict:
    return {"type": "info",
            "data": f"{count} earlier output lines were dropped (this session keeps a bounded tail)."}


async def relay(transport: CliAgentTransport, send: Send, translate: Translate,
                since_seq: int) -> None:
    """Forward one turn to one client, from that client's own cursor.

    stdout lines go through ``translate`` (provider event -> client frames);
    a failed turn's ERROR entry becomes an ``error`` frame and its EXIT a
    ``completed`` frame. Returns at EXIT, or as soon as ``send`` reports the
    client gone. Run it as that client's task and cancel it on disconnect.
    """
    async for kind, payload in transport.follow(since_seq):
        if kind == OUT:
            try:
                frames = translate(payload)
            except Exception as e:  # noqa: BLE001 — one odd event never ends the stream
                print(f"[CLI-TRANSPORT] {transport.label}: could not translate a line: {e}")
                frames = []
        elif kind == ERROR:
            frames = [{"type": "error", "data": payload}]
        elif kind == DROPPED:
            frames = [dropped_notice(payload)]
        elif kind == EXIT:
            frames = [{"type": "completed", "data": None}]
        else:
            frames = []
        for frame in frames:
            if not await send(frame):
                return


def cancel_task(task: Optional[asyncio.Task]) -> None:
    if task is not None and not task.done():
        task.cancel()


class SessionReaper:
    """Evicts finished sessions nobody is attached to (audit O06-frontier-agent-15).

    ``registry`` maps id -> session; a session is evictable when its
    ``transport`` exists and is not running, its ``websocket_clients`` list is
    empty and the transport has been idle for ``idle_seconds``. ``on_evict``
    runs for each evicted session after the lock is released (persist what a
    later resume needs).
    """

    def __init__(self, registry: Dict[str, Any], lock, *, label: str,
                 idle_seconds: float = SESSION_IDLE_SECONDS,
                 interval: float = REAP_INTERVAL_SECONDS,
                 on_evict: Optional[Callable[[Any], None]] = None):
        self.registry = registry
        self.lock = lock
        self.label = label
        self.idle_seconds = idle_seconds
        self.interval = interval
        self.on_evict = on_evict
        self._task: Optional[asyncio.Task] = None

    def sweep(self, now: Optional[float] = None) -> List[str]:
        now = time.monotonic() if now is None else now
        evicted = []
        with self.lock:
            for session_id, session in list(self.registry.items()):
                transport = getattr(session, "transport", None)
                if transport is None or transport.running:
                    continue
                if getattr(session, "websocket_clients", None):
                    continue
                if transport.idle_seconds(now) < self.idle_seconds:
                    continue
                evicted.append((session_id, self.registry.pop(session_id)))
        for session_id, session in evicted:
            if self.on_evict is not None:
                try:
                    self.on_evict(session)
                except Exception as e:  # noqa: BLE001
                    print(f"[CLI-TRANSPORT] {self.label} reaper: on_evict failed for {session_id[:8]}: {e}")
        if evicted:
            print(f"[CLI-TRANSPORT] {self.label} reaper: evicted {len(evicted)} finished session(s)")
        return [session_id for session_id, _ in evicted]

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval)
                self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a maintenance loop must never die
                print(f"[CLI-TRANSPORT] {self.label} reaper sweep failed (non-fatal): {e}")

    def start(self) -> asyncio.Task:
        """Start the periodic sweep on the running loop (from an app startup hook)."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._loop())
        return self._task
