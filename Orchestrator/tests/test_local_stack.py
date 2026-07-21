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
