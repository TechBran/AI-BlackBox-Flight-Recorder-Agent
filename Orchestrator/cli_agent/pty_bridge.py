import asyncio
import os
import select
from typing import Optional, Sequence

import ptyprocess

from .env_scrub import scrubbed_child_env


def _read_with_select(fd: int, timeout: float, chunk_size: int) -> bytes:
    """Block in select() up to `timeout` seconds, then read available data.

    This is the critical correctness fix: the previous implementation called
    a blocking `_proc.read()` inside `run_in_executor` and used
    `asyncio.wait_for` to time it out. That doesn't actually stop the thread
    — Python's ThreadPoolExecutor cannot cancel a thread blocked in
    `os.read()`. So every timed-out read leaked one thread (held until the
    PTY produced a byte, which the orphan thread then consumed — silently
    dropping the data on the floor while the caller had moved on).

    Using `select` inside the executor lets the kernel itself enforce the
    timeout. The thread always returns within `timeout` seconds, freeing
    its pool slot. No leaks, no dropped bytes, no starvation.
    """
    try:
        rlist, _, _ = select.select([fd], [], [], timeout)
    except (OSError, ValueError):
        # fd may have been closed underneath us during shutdown.
        return b""
    if not rlist:
        return b""
    try:
        return os.read(fd, chunk_size)
    except (OSError, BlockingIOError):
        return b""


class PtyBridge:
    def __init__(self, proc: ptyprocess.PtyProcess):
        self._proc = proc

    @classmethod
    def spawn(cls, command: Sequence[str], *,
              env: Optional[dict] = None,
              cwd: Optional[str] = None,
              cols: int = 80, rows: int = 24) -> "PtyBridge":
        proc = ptyprocess.PtyProcess.spawn(
            list(command),
            # Default to the scrubbed env (shared env_scrub table), never the
            # raw os.environ: whatever this PTY runs must not inherit the
            # server's credentials.
            env=env or scrubbed_child_env(),
            cwd=cwd,
            dimensions=(rows, cols),
        )
        return cls(proc)

    async def read(self, *, timeout: float = 0.1,
                    chunk_size: int = 4096) -> bytes:
        # Hand the timeout to the executor function via select(); no need
        # for asyncio.wait_for. The thread always returns promptly.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _read_with_select, self._proc.fd, timeout, chunk_size,
        )

    async def write(self, data: bytes) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._proc.write, data)

    def resize(self, *, cols: int, rows: int) -> None:
        try:
            self._proc.setwinsize(rows, cols)
        except Exception:
            pass

    def isalive(self) -> bool:
        return self._proc.isalive()

    def close(self) -> None:
        """Close the PTY without SIGKILL.

        Closing the PTY fd alone causes the child (typically `tmux attach`)
        to receive SIGHUP and detach gracefully. Sending SIGKILL via
        force=True propagates oddly through tmux and can take down the
        server itself, defeating session persistence — the whole point
        of using tmux. force=False keeps the inner session alive across
        WebSocket disconnects.
        """
        try:
            self._proc.close(force=False)
        except Exception:
            pass
