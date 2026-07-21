"""M3: keep-warm maps to a llama-swap member ttl (0 = warm/immune to idle
unload; >0 = cold, idle-unloads after ttl s). §6: --watch-config restarts the
whole proxy on any config edit — these are surgical, atomic single writes."""
import yaml
import pytest

from Orchestrator import local_stack

CONFIG = {
    "healthCheckTimeout": 120,
    "models": {
        "embed-qwen3-8b": {"proxy": "http://127.0.0.1:${PORT}", "ttl": 600},
        "rerank-qwen3-8b": {"proxy": "http://127.0.0.1:${PORT}", "ttl": 600},
    },
    "groups": {"retrieval": {"members": ["embed-qwen3-8b", "rerank-qwen3-8b"]}},
}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "llama-swap-config.yaml"
    path.write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    monkeypatch.setattr(local_stack, "llama_swap_config_path", lambda: path)
    return path


def test_ttl_constants_warm_is_zero_cold_is_600():
    assert local_stack.TTL_WARM == 0
    assert local_stack.TTL_COLD == 600


def test_get_member_ttl_reads_the_live_config(cfg):
    assert local_stack.get_member_ttl("embed-qwen3-8b") == 600


def test_get_member_ttl_none_when_no_config(monkeypatch):
    monkeypatch.setattr(local_stack, "llama_swap_config_path", lambda: None)
    assert local_stack.get_member_ttl("embed-qwen3-8b") is None


def test_get_member_ttl_none_for_absent_member(cfg):
    assert local_stack.get_member_ttl("not-a-member") is None


def test_set_member_ttl_warm_then_cold_roundtrips(cfg):
    local_stack.set_member_ttl("embed-qwen3-8b", local_stack.TTL_WARM)
    assert local_stack.get_member_ttl("embed-qwen3-8b") == 0
    # sibling member untouched — surgical single-key edit
    on_disk = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert on_disk["models"]["rerank-qwen3-8b"]["ttl"] == 600
    # ${PORT} literal preserved for llama-swap to fill
    assert on_disk["models"]["embed-qwen3-8b"]["proxy"] == "http://127.0.0.1:${PORT}"
    local_stack.set_member_ttl("embed-qwen3-8b", local_stack.TTL_COLD)
    assert local_stack.get_member_ttl("embed-qwen3-8b") == 600


def test_set_member_ttl_absent_member_raises(cfg):
    with pytest.raises(ValueError):
        local_stack.set_member_ttl("not-a-member", 0)


def test_set_member_ttl_no_config_raises(monkeypatch):
    monkeypatch.setattr(local_stack, "llama_swap_config_path", lambda: None)
    with pytest.raises(RuntimeError):
        local_stack.set_member_ttl("embed-qwen3-8b", 0)


# ── store keep_alive / placement localstack path ─────────────────────────────
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator.embeddings import store as emb_store
from Orchestrator.embeddings.store import (
    KEEP_ALIVE_COLD, KEEP_ALIVE_WARM, get_keep_alive, set_keep_alive, set_placement,
)
from Orchestrator.routes.embeddings_routes import router

LOCALSTACK_SLUG = "qwen3-embedding-8b-local"
LOCALSTACK_MEMBER = "embed-qwen3-8b"


def test_set_keep_alive_warm_sets_member_ttl_zero(cfg):
    value = set_keep_alive(LOCALSTACK_SLUG, warm=True)
    assert value == KEEP_ALIVE_WARM
    assert local_stack.get_member_ttl(LOCALSTACK_MEMBER) == 0


def test_set_keep_alive_cold_sets_member_ttl_600(cfg):
    assert set_keep_alive(LOCALSTACK_SLUG, warm=False) == KEEP_ALIVE_COLD
    assert local_stack.get_member_ttl(LOCALSTACK_MEMBER) == 600


def test_get_keep_alive_reflects_member_ttl(cfg):
    set_keep_alive(LOCALSTACK_SLUG, warm=True)
    assert emb_store.is_warm(get_keep_alive(LOCALSTACK_SLUG)) is True
    set_keep_alive(LOCALSTACK_SLUG, warm=False)
    assert emb_store.is_warm(get_keep_alive(LOCALSTACK_SLUG)) is False


def test_get_keep_alive_falls_back_to_registry_when_no_config(monkeypatch):
    monkeypatch.setattr(local_stack, "llama_swap_config_path", lambda: None)
    assert get_keep_alive(LOCALSTACK_SLUG) is None  # registry keep_alive default


def test_set_placement_localstack_raises_install_fixed(cfg):
    with pytest.raises(ValueError, match="install"):
        set_placement(LOCALSTACK_SLUG, "cpu")


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_keep_alive_route_accepts_localstack(cfg, client):
    r = client.post("/embeddings/keep_alive", json={"slug": LOCALSTACK_SLUG, "warm": True})
    assert r.status_code == 200
    assert local_stack.get_member_ttl(LOCALSTACK_MEMBER) == 0


def test_placement_route_rejects_localstack_with_install_fixed_message(client):
    r = client.post("/embeddings/placement", json={"slug": LOCALSTACK_SLUG, "placement": "cpu"})
    assert r.status_code == 400
    assert "install" in r.json()["detail"]
