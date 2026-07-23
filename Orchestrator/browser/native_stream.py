"""NativeDesktopStream — stream the REAL logged-in desktop on demand.

Brandon (2026-07-23): "if we have no task available, we should go directly to
the main desktop." When a viewer opens /cu/view/main, this module attaches a
``-shared`` x11vnc to the REAL X session (detected display, e.g. :0, with the
session's detected XAUTHORITY) plus a loopback websockify bridge, refcounts
viewers via the /cu/view/main/ws proxy — spawn on FIRST connect, reap on LAST
disconnect + a short grace (BLACKBOX_MAIN_STREAM_GRACE_S, default 60s) — and
NEVER touches the per-session Xvfb pipeline in display.py (separate module,
separate loopback ports, no slot table involvement).

SECURITY NOTE: this streams the REAL desktop. As with every other surface, the
Tailscale (+LAN) perimeter IS the auth boundary by design — no app-layer auth
(tailscale_security_perimeter). The /cu/view/main/ws route logs EVERY
main-stream WS connect/disconnect so real-desktop viewing is auditable in
journalctl.
"""
import os
import socket
import subprocess
import threading
import time
from typing import Dict, List, Optional

from Orchestrator.browser.config import _detect_xauthority
from Orchestrator.browser.display import (
    _live_view_available, _pids_matching, _terminate_pid, _terminate_proc,
)

# ── Loopback ports (deliberately OUTSIDE the per-session slot ranges:
#    VNC 5901..590N, websockify 6101..610N — see display.py) ──
MAIN_VNC_PORT = 5999
MAIN_WS_PORT = 6099
MAIN_STREAM_GRACE_ENV = "BLACKBOX_MAIN_STREAM_GRACE_S"
_DEFAULT_GRACE_S = 60.0
_PORT_WAIT_S = 5.0

# The customer-facing remediation string — mirrors preflight.check_display's
# "Log into the desktop session" guidance (same failure, same fix).
_UNAVAILABLE_REASON = "log into the desktop session"


# ── Availability probe ────────────────────────────────────────────────────


def _parse_display(value: str) -> Optional[int]:
    if value.startswith(":"):
        try:
            return int(value.split(":")[1].split(".")[0])
        except ValueError:
            pass
    return None


def _detect_session() -> Optional[tuple]:
    """Detect the LOGGED-IN X session as ``(display_num, xauthority)`` —
    xauthority may be "" when the source didn't reveal one — or None when no
    session exists. Mirrors config._detect_native_display's detection order but
    returns None instead of a fallback ``0`` (the probe must distinguish
    "desktop is up" from "nobody logged in" — the service starts at boot), and
    additionally harvests XAUTHORITY from the SESSION process itself: on a
    GDM/Xorg box the real file is /run/user/<uid>/gdm/Xauthority, which
    config._detect_xauthority's Mutter/classic patterns never find."""
    # Method 1: gnome-shell's environ from /proc (survives systemd's bare env)
    try:
        result = subprocess.run(["pgrep", "-x", "gnome-shell"],
                                capture_output=True, text=True, timeout=5)
        for pid in result.stdout.strip().split():
            try:
                with open(f"/proc/{pid}/environ", "rb") as f:
                    env_data = f.read().decode("utf-8", errors="replace")
            except OSError:
                continue
            sess_env = dict(entry.split("=", 1) for entry in env_data.split("\0")
                            if "=" in entry)
            num = _parse_display(sess_env.get("DISPLAY", ""))
            if num is not None:
                return num, sess_env.get("XAUTHORITY", "")
    except Exception:
        pass
    # Method 2: Xorg argv (":0" display arg + its -auth path)
    try:
        result = subprocess.run(["pgrep", "-a", "Xorg"],
                                capture_output=True, text=True, timeout=5)
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            num = next((_parse_display(p) for p in parts
                        if _parse_display(p) is not None), None)
            if num is not None:
                xauth = ""
                if "-auth" in parts:
                    i = parts.index("-auth")
                    if i + 1 < len(parts):
                        xauth = parts[i + 1]
                return num, xauth
    except Exception:
        pass
    # Method 3: our own environment (dev shells run inside the session)
    num = _parse_display(os.environ.get("DISPLAY", ""))
    if num is not None:
        return num, os.environ.get("XAUTHORITY", "")
    return None


def _detect_resolution(display: str, xauth: str) -> Optional[str]:
    """"WxH" of the real desktop via xrandr against the detected session, or
    None (resolution is informational — its absence never gates availability)."""
    env = {"DISPLAY": display, "PATH": "/usr/bin:/usr/local/bin:/bin"}
    if xauth:
        env["XAUTHORITY"] = xauth
    try:
        result = subprocess.run(["xrandr", "--current"], capture_output=True,
                                text=True, timeout=5, env=env)
        for line in result.stdout.splitlines():
            if "current" in line.lower():
                parts = line.split("current")[1].split(",")[0].strip()
                w, h = parts.split(" x ")
                return f"{int(w.strip())}x{int(h.strip())}"
    except Exception:
        pass
    return None


