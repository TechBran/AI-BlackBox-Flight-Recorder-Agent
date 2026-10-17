"""X-Twilio-Signature verification (audit X03-security-backend-08 / -02).

Orchestrator/phone/twilio_signature.py re-implements twilio-python's
RequestValidator with the stdlib (the twilio SDK is not a dependency of this
box). The known-answer vectors below were produced by
twilio.request_validator.RequestValidator from twilio==9.3.7 with the same
token, URL and params, so a drift from Twilio's algorithm fails here.

Also covers the public-URL reconstruction: `tailscale serve`/Funnel keep the
browser-facing Host and add X-Forwarded-*, and Twilio signs the URL it called,
not the loopback URL uvicorn sees.
"""
import base64
import hashlib
import hmac

import pytest

from Orchestrator.phone import twilio_signature as ts

TOKEN = "12345"
URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
PARAMS = {"CallSid": "CA1234567890ABCDE", "Caller": "+15555550100", "Digits": "1234",
          "From": "+15555550100", "To": "+15555550199"}


def reference(token, url, pairs):
    """Independent restatement of Twilio's published algorithm."""
    grouped = {}
    for k, v in pairs:
        grouped.setdefault(k, set()).add(v)
    s = url + "".join(k + v for k in sorted(grouped) for v in sorted(grouped[k]))
    return base64.b64encode(hmac.new(token.encode(), s.encode(), hashlib.sha1).digest()).decode()


# ── known answers from twilio-python ────────────────────────────────────────

def test_known_answer_vectors_from_twilio_sdk():
    assert ts.compute_signature(TOKEN, URL, PARAMS) == "ph4vszRJfwX/6ZN3rFuKXL3ZANI="
    assert ts.compute_signature(TOKEN, "https://mycompany.com:443/myapp.php?foo=1&bar=2",
                                PARAMS) == "ul7QWlPCCleseaWdHhLnkAwpdSU="
    assert ts.compute_signature(TOKEN, "https://example.ts.net/twilio/media/phone-abc",
                                {}) == "1dy7YgYIod1I83EbH324RwWS9Vc="
    # Multi-valued params: unique values, sorted, per sorted key.
    pairs = [("a", "2"), ("a", "1"), ("a", "1"), ("B", "x")]
    assert ts.compute_signature(TOKEN, "https://example.ts.net/twilio/status",
                                pairs) == "+1UsiHZbyxO7CVUhB7C6Mz40Bls="


def test_matches_independent_reference():
    pairs = list(PARAMS.items()) + [("Digits", "9")]
    assert ts.compute_signature(TOKEN, URL, pairs) == reference(TOKEN, URL, pairs)


# ── validate ────────────────────────────────────────────────────────────────

def test_valid_signature_passes():
    sig = ts.compute_signature(TOKEN, URL, PARAMS)
    assert ts.validate(TOKEN, URL, PARAMS, sig) is True


def test_port_variants_validate_like_twilio():
    with_port = ts.compute_signature(TOKEN, "https://mycompany.com:443/myapp.php?foo=1&bar=2", PARAMS)
    assert ts.validate(TOKEN, URL, PARAMS, with_port) is True
    without_port = ts.compute_signature(TOKEN, URL, PARAMS)
    assert ts.validate(TOKEN, "https://mycompany.com:443/myapp.php?foo=1&bar=2", PARAMS, without_port) is True


@pytest.mark.parametrize("tamper", [
    lambda p: {**p, "Digits": "1235"},                 # changed value
    lambda p: {**p, "Extra": "1"},                     # injected param
    lambda p: {k: v for k, v in p.items() if k != "To"},  # dropped param
])
def test_tampered_params_fail(tamper):
    sig = ts.compute_signature(TOKEN, URL, PARAMS)
    assert ts.validate(TOKEN, URL, tamper(PARAMS), sig) is False


def test_tampered_url_fails():
    sig = ts.compute_signature(TOKEN, URL, PARAMS)
    assert ts.validate(TOKEN, URL.replace("foo=1", "foo=2"), PARAMS, sig) is False
    assert ts.validate(TOKEN, URL.replace("https://", "http://"), PARAMS, sig) is False


def test_wrong_token_fails():
    sig = ts.compute_signature("other-token", URL, PARAMS)
    assert ts.validate(TOKEN, URL, PARAMS, sig) is False


@pytest.mark.parametrize("sig", ["", None, "not-base64", "café", "x" * 500])
def test_missing_or_garbage_signature_fails(sig):
    assert ts.validate(TOKEN, URL, PARAMS, sig) is False


def test_no_token_never_validates():
    sig = ts.compute_signature("", URL, PARAMS)
    assert ts.validate("", URL, PARAMS, sig) is False


# ── public URL reconstruction ───────────────────────────────────────────────

def test_candidates_use_forwarded_proto_and_host():
    urls = ts.candidate_urls(
        scheme="http", host_header="127.0.0.1:9091",
        forwarded_proto="https", forwarded_host="box.example.ts.net",
        path="/twilio/pin", query="session_id=phone-abc", configured_base="")
    assert urls[0] == "https://box.example.ts.net/twilio/pin?session_id=phone-abc"


def test_candidates_keep_preserved_host_when_proto_missing():
    # tailscale serve preserves Host; if X-Forwarded-Proto is absent the
    # public scheme is still https.
    urls = ts.candidate_urls(
        scheme="http", host_header="box.example.ts.net",
        forwarded_proto="", forwarded_host="",
        path="/twilio/voice", query="", configured_base="")
    assert "https://box.example.ts.net/twilio/voice" in urls


def test_candidates_include_configured_webhook_base():
    urls = ts.candidate_urls(
        scheme="http", host_header="localhost:9091",
        forwarded_proto="", forwarded_host="",
        path="/twilio/status", query="", configured_base="wss://box.example.ts.net/")
    assert "https://box.example.ts.net/twilio/status" in urls
    assert len(urls) == len(set(urls))


def test_candidates_take_first_forwarded_value():
    urls = ts.candidate_urls(
        scheme="http", host_header="127.0.0.1:9091",
        forwarded_proto="https, http", forwarded_host="box.example.ts.net, proxy.internal",
        path="/twilio/voice", query="", configured_base="")
    assert urls[0] == "https://box.example.ts.net/twilio/voice"
