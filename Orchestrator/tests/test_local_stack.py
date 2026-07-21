"""Tests for the on-box local model stack resolver (M1).

Config is re-read FRESH from a tmp config.ini via the local_stack.CONFIG_PATH
seam (custom_servers.py pattern); all llama-swap HTTP is mocked via the
local_stack._transport seam (httpx.MockTransport, exactly like
embeddings/ollama_io.py). No real network, no real config.ini touched.
"""
import json

import httpx
import pytest

from Orchestrator import config, local_stack  # local_stack module created in Task 1.3


# ── Task 1.1: config.py [local_models] declaration ────────────────────────────

def test_config_local_models_defaults():
    assert isinstance(config.LOCAL_MODELS_ENABLED, bool)
    assert config.LOCAL_MODELS_BASE_URL.endswith("/v1")
    # On a box with no [local_models] section (the dev box), the fallbacks apply.
    if not config.CFG.has_section("local_models"):
        assert config.LOCAL_MODELS_ENABLED is False
        assert config.LOCAL_MODELS_BASE_URL == "http://127.0.0.1:9098/v1"


# ── Task 1.3: config fresh-read + capability resolvers ────────────────────────

@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Point local_stack at a tmp config.ini the test writes; returns a writer."""
    path = tmp_path / "config.ini"
    monkeypatch.setattr(local_stack, "CONFIG_PATH", path)

    def write(body: str):
        path.write_text(body, encoding="utf-8")
    return write


def test_master_enabled_absent_file_is_false(cfg):
    # No file written at all — fail-soft to the fallback.
    assert local_stack.master_enabled() is False
    assert local_stack.is_installed() is False


def test_master_enabled_true(cfg):
    cfg("[local_models]\nenabled = true\n")
    assert local_stack.master_enabled() is True
    assert local_stack.is_installed() is True


def test_base_url_default_and_root(cfg):
    cfg("[local_models]\nenabled = true\n")  # no base_url -> fallback
    assert local_stack.base_url() == "http://127.0.0.1:9098/v1"
    assert local_stack.base_url_root() == "http://127.0.0.1:9098"


def test_base_url_override_and_root_strip(cfg):
    cfg("[local_models]\nenabled = true\nbase_url = http://127.0.0.1:9500/v1/\n")
    assert local_stack.base_url() == "http://127.0.0.1:9500/v1"
    assert local_stack.base_url_root() == "http://127.0.0.1:9500"


def test_base_url_root_without_v1(cfg):
    cfg("[local_models]\nenabled = true\nbase_url = http://127.0.0.1:9098\n")
    assert local_stack.base_url_root() == "http://127.0.0.1:9098"


def test_enabled_requires_master(cfg):
    # master off -> every capability off even if the per-cap flag is set.
    cfg("[local_models]\nenabled = false\nstt = true\n")
    assert local_stack.enabled("stt") is False


def test_enabled_per_capability(cfg):
    cfg("[local_models]\nenabled = true\nstt = true\ntts = false\n")
    assert local_stack.enabled("stt") is True
    assert local_stack.enabled("tts") is False
    assert local_stack.enabled("embeddings") is False  # unset -> fallback false


def test_enabled_unknown_capability_is_false(cfg):
    cfg("[local_models]\nenabled = true\nvision = true\n")
    assert local_stack.enabled("vision") is False


def test_enabled_is_fresh_across_edits(cfg):
    cfg("[local_models]\nenabled = true\nembeddings = false\n")
    assert local_stack.enabled("embeddings") is False
    cfg("[local_models]\nenabled = true\nembeddings = true\n")   # wizard flip
    assert local_stack.enabled("embeddings") is True             # no restart


# ── Task 1.4: llama-swap probes + download-state ──────────────────────────────

def _transport(routes: dict):
    """Sync httpx.MockTransport: path -> httpx.Response (or a raising callable).
    Mirrors test_embeddings_ollama.py's _get_transport."""
    def handler(request):
        target = routes.get(request.url.path)
        if callable(target):
            return target(request)
        if target is None:
            return httpx.Response(404)
        return target
    return httpx.MockTransport(handler)


@pytest.fixture
def installed_cfg(cfg):
    """A tmp config.ini with the stack installed (master enabled)."""
    cfg("[local_models]\nenabled = true\n")
    return cfg


def test_llama_swap_health_reachable(monkeypatch, installed_cfg):
    monkeypatch.setattr(local_stack, "_transport", _transport({
        "/health": httpx.Response(200, text="OK"),
    }))
    h = local_stack.llama_swap_health()
    assert h == {"reachable": True, "status_code": 200}


