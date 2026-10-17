"""Pairing routes — QR-based device pairing for AI BlackBox.

POST /pair/start   — Mint a one-time pairing token (TTL 5min). Optional body
                     {"operator": <roster name>}: the operator the device
                     starts as (default: config.pairing_operator()).
POST /pair/claim   — Redeem a token (called by the claiming device).
GET  /pair/status  — Check if a token has been claimed (for poll-style UX).
GET  /pair/qr/{token} — Render PNG QR code for the token (server-side).

Persistent paired-device registry (E13):
    `Manifest/paired_devices.json` survives service restarts. /pair/claim
    appends-or-updates on every successful claim. list_paired_devices() is
    consumed by /onboarding/current-config so the wizard can recognise
    already-paired devices on re-entry (instead of looping on the QR view
    forever when the phone already has stored credentials and never re-claims).
"""
from __future__ import annotations

import io
import json
import logging
import os
import secrets
import shutil
import time
from typing import Optional

import qrcode
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from Orchestrator import config
from Orchestrator.config import BLACKBOX_TAILNET_HOSTNAME
from Orchestrator.utils.paths import resolve

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pair", tags=["pairing"])

PAIR_TOKEN_TTL_SECS = 300

# Token store: token -> {created_at, claimed_at, claimed_by, device_kind}
# In-memory; tokens are short-lived. Restart-tolerant via TTL.
_pair_tokens: dict[str, dict] = {}

# Persistent registry of devices that have ever successfully claimed a token.
# JSON file at Manifest/paired_devices.json. Schema: list of dicts —
#   {device_name, device_kind, claimed_at, hostname, token_prefix}
# Dedup key: (device_name, device_kind) — re-pairing the same device updates
# its claimed_at + hostname rather than appending a duplicate row.
PAIRED_DEVICES_FILE = resolve("Manifest", "paired_devices.json")


def _load_paired_devices() -> list[dict]:
    """Read the persistent paired-device registry from disk.

    Returns empty list if file missing, unreadable, or malformed — we never
    want a corrupt JSON to brick wizard re-entry. A swallowed exception is
    logged (warning, not exception) so an operator can investigate without
    the route 500ing.
    """
    if not PAIRED_DEVICES_FILE.exists():
        return []
    try:
        data = json.loads(PAIRED_DEVICES_FILE.read_text())
        if not isinstance(data, list):
            logger.warning(
                "paired_devices.json malformed (expected list, got %s) — treating as empty",
                type(data).__name__,
            )
            return []
        # Defensive: each entry must be a dict; drop anything else.
        return [d for d in data if isinstance(d, dict)]
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("paired_devices.json read failed: %s — treating as empty", e)
        return []