# Probe cache: /cu/sessions is polled by the viewer on boot/reconnect and the
# probe shells out (pgrep/xrandr) — keep it cheap under polling.
_PROBE_TTL_S = 5.0
_probe_cache: Dict[str, object] = {"ts": 0.0, "payload": None}


def probe_main_desktop(force: bool = False) -> dict:
    """Is the real desktop streamable? Logged-in X session + resolvable xauth
    -> {available: True, display, resolution, xauthority}; otherwise
    {available: False, reason: "log into the desktop session"} — mirroring the
    preflight display check's remediation. ``xauthority`` is internal plumbing
    for the spawner; public payloads go through public_main_status()."""
    now = time.time()
    if (not force and _probe_cache["payload"] is not None
            and (now - float(_probe_cache["ts"])) < _PROBE_TTL_S):
        return dict(_probe_cache["payload"])  # type: ignore[arg-type]
    sess = _detect_session()
    if sess is None:
        display_num, xauth = None, ""
    else:
        display_num, xauth = sess
        if not (xauth and os.path.isfile(xauth)):
            xauth = _detect_xauthority()  # fallback: Mutter/classic patterns
    if display_num is None or not xauth or not os.path.isfile(xauth):
        payload: dict = {"available": False, "reason": _UNAVAILABLE_REASON}
    else:
        display = f":{display_num}"
        payload = {"available": True, "display": display,
                   "resolution": _detect_resolution(display, xauth),
                   "xauthority": xauth}
    _probe_cache["ts"], _probe_cache["payload"] = now, dict(payload)
    return payload


def public_main_status(status: Optional[dict] = None) -> dict:
    """The API-facing availability payload (xauthority stripped)."""
    status = status if status is not None else probe_main_desktop()
    return {k: status[k] for k in ("available", "display", "resolution", "reason")
            if k in status}


# ── Spawn / teardown of the x11vnc+websockify pair ────────────────────────


def _wait_port(port: int, timeout: float = _PORT_WAIT_S) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def reap_orphan_stream_procs() -> None:
    """Sweep restart-survivor x11vnc/websockify children on OUR two ports (a
    service restart reparents them to init — same story as
    display.reap_orphans, but for the main-stream pair; specific port
    identifiers, never a name kill). Called at boot by the startup reaper and
    defensively before every spawn."""
    for pattern in (f"rfbport {MAIN_VNC_PORT}", f"websockify 127.0.0.1:{MAIN_WS_PORT}"):
        for pid in _pids_matching(pattern):
            print(f"[CU-MAIN] reaping orphan pid {pid} ({pattern})")
            _terminate_pid(pid)


