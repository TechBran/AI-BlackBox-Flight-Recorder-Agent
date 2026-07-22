"""Per-session virtual-display allocation for computer use (M9).

Each CU session gets a private Xvfb screen (at the model's native resolution),
an openbox WM, an x11vnc server bound to loopback, and — when live-view assets
are present — a websockify WS bridge. Everything is tracked BY PID; teardown and
liveness are per-pid. There is NO global process-name kill (the singleton display
this replaces matched processes by name, which broke multi-session
correctness). display_arbiter.py still owns native-mode mutual exclusion; this
module owns virtual-session lifecycle only.
"""
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from Orchestrator.browser.config import (
    DISPLAY_DEPTH, ACTIVE_DISPLAY, CU_DISPLAY_WIDTH, CU_DISPLAY_HEIGHT,
)

# ── Slot ranges (loopback-only) ──
DISPLAY_BASE = 100            # Xvfb :100, :101, :102
VNC_BASE_PORT = 5901         # x11vnc RFB per session
WEBSOCKIFY_BASE_PORT = 6101  # websockify WS bridge per session
MAX_VIRTUAL_SESSIONS = 3     # concurrency cap (§9)
VIRTUAL_DISPLAY_TTL = 1800.0 # idle seconds before the TTL reaper (reap_idle) tears a session down
_STARTUP_WAIT = 1.0          # seconds to let Xvfb come up before openbox/x11vnc
_WM_STARTUP_WAIT = 0.3       # seconds to let openbox settle before x11vnc attaches
_NOVNC_DIR = "/usr/share/novnc"


def resolution_for_backend(backend: str) -> tuple:
    """Native CU resolution per backend (§9 / D6). ONE source per backend."""
    b = (backend or "anthropic").lower()
    if b in ("google", "gemini"):
        from Orchestrator.gemini_cu.config import GEMINI_CU_WIDTH, GEMINI_CU_HEIGHT
        return GEMINI_CU_WIDTH, GEMINI_CU_HEIGHT       # 1440x900
    from Orchestrator.browser.config import CU_DISPLAY_WIDTH, CU_DISPLAY_HEIGHT
    return CU_DISPLAY_WIDTH, CU_DISPLAY_HEIGHT           # 1280x720 (anthropic + openai)


def _live_view_available() -> bool:
    """websockify binary + noVNC assets both present -> live view can run."""
    import shutil
    return bool(shutil.which("websockify")) and os.path.isdir(_NOVNC_DIR)


def _xvfb_ready(display_num: int) -> bool:
    """One-shot readiness probe: xdpyinfo / scrot against :N succeeds."""
    env = {"DISPLAY": f":{display_num}", "PATH": "/usr/bin:/usr/local/bin:/bin"}
    try:
        r = subprocess.run(["scrot", "--overwrite", f"/tmp/xvfb_ready_{display_num}.png"],
                           env=env, capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _terminate_proc(p) -> None:
    """Tear down a TRACKED child we own (a Popen): terminate -> wait -> kill.
    Reaps the zombie via wait(). Used by release() (we hold the Popen)."""
    try:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=2)
    except Exception:
        pass