def _save_paired_devices(devices: list[dict]) -> None:
    """Atomically write the paired-device registry to disk.

    Uses the same backup→tmp→rename pattern as secrets_writer.update_env:
      1. Create timestamped backup of existing file (if any)
      2. Prune to 5 most recent backups (noise control)
      3. Write to .tmp sibling
      4. os.replace() atomically swaps tmp → real
    Mode 0644 (this is non-sensitive registry data, not credentials).
    """
    PAIRED_DEVICES_FILE.parent.mkdir(parents=True, exist_ok=True)

    if PAIRED_DEVICES_FILE.exists():
        ts = int(time.time())
        backup = PAIRED_DEVICES_FILE.with_suffix(f".json.backup.{ts}")
        try:
            shutil.copy2(PAIRED_DEVICES_FILE, backup)
        except OSError as e:
            logger.warning("paired_devices backup failed: %s — continuing anyway", e)
        # Prune to 5 most recent backups
        backups = sorted(
            PAIRED_DEVICES_FILE.parent.glob("paired_devices.json.backup.*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in backups[5:]:
            try:
                old.unlink()
            except OSError:
                pass

    tmp = PAIRED_DEVICES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(devices, indent=2))
    os.replace(tmp, PAIRED_DEVICES_FILE)


def record_pairing(
    device_name: str,
    device_kind: str,
    hostname: str,
    token: str,
) -> dict:
    """Append-or-update a paired device in the persistent registry.

    Dedup key is (device_name, device_kind) — pairing the same physical device
    again refreshes claimed_at + hostname + token_prefix rather than adding a
    duplicate entry. Returns the entry that was written/updated.

    Side effect: persists to PAIRED_DEVICES_FILE atomically.
    """
    devices = _load_paired_devices()
    now = time.time()
    entry = {
        "device_name": device_name,
        "device_kind": device_kind,
        "claimed_at": now,
        "hostname": hostname,
        "token_prefix": token[:6],
    }
    # Find existing entry with same (device_name, device_kind) — update in place
    for i, existing in enumerate(devices):
        if (
            existing.get("device_name") == device_name
            and existing.get("device_kind") == device_kind
        ):
            devices[i] = entry
            _save_paired_devices(devices)
            logger.info(
                "paired_device_updated name=%s kind=%s host=%s",
                device_name, device_kind, hostname,
            )
            return entry
    # No match → append
    devices.append(entry)
    _save_paired_devices(devices)
    logger.info(
        "paired_device_recorded name=%s kind=%s host=%s",
        device_name, device_kind, hostname,
    )
    return entry


def list_paired_devices() -> list[dict]:
    """Return the persistent list of devices that have ever paired.

    Public for /onboarding/current-config consumption. Empty list on first
    install. Read-only — no side effects.
    """
    return _load_paired_devices()


class PairStartRequest(BaseModel):
    # The operator the paired device starts as; None = config.pairing_operator()
    # at claim time (the wizard's choice, else the roster default).
    operator: Optional[str] = None


class PairStartResponse(BaseModel):
    type: str = "pair"
    token: str
    exp: int


class PairClaimRequest(BaseModel):
    token: str
    device_name: str
    device_kind: str  # "android", "desktop", "ios", etc.


class PairClaimResponse(BaseModel):
    success: bool
    operator: Optional[str] = None
    origin: Optional[str] = None


class PairStatusResponse(BaseModel):
    exists: bool
    claimed: bool
    claimed_by: Optional[str] = None
    device_kind: Optional[str] = None
    expires_in: int


def _purge_expired() -> None:
    now = time.time()
    expired = [
        t for t, m in list(_pair_tokens.items())  # snapshot to avoid concurrent-mutation race
        if now - m["created_at"] > PAIR_TOKEN_TTL_SECS
    ]
    for t in expired:
        _pair_tokens.pop(t, None)  # pop with default to handle concurrent removal


@router.post("/start", response_model=PairStartResponse)
def pair_start(req: Optional[PairStartRequest] = None) -> PairStartResponse:
    _purge_expired()
    requested = ((req.operator if req else None) or "").strip() or None
    if requested is not None and requested not in config.USERS_LIST:
        raise HTTPException(status_code=400, detail="operator is not on this box's roster")
    token = secrets.token_urlsafe(16)
    now = time.time()
    _pair_tokens[token] = {
        "created_at": now,
        "claimed_at": None,
        "claimed_by": None,
        "device_kind": None,
        "operator": requested,
    }
    logger.info("pair_token_minted token_id=%s exp=%d", token[:6], int(now + PAIR_TOKEN_TTL_SECS))
    return PairStartResponse(token=token, exp=int(now + PAIR_TOKEN_TTL_SECS))


@router.post("/claim", response_model=PairClaimResponse)
def pair_claim(req: PairClaimRequest, request: Request) -> PairClaimResponse:
    _purge_expired()
    meta = _pair_tokens.get(req.token)
    if not meta:
        raise HTTPException(status_code=404, detail="token unknown or expired")
    if meta["claimed_at"]:  # noqa: race-acceptable for human-paced QR flow
        logger.warning(
            "pair_claim_rejected_already_claimed token_id=%s by=%s",
            req.token[:6], req.device_name,
        )
        raise HTTPException(status_code=409, detail="token already claimed")
    meta["claimed_at"] = time.time()
    meta["claimed_by"] = req.device_name
    meta["device_kind"] = req.device_kind
    origin = str(request.base_url).rstrip('/')
    # Capture the claiming device's remote address (best-effort) for the
    # persistent registry. request.client is None in some ASGI test harnesses,
    # so fall back gracefully.
    hostname = request.client.host if request.client else ""
    # E13: persist the pairing so it survives service restart + so the wizard's
    # pair_phone step can detect already-paired devices on re-entry. Wrapped in
    # try/except — if disk write fails, the in-memory claim still succeeds and
    # the phone gets its credentials; we only lose the wizard's ability to show
    # "already paired" on next visit. Don't fail the claim over a write error.
    try:
        record_pairing(
            device_name=req.device_name,
            device_kind=req.device_kind,
            hostname=hostname,
            token=req.token,
        )
    except Exception:
        logger.exception("record_pairing failed — claim succeeds, registry stale")
    logger.info(
        "pair_claimed token_id=%s by=%s kind=%s",
        req.token[:6], req.device_name, req.device_kind,
    )
    return PairClaimResponse(success=True, operator=config.pairing_operator(meta.get("operator")),
                             origin=origin)


@router.get("/status", response_model=PairStatusResponse)
def pair_status(token: str) -> PairStatusResponse:
    _purge_expired()
    meta = _pair_tokens.get(token)
    if not meta:
        return PairStatusResponse(exists=False, claimed=False, expires_in=0)
    expires_in = max(0, int(PAIR_TOKEN_TTL_SECS - (time.time() - meta["created_at"])))
    return PairStatusResponse(
        exists=True,
        claimed=meta["claimed_at"] is not None,
        claimed_by=meta.get("claimed_by"),
        device_kind=meta.get("device_kind"),
        expires_in=expires_in,
    )


@router.get("/qr/{token}")
def pair_qr(token: str, request: Request):
    """Render PNG QR for a pairing token. Replaces external api.qrserver.com.

    Origin selection: prefer BLACKBOX_TAILNET_HOSTNAME (persisted by the
    onboarding Tailscale step T2.3.1 once validation succeeds). Fall back to
    request.base_url only when the env is unset (customer skipped Tailscale).

    Why this matters: the QR is consumed by a REMOTE phone, not the local
    browser. request.base_url reflects whatever URL the local browser used
    to load the Portal (often http://localhost:9091/ when accessed from the
    BlackBox itself, or via the wizard at /onboarding/) — the phone can't
    reach localhost. The tailnet Magic DNS name is the only origin a phone
    on a different network can hit. Without this preference, scanning the
    wizard's QR yields a useless localhost URL.
    """
    _purge_expired()
    meta = _pair_tokens.get(token)
    if not meta:
        raise HTTPException(status_code=404, detail="token unknown or expired")
    if BLACKBOX_TAILNET_HOSTNAME:
        origin = f"https://{BLACKBOX_TAILNET_HOSTNAME}"
    else:
        # No tailnet configured — fall back to request origin (works for LAN
        # setups where the phone can reach the BlackBox by IP).
        origin = str(request.base_url).rstrip('/')
    payload = json.dumps({
        "type": "pair",
        "token": token,
        "exp": int(meta["created_at"] + PAIR_TOKEN_TTL_SECS),
        "origin": origin,
        "operator": config.pairing_operator(meta.get("operator")),
    })
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")