def _spawn_stream_procs(display: str, xauth: str) -> Dict[str, subprocess.Popen]:
    """Attach x11vnc (-shared, session -auth) + a loopback websockify to the
    REAL desktop. Raises RuntimeError when x11vnc never starts listening."""
    reap_orphan_stream_procs()
    procs: Dict[str, subprocess.Popen] = {}
    procs["x11vnc"] = subprocess.Popen(
        ["x11vnc", "-display", display, "-auth", xauth, "-forever", "-shared",
         "-nopw", "-listen", "127.0.0.1", "-rfbport", str(MAIN_VNC_PORT),
         "-noxdamage", "-quiet"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not _wait_port(MAIN_VNC_PORT):
        for p in procs.values():
            _terminate_proc(p)
        raise RuntimeError(
            f"x11vnc did not come up on 127.0.0.1:{MAIN_VNC_PORT} for {display}")
    procs["websockify"] = subprocess.Popen(
        ["websockify", f"127.0.0.1:{MAIN_WS_PORT}", f"127.0.0.1:{MAIN_VNC_PORT}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Wait for the BRIDGE port too (first-connect race, field-found 2026-07-23:
    # the WS proxy dialed :6099 before websockify bound it -> 1011 'Upstream
    # unavailable' on the very first main-desktop viewer; the second connect
    # worked because the pair was already up).
    if not _wait_port(MAIN_WS_PORT):
        for p in procs.values():
            _terminate_proc(p)
        raise RuntimeError(
            f"websockify did not come up on 127.0.0.1:{MAIN_WS_PORT}")
    return procs


def _wake_display() -> None:
    """Best-effort un-blank of the real display when a viewer attaches: DPMS
    force-on + screensaver reset via xset, using the SAME display/xauthority the
    availability probe found. Never raises — a locked/blanked screen streaming
    black is a UX bug, not a functional one, and waking must not break attach.
    (The GNOME LOCK screen still requires the user's password — this only wakes
    the panel so the lock UI is visible instead of a black rectangle.)"""
    try:
        status = probe_main_desktop()
        if not status.get("available"):
            return
        env = dict(os.environ)
        env["DISPLAY"] = str(status.get("display", ":0"))
        xauth = str(status.get("xauthority", "") or "")
        if xauth:
            env["XAUTHORITY"] = xauth
        for args in (["xset", "dpms", "force", "on"], ["xset", "s", "reset"]):
            subprocess.run(args, env=env, timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001 — never let waking break attach
        print(f"[CU-MAIN] display wake skipped: {e}")


# ── Refcounted manager ────────────────────────────────────────────────────


class NativeDesktopStream:
    """Refcounted lifecycle for the ONE main-desktop stream pair. Contenders
    are WS proxy handlers on the event loop plus the grace-reap timer thread,
    so a process-wide RLock guards state. acquire() spawns on the first viewer
    (cancelling any pending grace reap); release() schedules the reap when the
    last viewer leaves. The per-session Xvfb allocator is never involved."""

    def __init__(self, grace_s: Optional[float] = None):
        self._lock = threading.RLock()
        self._procs: Dict[str, subprocess.Popen] = {}
        self._viewers = 0
        self._reap_timer: Optional[threading.Timer] = None
        if grace_s is None:
            try:
                grace_s = float(os.environ.get(MAIN_STREAM_GRACE_ENV,
                                               _DEFAULT_GRACE_S))
            except ValueError:
                grace_s = _DEFAULT_GRACE_S
        self.grace_s = grace_s

    @property
    def viewers(self) -> int:
        with self._lock:
            return self._viewers

    def _alive_locked(self) -> bool:
        return bool(self._procs) and all(
            p.poll() is None for p in self._procs.values())

    def acquire(self) -> int:
        """Register a viewer; spawn the stream pair if it isn't running.
        Returns the loopback websockify port to proxy to. Raises RuntimeError
        when the desktop is unavailable or the pair can't start. Every viewer
        attach also pokes the display awake (field finding 2026-07-23: Brandon's
        first main-desktop view was a faithfully-streamed BLACK screen — the
        box's GNOME session sat blanked at the screensaver)."""
        with self._lock:
            if self._reap_timer is not None:
                self._reap_timer.cancel()
                self._reap_timer = None
            if not self._alive_locked():
                self._stop_locked()  # clear any half-dead pair before respawn
                status = probe_main_desktop(force=True)
                if not status.get("available"):
                    raise RuntimeError(str(status.get("reason", _UNAVAILABLE_REASON)))
                if not _live_view_available():
                    raise RuntimeError(
                        "live view unavailable — install websockify + novnc "
                        "(SHOULD_HAVE in system-packages.txt)")
                self._procs = _spawn_stream_procs(
                    str(status["display"]), str(status.get("xauthority", "")))
                print(f"[CU-MAIN] attached to real desktop {status['display']} "
                      f"(rfb {MAIN_VNC_PORT}, ws {MAIN_WS_PORT})")
            _wake_display()   # un-blank DPMS/screensaver so the first frame isn't black
            self._viewers += 1
            return MAIN_WS_PORT

    def release(self) -> None:
        """Deregister a viewer. When the LAST one leaves, schedule teardown
        after the grace window (a quick tab reload should not restart x11vnc)."""
        with self._lock:
            self._viewers = max(0, self._viewers - 1)
            if self._viewers == 0 and self._procs and self._reap_timer is None:
                t = threading.Timer(self.grace_s, self._reap_if_idle)
                t.daemon = True
                self._reap_timer = t
                t.start()

    def _reap_if_idle(self) -> None:
        with self._lock:
            self._reap_timer = None
            if self._viewers == 0:
                self._stop_locked()

    def _stop_locked(self) -> None:
        procs, self._procs = self._procs, {}
        for role in ("websockify", "x11vnc"):  # dependents before the source
            p = procs.get(role)
            if p is not None:
                _terminate_proc(p)
        if procs:
            print("[CU-MAIN] main-desktop stream reaped (no viewers)")

    def shutdown(self) -> None:
        with self._lock:
            if self._reap_timer is not None:
                self._reap_timer.cancel()
                self._reap_timer = None
            self._stop_locked()


# ── Auto-view resolver (pure — the /cu/view/auto policy in one place) ─────


def resolve_auto_view(sessions: List[dict], main_status: dict) -> dict:
    """Where should /cu/view/auto land? A live agent/manual session first
    (oldest — allocator insertion order), else the main desktop when available,
    else nothing-to-show with the probe's reason. Pure function of its inputs."""
    if sessions:
        sid = str(sessions[0].get("session_id", ""))
        return {"kind": "session", "session_id": sid,
                "url": sessions[0].get("view_url") or f"/cu/view/{sid}"}
    if main_status.get("available"):
        return {"kind": "main", "url": "/cu/view/main"}
    return {"kind": "none",
            "reason": str(main_status.get("reason", _UNAVAILABLE_REASON))}


# ── Module singleton ──────────────────────────────────────────────────────

_stream: Optional[NativeDesktopStream] = None
_stream_lock = threading.Lock()


def get_native_stream() -> NativeDesktopStream:
    global _stream
    with _stream_lock:
        if _stream is None:
            _stream = NativeDesktopStream()
        return _stream
