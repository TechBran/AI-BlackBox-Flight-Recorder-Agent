"""Tests for the image-generation param catalog (the param SoT) + the
schema<->GenIn<->adapter<->catalog coherence contract.

NO real API calls -- requests.post/get and availability._read_env are
monkeypatched. The coherence test is the load-bearing one: it asserts that every
param the catalog advertises is BOTH a GenIn field AND actually forwarded by the
matching provider adapter, so any future drift in those four layers fails CI.
"""
import base64

import pytest

from Orchestrator import image_catalog
from Orchestrator.image_catalog import IMAGE_PROVIDER_SPECS, build_image_catalog
from Orchestrator import image_providers
from Orchestrator.startup import GenIn


# ---------------------------------------------------------------------------
# build_image_catalog -- enabled-set + default-flag behavior
# ---------------------------------------------------------------------------

def _patch_env(monkeypatch, env: dict):
    """Force availability._read_env() (used by both enabled_providers and the
    IMAGE_DEFAULT lookup) to return exactly `env`."""
    from Orchestrator.toolvault import availability
    monkeypatch.setattr(availability, "_read_env", lambda: dict(env))


def test_all_three_when_keys_present_and_enabled_unset(monkeypatch):
    _patch_env(monkeypatch, {
        "GOOGLE_API_KEY": "g", "OPENAI_API_KEY": "o", "XAI_API_KEY": "x",
    })
    cat = build_image_catalog()
    assert [p["provider"] for p in cat] == ["gemini", "openai", "grok"]
    for p in cat:
        spec = IMAGE_PROVIDER_SPECS[p["provider"]]
        assert p["label"] == spec["label"]
        assert p["params"] == spec["params"]
        assert isinstance(p["default"], bool)


def test_enabled_pref_subsets_providers(monkeypatch):
    _patch_env(monkeypatch, {
        "GOOGLE_API_KEY": "g", "OPENAI_API_KEY": "o", "XAI_API_KEY": "x",
        "IMAGE_ENABLED": "gemini,openai",
    })
    cat = build_image_catalog()
    assert [p["provider"] for p in cat] == ["gemini", "openai"]


def test_default_flag_from_image_default(monkeypatch):
    _patch_env(monkeypatch, {
        "GOOGLE_API_KEY": "g", "OPENAI_API_KEY": "o", "XAI_API_KEY": "x",
        "IMAGE_DEFAULT": "openai",
    })
    cat = {p["provider"]: p for p in build_image_catalog()}
    assert cat["openai"]["default"] is True
    assert cat["gemini"]["default"] is False
    assert cat["grok"]["default"] is False


def test_no_default_when_image_default_unset(monkeypatch):
    _patch_env(monkeypatch, {"GOOGLE_API_KEY": "g"})
    cat = build_image_catalog()
    assert all(p["default"] is False for p in cat)


# ---------------------------------------------------------------------------
# GET /image/catalog -- route shape
# ---------------------------------------------------------------------------

def test_image_catalog_route_ok():
    import Orchestrator.app  # noqa: F401  -- side-effect: registers routes onto the shared app
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    r = TestClient(app).get("/image/catalog")
    assert r.status_code == 200
    body = r.json()
    assert "providers" in body and isinstance(body["providers"], list)
    for p in body["providers"]:
        assert set(p.keys()) >= {"provider", "label", "default", "params"}
        assert isinstance(p["params"], list)
        for prm in p["params"]:
            assert "name" in prm and "type" in prm


# ---------------------------------------------------------------------------
# COHERENCE -- the contract lock: schema/spec <-> GenIn <-> adapter <-> catalog
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, json_data=None, content=None, status_code=200, text=""):
        self._json = json_data or {}
        self.content = content or b""
        self.status_code = status_code   # call_imagen inspects status_code/text
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


