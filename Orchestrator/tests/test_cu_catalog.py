"""CU production pass — model catalog config + filter rules.

Per docs/plans/2026-06-10-cu-production-pass-design.md §1.
"""
import re

import pytest

from Orchestrator.config import (
    CU_MODEL_DEFAULT,
    CU_GEMINI_MODEL_DEFAULT,
    CU_MODEL_FILTERS,
    CU_NATIVE_MODE,
    CU_CHROME_PATH,
    CU_MAX_ITERATIONS,
    CU_SESSION_TIMEOUT,
)


def test_cu_config_values_exist_and_typed():
    assert isinstance(CU_MODEL_DEFAULT, str) and CU_MODEL_DEFAULT.startswith("claude-")
    assert "computer-use" in CU_GEMINI_MODEL_DEFAULT
    assert isinstance(CU_NATIVE_MODE, bool)
    assert isinstance(CU_CHROME_PATH, str)
    assert CU_MAX_ITERATIONS > 0
    assert CU_SESSION_TIMEOUT > 0


@pytest.mark.parametrize("backend,model_id,expected", [
    # Anthropic: 4+-series opus/sonnet pass, haiku and 3.x fail
    ("anthropic", "claude-opus-4-6", True),
    ("anthropic", "claude-opus-4-8", True),
    ("anthropic", "claude-sonnet-4-6", True),
    ("anthropic", "claude-opus-5", True),            # future-shaped
    ("anthropic", "claude-sonnet-5-2", True),        # future-shaped
    ("anthropic", "claude-haiku-4-5-20251001", False),
    ("anthropic", "claude-3-5-sonnet-20241022", False),
    # Google: id must contain computer-use
    ("google", "gemini-2.5-computer-use-preview-10-2025", True),
    ("google", "gemini-3-computer-use-preview", True),  # future-shaped
    ("google", "gemini-2.5-flash", False),
    ("google", "gemini-3.1-pro-preview", False),
    # OpenAI: computer-use-preview family only
    ("openai", "computer-use-preview", True),
    ("openai", "computer-use-preview-2025-03-11", True),
    ("openai", "gpt-5.1", False),
])
def test_cu_filter_rules(backend, model_id, expected):
    pattern = CU_MODEL_FILTERS[backend]
    assert bool(re.match(pattern, model_id)) is expected, (
        f"{backend} filter {pattern!r} on {model_id!r}: expected {expected}"
    )


# ---------------------------------------------------------------------------
# GET /models/computer-use — merged live catalog (plan task 2)
# ---------------------------------------------------------------------------

from Orchestrator.utils import models_cache


@pytest.fixture(autouse=True)
def _clear_models_cache():
    models_cache.invalidate()
    yield
    models_cache.invalidate()


def _mk(provider, ids):
    """Vendor-fetcher stub result in the _wrap envelope."""
    from Orchestrator.routes.admin_routes import _wrap
    return _wrap(provider, [{"id": i, "name": i} for i in ids], "live")


def test_cu_catalog_merges_and_filters(monkeypatch):
    from Orchestrator.routes import admin_routes
    monkeypatch.setitem(admin_routes._FETCHERS, "anthropic",
        lambda: _mk("anthropic", ["claude-opus-4-8", "claude-haiku-4-5-20251001"]))
    monkeypatch.setitem(admin_routes._FETCHERS, "google",
        lambda: _mk("google", ["gemini-2.5-computer-use-preview-10-2025", "gemini-2.5-flash"]))
    monkeypatch.setitem(admin_routes._FETCHERS, "openai",
        lambda: _mk("openai", ["computer-use-preview", "gpt-5.1"]))

    out = admin_routes.get_available_models("computer-use")
    ids = {m["id"] for m in out["models"]}
    assert ids == {"claude-opus-4-8",
                   "gemini-2.5-computer-use-preview-10-2025",
                   "computer-use-preview"}
    # Locked contract + new backend field
    assert out["provider"] == "computer-use"
    assert out["source"] == "live"
    assert out["default_id"]
    for m in out["models"]:
        assert m["backend"] in ("anthropic", "google", "openai")


def test_cu_catalog_partial_vendor_failure(monkeypatch):
    """One vendor down -> still live, with the healthy vendors' models."""
    from Orchestrator.routes import admin_routes
    monkeypatch.setitem(admin_routes._FETCHERS, "anthropic",
        lambda: _mk("anthropic", ["claude-sonnet-4-6"]))
    monkeypatch.setitem(admin_routes._FETCHERS, "google",
        lambda: (_ for _ in ()).throw(RuntimeError("google down")))
    monkeypatch.setitem(admin_routes._FETCHERS, "openai", lambda: None)

    out = admin_routes.get_available_models("computer-use")
    assert out["source"] == "live"
    assert [m["id"] for m in out["models"]] == ["claude-sonnet-4-6"]


def test_cu_catalog_all_down_falls_back(monkeypatch):
    from Orchestrator.routes import admin_routes
    for p in ("anthropic", "google", "openai"):
        monkeypatch.setitem(admin_routes._FETCHERS, p, lambda: None)
    out = admin_routes.get_available_models("computer-use")
    assert out["source"] == "fallback"
    assert out["models"], "static fallback must not be empty"
    assert all(m.get("backend") for m in out["models"])
