#!/usr/bin/env python3
"""Write origin_proof_vectors.json: the golden vectors of the wellness origin
proof (X-BlackBox-Origin, Orchestrator/wellness/origin_proof.py).

The Android OriginSignerGoldenTest reads the same file from the repo root and
must reproduce every expected_sig, so the two signers can never drift apart.
Everything here is deterministic (keys and nonces are hashes of fixed labels;
the gzip vector's compressed bytes are a literal, because zlib builds differ
in the bytes they emit for the same input).

Each vector: {k_b64url, enr, ts, nonce, method, path, body_utf8, body_b64,
content_encoding, expected_sig, request_target, canonical}. body_b64 is the
exact byte string that was signed. For the gzip vector, body_utf8 is the
decompressed JSON and content_encoding is "gzip"; for every other vector
body_b64 is the UTF-8 of body_utf8 and content_encoding is null.

request_target is the path a client asks for, query string included (equal to
`path` when there is none); `path` is the same without the query, which is
what is signed. `method` is as the client spelled it (one vector is lower-case
"get"); the signed method is upper-cased. `canonical` is the exact signed
string, so a signer that drifts can be told where. The first six vectors are
the original set, unchanged; new ones are appended.

    python Orchestrator/tests/fixtures/wellness/make_origin_proof_vectors.py
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "origin_proof_vectors.json"

# gzip.compress(GZIP_TEXT, compresslevel=9, mtime=0), frozen as bytes.
GZIP_TEXT = (
    '{"batch_id":"vector-gzip-0001","buckets":[],"deleted_ids":[],"enrollment_id":'
    '"en_00c0ffee","epoch":1,"origins":[],"readings":[],"schema":"bbx.wellness.ingest/1",'
    '"sessions":[],"window":{"from":"2026-10-27T11:00:00Z","to":"2026-10-27T12:00:00Z",'
    '"type":"changes"}}'
)
GZIP_B64 = (
    "H4sIAAAAAAACA12OQY7CMAxF7+J1C04XM1LOMSsQQm3y20akSZUECoO4+xhRJDSSN9/Pz/aduraY8egsabrAlJjq"
    "4dfNNTMrqqg7mxNKJr0/VGThUWBleG0gpOj9hFBeCxCOzIb7HhAXczQjaVVRTG5wYZUSWuvCsKZsRkytuF133Szw"
    "PiDnjXDksn1+kCW7+JYXF2xcSN+pT3ESreHmq1ZcN98/SmlmqZ1YJf5jzQe7zRBqxvZ5hR6PP5cFXkoFAQAA"
)

# (label, enrollment id, method as sent, request target, body text or None for
# the gzip one)
CASES = [
    ("empty-get", "en_00000001", "GET", "/wellness/status", ""),
    ("capabilities", "en_00000002", "POST", "/wellness/capabilities",
     '{"phone":{"manufacturer":"samsung","model":"SM-TEST","device_name":"Test phone",'
     '"sdk_int":36,"app_version":"1.0"},"reported_at":"2026-10-28T12:00:00Z"}'),
    ("unicode", "en_00000003", "POST", "/wellness/ingest",
     '{"label":"Montre connectée — Ärzte ✓ 心拍","n":1}'),
    ("gzip", "en_00c0ffee", "POST", "/wellness/ingest", None),
    ("settings-put", "en_00000005", "PUT", "/wellness/settings",
     '{"operator":"wellness-fixture","categories":{"blood_pressure":"ask"}}'),
    ("ref-get", "en_00000006", "GET", "/wellness/ref/w-20261028-ab12", ""),
    # A signed ledger read as the in-app Portal asks for it: the query is not signed.
    ("query-get", "en_00000007", "GET",
     "/fossil/snapshot/SNAP-20261028-0001?operator=wellness-fixture", ""),
    # A client that spells the method in lower case: the signed method is "GET".
    ("lowercase-get", "en_00000008", "get", "/wellness/disclosures", ""),
]
TS0 = 1_793_000_000


def _key(label: str) -> bytes:
    return hashlib.sha256(f"bbx-wellness-origin-vector-key:{label}".encode()).digest()


def _nonce(label: str) -> str:
    return hashlib.sha256(f"bbx-wellness-origin-vector-nonce:{label}".encode()).hexdigest()[:32]


def _canonical(enr: str, ts: int, nonce: str, method: str, target: str, body: bytes) -> str:
    """The signed string, written out independently of origin_proof.canonical:
    the method upper-cased, the path without its query, the body's SHA-256."""
    path = target.split("?", 1)[0]
    return f"v1|{enr}|{ts}|{nonce}|{method.upper()}|{path}|{hashlib.sha256(body).hexdigest()}"


def _sign(key: bytes, canonical: str) -> str:
    return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def build_vectors() -> list:
    gz = base64.b64decode(GZIP_B64)
    assert gzip.decompress(gz).decode("utf-8") == GZIP_TEXT
    out = []
    for i, (label, enr, method, target, text) in enumerate(CASES):
        key = _key(label)
        ts = TS0 + i * 61
        nonce = _nonce(label)
        if text is None:
            body, body_text, encoding = gz, GZIP_TEXT, "gzip"
        else:
            body, body_text, encoding = text.encode("utf-8"), text, None
        canonical = _canonical(enr, ts, nonce, method, target, body)
        out.append({
            "k_b64url": base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii"),
            "enr": enr,
            "ts": ts,
            "nonce": nonce,
            "method": method,
            "path": target.split("?", 1)[0],
            "body_utf8": body_text,
            "body_b64": base64.b64encode(body).decode("ascii"),
            "content_encoding": encoding,
            "expected_sig": _sign(key, canonical),
            "request_target": target,
            "canonical": canonical,
        })
    return out


def main() -> None:
    OUT.write_text(json.dumps(build_vectors(), indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
