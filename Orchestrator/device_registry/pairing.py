"""Pairing secrets for the phone's control listener (audit X02/X04).

The Android app hosts an HTTP listener on the tailnet (RemoteControlServer, port 8765) that
the box calls to drive the device: /task, /status, /action, /stream, /kill*, /telemetry and
/notify. Before this module the only checks on it were "the caller has a 100.64/10 address"
and "the body names the bound operator" — both satisfiable by any other app on the phone or
by anyone on a carrier-grade-NAT network that shares that range. The phone now requires
``Authorization: Bearer <secret>`` on every route except ``GET /healthz``.

Flow: the phone mints a random 256-bit secret on first run, keeps it in its app-private
no-backup storage, and registers it here with ``POST /devices/pairing`` every time its
listener starts (app launch, boot). Every box-side caller of the listener reads it back
with :func:`auth_headers_for_node` and sends it. A device with no recorded secret fails with
an actionable "open the app once" message (:class:`DeviceNotPaired`) — never a silent
unauthenticated call, which the phone would refuse anyway.

Not the QR origin pairing in ``routes/pairing_routes.py`` (that tells the phone which box
to talk to); this is the reverse direction — what the box presents when it calls the phone.

Storage: ``credentials/device_pairing.json`` (gitignored), mode 0600, written atomically,
read FRESH on every lookup so a re-pair takes effect with no restart. Rows are keyed by the
device's tailnet IP — the exact address the box resolves from ``tailscale status`` when it
reaches the device (``mesh.Node.ip``) — and a lookup never falls back to a name match, so
one device's secret can never be handed to another node. A registration always replaces
the address and the secret together; there is no address-only update that could re-point
an existing secret at a different node. Secrets are never logged and never returned by any
endpoint.

Registration itself is open inside the Tailscale/LAN perimeter, like the rest of the box API
(no app-layer auth, by design). The worst a hostile peer can do through it is overwrite a row,
which makes the device refuse this box (401) until its app re-registers on the next launch; it
can never read a secret back, so it can never drive the device.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

from Orchestrator.utils.paths import resolve

logger = logging.getLogger(__name__)

PAIRING_PATH = resolve("credentials", "device_pairing.json")

# Serializes read-modify-write within this process (the Orchestrator is the only writer).
_LOCK = threading.Lock()

ERROR_KIND_NOT_PAIRED = "not_paired"

NOT_PAIRED_MESSAGE = (
    "{device} has not paired its control listener with this BlackBox yet. Open the "
    "BlackBox app on that device once (it pairs automatically when it starts), then try "
    "again.")
PAIRING_REJECTED_MESSAGE = (
    "{device} rejected this BlackBox's pairing secret (HTTP 401). Open the BlackBox app on "
    "that device once so it re-pairs with this box, then try again.")

# At least 256 bits: 64 hex chars (what the phone mints) or 43+ base64url chars.
_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{43,128}")
_TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")     # Tailscale CGNAT range
_TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")  # Tailscale ULA
_MAX_DEVICE_ID = 128
_MAX_LABEL = 80
_MAX_KIND = 32


class DeviceNotPaired(Exception):
    """The box holds no pairing secret for the device it is about to call."""

    kind = ERROR_KIND_NOT_PAIRED

    def __init__(self, device: str):
        self.device = device
        self.message = NOT_PAIRED_MESSAGE.format(device=device or "The device")
        super().__init__(self.message)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_tailnet_ip(value) -> str:
    """The canonical text form of a Tailscale address, or ValueError for anything else.

    Only the Tailscale ranges are accepted: a LAN, loopback or public address is never a
    place the box reaches a device's control listener."""
    text = str(value or "").strip()
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        raise ValueError("tailnet_ip must be the device's Tailscale IP address") from None
    if addr not in _TAILNET_V4 and addr not in _TAILNET_V6:
        raise ValueError("tailnet_ip must be a Tailscale address (100.64.0.0/10 or "
                         "fd7a:115c:a1e0::/48)")
    return str(addr)


def _quarantine(path: str) -> Optional[str]:
    dest = f"{path}.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    try:
        os.replace(path, dest)
        return dest
    except OSError:
        return None