def _capture_openai(monkeypatch, options):
    """Call _openai_images with all params and capture the request body."""
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        captured["json"] = json
        b64 = base64.b64encode(b"X").decode()
        return _FakeResp(json_data={"data": [{"b64_json": b64}]})

    monkeypatch.setattr(image_providers.requests, "post", fake_post)
    image_providers._openai_images("a cat", options)
    return captured["json"]


def _capture_xai(monkeypatch, options):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        captured["json"] = json
        b64 = base64.b64encode(b"X").decode()
        return _FakeResp(json_data={"data": [{"b64_json": b64}]})

    monkeypatch.setattr(image_providers.requests, "post", fake_post)
    image_providers._xai_images("a dog", options)
    return captured["json"]


def _capture_gemini(monkeypatch, options):
    """_gemini_images delegates to call_imagen; capture the Gemini payload."""
    captured = {}
    from Orchestrator.routes import tts_routes

    def fake_post(url, json=None, timeout=None, **kwargs):
        captured["json"] = json
        b64 = base64.b64encode(b"X").decode()
        return _FakeResp(json_data={
            "candidates": [{"content": {"parts": [{"inlineData": {"data": b64}}]}}]
        })

    monkeypatch.setattr(tts_routes, "GOOGLE_API_KEY", "g", raising=False)
    monkeypatch.setattr(tts_routes.requests, "post", fake_post)
    image_providers._gemini_images("a bird", options)
    return captured["json"]


# Per-provider: the option value we send for each advertised param, and a
# predicate that confirms it landed in the outbound request body.
_ADAPTER_PROBES = {
    "openai": {
        "capture": _capture_openai,
        # param name -> (value to send, predicate(body, value) -> bool)
        "checks": {
            "size": ("1536x1024", lambda b, v: b.get("size") == v),
            "quality": ("medium", lambda b, v: b.get("quality") == v),
            "numberOfImages": (3, lambda b, v: b.get("n") == v),
        },
    },
    "grok": {
        "capture": _capture_xai,
        "checks": {
            "aspectRatio": ("16:9", lambda b, v: v in (b.get("aspect_ratio"), b.get("aspectRatio"))),
            "numberOfImages": (2, lambda b, v: b.get("n") == v),
        },
    },
    "gemini": {
        "capture": _capture_gemini,
        "checks": {
            "aspectRatio": ("9:16", lambda b, v: b["generationConfig"]["imageConfig"]["aspectRatio"] == v),
            "resolution": ("2K", lambda b, v: b["generationConfig"]["imageConfig"]["imageSize"] == v),
            # numberOfImages controls the generation LOOP count, not a body field;
            # coverage for it lives in test_image_providers routing tests.
            "numberOfImages": (1, lambda b, v: True),
        },
    },
}


def test_every_catalog_param_is_a_genin_field():
    """(a) Every advertised param name is a field GenIn accepts."""
    fields = set(GenIn.model_fields)
    for prov, spec in IMAGE_PROVIDER_SPECS.items():
        for prm in spec["params"]:
            assert prm["name"] in fields, (
                f"{prov} advertises {prm['name']!r} but GenIn has no such field "
                f"-- pydantic would drop it before it reaches the worker")


def test_every_catalog_param_reaches_its_adapter(monkeypatch):
    """(b) Every advertised param is actually forwarded by that provider's
    adapter into the outbound request. Locks schema<->adapter<->catalog."""
    for prov, spec in IMAGE_PROVIDER_SPECS.items():
        probe = _ADAPTER_PROBES[prov]
        assert set(p["name"] for p in spec["params"]) == set(probe["checks"]), (
            f"{prov}: catalog params and adapter probe checks drifted")
        # Send all advertised params at once, mirroring image_options.
        options = {prm["name"]: probe["checks"][prm["name"]][0] for prm in spec["params"]}
        body = probe["capture"](monkeypatch, options)
        for name, (value, predicate) in probe["checks"].items():
            assert predicate(body, value), (
                f"{prov}: advertised param {name!r}={value!r} did not reach the "
                f"adapter request body: {body}")