def test_llama_swap_health_non_200(monkeypatch, installed_cfg):
    monkeypatch.setattr(local_stack, "_transport", _transport({
        "/health": httpx.Response(503, text="loading"),
    }))
    assert local_stack.llama_swap_health() == {"reachable": False, "status_code": 503}


def test_llama_swap_health_unreachable(monkeypatch, installed_cfg):
    def refuse(request):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(local_stack, "_transport", _transport({"/health": refuse}))
    assert local_stack.llama_swap_health() == {"reachable": False, "status_code": None}


def test_is_healthy_true_when_installed_and_reachable(monkeypatch, installed_cfg):
    monkeypatch.setattr(local_stack, "_transport", _transport({
        "/health": httpx.Response(200),
    }))
    assert local_stack.is_healthy() is True


def test_is_healthy_false_when_not_installed(monkeypatch, cfg):
    cfg("[local_models]\nenabled = false\n")
    # No probe should even be attempted; a raising transport proves short-circuit.
    def boom(request):
        raise AssertionError("must not probe when not installed")
    monkeypatch.setattr(local_stack, "_transport", _transport({"/health": boom}))
    assert local_stack.is_healthy() is False


def test_is_healthy_false_when_unreachable(monkeypatch, installed_cfg):
    def refuse(request):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(local_stack, "_transport", _transport({"/health": refuse}))
    assert local_stack.is_healthy() is False


def test_should_route_onbox(monkeypatch, cfg):
    cfg("[local_models]\nenabled = true\nstt = true\ntts = false\n")
    monkeypatch.setattr(local_stack, "_transport", _transport({
        "/health": httpx.Response(200),
    }))
    assert local_stack.should_route_onbox("stt") is True    # seeded + healthy
    assert local_stack.should_route_onbox("tts") is False    # not seeded


def test_should_route_onbox_seeded_but_down(monkeypatch, cfg):
    cfg("[local_models]\nenabled = true\nstt = true\n")
    def refuse(request):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(local_stack, "_transport", _transport({"/health": refuse}))
    assert local_stack.should_route_onbox("stt") is False    # seeded but unhealthy


def test_running_members_object_shape(monkeypatch, installed_cfg):
    monkeypatch.setattr(local_stack, "_transport", _transport({
        "/running": httpx.Response(200, json={"running": [
            {"model": "embed-qwen3-8b", "state": "ready"},
            {"model": "rerank-qwen3-8b"},            # state omitted -> "ready"
            {"missing_model_key": True},              # ignored
        ]}),
    }))
    assert local_stack.running_members() == [
        {"model": "embed-qwen3-8b", "state": "ready"},
        {"model": "rerank-qwen3-8b", "state": "ready"},
    ]


def test_running_members_bare_list(monkeypatch, installed_cfg):
    monkeypatch.setattr(local_stack, "_transport", _transport({
        "/running": httpx.Response(200, json=[{"model": "speaches", "state": "loading"}]),
    }))
    assert local_stack.running_members() == [{"model": "speaches", "state": "loading"}]


def test_running_members_empty_when_idle(monkeypatch, installed_cfg):
    monkeypatch.setattr(local_stack, "_transport", _transport({
        "/running": httpx.Response(200, json={"running": []}),
    }))
    assert local_stack.running_members() == []   # up, nothing resident


def test_running_members_none_when_unreachable(monkeypatch, installed_cfg):
    def refuse(request):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(local_stack, "_transport", _transport({"/running": refuse}))
    assert local_stack.running_members() is None  # distinct from [] (idle)


def test_read_download_state_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(local_stack, "DOWNLOAD_STATE_PATH", tmp_path / "downloads.json")
    assert local_stack.read_download_state() == {}


def test_read_download_state_happy(monkeypatch, tmp_path):
    p = tmp_path / "downloads.json"
    p.write_text(json.dumps({"embed-qwen3-8b": {"state": "downloaded"}}), encoding="utf-8")
    monkeypatch.setattr(local_stack, "DOWNLOAD_STATE_PATH", p)
    assert local_stack.read_download_state() == {"embed-qwen3-8b": {"state": "downloaded"}}


def test_read_download_state_corrupt_is_empty(monkeypatch, tmp_path):
    p = tmp_path / "downloads.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(local_stack, "DOWNLOAD_STATE_PATH", p)
    assert local_stack.read_download_state() == {}


def test_members_and_gate_constants():
    ids = [m["model"] for m in local_stack.MEMBERS]
    assert ids == ["embed-qwen3-8b", "rerank-qwen3-8b", "speaches", "qwen-tts"]
    caps = {m["capability"] for m in local_stack.MEMBERS}
    assert caps == set(local_stack.CAPABILITIES)
    assert local_stack.DISK_GATE_MB == 40 * 1024
