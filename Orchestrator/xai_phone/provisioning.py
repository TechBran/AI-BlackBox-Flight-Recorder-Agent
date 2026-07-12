"""xAI sovereign phone line — provisioning + credential store.

POST https://api.x.ai/v2/phone-numbers provisions the account's free number
and returns the webhook signing secret ONCE. The full raw response is
persisted verbatim to credentials/xai_phone.json (gitignored via the
`credentials/` rule, 0600, atomic writes) so a mis-guessed response field
name can never lose the secret.

Store conventions follow Orchestrator/onboarding/custom_servers.py:
fresh read per call, tmp-file + os.replace atomic writes, corrupt-file
quarantine (*.corrupt-<ts>). Single-writer process assumption (Orchestrator).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Optional

import httpx

from Orchestrator.config import XAI_API_KEY
from Orchestrator.utils.paths import resolve

logger = logging.getLogger(__name__)

STORE_PATH = str(resolve("credentials", "xai_phone.json"))
XAI_API_BASE = "https://api.x.ai"
# UNCERTAIN (recon xaiResearch.json): docs confirm `origin` is required and
# 'byo_trunk' is the value for customer-owned numbers; the enum value for the
# free xAI-provisioned number is undocumented. Live validation (Task P5.8)
# confirms; adjust ONLY this constant if the API rejects it.
ORIGIN_PROVISIONED = "provisioned"

_LOCK = threading.Lock()


class AlreadyProvisionedError(RuntimeError):
    """A number is already provisioned; pass force=True to re-provision."""


# ---------------------------------------------------------------- persistence

def _quarantine(path: str) -> Optional[str]:
    dest = f"{path}.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    try:
        os.replace(path, dest)
        return dest
    except OSError:
        return None


def read_store() -> dict:
    """Load the store fresh from disk. Fail-soft: NEVER raises."""
    path = str(STORE_PATH)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        quarantined = _quarantine(path)
        logger.warning("[XAI-PHONE] corrupt store at %s (%s) — quarantined to %s",
                       path, exc, quarantined or "<quarantine failed>")
        return {}
    except OSError as exc:
        logger.warning("[XAI-PHONE] unreadable store at %s (%s)", path, exc)
        return {}
    if not isinstance(data, dict):
        quarantined = _quarantine(path)
        logger.warning("[XAI-PHONE] wrong-shape store at %s — quarantined to %s",
                       path, quarantined or "<quarantine failed>")
        return {}
    return data


def _write_store(data: dict) -> None:
    """Atomically persist the store (tmp file + os.replace), 0600 perms."""
    path = str(STORE_PATH)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory,
        prefix=".xai_phone.", suffix=".tmp", delete=False,
    )
    try:
        json.dump(data, tmp, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.replace(tmp.name, path)
    except BaseException:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


# ------------------------------------------------------------------ read API

def get_status() -> dict:
    """Public line status. NEVER includes the signing secret or raw response."""
    data = read_store()
    return {
        "provisioned": bool(data.get("phone_number")),
        "phone_number": data.get("phone_number") or None,
        "webhook_url": data.get("webhook_url") or None,
        "has_signing_secret": bool(data.get("signing_secret")),
        "default_preset_id": data.get("default_preset_id"),
        "provisioned_at": data.get("provisioned_at"),
    }


def get_signing_secret() -> str:
    return read_store().get("signing_secret", "") or ""


def get_default_preset_id() -> Optional[str]:
    return read_store().get("default_preset_id") or None


def set_default_preset_id(preset_id: Optional[str]) -> None:
    with _LOCK:
        data = read_store()
        data["default_preset_id"] = preset_id
        _write_store(data)


# --------------------------------------------------------------- provisioning

async def _api_post(path: str, payload: dict) -> dict:
    """POST to the xAI REST API. Module-level so tests monkeypatch it."""
    if not XAI_API_KEY:
        raise RuntimeError("XAI_API_KEY not configured (Orchestrator/config.py:446)")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{XAI_API_BASE}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {XAI_API_KEY}"},
        )
        resp.raise_for_status()
        return resp.json()


def _extract_secret(resp: dict) -> str:
    """The signing secret is returned ONCE; field name unconfirmed — try all
    plausible spellings, including nested under 'webhook'."""
    for key in ("signing_secret", "webhook_secret", "webhook_signing_secret", "secret"):
        if isinstance(resp.get(key), str) and resp[key]:
            return resp[key]
    nested = resp.get("webhook")
    if isinstance(nested, dict):
        for key in ("signing_secret", "secret"):
            if isinstance(nested.get(key), str) and nested[key]:
                return nested[key]
    return ""


async def provision_number(name: str, webhook_url: str, *, force: bool = False) -> dict:
    """Provision the account's free number with a webhook attach. Idempotent:
    refuses if already provisioned unless force=True. Returns get_status()."""
    existing = read_store()
    if existing.get("phone_number") and not force:
        raise AlreadyProvisionedError(
            f"Already provisioned: {existing['phone_number']} "
            f"(webhook {existing.get('webhook_url')}). Pass force=true to re-provision."
        )

    resp = await _api_post("/v2/phone-numbers", {
        "origin": ORIGIN_PROVISIONED,
        "name": name,
        "webhook": webhook_url,
    })

    secret = _extract_secret(resp)
    if not secret:
        logger.warning("[XAI-PHONE] no signing secret found in provisioning response "
                       "— check raw_response in %s", STORE_PATH)
    phone_number = resp.get("phone_number") or resp.get("number") or ""

    with _LOCK:
        _write_store({
            "version": 1,
            "phone_number": phone_number,
            "webhook_url": webhook_url,
            "name": name,
            "signing_secret": secret,
            "default_preset_id": existing.get("default_preset_id"),
            "provisioned_at": datetime.now(timezone.utc).isoformat(),
            "raw_response": resp,  # secret is returned ONCE — keep everything
        })
    logger.info("[XAI-PHONE] provisioned %s (webhook %s)", phone_number or "<no number in resp>", webhook_url)
    return get_status()
