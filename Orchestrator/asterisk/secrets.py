"""Secrets-at-rest helper for telephony gateway credentials.

Gateway configs store credentials (HTTP/API passwords, AMI secrets) that must
not sit in plaintext on disk. This module provides a small, reusable Fernet
(AES-128-CBC + HMAC) wrapper with three properties tuned for live production:

- Encrypted values carry an ``enc:`` prefix so we can tell wrapped from legacy
  plaintext at a glance and tolerate gradual migration.
- ``decrypt`` never raises on malformed/foreign tokens — prod runs live from the
  working tree, so a bad value logs a warning and passes through unchanged rather
  than crashing the app.
- The Fernet key is derived lazily and cached, so importing the module is always
  safe even before ``TELEPHONY_SECRET_KEY`` is configured.

Key derivation: prefer a valid Fernet key in ``TELEPHONY_SECRET_KEY`` verbatim;
otherwise SHA-256 the configured value and url-safe-base64 the digest. With no
key set, fall back to a constant dev key and warn once so prod is reminded to
set a stable random ``TELEPHONY_SECRET_KEY``.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from Orchestrator import config

logger = logging.getLogger(__name__)

_PREFIX = "enc:"
# Constant dev fallback so the module is importable / usable with no config.
# NOT secure — only here so local dev and tests work; prod must set the env var.
_DEV_FALLBACK = "blackbox-telephony-insecure-dev-key"

_fernet: Fernet | None = None
_warned_insecure = False


def _derive_key(raw: str) -> bytes:
    """Return a 32-byte url-safe base64 Fernet key from an arbitrary string.

    If ``raw`` is already a valid Fernet key, use it verbatim; otherwise derive
    one deterministically via SHA-256 so any passphrase works.
    """
    candidate = raw.encode()
    try:
        # Validate: a real Fernet key is 32 bytes url-safe-base64-decoded.
        Fernet(candidate)
        return candidate
    except (ValueError, TypeError):
        return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())


def _get_fernet() -> Fernet:
    """Lazily build and cache the module Fernet instance."""
    global _fernet, _warned_insecure
    if _fernet is not None:
        return _fernet

    raw = getattr(config, "TELEPHONY_SECRET_KEY", "") or ""
    if not raw:
        if not _warned_insecure:
            logger.warning(
                "TELEPHONY_SECRET_KEY is not set — encrypting gateway credentials "
                "with an INSECURE default key. Set a stable random "
                "TELEPHONY_SECRET_KEY in the environment for production."
            )
            _warned_insecure = True
        raw = _DEV_FALLBACK

    _fernet = Fernet(_derive_key(raw))
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt ``plaintext`` to an ``enc:``-prefixed Fernet token.

    Empty input returns empty output (keeps "no secret set" clean). Already
    encrypted input (``enc:`` prefix) is returned unchanged (idempotent).
    """
    if not plaintext:
        return ""
    if plaintext.startswith(_PREFIX):
        return plaintext
    token = _get_fernet().encrypt(plaintext.encode()).decode()
    return _PREFIX + token


def decrypt(value: str) -> str:
    """Decrypt an ``enc:``-prefixed value; pass through anything else.

    Legacy plaintext (no prefix) returns unchanged. On decryption failure, log
    a warning and return the value unchanged rather than crash.
    """
    if not value or not value.startswith(_PREFIX):
        return value
    token = value[len(_PREFIX):]
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError, TypeError):
        logger.warning(
            "Failed to decrypt a telephony secret (wrong TELEPHONY_SECRET_KEY or "
            "corrupt value); returning the stored value unchanged."
        )
        return value


def mask(value) -> bool:
    """True if ``value`` is a non-empty string, else False.

    Lets GET responses return ``has_secret`` instead of the secret itself.
    """
    return bool(value) and isinstance(value, str)
