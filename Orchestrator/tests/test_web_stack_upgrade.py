"""The web stack is the upgraded one, and its fixes reach this app's routes.

The lock froze fastapi 0.118 / starlette 0.48 / uvicorn 0.30 / pydantic 2.9,
because fastapi 0.118 required starlette<0.49 (audit X01-deps-python-03, rank
70). Starlette 0.48 carried six advisories: a quadratic Range-header DoS on
FileResponse/StaticFiles (CVE-2025-62727), urlencoded forms ignoring
max_fields/max_part_size (CVE-2026-54283), a Host header that changed
request.url.path (CVE-2026-48710) and three more; the supply-chain gate
waived all six.

The locked versions themselves are pinned by test_dependency_manifest.py. The
route tests run against the installed stack, so on a venv built before the
upgrade they skip and say to rebuild it from requirements.lock.txt (an ops
step of rank 70); CI builds from the lock and runs them.
"""
from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

# The first starlette release with all six fixes (requirements.in's floor).
_STARLETTE_FLOOR = (1, 3, 1)


def _version(text: str):
    return tuple(int(p) for p in re.findall(r"\d+", text)[:3])


def _require_upgraded_stack():
    import starlette
    if _version(starlette.__version__) < _STARLETTE_FLOOR:
        pytest.skip(f"this venv runs starlette {starlette.__version__}, older than the "
                    "lock: rebuild it with pip install --require-hashes -r "
                    "requirements.lock.txt (audit rank 70 ops step)")


def test_a_json_route_refuses_a_body_without_a_json_content_type(app_client):
    """strict_content_type (fastapi 0.132+) stays on: a model-bound route
    parses only an application/json body, before its handler runs."""
    _require_upgraded_stack()
    body = b'{"operator": "op", "name": "Ada"}'
    for headers in ({}, {"Content-Type": "text/plain"}):
        r = app_client.post("/contacts", content=body, headers=headers)
        assert r.status_code == 422, (headers, r.text)
    r = app_client.post("/contacts", content=b"{}",
                        headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert {e["loc"][-1] for e in r.json()["detail"]} >= {"operator", "name"}


def test_a_static_file_ignores_a_flood_of_ranges(app_client):
    """CVE-2025-62727: a request for many ranges of a /ui asset made
    FileResponse do quadratic work building a multipart answer. Starlette
    1.5.1+ caps a request at 100 ranges: past that it ignores the Range header
    and sends the file whole (starlette 0.48 answered 206 multipart)."""
    _require_upgraded_stack()
    ranges = ",".join(f"{i}-{i}" for i in range(0, 202, 2))   # 101 ranges
    r = app_client.get("/ui/app-modular.js", headers={"Range": f"bytes={ranges}"})
    assert r.status_code == 200, r.status_code
    assert "multipart" not in r.headers.get("content-type", "")
    ok = app_client.get("/ui/app-modular.js", headers={"Range": "bytes=0-9"})
    assert ok.status_code == 206 and len(ok.content) == 10


def test_an_urlencoded_form_is_held_to_max_fields(app_client):
    """CVE-2026-54283: the urlencoded parser ignored max_fields, so one POST
    could make a Form route allocate an unbounded field list. Starlette 1.3.1+
    refuses it with 400 while parsing, before the handler."""
    _require_upgraded_stack()
    body = "&".join(f"f{i}=x" for i in range(1001)) + "&session_id=s"
    r = app_client.post("/upload/session", content=body.encode(),
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 400, (r.status_code, r.text)
    assert "fields" in r.text.lower()