def _read() -> Dict[str, dict]:
    """Load ``{tailnet_ip: row}``. Fail-soft: a missing, corrupt or wrong-shape file reads
    as empty (a corrupt file is quarantined first so the next write cannot destroy it)."""
    path = str(PAIRING_PATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        quarantined = _quarantine(path)
        logger.warning("[PAIRING] corrupt store at %s (%s) -- quarantined to %s, treating "
                       "as empty", path, type(exc).__name__, quarantined or "<failed>")
        return {}
    except OSError as exc:
        logger.warning("[PAIRING] unreadable store at %s (%s) -- treating as empty",
                       path, type(exc).__name__)
        return {}
    devices = data.get("devices") if isinstance(data, dict) else None
    if not isinstance(devices, dict):
        quarantined = _quarantine(path)
        logger.warning("[PAIRING] store at %s has the wrong shape -- quarantined to %s",
                       path, quarantined or "<failed>")
        return {}
    return {ip: row for ip, row in devices.items()
            if isinstance(row, dict) and isinstance(row.get("secret"), str)}


def _write(devices: Dict[str, dict]) -> None:
    """Persist atomically: a 0600 temp file in the same directory, fsync, os.replace."""
    path = str(PAIRING_PATH)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    # mkstemp creates the file 0600 before a byte is written, so the secret is never
    # readable by anyone else, not even for the instant between write and rename.
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory,
        prefix=".device_pairing.", suffix=".tmp", delete=False,
    )
    try:
        json.dump({"version": 1, "devices": devices}, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.chmod(tmp.name, 0o600)
        os.replace(tmp.name, path)
    except BaseException:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def _public(ip: str, row: dict) -> dict:
    return {
        "tailnet_ip": ip,
        "device_id": row.get("device_id", ""),
        "display_name": row.get("display_name", ""),
        "device_kind": row.get("device_kind", ""),
        "paired_at": row.get("paired_at", ""),
    }


def register(device_id: str, tailnet_ip: str, secret: str,
             display_name: str = "", device_kind: str = "") -> dict:
    """Record (or replace) a device's listener secret. Returns the public row (no secret).

    Raises ValueError on a non-Tailscale address, a secret weaker than 256 bits, or a
    missing device id. A device that re-registers from a NEW address drops its old row, so
    a stale address never keeps a live secret."""
    did = str(device_id or "").strip()
    if not did or len(did) > _MAX_DEVICE_ID:
        raise ValueError("device_id is required (at most 128 characters)")
    ip = normalize_tailnet_ip(tailnet_ip)
    if not isinstance(secret, str) or not _SECRET_RE.fullmatch(secret):
        # Never echo the value: this message reaches the HTTP response.
        raise ValueError("secret must be at least 256 bits of hex or base64url "
                         "(43-128 characters)")
    row = {
        "device_id": did,
        "secret": secret,
        "display_name": str(display_name or "").strip()[:_MAX_LABEL],
        "device_kind": str(device_kind or "").strip()[:_MAX_KIND],
        "paired_at": _now_iso(),
    }
    with _LOCK:
        devices = _read()
        for other_ip in [k for k, v in devices.items() if v.get("device_id") == did and k != ip]:
            del devices[other_ip]
        devices[ip] = row
        _write(devices)
    logger.info("[PAIRING] registered device=%s ip=%s kind=%s", did, ip, row["device_kind"])
    return _public(ip, row)


def forget(tailnet_ip: str) -> bool:
    """Drop a device's secret (the box then refuses to call it until it re-pairs)."""
    try:
        ip = normalize_tailnet_ip(tailnet_ip)
    except ValueError:
        return False
    with _LOCK:
        devices = _read()
        if ip not in devices:
            return False
        del devices[ip]
        _write(devices)
    logger.info("[PAIRING] forgot ip=%s", ip)
    return True


def public_rows() -> List[dict]:
    """Every paired device's metadata. Secrets are never included."""
    return [_public(ip, row) for ip, row in sorted(_read().items())]


def secret_for_ip(ip: str) -> Optional[str]:
    """The secret recorded for exactly this tailnet IP, or None."""
    try:
        key = normalize_tailnet_ip(ip)
    except ValueError:
        return None
    row = _read().get(key)
    return row.get("secret") if row else None


def auth_headers_for_ip(ip: str, device_label: str = "") -> Dict[str, str]:
    """``{"Authorization": "Bearer <secret>"}`` for the device at ``ip``.

    Raises :class:`DeviceNotPaired` (with the user-facing message) when no secret is on
    record — a caller must never fall back to an unauthenticated request."""
    secret = secret_for_ip(ip)
    if not secret:
        raise DeviceNotPaired(device_label or ip)
    return {"Authorization": f"Bearer {secret}"}


def auth_headers_for_node(node) -> Dict[str, str]:
    """Auth headers for a resolved ``mesh.Node`` — looked up by its tailnet IP only."""
    label = getattr(node, "dns_name", "") or getattr(node, "ip", "")
    return auth_headers_for_ip(getattr(node, "ip", ""), label)


def is_pairing_rejection(exc: BaseException) -> bool:
    """True when ``exc`` is an HTTP 401 from the device (aiohttp or httpx error shapes):
    the device has a different secret than the one on record here."""
    status = getattr(exc, "status", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 401


def rejected_message(device: str) -> str:
    return PAIRING_REJECTED_MESSAGE.format(device=device or "The device")