def _pids_matching(pattern: str) -> List[int]:
    """Find pids whose /proc/<pid>/cmdline contains ``pattern`` by scanning /proc
    directly — NOT via a process-name matcher (which would name-match across every
    session and false-positive). The caller passes a SPECIFIC slot identifier
    (e.g. 'Xvfb :102', 'rfbport 5903', 'websockify 127.0.0.1:6103') so each hit is
    scoped to one slot. Vanished/permission-denied pids are silently skipped."""
    hits: List[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return hits
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmdline = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue  # process vanished or not ours to read
        if pattern in cmdline:
            hits.append(int(entry))
    return hits


def _terminate_pid(pid: int) -> None:
    """Kill a SPECIFIC orphan pid (SIGTERM, brief grace, SIGKILL). Used only by the
    boot reaper for restart-survivors we no longer hold a Popen for — a service
    restart reparents them to init (KillMode=process), so we cannot wait() a
    non-child and must signal by pid. Swallows ESRCH (already gone)."""
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return  # already gone / not permitted
    for _ in range(30):  # ~1.5s grace before the hard kill
        try:
            os.kill(pid, 0)
        except OSError:
            return  # exited on SIGTERM
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


@dataclass
class DisplayHandle:
    session_id: str
    slot: int
    backend: str
    operator: str
    width: int
    height: int
    display_num: int
    vnc_port: int
    ws_port: int
    live_view: bool = False
    pids: Dict[str, int] = field(default_factory=dict)  # role -> pid (introspection)
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)

    @property
    def display(self) -> str:
        return f":{self.display_num}"

    def get_env(self) -> dict:
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        return env

    def touch(self) -> None:
        self.last_activity = time.time()

    def to_public(self) -> dict:
        return {
            "session_id": self.session_id,
            "operator": self.operator,
            "backend": self.backend,
            "width": self.width,
            "height": self.height,
            "display": self.display,
            "live_view": self.live_view,
            "view_url": f"/cu/view/{self.session_id}",
            "started_at": self.created_at,
        }


