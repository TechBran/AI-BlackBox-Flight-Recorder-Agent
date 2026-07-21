"""Tests for the on-box local model stack resolver (M1).

Config is re-read FRESH from a tmp config.ini via the local_stack.CONFIG_PATH
seam (custom_servers.py pattern); all llama-swap HTTP is mocked via the
local_stack._transport seam (httpx.MockTransport, exactly like
embeddings/ollama_io.py). No real network, no real config.ini touched.
"""
import json

import httpx
import pytest

from Orchestrator import config  # local_stack imported from Task 1.3 (module created there)


# ── Task 1.1: config.py [local_models] declaration ────────────────────────────

def test_config_local_models_defaults():
    assert isinstance(config.LOCAL_MODELS_ENABLED, bool)
    assert config.LOCAL_MODELS_BASE_URL.endswith("/v1")
    # On a box with no [local_models] section (the dev box), the fallbacks apply.
    if not config.CFG.has_section("local_models"):
        assert config.LOCAL_MODELS_ENABLED is False
        assert config.LOCAL_MODELS_BASE_URL == "http://127.0.0.1:9098/v1"
