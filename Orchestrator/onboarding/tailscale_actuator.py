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
        try:
            proc.terminate()
        except ProcessLookupError:
            pass  # E3: subprocess may have died (daemon-dead path)
        raise RuntimeError("Timeout waiting for Tailscale login URL")

    if not login_url:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass  # E3: subprocess may have died (daemon-dead path)
        # E3: if subprocess died with rc!=0 and tailscaled is gone,
        # surface a more useful error so the bare "did not emit" doesn't
        # mask the actual problem.
        rc = proc.returncode
        if rc is not None and rc != 0:
            raise RuntimeError(
                "tailscaled daemon not running or unreachable — "
                "try: sudo systemctl restart tailscaled"
            )
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
                        try:
                            _active_up_process.terminate()
                        except ProcessLookupError:
                            pass  # E3: subprocess may have died between wait() and terminate()
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
        try:
            _active_up_process.terminate()
        except ProcessLookupError:
            pass  # E3: subprocess may have already died
        try:
            _active_up_process.wait(timeout=2)
        except Exception:
            try:
                _active_up_process.kill()
            except ProcessLookupError:
                pass  # E3: subprocess may have already died
        _active_up_process = None
    _active_up_login_url = None


# ── T5: cert + accept-dns ──

HTTPS_DISABLED_PATTERN = re.compile(r"HTTPS is disabled|HTTPS.*not enabled", re.IGNORECASE)


async def request_cert() -> dict:
    """Run `tailscale cert <hostname>`. Returns:
       - {ok: true, cert_path, key_path, hostname} on success
       - {ok: false, https_disabled: true, admin_url} if tailnet HTTPS toggle off
       - {ok: false, error: <msg>} otherwise"""
    # Get current hostname from status (must match what Tailscale assigned).
    # subprocess.run wrapped in to_thread per T4's I1 fix (event-loop non-blocking).
    try:
        status = await asyncio.to_thread(
            subprocess.run,
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=3,
        )
        data = json.loads(status.stdout)
        hostname = (data.get("Self") or {}).get("DNSName", "").rstrip(".")
        if not hostname:
            return {"ok": False, "error": "no hostname assigned by Tailscale"}
    except Exception as e:
        return {"ok": False, "error": f"status probe failed: {e}"}

    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", "/usr/bin/tailscale", "cert", hostname,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    out = (stdout + stderr).decode("utf-8", errors="replace")

    if proc.returncode == 0:
        return {
            "ok": True,
            "cert_path": f"/var/lib/tailscale/certs/{hostname}.crt",
            "key_path": f"/var/lib/tailscale/certs/{hostname}.key",
            "hostname": hostname,
        }
    if HTTPS_DISABLED_PATTERN.search(out):
        return {"ok": False, "https_disabled": True,
                "admin_url": "https://login.tailscale.com/admin/dns"}
    return {"ok": False, "error": out.strip()[:500]}


async def set_accept_dns() -> dict:
    """Run `tailscale set --accept-dns=true`. Idempotent."""
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", "/usr/bin/tailscale", "set", "--accept-dns=true",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode == 0:
        return {"ok": True}
    return {"ok": False, "error": stderr.decode("utf-8", errors="replace").strip()}


async def setup_serve() -> dict:
    """Run `tailscale serve --bg --https=443 9091` to set up a persistent
    HTTPS reverse proxy. Tailscale handles cert + renewal + TLS termination
    — Portal stays HTTP-only on 9091 internally; Tailscale fronts it with
    HTTPS on 443. After success, https://<hostname>/ reaches the Portal.
    Android app pairing requires this (cleartext blocked since Android API 28).

    Port-only `9091` form (not `http://127.0.0.1:9091`) is used because:
    (1) tailscale 1.98 CLI doesn't accept the `/ <url>` form the audit doc
        assumed (invalid argument format),
    (2) port-only avoids unescaped `:` characters in sudoers grammar,
    (3) tailscale serve interprets bare port as http://127.0.0.1:<port>
        automatically — same end result.

    Returns:
       - {ok: true} on success — Portal now HTTPS-accessible
       - {ok: false, https_disabled: true, admin_url} if tailnet HTTPS toggle off
       - {ok: false, error: <msg>} otherwise"""
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", "/usr/bin/tailscale", "serve",
        "--bg", "--https=443", "9091",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    out = (stdout + stderr).decode("utf-8", errors="replace")

    if proc.returncode == 0:
        return {"ok": True}
    if HTTPS_DISABLED_PATTERN.search(out):
        return {"ok": False, "https_disabled": True,
                "admin_url": "https://login.tailscale.com/admin/dns"}
    return {"ok": False, "error": out.strip()[:500]}


# ── E1-reversal: install button resurrected with apt-get (apt repo
#   was configured by install.sh Step 1b's root-context curl|sh) ──

async def stream_install():
    """Yield SSE-formatted progress events while apt-get installs tailscale.
    Caller must hold _install_lock per audit C3."""
    cmd = ["sudo", "-n", "/usr/bin/apt-get", "install", "-y", "tailscale"]
    yield b"event: start\ndata: Installing Tailscale...\n\n"
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            yield f"data: {text}\n\n".encode("utf-8")
        rc = await proc.wait()
        if rc == 0:
            yield b"event: done\ndata: installed\n\n"
        else:
            yield f"event: error\ndata: install exited rc={rc}\n\n".encode("utf-8")
    except asyncio.CancelledError:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass  # E3: subprocess may have died
        raise
