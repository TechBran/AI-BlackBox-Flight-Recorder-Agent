"""Per-boot shared secret for loopback control calls between BlackBox processes.

Audit 2026-10-09 M3 U20 item 3 (O20-mcp-server-08).

"The caller is 127.0.0.1" proves nothing on a BlackBox: `tailscale serve`
hands every tailnet request to the Orchestrator from loopback, and Tailscale
Funnel hands INTERNET requests to the MCP server's 127.0.0.1:9093 listener.
A control endpoint that must stay internal (MCP POST /internal/reload) checks
a secret only processes running as the service user can read instead:

* one file, credentials/mcp_reload.secret (BLACKBOX_MCP_RELOAD_SECRET_FILE
  overrides), mode 0600, JSON {"boot_id": ..., "secret": ...};
* bound to this boot (the kernel's boot_id): a secret written before the last
  reboot is dead, and whichever side needs one first writes a fresh one
  (ensure_secret). The endpoint side only reads (verify), and a file that is
  not a private regular file of this user is never trusted;
* compared in constant time.

No login, sessions or API keys for people: this is process-to-process only.
Stdlib only, like utils.paths, so the lean MCP venv can import it.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Optional

from Orchestrator.utils.paths import blackbox_root

SECRET_HEADER = "X-BlackBox-Internal-Secret"
_BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"
_MAX_FILE_BYTES = 4096


def mcp_reload_secret_path(root=None) -> Path:
    """Where the MCP reload secret lives: the env override, else
    <root>/credentials/mcp_reload.secret (root defaults to blackbox_root())."""
    env = os.getenv("BLACKBOX_MCP_RELOAD_SECRET_FILE", "").strip()
    if env:
        return Path(env)
    base = Path(root) if root is not None else blackbox_root()
    return base / "credentials" / "mcp_reload.secret"


def _boot_id() -> str:
    """This boot's id, or "" where the kernel offers none (the secret then
    persists across reboots, still private and random)."""
    try:
        with open(_BOOT_ID_FILE, encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return ""


def read_secret(path) -> Optional[str]:
    """The current boot's secret, or None (missing, stale, corrupt, or not a
    0600-style regular file owned by this user)."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                or st.st_mode & 0o077 or st.st_size > _MAX_FILE_BYTES):
            return None
        raw = os.read(fd, _MAX_FILE_BYTES)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("boot_id") != _boot_id():
        return None
    secret = data.get("secret")
    return secret if isinstance(secret, str) and secret else None


def ensure_secret(path) -> str:
    """The current boot's secret, writing a fresh one when there is none.

    The first writer wins (link() refuses an existing name), so concurrent
    callers end up agreeing on one file; a dead file (stale boot, corrupt,
    wrong mode) is replaced. Raises OSError when the file cannot be written.
    """
    path = Path(path)
    current = read_secret(path)
    if current:
        return current
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps({"boot_id": _boot_id(), "secret": secrets.token_urlsafe(32)})
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        try:
            os.link(tmp, path)
        except FileExistsError:
            if read_secret(path) is None:      # what is there is dead: replace it
                os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    result = read_secret(path)
    if not result:
        raise OSError(f"could not establish the internal secret at {path}")
    return result


def verify(presented, path) -> bool:
    """Does `presented` equal the current boot's secret? Never creates one."""
    expected = read_secret(path)
    if not expected or not isinstance(presented, str) or not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
