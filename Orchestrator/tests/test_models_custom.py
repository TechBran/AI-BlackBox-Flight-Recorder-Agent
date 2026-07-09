# Orchestrator/tests/test_models_custom.py
"""GET /models/custom — live-merged catalog for user-registered
OpenAI-compatible servers (custom-model-providers plan Task 3.1).

Pins:
- Alias-qualified ids ("alias::model"), display "model (alias)", additive
  per-model `server` + `status` fields on the LOCKED _wrap envelope
  {provider, models, source, default_id, fetched_iso, cached}.
- llama-swap warm-status extension (data[].status.value) parsed defensively;
  plain OpenAI payloads (no extension) -> status None.
- Empty registry -> 200 empty catalog (source "fallback"), NOT 404.
- Dead server -> its cached last_models backfilled (status None); source
  "live" iff >=1 probe succeeded.
- NEVER cached: fresh probe every call, no models_cache entry for "custom".

House pattern: direct route-function calls (test_cu_catalog.py precedent) +
cs.REGISTRY_PATH -> tmp_path (test_custom_servers_routes.py precedent).
httpx.get is monkeypatched at module level (the fetcher lazy-imports httpx,
so the global attribute is the live seam).
"""
import pytest

from Orchestrator.onboarding import custom_servers as cs
from Orchestrator.routes import admin_routes
from Orchestrator.utils import models_cache