class DisplayAllocator:
    """Thread-safe per-session virtual-display lifecycle. Contenders are OS
    threads (tasks.py ThreadPoolExecutor), so a process-wide RLock guards the
    slot table."""

    def __init__(self):
        self._lock = threading.RLock()
        self._sessions: Dict[str, DisplayHandle] = {}       # session_id -> handle
        self._slots: Dict[int, str] = {}                    # slot -> session_id
        self._procs: Dict[str, Dict[str, subprocess.Popen]] = {}  # session_id -> role -> Popen

    def _free_slot(self) -> int:
        for slot in range(MAX_VIRTUAL_SESSIONS):
            if slot not in self._slots:
                return slot
        raise RuntimeError(
            f"CU virtual-display cap reached ({MAX_VIRTUAL_SESSIONS} concurrent sessions)")

    def allocate(self, session_id: str, backend: str = "anthropic",
                 operator: str = "system") -> DisplayHandle:
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                existing.touch()
                return existing
            slot = self._free_slot()  # raises when capped
            width, height = resolution_for_backend(backend)
            h = DisplayHandle(
                session_id=session_id, slot=slot, backend=backend, operator=operator,
                width=width, height=height, display_num=DISPLAY_BASE + slot,
                vnc_port=VNC_BASE_PORT + slot, ws_port=WEBSOCKIFY_BASE_PORT + slot,
            )
            self._start_quartet(h)
            self._sessions[session_id] = h
            self._slots[slot] = session_id
            return h

    def _start_quartet(self, h: DisplayHandle) -> None:
        procs: Dict[str, subprocess.Popen] = {}
        # 1. Xvfb at the backend's native resolution — scale 1.0, no LANCZOS.
        procs["xvfb"] = subprocess.Popen(
            ["Xvfb", h.display, "-screen", "0", f"{h.width}x{h.height}x{DISPLAY_DEPTH}",
             "-nolisten", "tcp", "-ac"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(_STARTUP_WAIT)
        if not _xvfb_ready(h.display_num):
            for p in procs.values():
                _terminate_proc(p)
            raise RuntimeError(f"Xvfb {h.display} failed to become ready")
        env = h.get_env()
        # 2. openbox WM (DISPLAY via env — the singleton's name-match kill of
        #    'openbox' by argv was a dead no-op: DISPLAY lives in env, not argv).
        procs["openbox"] = subprocess.Popen(
            ["openbox", "--config-file", "/dev/null"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(_WM_STARTUP_WAIT)
        # 3. x11vnc bound to loopback on THIS session's rfbport (no session dbus).
        procs["x11vnc"] = subprocess.Popen(
            ["x11vnc", "-display", h.display, "-forever", "-shared", "-nopw",
             "-listen", "127.0.0.1", "-rfbport", str(h.vnc_port), "-noxdamage", "-quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 4. websockify WS bridge (only when assets present — SHOULD_HAVE).
        if _live_view_available():
            procs["websockify"] = subprocess.Popen(
                ["websockify", f"127.0.0.1:{h.ws_port}", f"127.0.0.1:{h.vnc_port}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            h.live_view = True
        self._procs[h.session_id] = procs
        h.pids = {role: p.pid for role, p in procs.items()}  # introspection mirror

    def get(self, session_id: str) -> Optional[DisplayHandle]:
        with self._lock:
            return self._sessions.get(session_id)

    def release(self, session_id: str) -> None:
        with self._lock:
            h = self._sessions.pop(session_id, None)
            procs = self._procs.pop(session_id, {})
            # Do NOT free the slot yet. A concurrent allocate() (contenders are
            # tasks.py ThreadPoolExecutor threads) must not reclaim this slot —
            # and thus its display_num/rfbport — while the old Xvfb/x11vnc are
            # still terminating, or the new Xvfb dies with "Server is already
            # active for display N". The slot stays reserved until teardown
            # below completes.
        # Teardown the tracked children we own (Popen objects), outside the lock.
        # Dependents before Xvfb. This kills the SPECIFIC pids we spawned — never
        # a process-name kill.
        for role in ("websockify", "x11vnc", "openbox", "xvfb"):
            p = procs.get(role)
            if p is not None:
                _terminate_proc(p)
        # Children are down; now the slot (and its ports/display) is safe to reuse.
        if h is not None:
            with self._lock:
                if self._slots.get(h.slot) == session_id:
                    self._slots.pop(h.slot, None)

    def reap_idle(self, ttl: float = VIRTUAL_DISPLAY_TTL,
                  now: Optional[float] = None) -> List[str]:
        """TTL reaper: release every session idle longer than ``ttl`` seconds and
        return the swept session_ids. Stale handles are snapshotted under the lock,
        then released outside it. Teardown of already-dead children is a no-op
        (release -> _terminate_proc swallows terminate/wait on exited Popens), so
        this is safe to run repeatedly from a background sweep."""
        cutoff = (now if now is not None else time.time()) - ttl
        with self._lock:
            stale = [sid for sid, h in self._sessions.items()
                     if h.last_activity < cutoff]
        for sid in stale:
            self.release(sid)
        return stale

    def active_sessions(self) -> List[dict]:
        with self._lock:
            return [h.to_public() for h in self._sessions.values()]

    def reap_orphans(self) -> None:
        """Sweep restart-survivor children on OUR slot displays/ports that we no
        longer track (a service restart reparents them to init — KillMode=process).
        Targets SPECIFIC slot identifiers, one pid at a time; never a blanket
        process-name kill. Call once at boot."""
        with self._lock:
            tracked = {p.pid for procs in self._procs.values() for p in procs.values()}
        for slot in range(MAX_VIRTUAL_SESSIONS):
            display = f":{DISPLAY_BASE + slot}"
            vnc = VNC_BASE_PORT + slot
            ws = WEBSOCKIFY_BASE_PORT + slot
            for pattern in (f"Xvfb {display}", f"rfbport {vnc}", f"websockify 127.0.0.1:{ws}"):
                for pid in _pids_matching(pattern):
                    if pid not in tracked:
                        print(f"[DISPLAY] boot-reaping orphan pid {pid} ({pattern})")
                        _terminate_pid(pid)

    def shutdown_all(self) -> None:
        for sid in list(self._sessions):
            self.release(sid)


# ── Module singleton ──
_allocator: Optional[DisplayAllocator] = None
_allocator_lock = threading.Lock()

def get_allocator() -> DisplayAllocator:
    global _allocator
    with _allocator_lock:
        if _allocator is None:
            _allocator = DisplayAllocator()
        return _allocator
