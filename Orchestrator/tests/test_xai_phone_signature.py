"""Standard-Webhooks HMAC verification for the xAI voice webhook.

Security-critical: this is the ONLY auth on the one publicly-funneled path.
Covers: valid / tampered body / wrong secret / stale / future-dated /
replayed id / missing+malformed headers / multi-signature header /
whsec_-prefixed and raw-string secrets / case-insensitive headers.
"""
import base64
import hashlib
import hmac
import inspect

import pytest

from Orchestrator.xai_phone.signature import ReplayCache, verify_signature

SECRET = "whsec_" + base64.b64encode(b"test-signing-key-32-bytes-long!!").decode()
BODY = b'{"type":"realtime.call.incoming","call_id":"call-123"}'
NOW = 1_800_000_000.0


def sign(body: bytes, msg_id="msg_1", ts=None, secret=SECRET, version="v1"):
    ts = str(int(NOW)) if ts is None else ts
    if secret.startswith("whsec_"):
        key = base64.b64decode(secret[len("whsec_"):])
    else:
        key = secret.encode()
    mac = hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": f"{version},{base64.b64encode(mac).decode()}",
    }


def _fresh():
    return ReplayCache()


def test_valid_signature_accepted():
    ok, reason = verify_signature(SECRET, sign(BODY), BODY, now=NOW, replay_cache=_fresh())
    assert ok, reason


def test_tampered_body_rejected():
    ok, reason = verify_signature(SECRET, sign(BODY), BODY + b"x", now=NOW, replay_cache=_fresh())
    assert not ok and reason == "signature mismatch"


def test_wrong_secret_rejected():
    other = "whsec_" + base64.b64encode(b"a-completely-different-key!!!!!!").decode()
    ok, reason = verify_signature(other, sign(BODY), BODY, now=NOW, replay_cache=_fresh())
    assert not ok and reason == "signature mismatch"


def test_stale_timestamp_rejected():
    headers = sign(BODY, ts=str(int(NOW) - 301))
    ok, reason = verify_signature(SECRET, headers, BODY, now=NOW, replay_cache=_fresh())
    assert not ok and reason == "timestamp outside tolerance"


def test_future_timestamp_rejected():
    headers = sign(BODY, ts=str(int(NOW) + 301))
    ok, reason = verify_signature(SECRET, headers, BODY, now=NOW, replay_cache=_fresh())
    assert not ok and reason == "timestamp outside tolerance"


def test_replay_rejected():
    cache = _fresh()
    headers = sign(BODY, msg_id="msg_replay")
    ok1, _ = verify_signature(SECRET, headers, BODY, now=NOW, replay_cache=cache)
    ok2, reason = verify_signature(SECRET, headers, BODY, now=NOW, replay_cache=cache)
    assert ok1
    assert not ok2 and reason == "replayed webhook-id"


def test_missing_headers_rejected():
    ok, reason = verify_signature(SECRET, {}, BODY, now=NOW, replay_cache=_fresh())
    assert not ok and reason == "missing webhook headers"


def test_malformed_timestamp_rejected():
    headers = sign(BODY)
    headers["webhook-timestamp"] = "not-a-number"
    ok, reason = verify_signature(SECRET, headers, BODY, now=NOW, replay_cache=_fresh())
    assert not ok and reason == "malformed timestamp"


def test_empty_secret_rejected():
    ok, reason = verify_signature("", sign(BODY), BODY, now=NOW, replay_cache=_fresh())
    assert not ok and reason == "no signing secret configured"


def test_case_insensitive_headers():
    headers = {k.upper(): v for k, v in sign(BODY, msg_id="msg_upper").items()}
    ok, _ = verify_signature(SECRET, headers, BODY, now=NOW, replay_cache=_fresh())
    assert ok


def test_multiple_signatures_one_valid():
    headers = sign(BODY, msg_id="msg_multi")
    headers["webhook-signature"] = "v2,Z2FyYmFnZQ== " + headers["webhook-signature"]
    ok, _ = verify_signature(SECRET, headers, BODY, now=NOW, replay_cache=_fresh())
    assert ok


def test_raw_string_secret_supported():
    raw = "plain-secret-no-prefix"
    headers = sign(BODY, msg_id="msg_raw", secret=raw)
    ok, _ = verify_signature(raw, headers, BODY, now=NOW, replay_cache=_fresh())
    assert ok


def test_replay_cache_bounded():
    cache = ReplayCache(maxsize=2)
    assert not cache.seen_before("a")
    assert not cache.seen_before("b")
    assert not cache.seen_before("c")   # evicts "a"
    assert cache.seen_before("b")


# ---------------------------------------------------------------------------
# Security hardening added beyond the plan's suite (caller-mandated, P5.1):
# the plan's tests do not explicitly pin the constant-time compare, the
# per-header fail-closed behaviour, malformed-signature handling, or the
# no-secret-leak guarantee. These are the security-critical invariants, so
# they get their own tests.
# ---------------------------------------------------------------------------