@pytest.fixture
def tmp_registry(tmp_path, monkeypatch):
    path = tmp_path / "custom_models.json"
    monkeypatch.setattr(cs, "REGISTRY_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def _clear_models_cache():
    models_cache.invalidate()
    yield
    models_cache.invalidate()


# ------------------------------------------------------------------ fixtures

class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _openai_payload(ids, statuses=None):
    """OpenAI-shaped /models list; statuses[i] adds the llama-swap extension."""
    data = []
    for i, mid in enumerate(ids):
        item = {"id": mid, "object": "model"}
        if statuses and statuses[i] is not None:
            item["status"] = {"value": statuses[i]}
        data.append(item)
    return {"object": "list", "data": data}


def _route_by_url(monkeypatch, table, calls=None):
    """Monkeypatch httpx.get: base_url -> payload dict | Exception to raise.

    Records each call's url/headers/timeout into `calls` when given (append is
    GIL-atomic, safe from the fetcher's worker threads).
    """
    def fake_get(url, headers=None, timeout=None, **kw):
        if calls is not None:
            calls.append({"url": url, "headers": dict(headers or {}), "timeout": timeout})
        for base, payload in table.items():
            if url == f"{base}/models":
                if isinstance(payload, Exception):
                    raise payload
                return _FakeResponse(payload)
        raise AssertionError(f"unexpected URL probed: {url}")

    monkeypatch.setattr("httpx.get", fake_get)


# --------------------------------------------------------------------- tests

def test_models_custom_merges_servers_with_qualified_ids(tmp_registry, monkeypatch):
    cs.add_server(alias="one", base_url="http://10.0.0.1:8080/v1")
    cs.add_server(alias="two", base_url="http://10.0.0.2:8080/v1")
    _route_by_url(monkeypatch, {
        "http://10.0.0.1:8080/v1": _openai_payload(["gemma-26b"]),
        "http://10.0.0.2:8080/v1": _openai_payload(["qwen-7b"]),
    })

    out = admin_routes.get_available_models("custom")

    # Locked envelope contract — exactly these keys, nothing dropped
    assert set(out) == {"provider", "models", "source", "default_id",
                        "fetched_iso", "cached"}
    assert out["provider"] == "custom"
    assert out["source"] == "live"
    assert out["default_id"] == "one::gemma-26b"   # first model of first server
    assert out["cached"] is False

    # Registry order preserved; ids alias-qualified; additive fields present
    assert [m["id"] for m in out["models"]] == ["one::gemma-26b", "two::qwen-7b"]
    m0 = out["models"][0]
    assert m0["name"] == "gemma-26b (one)"
    assert m0["server"] == "one"
    assert "status" in m0

    # Successful probe refreshes the server's cached last_models
    by_alias = {s["alias"]: s for s in cs.list_servers()}
    assert by_alias["one"]["last_models"] == ["gemma-26b"]
    assert by_alias["two"]["last_models"] == ["qwen-7b"]


def test_models_custom_includes_warm_status(tmp_registry, monkeypatch):
    cs.add_server(alias="swap", base_url="http://10.0.0.3:8080/v1")
    cs.add_server(alias="plain", base_url="http://10.0.0.4:8080/v1")
    # `plain` is a bare OpenAI payload — no status extension at all — plus one
    # malformed entry (status not a dict) that must parse defensively to None.
    plain_payload = _openai_payload(["c"])
    plain_payload["data"].append({"id": "d", "object": "model", "status": "loaded"})
    _route_by_url(monkeypatch, {
        "http://10.0.0.3:8080/v1": _openai_payload(
            ["a", "b"], statuses=["loaded", "unloaded"]),
        "http://10.0.0.4:8080/v1": plain_payload,
    })

    out = admin_routes.get_available_models("custom")
    by_id = {m["id"]: m for m in out["models"]}

    assert by_id["swap::a"]["status"] == "loaded"
    assert by_id["swap::b"]["status"] == "unloaded"
    assert by_id["plain::c"]["status"] is None        # extension absent
    assert by_id["plain::d"]["status"] is None        # extension malformed


def test_models_custom_no_servers_empty_not_404(tmp_registry, monkeypatch):
    monkeypatch.setattr(
        "httpx.get", lambda *a, **k: pytest.fail("no HTTP probe expected"))

    out = admin_routes.get_available_models("custom")   # must NOT raise 404

    assert out["provider"] == "custom"
    assert out["models"] == []
    assert out["source"] == "fallback"
    assert out["default_id"] == ""
    assert out["cached"] is False


def test_models_custom_disabled_server_not_probed(tmp_registry, monkeypatch):
    srv = cs.add_server(alias="off", base_url="http://10.0.0.10:8080/v1")
    cs.update_server(srv["id"], {"enabled": False, "last_models": ["m"]})
    monkeypatch.setattr(
        "httpx.get", lambda *a, **k: pytest.fail("disabled server was probed"))

    out = admin_routes.get_available_models("custom")

    assert out["models"] == []
    assert out["source"] == "fallback"


def test_models_custom_dead_server_falls_back_to_last_models(tmp_registry, monkeypatch):
    cs.add_server(alias="alive", base_url="http://10.0.0.5:8080/v1")
    dead = cs.add_server(alias="dead", base_url="http://10.0.0.6:8080/v1")
    cs.update_server(dead["id"], {"last_models": ["old-model"]})
    _route_by_url(monkeypatch, {
        "http://10.0.0.5:8080/v1": _openai_payload(["fresh"], statuses=["loaded"]),
        "http://10.0.0.6:8080/v1": ConnectionError("LAN box is off"),
    })

    out = admin_routes.get_available_models("custom")

    assert out["source"] == "live"                     # one probe succeeded
    by_id = {m["id"]: m for m in out["models"]}
    assert by_id["alive::fresh"]["status"] == "loaded"
    assert by_id["dead::old-model"]["status"] is None  # cached -> warm unknown
    assert by_id["dead::old-model"]["server"] == "dead"
    # The dead server's stored last_models are untouched
    assert cs.get_server(dead["id"])["last_models"] == ["old-model"]


def test_models_custom_all_dead_source_fallback(tmp_registry, monkeypatch):
    srv = cs.add_server(alias="dead", base_url="http://10.0.0.7:8080/v1")
    cs.update_server(srv["id"], {"last_models": ["m1", "m2"]})
    _route_by_url(monkeypatch, {
        "http://10.0.0.7:8080/v1": ConnectionError("off"),
    })

    out = admin_routes.get_available_models("custom")

    assert out["source"] == "fallback"
    assert [m["id"] for m in out["models"]] == ["dead::m1", "dead::m2"]
    assert out["default_id"] == "dead::m1"


def test_models_custom_never_cached(tmp_registry, monkeypatch):
    cs.add_server(alias="one", base_url="http://10.0.0.8:8080/v1")
    calls = []
    _route_by_url(monkeypatch, {
        "http://10.0.0.8:8080/v1": _openai_payload(["m"]),
    }, calls=calls)

    out1 = admin_routes.get_available_models("custom")
    out2 = admin_routes.get_available_models("custom")

    assert len(calls) == 2                       # fetch ran BOTH times
    assert out1["cached"] is False and out2["cached"] is False
    assert "custom" not in models_cache.cache_state()   # cache never touched


def test_models_custom_bearer_only_when_key_present(tmp_registry, monkeypatch):
    cs.add_server(alias="keyed", base_url="http://10.0.1.1:8080/v1",
                  api_key="sk-secret")
    cs.add_server(alias="open", base_url="http://10.0.1.2:8080/v1")
    calls = []
    _route_by_url(monkeypatch, {
        "http://10.0.1.1:8080/v1": _openai_payload(["a"]),
        "http://10.0.1.2:8080/v1": _openai_payload(["b"]),
    }, calls=calls)

    admin_routes.get_available_models("custom")

    by_url = {c["url"]: c for c in calls}
    assert by_url["http://10.0.1.1:8080/v1/models"]["headers"].get(
        "Authorization") == "Bearer sk-secret"
    assert "Authorization" not in by_url["http://10.0.1.2:8080/v1/models"]["headers"]


def test_models_custom_persist_skipped_when_last_models_unchanged(tmp_registry, monkeypatch):
    """This endpoint is polled (never cached); update_server is a full
    fsync+rename of the registry. An unchanged model list must not write."""
    srv = cs.add_server(alias="one", base_url="http://10.0.2.1:8080/v1")
    _route_by_url(monkeypatch, {
        "http://10.0.2.1:8080/v1": _openai_payload(["m1", "m2"]),
    })

    admin_routes.get_available_models("custom")           # first fetch persists
    assert cs.get_server(srv["id"])["last_models"] == ["m1", "m2"]

    calls = []
    real_update = cs.update_server

    def spy(server_id, patch):
        calls.append((server_id, patch))
        return real_update(server_id, patch)

    monkeypatch.setattr(cs, "update_server", spy)

    admin_routes.get_available_models("custom")           # identical payload
    assert calls == []                                    # dirty-check: no write

    # Positive companion (proves the spy is live): a CHANGED list persists.
    _route_by_url(monkeypatch, {
        "http://10.0.2.1:8080/v1": _openai_payload(["m1", "m2", "m3"]),
    })
    admin_routes.get_available_models("custom")
    assert calls == [(srv["id"], {"last_models": ["m1", "m2", "m3"]})]


@pytest.mark.parametrize("payload,expected_ids", [
    ("not-a-dict", []),                                     # payload not a dict
    ({"object": "list"}, []),                               # data missing
    ({"data": "nope"}, []),                                 # data not a list
    ({"data": ["str", 42, {"id": "ok"}]}, ["shape::ok"]),   # non-dict items
    ({"data": [{"id": 123}, {"id": ""}, {"id": "ok"}]},     # non-string/empty ids
     ["shape::ok"]),
])
def test_models_custom_payload_shapes_degrade_gracefully(
        tmp_registry, monkeypatch, payload, expected_ids):
    """Odd payload shapes must skip/None gracefully — the endpoint never 500s."""
    cs.add_server(alias="shape", base_url="http://10.0.3.1:8080/v1")
    _route_by_url(monkeypatch, {"http://10.0.3.1:8080/v1": payload})

    out = admin_routes.get_available_models("custom")     # must not raise

    assert [m["id"] for m in out["models"]] == expected_ids
    assert all(m["status"] in ("loaded", "unloaded", None) for m in out["models"])


def test_models_custom_server_vanished_mid_probe_does_not_crash(tmp_registry, monkeypatch):
    """update_server raises KeyError if the server was deleted between
    list_servers and the last_models persist — the catalog must still return."""
    srv = cs.add_server(alias="gone", base_url="http://10.0.0.9:8080/v1")

    def fake_get(url, headers=None, timeout=None, **kw):
        cs.delete_server(srv["id"])       # vanishes while the probe is in flight
        return _FakeResponse(_openai_payload(["m"]))

    monkeypatch.setattr("httpx.get", fake_get)

    out = admin_routes.get_available_models("custom")

    assert [m["id"] for m in out["models"]] == ["gone::m"]
    assert out["source"] == "live"
    assert cs.list_servers() == []        # nothing resurrected/persisted
