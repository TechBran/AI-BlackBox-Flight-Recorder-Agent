"""Tailscale wizard actuator — long-running operations the onboarding UI
   triggers (authenticate, cert, etc). All sudo wrapped via the NOPASSWD
   sudoers entry from install.sh Step 4e.

   Audit refs:
   - C3 (double-click race): per-operation asyncio.Lock
   - C4 (shell injection): subprocess argv lists only, hostname sanitized
   - M5 (sudoers idempotency): handled in install.sh, not here
   - M6 (auth timeout): 15s timeout waiting for Login URL line
   - E1 (T3 skipped): Tailscale pre-installed by install.sh; no install
     stream endpoint needed.
"""
import asyncio
import json
import re
import shutil
import socket
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# One lock per operation so install + auth can serialize independently.
_install_lock = asyncio.Lock()
_up_lock = asyncio.Lock()

# Background process handle for active `tailscale up` — needed to poll
# completion + clean up when status transitions to Running OR timeout.
_active_up_process: subprocess.Popen | None = None
_active_up_login_url: str | None = None

LOGIN_URL_PATTERN = re.compile(r"https://login\.tailscale\.com/a/[a-zA-Z0-9]+")


def _poll_response(state: str, *, detail=None, backend_state: str | None = None,
                   login_url: str | None = None) -> dict:
    """Normalized poll response shape — consumers can be branch-free.

    Always returns {state, detail, backend_state, login_url}; absent values
    are None. Reviewer N2.
    """
    return {
        "state": state,
        "detail": detail,
        "backend_state": backend_state,
        "login_url": login_url,
    }


@asynccontextmanager
async def operation_lock(lock: asyncio.Lock, name: str):
    """Acquire lock or raise 409-equivalent."""
    if lock.locked():
        raise RuntimeError(f"{name} already in progress")
    async with lock:
        yield


def _safe_hostname() -> str:
    """Return a sanitized hostname suitable for --hostname= flag.
    Audit C4: only [a-z0-9-] allowed."""
    raw = socket.gethostname().lower()
    safe = re.sub(r"[^a-z0-9-]", "-", raw).strip("-")
    return safe or "blackbox"


async def start_up() -> str:
    """Start `tailscale up`, capture login URL, leave process running.
    Returns the login URL. Caller must hold _up_lock until poll completes."""
    global _active_up_process, _active_up_login_url

    # Pre-flight: daemon alive? (audit M6)
    # Reviewer I1: subprocess.run wrapped in asyncio.to_thread so the
    # 3s timeout can't freeze the uvicorn event loop.
    try:
        check = await asyncio.to_thread(
            subprocess.run,
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=3,
        )
        # rc != 0 with "not running" is OK (NeedsLogin); rc != 0 with
        # other errors means daemon is dead.
        if check.returncode != 0 and "daemon" in check.stderr.lower():
            raise RuntimeError(
                "tailscaled daemon not running — try `sudo systemctl restart tailscaled`"
            )
    except subprocess.TimeoutExpired:
        raise RuntimeError("tailscaled status check timed out")

    hostname = _safe_hostname()
    cmd = ["sudo", "-n", "/usr/bin/tailscale", "up",
           "--accept-dns=true", f"--hostname={hostname}"]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    # Wait up to 15s for the Login URL line (audit M6)
    login_url = None
    try:
        async with asyncio.timeout(15):
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                m = LOGIN_URL_PATTERN.search(line.decode("utf-8", errors="replace"))
                if m:
                    login_url = m.group(0)
                    break
    except TimeoutError:
        proc.terminate()
        raise RuntimeError("Timeout waiting for Tailscale login URL")

    if not login_url:
        proc.terminate()
        raise RuntimeError("Tailscale did not emit a login URL")

    _active_up_process = proc
    _active_up_login_url = login_url

    # Best-effort: open in default browser (audit I1)
    # Reviewer I3: start_new_session=True so PID 1 (init) reaps the
    # xdg-open child — prevents zombie accumulation on long-running uvicorn.
    try:
        subprocess.Popen(["xdg-open", login_url],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:
        pass  # UI also renders clickable link

    return login_url


async def poll_up() -> dict:
    """Check whether the active `tailscale up` has authenticated.
    Returns normalized {state, detail, backend_state, login_url} shape."""
    global _active_up_process, _active_up_login_url

    try:
        # Reviewer I1: subprocess.run wrapped in asyncio.to_thread so the
        # 3s timeout can't freeze the uvicorn event loop (wizard polls
        # every 2s — sync call would freeze the entire Portal during auth).
        result = await asyncio.to_thread(
            subprocess.run,
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            backend = data.get("BackendState", "unknown")
            if backend == "Running":
                # Clean up the up-process.
                # Reviewer I2: _active_up_process is an asyncio.subprocess.Process
                # whose .wait() is a coroutine WITHOUT a timeout kwarg.
                # The previous proc.wait(timeout=2) raised TypeError which the
                # bare except hid — terminate() was ALWAYS being called.
                # Use asyncio.wait_for for the timeout instead.
                if _active_up_process is not None:
                    try:
                        await asyncio.wait_for(_active_up_process.wait(), timeout=2)
                    except (asyncio.TimeoutError, Exception):
                        _active_up_process.terminate()
                    _active_up_process = None
                    _active_up_login_url = None
                return _poll_response("running", detail=data.get("Self", {}))
            return _poll_response("pending",
                                  backend_state=backend,
                                  login_url=_active_up_login_url)
    except Exception as e:
        return _poll_response("failed", detail=str(e))

    return _poll_response("pending", login_url=_active_up_login_url)


async def cancel_up() -> None:
    global _active_up_process, _active_up_login_url
    if _active_up_process is not None:
        _active_up_process.terminate()
        try:
            _active_up_process.wait(timeout=2)
        except Exception:
            _active_up_process.kill()
        _active_up_process = None
    _active_up_login_url = None
