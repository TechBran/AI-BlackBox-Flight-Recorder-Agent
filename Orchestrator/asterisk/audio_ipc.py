#!/usr/bin/env python3
"""
audio_ipc.py - Main-process side of the audio subprocess IPC.

Spawns the audio subprocess and communicates via a Unix socketpair
using a binary framing protocol. voice_bridge.py and asterisk_routes.py
use its per-channel interface.

The child is started isolated (python -I) on the AudioSocket address from
Orchestrator.asterisk.config, and counts as running only after it says READY:
a flag set at spawn time reported a child that had already died (audit W14).

The child is supervised: when it dies, the calls it carried end at once and it
is restarted with exponential backoff; after MAX_RESTARTS failures in a row
the supervisor gives up with a loud error. Before, nothing respawned it and
every later call timed out at wait_for_channel until a service restart
(O11-telephony-asterisk-cellular-10).

IPC Protocol (matches audio_subprocess.py):
  [type: 1 byte] [length: 4 bytes LE uint32] [payload]

  0x01 = PHONE_AUDIO   subprocess→main   uuid(36B ASCII) + 8kHz PCM16
  0x02 = AI_AUDIO       main→subprocess   uuid(36B ASCII) + 24kHz PCM16
  0x03 = CHANNEL_OPEN   subprocess→main   uuid string (36B)
  0x04 = CHANNEL_CLOSE  subprocess→main   uuid string (36B)
  0x05 = CLEAR_BUFFER   main→subprocess   uuid string (36B)
  0x06 = SHUTDOWN        main→subprocess   empty payload
  0x07 = READY           subprocess→main   "host:port" once AudioSocket listens
"""

import asyncio
import collections
import os
import socket
import struct
import sys
import time
from typing import Optional, Callable, Awaitable, Dict, Tuple

# IPC message types
MSG_PHONE_AUDIO = 0x01
MSG_AI_AUDIO = 0x02
MSG_CHANNEL_OPEN = 0x03
MSG_CHANNEL_CLOSE = 0x04
MSG_CLEAR_BUFFER = 0x05
MSG_SHUTDOWN = 0x06
MSG_READY = 0x07

# IPC header: 1 byte type + 4 bytes LE uint32 length
IPC_HEADER_SIZE = 5
IPC_HEADER_FMT = "<BI"

SUBPROCESS_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_subprocess.py")

# How long the child may take to import numpy/scipy, bind and say READY.
READY_TIMEOUT_S = 20.0

# Supervision: restart delays double from the first to the cap; a child that
# stayed up STABLE_AFTER_S resets the count, and MAX_RESTARTS failures in a row
# (failed starts, or deaths sooner than that) make the supervisor give up.
RESTART_BACKOFF_INITIAL_S = 1.0
RESTART_BACKOFF_MAX_S = 30.0
STABLE_AFTER_S = 60.0
MAX_RESTARTS = 6

# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_ipc_client: Optional["AudioIPCClient"] = None


def get_ipc_client() -> Optional["AudioIPCClient"]:
    """Get the singleton IPC client."""
    return _ipc_client


async def init_ipc_client() -> Optional["AudioIPCClient"]:
    """Initialize and start the singleton IPC client.

    Returns the client even when its child is not running, so the caller can
    say why (``last_error``); its supervisor keeps trying, and call paths
    check ``is_running``."""
    global _ipc_client
    if _ipc_client and (_ipc_client.is_running or _ipc_client.supervising):
        return _ipc_client
    client = AudioIPCClient()
    await client.start()
    _ipc_client = client
    return client


