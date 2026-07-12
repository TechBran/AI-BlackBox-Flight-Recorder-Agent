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
import logging
import re
import threading
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)

TOLERANCE_SEC = 300  # ±5 minutes
_REPLAY_CACHE_MAX = 4096
_TIMESTAMP_RE = re.compile(r"[0-9]{1,15}")


class ReplayCache:
    """Bounded first-seen-wins webhook-id cache (per-process).

    NOTE: per-process — under `uvicorn --workers N` each worker has its own
    cache, so a captured webhook could be accepted once per worker. This box
    runs single-worker; if that changes, back this with a shared TTL store
    (Redis/DB) keyed by webhook-id with TTL = signature tolerance.
    """

    def __init__(self, maxsize: int = _REPLAY_CACHE_MAX):
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def seen_before(self, webhook_id: str) -> bool:
        # Atomic check-then-insert: without the lock, two concurrent requests
        # with the same captured signature can both observe "not seen" (a GIL
        # preemption point sits between the membership test and the insert) and
        # both be accepted — a replay race on the auth path.
        with self._lock:
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
            # Operator misconfig: a whsec_-prefixed secret that isn't valid
            # base64. Surface it (never log the secret itself) rather than
            # silently masking it as a total verification outage.
            logger.warning(
                "xai_phone signing secret has whsec_ prefix but invalid base64 "
                "body — falling back to raw bytes; verification will fail if the "
                "provider signs with the decoded key. Re-check the stored secret."
            )
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

    # Strict-parse: int() alone accepts whitespace/underscores/'+'/unicode
    # digits/trailing-newline, which is spec-lax and (via a raw-timestamp log)
    # a log-injection vector. The timestamp is HMAC-bound so this is not a
    # forgery surface, but reject anything but plain ASCII digits.
    if not _TIMESTAMP_RE.fullmatch(timestamp):
        return False, "malformed timestamp"
    ts = int(timestamp)
    current = time.time() if now is None else now
    if abs(current - ts) > tolerance:
        return False, "timestamp outside tolerance"

    signed_content = msg_id.encode() + b"." + timestamp.encode() + b"." + body
    expected = base64.b64encode(
        hmac.new(_secret_bytes(secret), signed_content, hashlib.sha256).digest()
    ).decode("ascii")

    expected_b = expected.encode("ascii")
    valid = False
    for candidate in sig_header.split(" "):
        if "," not in candidate:
            continue
        version, sig = candidate.split(",", 1)
        # Compare BYTES, not str: hmac.compare_digest on str raises TypeError
        # for any non-ASCII char, and `sig` is attacker-controlled — a header
        # like `v1,café` would crash the sole public auth path (and, with
        # no break, poison an otherwise-valid multi-signature request). bytes
        # compare_digest has no ASCII restriction. break once matched.
        if version == "v1" and hmac.compare_digest(sig.encode("utf-8"), expected_b):
            valid = True
            break
    if not valid:
        return False, "signature mismatch"

    cache = _default_replay_cache if replay_cache is None else replay_cache
    if cache.seen_before(msg_id):
        return False, "replayed webhook-id"
    return True, "ok"