def test_signature_comparison_is_constant_time():
    """Pins the timing-side-channel guard (required case #9): the signature
    bytes MUST be compared with hmac.compare_digest, never a short-circuiting
    `==`. A plain `==` on a secret-derived digest leaks the valid signature
    one byte at a time via response-timing, defeating the whole scheme.
    """
    src = inspect.getsource(verify_signature)
    assert "hmac.compare_digest(" in src, "verify_signature must call hmac.compare_digest"
    # And must NOT fall back to a naive equality on the expected signature.
    assert "== expected" not in src
    assert "expected ==" not in src


@pytest.mark.parametrize("drop", ["webhook-id", "webhook-timestamp", "webhook-signature"])
def test_each_missing_header_rejected(drop):
    """Fail-closed when ANY single required header is absent (not just all)."""
    headers = sign(BODY, msg_id="msg_drop")
    del headers[drop]
    ok, reason = verify_signature(SECRET, headers, BODY, now=NOW, replay_cache=_fresh())
    assert not ok and reason == "missing webhook headers"


@pytest.mark.parametrize(
    "bad_sig",
    ["", "garbage-no-comma", "v1,", "v1,!!!!not-base64", "novee,abc", ","],
)
def test_malformed_signature_header_rejected(bad_sig):
    """Any malformed webhook-signature value fails closed (never crashes)."""
    headers = sign(BODY, msg_id="msg_badsig")
    headers["webhook-signature"] = bad_sig
    ok, reason = verify_signature(SECRET, headers, BODY, now=NOW, replay_cache=_fresh())
    assert not ok
    # "" trips the missing-header guard; everything else is a signature mismatch.
    assert reason in ("missing webhook headers", "signature mismatch")


def test_secret_never_leaked_in_reason():
    """No failure path may echo the signing secret in the (log-only) reason.

    Exercises every real-secret-bearing branch with a unique marker secret and
    asserts neither the whsec_-prefixed form nor its decoded-key base64 ever
    appears in the returned reason string.
    """
    inner = base64.b64encode(b"UNIQUE-MARKER-SECRET-DO-NOT-LEAK!").decode()
    marker = "whsec_" + inner
    cases = [
        (marker, {}, BODY),                                                        # missing headers
        (marker, {**sign(BODY, secret=marker), "webhook-timestamp": "x"}, BODY),   # malformed ts
        (marker, sign(BODY, secret=marker, ts=str(int(NOW) - 9999)), BODY),        # stale
        (marker, sign(BODY, secret=SECRET), BODY),                                 # signature mismatch
    ]
    for sec, headers, body in cases:
        ok, reason = verify_signature(sec, headers, body, now=NOW, replay_cache=_fresh())
        assert not ok
        assert marker not in reason
        assert inner not in reason



# --- P5.1 security-review fixes (HIGH non-ASCII sig; MED replay lock; LOW strict ts) ---

def test_non_ascii_signature_fails_closed_no_crash():
    """HIGH: a non-ASCII byte in webhook-signature fails closed, never raises
    TypeError on the auth path (str compare_digest rejects non-ASCII)."""
    h = {"webhook-id": "msg_x", "webhook-timestamp": str(int(NOW)),
         "webhook-signature": "v1,caféé"}
    ok, reason = verify_signature(SECRET, h, BODY, now=NOW, replay_cache=_fresh())
    assert ok is False and reason == "signature mismatch"


def test_valid_signature_survives_trailing_non_ascii_candidate():
    """HIGH: a valid sig followed by a poisoned non-ASCII candidate still
    verifies True — the loop must break on match and never crash."""
    h = sign(BODY, msg_id="msg_ok")
    h["webhook-signature"] = h["webhook-signature"] + " v1,caféé"
    ok, reason = verify_signature(SECRET, h, BODY, now=NOW, replay_cache=_fresh())
    assert ok is True, reason


def test_lax_timestamp_forms_rejected():
    """LOW: int() accepts spaces/underscores/'+'/unicode-digits/newline; strict
    digit-only parse rejects them (log-injection + spec-laxity defense)."""
    base = str(int(NOW))
    for bad in (f" {base} ", f"+{base}", f"{base}\n", "1_800_000_000", "١٨"):
        h = {"webhook-id": "m", "webhook-timestamp": bad, "webhook-signature": "v1,x"}
        ok, reason = verify_signature(SECRET, h, BODY, now=NOW, replay_cache=_fresh())
        assert ok is False and reason == "malformed timestamp", (bad, reason)


def test_replay_cache_seen_before_is_locked():
    """MED: the replay cache guards check-then-insert with a lock."""
    c = ReplayCache()
    assert hasattr(c, "_lock")
    assert c.seen_before("id-1") is False
    assert c.seen_before("id-1") is True
