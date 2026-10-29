"""The AudioSocket child exits, and frees its port, when its parent dies
without sending SHUTDOWN.

blackbox.service runs with KillMode=process (CLI-agent sessions outlive a
restart), so when uvicorn is SIGKILLed (the OOM killer at MemoryMax=, a crash)
systemd leaves the audio child running. It has to notice on its own: an
orphan kept squatting the AudioSocket port and the next start failed "address
already in use". The unit's `ExecStartPre=-/usr/bin/pkill -9 -f
audio_subprocess.py` was the backstop for that; these tests are what let it go.

The parent here is a real process running the real AudioIPCClient (socketpair
IPC, the child's stdout piped back for logging, exactly as in the service),
and the child is the real audio_subprocess.py. The test SIGKILLs the parent,
idle and with a call up (the test holds Asterisk's side of that call, which
outlives the parent as Asterisk does).
"""
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from Orchestrator.asterisk import audio_ipc

REPO_ROOT = Path(audio_ipc.__file__).resolve().parents[2]

# The service's side, in its own process: start the supervised child, report
# its pid and port, then wait to be killed. "holder" first forks a process that
# keeps every fd of the parent, its end of the IPC socket included, so the
# child sees no EOF when the parent dies (a forked worker would do the same).
_PARENT = r"""
import asyncio, os, sys, time
sys.path.insert(0, sys.argv[1])
from Orchestrator.asterisk import audio_ipc

async def main():
    client = audio_ipc.AudioIPCClient(host="127.0.0.1", port=0)
    if not await client.start():
        print("START-FAILED", client.last_error, flush=True)
        return
    if sys.argv[2] == "holder":
        pid = os.fork()
        if pid == 0:
            time.sleep(3600)
            os._exit(0)
        print("HOLDER", pid, flush=True)
    print("CHILD", client.pid, client.address[1], flush=True)
    await asyncio.sleep(3600)

asyncio.run(main())
"""


def _alive(pid: int) -> bool:
    """Running, as opposed to gone or a zombie waiting for its new parent."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state not in ("Z", "X")


def _port_free(port: int) -> bool:
    """Whether a new child could listen there: bind + listen with
    SO_REUSEADDR, as asyncio.start_server does (a closed call's TIME_WAIT
    does not block that; a live listener does)."""
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(1)
        return True
    except OSError:
        return False
    finally:
        s.close()


def _start_parent(mode: str):
    """(parent, child pid, child port, holder pid or None)."""
    parent = subprocess.Popen(
        [sys.executable, "-I", "-c", _PARENT, str(REPO_ROOT), mode],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    lines: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=lambda: [lines.put(ln) for ln in parent.stdout], daemon=True).start()
    seen = []
    holder = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=1.0)
        except queue.Empty:
            if parent.poll() is not None:
                break
            continue
        seen.append(line.rstrip())
        if line.startswith("HOLDER "):
            holder = int(line.split()[1])
        if line.startswith("CHILD "):
            _, pid, port = line.split()
            return parent, int(pid), int(port), holder
        if line.startswith("START-FAILED"):
            break
    parent.kill()
    pytest.fail("the parent never reported its audio child:\n" + "\n".join(seen[-20:]))


def _open_call(port: int) -> socket.socket:
    """Asterisk's side of a call: a connection that outlives the parent."""
    conn = socket.create_connection(("127.0.0.1", port), timeout=5)
    conn.sendall(bytes([0x01]) + (16).to_bytes(2, "big") + uuid.uuid4().bytes)
    conn.sendall(bytes([0x10]) + (320).to_bytes(2, "big") + b"\x00" * 320)
    return conn


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
@pytest.mark.parametrize("mode", ["idle", "mid-call", "holder"])
def test_the_child_exits_and_frees_its_port_when_the_parent_is_killed(mode):
    """idle / mid-call: the IPC socket's EOF. holder: no EOF comes (another
    process keeps the parent's end), so the child's reparent check does it."""
    parent, child_pid, port, holder = _start_parent(mode)
    call = None
    try:
        assert _alive(child_pid)
        assert not _port_free(port), "the child listens on its port"
        if mode == "mid-call":
            call = _open_call(port)
            time.sleep(0.3)                   # the child forwards CHANNEL_OPEN
        os.kill(parent.pid, signal.SIGKILL)   # no SHUTDOWN, no SIGTERM: an OOM kill
        parent.wait(timeout=10)
        deadline = time.monotonic() + 10
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child_pid), "the orphaned audio child is still running"
        deadline = time.monotonic() + 5
        while not _port_free(port) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _port_free(port), f"port {port} is still held after the child exited"
    finally:
        if call is not None:
            call.close()
        if parent.poll() is None:
            parent.kill()
        for pid in (child_pid, holder):
            if pid and _alive(pid):
                os.kill(pid, signal.SIGKILL)