# ---------------------------------------------------------------------------
# AudioIPCClient
# ---------------------------------------------------------------------------
class AudioIPCClient:
    """
    Spawns the audio subprocess and provides its channel interface:
      - expect_channel / wait_for_channel
      - register_channel / unregister_channel
      - send_ai_audio (24kHz PCM16 → subprocess handles resampling + pacing)
      - clear_buffer (barge-in)
      - get_channel_rate (always 8000 — subprocess handles conversion)
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        max_restarts: int = MAX_RESTARTS,
        backoff_initial_s: float = RESTART_BACKOFF_INITIAL_S,
        backoff_max_s: float = RESTART_BACKOFF_MAX_S,
    ):
        from Orchestrator.asterisk import config as asterisk_config
        # The address the provisioned dialplan's AudioSocket() dials.
        self.host = host or asterisk_config.ASTERISK_AUDIOSOCKET_HOST
        self.port = int(asterisk_config.ASTERISK_AUDIOSOCKET_PORT if port is None else port)

        # Supervision state (status() reports it in GET /asterisk/status)
        self.max_restarts = max_restarts
        self.backoff_initial_s = backoff_initial_s
        self.backoff_max_s = backoff_max_s
        self.restarts = 0
        self.gave_up = False
        self._stopping = False
        self._supervisor_task: Optional[asyncio.Task] = None
        self._ready_at = 0.0
        # Set when the current child's IPC ends (it died or closed the socket).
        self._died: asyncio.Event = asyncio.Event()

        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._running = False
        # Resolved with the bound "host:port" by READY; failed if IPC ends first.
        self._ready: Optional[asyncio.Future] = None
        self._address: Optional[Tuple[str, int]] = None
        self.last_error = ""
        # The child's last log lines, quoted when it fails to start.
        self._output_tail: collections.deque = collections.deque(maxlen=20)

        self._listener_task: Optional[asyncio.Task] = None
        self._log_task: Optional[asyncio.Task] = None

        # Channel UUID → asyncio.Event (fires when subprocess reports CHANNEL_OPEN)
        self._connect_events: Dict[str, asyncio.Event] = {}
        # Active channel tracking
        self._active_channels: set = set()

        # Per-channel callbacks
        self._audio_handlers: Dict[str, Callable[[str, bytes], Awaitable[None]]] = {}
        self._hangup_handlers: Dict[str, Callable[[str], Awaitable[None]]] = {}
        # Per-channel callbacks run as tasks; held here until they end.
        self._handler_tasks: set = set()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """True from the child's READY until it, or its IPC socket, goes away."""
        return self._running and not self._died.is_set()

    @property
    def address(self) -> Optional[Tuple[str, int]]:
        """(host, port) the child's AudioSocket server listens on, once READY."""
        return self._address

    @property
    def pid(self) -> Optional[int]:
        return self._process.pid if self._process else None

    @property
    def active_channels(self) -> set:
        return self._active_channels.copy()

    @property
    def supervising(self) -> bool:
        """True while the supervisor watches the child or retries a start."""
        return bool(self._supervisor_task and not self._supervisor_task.done())

    def status(self) -> dict:
        """The audio subprocess as GET /asterisk/status reports it."""
        return {
            "running": self.is_running,
            "pid": self.pid if self.is_running else None,
            "address": f"{self._address[0]}:{self._address[1]}" if self._address and self.is_running else None,
            "restarts": self.restarts,
            "gave_up": self.gave_up,
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _python() -> str:
        """The venv's python (the child needs numpy and scipy)."""
        venv_python = os.path.join(os.path.dirname(__file__), "..", "venv", "bin", "python3")
        return venv_python if os.path.exists(venv_python) else sys.executable

    def _argv(self, child_fd: int) -> list:
        """The child's command line. -I keeps this directory off its sys.path
        (and ignores PYTHON* variables), so no module here can shadow the
        stdlib for numpy and scipy; -u sends its log lines as they happen.
        The script path stays in argv: the unit's ExecStartPre matches it."""
        return [self._python(), "-I", "-u", SUBPROCESS_SCRIPT,
                str(child_fd), self.host, str(self.port)]

    async def start(self) -> bool:
        """Start the audio subprocess and supervise it from then on.

        True only once the child listens on its AudioSocket address. On
        False, ``last_error`` says why; the supervisor keeps retrying with
        backoff either way, until shutdown() or it gives up."""
        self._stopping = False
        self.gave_up = False
        started = await self._spawn()
        if not self._stopping:
            self._supervisor_task = asyncio.create_task(
                self._supervise(up=started), name="asterisk-audio-supervisor")
        return started

    async def _supervise(self, up: bool) -> None:
        """Restart the child when it dies, with exponential backoff.
        ``up``: the last start reached READY (that child may die any time)."""
        failures = 0 if up else 1
        delay = self.backoff_initial_s
        try:
            while not self._stopping:
                if up:
                    await self._died.wait()
                    if self._stopping:
                        return
                    up = False
                    lived = time.monotonic() - self._ready_at
                    await self._wait_exit(timeout=5.0)  # keep its last lines
                    await self._stop_child()
                    code = self._process.returncode if self._process else None
                    self._end_calls()
                    self.last_error = f"exited with code {code} after {lived:.0f}s"
                    tail = " | ".join(self._output_tail)
                    print(f"[IPC] Audio subprocess died ({self.last_error})"
                          + (f"; last output: {tail}" if tail else ""))
                    if lived >= STABLE_AFTER_S:
                        failures, delay = 0, self.backoff_initial_s
                    failures += 1
                if failures >= self.max_restarts:
                    self.gave_up = True
                    print(f"[IPC] ERROR: the audio subprocess failed {failures} times in a row;"
                          f" giving up. Asterisk calls are refused until blackbox.service"
                          f" restarts. Last error: {self.last_error}")
                    return
                print(f"[IPC] Restarting the audio subprocess in {delay:g}s"
                      f" (failure {failures} of {self.max_restarts})")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.backoff_max_s)
                if self._stopping:
                    return
                up = await self._spawn()
                if up:
                    self.restarts += 1
                else:
                    failures += 1
        except asyncio.CancelledError:
            pass

    def _end_calls(self) -> None:
        """The child died: end the calls it carried and fail their waiters."""
        lost = list(self._active_channels)
        self._active_channels.clear()
        for event in self._connect_events.values():
            event.set()  # wait_for_channel sees the channel is not open: False
        self._connect_events.clear()
        for channel_id in lost:
            handler = self._hangup_handlers.get(channel_id)
            if handler:
                self._spawn_handler(handler(channel_id))
        if lost:
            print(f"[IPC] Ended {len(lost)} call(s) the audio subprocess carried")

    def _spawn_handler(self, coro) -> None:
        """Run a per-channel callback as its own task, held until it ends
        (the loop keeps only weak references to tasks)."""
        task = asyncio.ensure_future(coro)
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    async def _spawn(self) -> bool:
        """Spawn one audio subprocess, wire up IPC and wait for its READY.

        True only once the child listens on its AudioSocket address. On
        False, ``last_error`` says why and no child is left running."""
        self.last_error = ""
        self._address = None
        self._output_tail.clear()
        parent_sock = None
        try:
            parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self._argv(child_sock.fileno()),
                    pass_fds=(child_sock.fileno(),),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            finally:
                child_sock.close()  # Parent doesn't need the child end

            # Wrap our socket end as asyncio streams
            reader, writer = await asyncio.open_connection(sock=parent_sock)
            parent_sock = None  # the writer owns it now
            self._reader = reader
            self._writer = writer
            self._ready = asyncio.get_running_loop().create_future()
            self._died = asyncio.Event()

            # Start listener for messages from subprocess
            self._listener_task = asyncio.create_task(self._ipc_listener())
            # Forward subprocess stdout to our logging
            self._log_task = asyncio.create_task(self._log_forwarder())
        except Exception as e:
            self.last_error = f"could not spawn it: {e}"
            print(f"[IPC] Failed to start audio subprocess: {e}")
            if parent_sock is not None:
                parent_sock.close()
            await self._stop_child()
            return False

        try:
            bound = await asyncio.wait_for(asyncio.shield(self._ready), timeout=READY_TIMEOUT_S)
        except asyncio.TimeoutError:
            self._ready.cancel()
            self.last_error = f"no READY within {READY_TIMEOUT_S:.0f}s"
        except ConnectionError:
            # IPC ended before READY: the child died (an import or bind
            # failure). Let the forwarder pass on its last lines first.
            await self._wait_exit(timeout=5.0)
            code = self._process.returncode if self._process else None
            self.last_error = f"exited with code {code} before it listened"
        else:
            host, _, port = bound.rpartition(":")
            self._address = (host, int(port))
            self._running = True
            self._ready_at = time.monotonic()
            print(f"[IPC] Audio subprocess ready (pid {self._process.pid}), "
                  f"AudioSocket listening on {bound}")
            return True

        tail = " | ".join(self._output_tail)
        if tail:
            self.last_error += f"; last output: {tail}"
        if "address already in use" in tail.lower():
            self.last_error += (f" (port {self.port} is in use, often by an orphaned"
                                f" audio_subprocess.py of an earlier run)")
        print(f"[IPC] Audio subprocess failed to start: {self.last_error}")
        await self._stop_child()
        return False

    async def _wait_exit(self, timeout: float) -> None:
        """Wait for the child to exit and its output to be forwarded."""
        if self._process:
            try:
                await asyncio.wait_for(self._process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return
        if self._log_task:
            try:
                await asyncio.wait_for(asyncio.shield(self._log_task), timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass

    async def _stop_child(self) -> None:
        """Stop the child (SHUTDOWN, then SIGTERM, then SIGKILL) and its tasks."""
        self._running = False
        process = self._process
        if self._writer and process and process.returncode is None:
            try:
                header = struct.pack(IPC_HEADER_FMT, MSG_SHUTDOWN, 0)
                self._writer.write(header)
                await self._writer.drain()
            except Exception:
                pass

        if process and process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except (asyncio.TimeoutError, ProcessLookupError):
                    pass

        # Cancel background tasks
        for task in [self._listener_task, self._log_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Close writer
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None
        self._listener_task = None
        self._log_task = None

    async def shutdown(self):
        """Stop supervising, then stop the subprocess."""
        self._stopping = True
        task = self._supervisor_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._supervisor_task = None
        await self._stop_child()

        # Cleanup state
        self._active_channels.clear()
        self._connect_events.clear()
        self._audio_handlers.clear()
        self._hangup_handlers.clear()
        self._process = None

        print("[IPC] Audio subprocess stopped")

    # ------------------------------------------------------------------
    # Per-channel registration
    # ------------------------------------------------------------------

    def register_channel(
        self,
        channel_id: str,
        on_audio: Callable[[str, bytes], Awaitable[None]] = None,
        on_hangup: Callable[[str], Awaitable[None]] = None,
    ):
        """Register per-channel audio/hangup handlers."""
        if on_audio:
            self._audio_handlers[channel_id] = on_audio
        if on_hangup:
            self._hangup_handlers[channel_id] = on_hangup

    def unregister_channel(self, channel_id: str):
        """Remove all handlers for a channel."""
        self._audio_handlers.pop(channel_id, None)
        self._hangup_handlers.pop(channel_id, None)
        self._active_channels.discard(channel_id)
        self._connect_events.pop(channel_id, None)

    def get_channel_rate(self, channel_id: str) -> int:
        """Get the sample rate for a channel. Always 8000 — subprocess handles conversion."""
        return 8000

    # ------------------------------------------------------------------
    # Connection waiting
    # ------------------------------------------------------------------

    def expect_channel(self, channel_id: str) -> asyncio.Event:
        """
        Register expectation for a channel connection.

        Returns an Event that fires when the subprocess reports CHANNEL_OPEN.
        Used by outbound call flow to wait for Asterisk to connect back.
        """
        event = asyncio.Event()
        self._connect_events[channel_id] = event
        return event

    async def wait_for_channel(self, channel_id: str, timeout: float = 15.0) -> bool:
        """Wait for a specific channel to open in the subprocess."""
        # The open may already have arrived: callers await an ARI request
        # (continue_in_dialplan) between expect_channel and this wait.
        if channel_id in self._active_channels:
            self._connect_events.pop(channel_id, None)
            return True
        event = self._connect_events.get(channel_id)
        if not event:
            event = self.expect_channel(channel_id)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"[IPC] Timeout waiting for channel: {channel_id}")
            self._connect_events.pop(channel_id, None)
            return False
        return channel_id in self._active_channels

    # ------------------------------------------------------------------
    # Sending to subprocess
    # ------------------------------------------------------------------

    async def send_ai_audio(self, channel_uuid: str, pcm16_24k: bytes):
        """Send raw 24kHz AI audio to subprocess for resampling + pacing."""
        if not self._running or not self._writer:
            return
        payload = channel_uuid.encode("ascii") + pcm16_24k
        header = struct.pack(IPC_HEADER_FMT, MSG_AI_AUDIO, len(payload))
        self._writer.write(header + payload)
        # Periodic drain to prevent OS buffer overflow without blocking every frame
        if not hasattr(self, '_ai_audio_frame_count'):
            self._ai_audio_frame_count = 0
        self._ai_audio_frame_count += 1
        if self._ai_audio_frame_count % 5 == 0:  # Drain every 5 frames (~100ms)
            await self._writer.drain()

    async def clear_buffer(self, channel_uuid: str):
        """Send clear-buffer command to subprocess (barge-in)."""
        if not self._running or not self._writer:
            return
        payload = channel_uuid.encode("ascii")
        header = struct.pack(IPC_HEADER_FMT, MSG_CLEAR_BUFFER, len(payload))
        self._writer.write(header + payload)
        await self._writer.drain()  # Barge-in is high priority — drain immediately

    # ------------------------------------------------------------------
    # IPC listener (receives messages from subprocess)
    # ------------------------------------------------------------------

    async def _ipc_listener(self):
        """Receive messages from subprocess."""
        reader, ready, died = self._reader, self._ready, self._died
        try:
            while True:
                # Read 5-byte header
                header = await reader.readexactly(IPC_HEADER_SIZE)
                msg_type = header[0]
                length = int.from_bytes(header[1:5], "little")
                payload = await reader.readexactly(length) if length > 0 else b""

                if msg_type == MSG_PHONE_AUDIO:
                    # Phone audio from subprocess: uuid(36) + 8kHz PCM16
                    uuid_str = payload[:36].decode("ascii")
                    audio = payload[36:]
                    handler = self._audio_handlers.get(uuid_str)
                    if handler:
                        self._spawn_handler(handler(uuid_str, audio))

                elif msg_type == MSG_CHANNEL_OPEN:
                    uuid_str = (
                        payload[:36].decode("ascii")
                        if len(payload) >= 36
                        else payload.decode("ascii")
                    )
                    print(f"[IPC] Channel opened: {uuid_str}")
                    self._active_channels.add(uuid_str)
                    evt = self._connect_events.pop(uuid_str, None)
                    if evt:
                        evt.set()

                elif msg_type == MSG_CHANNEL_CLOSE:
                    uuid_str = (
                        payload[:36].decode("ascii")
                        if len(payload) >= 36
                        else payload.decode("ascii")
                    )
                    print(f"[IPC] Channel closed: {uuid_str}")
                    handler = self._hangup_handlers.get(uuid_str)
                    if handler:
                        self._spawn_handler(handler(uuid_str))
                    self.unregister_channel(uuid_str)

                elif msg_type == MSG_READY:
                    if ready and not ready.done():
                        ready.set_result(payload.decode("ascii", errors="replace"))

        except asyncio.IncompleteReadError:
            print("[IPC] Subprocess disconnected")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[IPC] Listener error: {e}")
        finally:
            self._running = False
            died.set()  # wakes the supervisor
            if ready and not ready.done():
                ready.set_exception(ConnectionError("audio subprocess closed IPC"))

    # ------------------------------------------------------------------
    # Log forwarder (subprocess stdout → our log)
    # ------------------------------------------------------------------

    async def _log_forwarder(self):
        """Forward subprocess stdout to our log, to EOF: the lines a dying
        child prints last (a traceback) are the ones that say why."""
        process = self._process
        try:
            while process and process.stdout:
                line = await process.stdout.readline()
                if not line:
                    break
                text = line.decode(errors='replace').rstrip()
                self._output_tail.append(text)
                print(f"[AUDIO-SUB] {text}")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
