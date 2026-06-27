"""MCP onboarding actuator -- privileged box-setup operations the onboarding UI
triggers: bring up the public Funnel, and start/restart the MCP service.

All sudo is wrapped via the NOPASSWD grants in the MCP section of
installer/templates/sudoers-blackbox-system. The unit INSTALL + enable is
installer-time (it writes /etc, which ProtectSystem=strict makes read-only in the
service namespace) -- this module only does the /etc-free RUNTIME operations
(tailscaled-socket funnel ops, PID-1 service transitions).

Security (mirrors tailscale_actuator):
- subprocess argv LISTS only (no shell); every token literal and matching the
  exact sudoers grant token-for-token (no wildcards).
- Funnel-up is PUBLIC-INTERNET exposure -> the calling route MUST gate it behind
  an explicit user confirmation.
- per-operation asyncio.Lock to serialize double-clicks.
"""
import asyncio
import subprocess

FUNNEL_PORT = "8443"
LOCAL_PORT = "9093"
MCP_SERVICE = "blackbox-mcp.service"

_funnel_lock = asyncio.Lock()
_service_lock = asyncio.Lock()


async def _run(*argv, timeout: int = 15):
    """Run an argv list; return (rc, stdout, stderr). Never shell."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", "timeout"
    return (proc.returncode,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"))


async def funnel_up() -> dict:
    """`sudo -n tailscale funnel --bg --https=8443 9093` -- exposes the MCP server
    on the PUBLIC internet. The route MUST have confirmed with the user first."""
    if _funnel_lock.locked():
        return {"ok": False, "error": "a funnel operation is already in progress"}
    async with _funnel_lock:
        rc, out, err = await _run("sudo", "-n", "/usr/bin/tailscale", "funnel",
                                  "--bg", f"--https={FUNNEL_PORT}", LOCAL_PORT)
    if rc == 0:
        return {"ok": True}
    return {"ok": False, "error": (out + err).strip()[:500] or f"rc={rc}"}


async def funnel_reset() -> dict:
    """`sudo -n tailscale funnel reset` -- tears down the public exposure."""
    if _funnel_lock.locked():
        return {"ok": False, "error": "a funnel operation is already in progress"}
    async with _funnel_lock:
        rc, out, err = await _run("sudo", "-n", "/usr/bin/tailscale", "funnel", "reset")
    return {"ok": rc == 0, "error": None if rc == 0 else (out + err).strip()[:500]}


async def service_action(action: str) -> dict:
    """start|restart the MCP service via the narrow sudoers grant (no /etc write)."""
    if action not in ("start", "restart", "stop"):
        return {"ok": False, "error": f"unsupported action {action!r}"}
    async with _service_lock:
        rc, out, err = await _run("sudo", "-n", "/usr/bin/systemctl", action, MCP_SERVICE)
    return {"ok": rc == 0, "error": None if rc == 0 else (out + err).strip()[:500]}


def service_state() -> dict:
    """Read-only unit state (no sudo). installed=True means the installer
    bootstrapped the unit (loaded+enabled). not-found means it must be installed by
    re-running the installer -- a guided step (an /etc write can't be done at
    runtime from the service namespace)."""
    try:
        out = subprocess.run(
            ["systemctl", "show", "-p", "LoadState,UnitFileState,ActiveState", MCP_SERVICE],
            capture_output=True, text=True, timeout=6)
        fields = dict(line.split("=", 1)
                      for line in out.stdout.strip().splitlines() if "=" in line)
        return {
            "installed": (fields.get("LoadState") == "loaded"
                          and fields.get("UnitFileState") in ("enabled", "static", "linked")),
            "load_state": fields.get("LoadState", ""),
            "unit_file_state": fields.get("UnitFileState", ""),
            "active": fields.get("ActiveState") == "active",
        }
    except Exception as e:
        return {"installed": False, "load_state": "error",
                "unit_file_state": "", "active": False, "error": str(e)[:200]}
