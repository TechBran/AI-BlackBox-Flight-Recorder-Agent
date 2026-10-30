"""The wellness origin proof (X-BlackBox-Origin, spec §3.3): the canonical
string, the published golden vectors the Android OriginSignerGoldenTest
replays, clock skew, nonce replay and the constant-time compare."""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import importlib.util
import json
import threading
from pathlib import Path

import pytest

from Orchestrator.wellness import origin_proof as op

REPO = Path(__file__).resolve().parents[2]
VECTORS = REPO / "Orchestrator" / "tests" / "fixtures" / "wellness" / "origin_proof_vectors.json"
GENERATOR = VECTORS.parent / "make_origin_proof_vectors.py"

KEY = bytes(range(32))
K_B64 = base64.urlsafe_b64encode(KEY).rstrip(b"=").decode()
ENR = "en_0a1b2c3d"
NOW = 1_790_000_000


def _load_generator():
    spec = importlib.util.spec_from_file_location("make_vectors", GENERATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _header(ts=NOW, nonce="ab" * 16, method="POST", path="/wellness/ingest", body=b"{}", key=KEY):
    sig = op.sign(key, ENR, ts, nonce, method, path, body)
    return f"v1;enr={ENR};ts={ts};nonce={nonce};sig={sig}"


def _lookup(enr):
    return KEY if enr == ENR else None


# ------------------------------------------------------------ canonical form

def test_canonical_string_is_the_contract():
    body = b'{"a":1}'
    expect = "v1|%s|%d|%s|POST|/wellness/ingest|%s" % (
        ENR, NOW, "ab" * 16, hashlib.sha256(body).hexdigest())
    assert op.canonical(ENR, NOW, "ab" * 16, "post", "/wellness/ingest?x=1", body) == expect.encode()


def test_sign_is_hmac_sha256_hex_lowercase():
    body = b""
    msg = op.canonical(ENR, NOW, "cd" * 16, "GET", "/wellness/status", body)
    want = hmac.new(KEY, msg, hashlib.sha256).hexdigest()
    got = op.sign(KEY, ENR, NOW, "cd" * 16, "GET", "/wellness/status", body)
    assert got == want and got == got.lower() and len(got) == 64


def test_key_codec_round_trips_base64url_without_padding():
    assert op.decode_key(K_B64) == KEY
    assert op.encode_key(KEY) == K_B64
    assert "=" not in op.encode_key(KEY)
    for bad in ("", "abc", K_B64 + "AA", "!" * 43):
        with pytest.raises(ValueError):
            op.decode_key(bad)


# ------------------------------------------------------------ golden vectors

def test_vectors_file_matches_its_generator():
    """The committed file is exactly what the committed generator writes (so
    the Kotlin side and this side replay the same bytes)."""
    committed = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert committed == _load_generator().build_vectors()


def test_vectors_cover_the_required_shapes():
    """Every vector carries the contract's keys plus body_b64, the exact bytes
    that were signed: a gzip body is not text, so body_utf8 holds its
    decompressed JSON and content_encoding says it was sent compressed. For
    every other vector body_b64 is the UTF-8 of body_utf8."""
    vectors = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert len(vectors) == 6
    for v in vectors:
        assert {"k_b64url", "enr", "ts", "nonce", "method", "path", "body_utf8",
                "expected_sig", "body_b64", "content_encoding"} == set(v)
        raw = base64.b64decode(v["body_b64"])
        if v["content_encoding"] == "gzip":
            assert gzip.decompress(raw) == v["body_utf8"].encode("utf-8")
        else:
            assert v["content_encoding"] is None
            assert raw == v["body_utf8"].encode("utf-8")
    bodies = [v["body_utf8"] for v in vectors]
    assert "" in bodies                                       # an empty body
    assert any(any(ord(c) > 127 for c in b) for b in bodies)  # unicode
    assert sum(v["content_encoding"] == "gzip" for v in vectors) == 1
    assert {v["method"] for v in vectors} >= {"GET", "POST", "PUT"}


def test_every_vector_verifies_and_reproduces():
    for v in json.loads(VECTORS.read_text(encoding="utf-8")):
        body = base64.b64decode(v["body_b64"])
        key = op.decode_key(v["k_b64url"])
        assert op.sign(key, v["enr"], v["ts"], v["nonce"], v["method"], v["path"], body) \
            == v["expected_sig"], v
        header = op.format_header(v["enr"], v["ts"], v["nonce"], v["expected_sig"])
        verified = op.verify_request(header, v["method"], v["path"], body,
                                     lookup_key=lambda enr, k=key: k, now=v["ts"],
                                     nonces=op.NonceCache())
        assert verified.enrollment_id == v["enr"]


# ------------------------------------------------------------ verification

def test_valid_request_verifies():
    v = op.verify_request(_header(), "POST", "/wellness/ingest", b"{}",
                          lookup_key=_lookup, now=NOW, nonces=op.NonceCache())
    assert v.enrollment_id == ENR and v.ts == NOW


@pytest.mark.parametrize("header", [
    None, "", "v2;enr=en_0a1b2c3d;ts=1;nonce=" + "a" * 32 + ";sig=" + "0" * 64,
    "v1;enr=en_0a1b2c3d;ts=x;nonce=" + "a" * 32 + ";sig=" + "0" * 64,
    "v1;enr=en_0a1b2c3d;ts=1;nonce=" + "A" * 32 + ";sig=" + "0" * 64,   # not lowercase
    "v1;enr=en_0a1b2c3d;ts=1;nonce=" + "a" * 30 + ";sig=" + "0" * 64,   # short nonce
    "v1;enr=en_0a1b2c3d;ts=1;nonce=" + "a" * 32 + ";sig=" + "0" * 63,
    "v1;enr=en_0a1b2c3d;enr=en_0a1b2c3d;ts=1;nonce=" + "a" * 32 + ";sig=" + "0" * 64,
    "v1;enr=../x;ts=1;nonce=" + "a" * 32 + ";sig=" + "0" * 64,
])
def test_malformed_header_is_bad_signature(header):
    with pytest.raises(op.OriginProofError) as e:
        op.verify_request(header, "POST", "/wellness/ingest", b"{}", lookup_key=_lookup,
                          now=NOW, nonces=op.NonceCache())
    assert e.value.status == 401 and e.value.code == "bad_signature"


@pytest.mark.parametrize("change", [
    dict(method="PUT"), dict(path="/wellness/capabilities"), dict(body=b"{} "),
    dict(key=bytes(32)),
])
def test_any_changed_part_fails(change):
    header = _header(**{k: v for k, v in change.items() if k == "key"})
    method = change.get("method", "POST")
    path = change.get("path", "/wellness/ingest")
    body = change.get("body", b"{}")
    with pytest.raises(op.OriginProofError) as e:
        op.verify_request(header, method, path, body, lookup_key=_lookup, now=NOW,
                          nonces=op.NonceCache())
    assert e.value.code == "bad_signature"


def test_unknown_enrollment_is_bad_signature():
    with pytest.raises(op.OriginProofError) as e:
        op.verify_request(_header(), "POST", "/wellness/ingest", b"{}",
                          lookup_key=lambda enr: None, now=NOW, nonces=op.NonceCache())
    assert e.value.status == 401


@pytest.mark.parametrize("skew,ok", [(0, True), (120, True), (-120, True), (121, False),
                                     (-121, False), (100_000, False)])
def test_clock_skew_window(skew, ok):
    header = _header(ts=NOW + skew)
    call = lambda: op.verify_request(header, "POST", "/wellness/ingest", b"{}",  # noqa: E731
                                     lookup_key=_lookup, now=NOW, nonces=op.NonceCache())
    if ok:
        assert call().enrollment_id == ENR
    else:
        with pytest.raises(op.OriginProofError) as e:
            call()
        assert e.value.status == 401 and e.value.code == "bad_signature"


def test_replayed_nonce_is_refused_within_ten_minutes():
    cache = op.NonceCache()
    header = _header()
    op.verify_request(header, "POST", "/wellness/ingest", b"{}", lookup_key=_lookup,
                      now=NOW, nonces=cache)
    with pytest.raises(op.OriginProofError) as e:
        op.verify_request(header, "POST", "/wellness/ingest", b"{}", lookup_key=_lookup,
                          now=NOW + 30, nonces=cache)
    assert e.value.status == 401
    # The same nonce from a DIFFERENT enrollment is a different key space.
    assert cache.seen("en_ffffffff", "ab" * 16, NOW + 30) is False


def test_nonce_cache_expires_after_the_window():
    cache = op.NonceCache(window_s=600)
    assert cache.seen(ENR, "ab" * 16, NOW) is False
    assert cache.seen(ENR, "ab" * 16, NOW + 599) is True
    assert cache.seen(ENR, "cd" * 16, NOW + 601) is False
    assert cache.seen(ENR, "ab" * 16, NOW + 1200) is False   # pruned, recorded again


def test_failed_signature_never_burns_the_nonce():
    """A forged request must not let an attacker pre-poison a nonce."""
    cache = op.NonceCache()
    forged = _header(key=bytes(32))
    with pytest.raises(op.OriginProofError):
        op.verify_request(forged, "POST", "/wellness/ingest", b"{}", lookup_key=_lookup,
                          now=NOW, nonces=cache)
    assert op.verify_request(_header(), "POST", "/wellness/ingest", b"{}",
                             lookup_key=_lookup, now=NOW, nonces=cache).enrollment_id == ENR


def test_nonce_cache_is_thread_safe_one_winner():
    cache = op.NonceCache()
    results = []
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()
        results.append(cache.seen(ENR, "ef" * 16, NOW))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(False) == 1 and results.count(True) == 15


def test_compare_is_constant_time_by_construction(monkeypatch):
    """verify_request compares through hmac.compare_digest, never ==."""
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(op.hmac, "compare_digest", spy)
    op.verify_request(_header(), "POST", "/wellness/ingest", b"{}", lookup_key=_lookup,
                      now=NOW, nonces=op.NonceCache())
    assert calls, "the signature was not compared with hmac.compare_digest"


def test_module_never_logs(caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    with pytest.raises(op.OriginProofError):
        op.verify_request(_header(key=bytes(32)), "POST", "/wellness/ingest", b"{}",
                          lookup_key=_lookup, now=NOW, nonces=op.NonceCache())
    op.verify_request(_header(), "POST", "/wellness/ingest", b"{}", lookup_key=_lookup,
                      now=NOW, nonces=op.NonceCache())
    assert caplog.records == []


def test_signed_headers_helper_for_clients():
    headers = op.signed_headers(KEY, ENR, "GET", "/wellness/status", b"", now=NOW,
                                nonce="12" * 16)
    v = op.verify_request(headers[op.HEADER], "GET", "/wellness/status", b"",
                          lookup_key=_lookup, now=NOW, nonces=op.NonceCache())
    assert v.nonce == "12" * 16
