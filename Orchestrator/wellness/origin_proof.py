"""The wellness origin proof: HMAC-SHA256 over the request, keyed with the
device key K that the box delivered to the enrolled phone (spec §3.3).

    X-BlackBox-Origin: v1;enr=<enrollment_id>;ts=<unix s>;nonce=<32 hex>;sig=<64 hex>

    sig = HMAC-SHA256(K, "v1|<enr>|<ts>|<nonce>|<METHOD>|<path>|<sha256(body) hex>")

K is the raw 32 bytes (base64url without padding on the wire and at rest);
the path carries no query string; the body hash is over the exact bytes sent
(the gzip bytes for a compressed batch, the empty string for no body).

The box accepts a request when the signature matches (hmac.compare_digest),
|now - ts| <= 120 s, and the nonce was not seen for that enrollment in the
last 10 minutes. A nonce is recorded only AFTER the signature checks out, so a
forged request can never burn a real one. Every failure is the same 401
bad_signature: the caller learns nothing about which part failed. The state
and ownership checks (409 owner_changed, 410 source_moved/revoked) belong to
the enrollment (enroll.verify_signed_request) and run after this.

This module never logs and never stores anything but nonces. Stdlib only.
"""
from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import hmac
import re
import secrets
import threading
import time
from typing import Callable, Dict, Optional

HEADER = "X-BlackBox-Origin"
VERSION = "v1"
MAX_SKEW_S = 120
NONCE_WINDOW_S = 600
KEY_BYTES = 32

_ENR_RE = re.compile(r"en_[0-9a-f]{8}")
_NONCE_RE = re.compile(r"[0-9a-f]{32}")
_SIG_RE = re.compile(r"[0-9a-f]{64}")
_TS_RE = re.compile(r"[0-9]{1,12}")
_FIELDS = ("enr", "ts", "nonce", "sig")


class OriginProofError(Exception):
    """A request that does not carry a valid origin proof."""

    def __init__(self, status: int = 401, code: str = "bad_signature"):
        self.status = status
        self.code = code
        super().__init__(code)


@dataclasses.dataclass(frozen=True)
class VerifiedOrigin:
    enrollment_id: str
    ts: int
    nonce: str


# ------------------------------------------------------------------ keys

def new_key() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def encode_key(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii")


def decode_key(text: str) -> bytes:
    """The 32 raw key bytes from base64url (no padding). ValueError otherwise."""
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", text):
        raise ValueError("a device key is 43 base64url characters")
    try:
        raw = base64.urlsafe_b64decode(text + "=")
    except (binascii.Error, ValueError):
        raise ValueError("a device key is 43 base64url characters") from None
    if len(raw) != KEY_BYTES:
        raise ValueError("a device key is 32 bytes")
    return raw


# ------------------------------------------------------------------ signing

def canonical(enr: str, ts: int, nonce: str, method: str, path: str, body: bytes) -> bytes:
    """The signed message, as bytes."""
    path = (path or "").split("?", 1)[0]
    digest = hashlib.sha256(body or b"").hexdigest()
    return f"{VERSION}|{enr}|{int(ts)}|{nonce}|{method.upper()}|{path}|{digest}".encode("utf-8")


def sign(key: bytes, enr: str, ts: int, nonce: str, method: str, path: str,
         body: bytes) -> str:
    return hmac.new(key, canonical(enr, ts, nonce, method, path, body),
                    hashlib.sha256).hexdigest()


def format_header(enr: str, ts: int, nonce: str, sig: str) -> str:
    return f"{VERSION};enr={enr};ts={int(ts)};nonce={nonce};sig={sig}"


def signed_headers(key: bytes, enr: str, method: str, path: str, body: bytes = b"", *,
                   now: Optional[int] = None, nonce: Optional[str] = None) -> Dict[str, str]:
    """{X-BlackBox-Origin: ...} for a client (the simulator, the tests)."""
    ts = int(time.time()) if now is None else int(now)
    nonce = nonce or secrets.token_hex(16)
    return {HEADER: format_header(enr, ts, nonce, sign(key, enr, ts, nonce, method, path, body))}


def parse_header(value) -> Optional[Dict[str, str]]:
    """The header's fields, or None when it is not exactly one well-formed v1 proof."""
    if not isinstance(value, str) or len(value) > 256:
        return None
    parts = value.strip().split(";")
    if not parts or parts[0] != VERSION or len(parts) != 1 + len(_FIELDS):
        return None
    fields: Dict[str, str] = {}
    for part in parts[1:]:
        name, sep, val = part.partition("=")
        if not sep or name not in _FIELDS or name in fields:
            return None
        fields[name] = val
    if not (_ENR_RE.fullmatch(fields["enr"]) and _TS_RE.fullmatch(fields["ts"])
            and _NONCE_RE.fullmatch(fields["nonce"]) and _SIG_RE.fullmatch(fields["sig"])):
        return None
    return fields


# ------------------------------------------------------------------ nonces

class NonceCache:
    """Nonces seen per enrollment in the last `window_s` seconds. Thread-safe:
    the routes run in the threadpool, and of two concurrent requests with one
    nonce exactly one is first."""

    def __init__(self, window_s: int = NONCE_WINDOW_S, max_per_enrollment: int = 10_000):
        self.window_s = window_s
        self.max_per_enrollment = max_per_enrollment
        self._lock = threading.Lock()
        self._seen: Dict[str, Dict[str, int]] = {}

    def seen(self, enr: str, nonce: str, now: int) -> bool:
        """True when `nonce` was already used for `enr` inside the window;
        otherwise records it and returns False."""
        with self._lock:
            book = self._seen.setdefault(enr, {})
            horizon = now - self.window_s
            if len(book) > 64:
                for old in [n for n, at in book.items() if at < horizon]:
                    del book[old]
            at = book.get(nonce)
            if at is not None and at >= horizon:
                return True
            if len(book) >= self.max_per_enrollment:
                # A flood inside one window: forget the oldest half rather
                # than grow without bound (a replay then needs a valid sig).
                for old in sorted(book, key=book.get)[: len(book) // 2]:
                    del book[old]
            book[nonce] = now
            return False

    def forget(self, enr: str) -> None:
        with self._lock:
            self._seen.pop(enr, None)


# The process-wide cache the routes use.
NONCES = NonceCache()


# ------------------------------------------------------------------ verify

def verify_request(header, method: str, path: str, body: bytes, *,
                   lookup_key: Callable[[str], Optional[bytes]], now: Optional[int] = None,
                   nonces: Optional[NonceCache] = None) -> VerifiedOrigin:
    """Check one request's origin proof. Raises OriginProofError(401) on any
    failure; returns the verified enrollment id otherwise."""
    fields = parse_header(header)
    if fields is None:
        raise OriginProofError()
    now = int(time.time()) if now is None else int(now)
    ts = int(fields["ts"])
    key = lookup_key(fields["enr"])
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_BYTES:
        raise OriginProofError()
    want = sign(bytes(key), fields["enr"], ts, fields["nonce"], method, path, body)
    if not hmac.compare_digest(want.encode("ascii"), fields["sig"].encode("ascii")):
        raise OriginProofError()
    if abs(now - ts) > MAX_SKEW_S:
        raise OriginProofError()
    cache = nonces if nonces is not None else NONCES
    if cache.seen(fields["enr"], fields["nonce"], now):
        raise OriginProofError()
    return VerifiedOrigin(enrollment_id=fields["enr"], ts=ts, nonce=fields["nonce"])
