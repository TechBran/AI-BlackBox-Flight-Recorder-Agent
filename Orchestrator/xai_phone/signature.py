"""Standard-Webhooks signature verification for the xAI voice webhook.

xAI signs `realtime.call.incoming` webhooks with the Standard Webhooks scheme
(https://www.standardwebhooks.com/):

    signed_content = f"{webhook-id}.{webhook-timestamp}." + raw_body
    signature      = base64( HMAC-SHA256(secret, signed_content) )

delivered via three headers:
    webhook-id:        unique message id (also the replay key)
    webhook-timestamp: unix seconds
    webhook-signature: space-separated list of "v1,<base64sig>"

Security properties enforced here:
  * constant-time compare (hmac.compare_digest) — no timing oracle;
  * timestamp tolerance ±TOLERANCE_SEC (stale AND future-dated rejected);
  * replay rejection: a webhook-id is accepted at most once per process
    (bounded first-seen-wins cache; ids outside tolerance can't replay anyway).

The secret is used as raw bytes; a `whsec_` prefix (Standard Webhooks portable
secret format) is stripped and base64-decoded when present.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from collections import OrderedDict

TOLERANCE_SEC = 300  # ±5 minutes
_REPLAY_CACHE_MAX = 4096


class ReplayCache:
    """Bounded first-seen-wins webhook-id cache (per-process)."""

    def __init__(self, maxsize: int = _REPLAY_CACHE_MAX):
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._maxsize = maxsize

    def seen_before(self, webhook_id: str) -> bool:
        if webhook_id in self._seen:
            return True
        self._seen[webhook_id] = time.time()
        if len(self._seen) > self._maxsize:
            self._seen.popitem(last=False)
        return False


_default_replay_cache = ReplayCache()


def _secret_bytes(secret: str) -> bytes:
    if secret.startswith("whsec_"):
        try:
            return base64.b64decode(secret[len("whsec_"):], validate=True)
        except (binascii.Error, ValueError):
            pass  # fall through: treat the whole string as raw bytes
    return secret.encode("utf-8")


def verify_signature(
    secret: str,
    headers: dict,
    body: bytes,
    *,
    now: float | None = None,
    tolerance: int = TOLERANCE_SEC,
    replay_cache: ReplayCache | None = None,
) -> tuple[bool, str]:
    """Verify a Standard-Webhooks-signed request.

    Returns (ok, reason). `reason` is for server-side logs ONLY — never echo
    it to the caller beyond a generic 401.
    """
    if not secret:
        return False, "no signing secret configured"

    lowered = {str(k).lower(): v for k, v in headers.items()}
    msg_id = lowered.get("webhook-id", "")
    timestamp = lowered.get("webhook-timestamp", "")
    sig_header = lowered.get("webhook-signature", "")
    if not msg_id or not timestamp or not sig_header:
        return False, "missing webhook headers"

    try:
        ts = int(timestamp)
    except ValueError:
        return False, "malformed timestamp"
    current = time.time() if now is None else now
    if abs(current - ts) > tolerance:
        return False, "timestamp outside tolerance"

    signed_content = msg_id.encode() + b"." + timestamp.encode() + b"." + body
    expected = base64.b64encode(
        hmac.new(_secret_bytes(secret), signed_content, hashlib.sha256).digest()
    ).decode("ascii")

    valid = False
    for candidate in sig_header.split(" "):
        if "," not in candidate:
            continue
        version, sig = candidate.split(",", 1)
        if version == "v1" and hmac.compare_digest(sig, expected):
            valid = True
    if not valid:
        return False, "signature mismatch"

    cache = _default_replay_cache if replay_cache is None else replay_cache
    if cache.seen_before(msg_id):
        return False, "replayed webhook-id"
    return True, "ok"
