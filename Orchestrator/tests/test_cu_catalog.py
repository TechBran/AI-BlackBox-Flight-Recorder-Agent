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
    # Anthropic: 4+-series opus/sonnet/fable/mythos pass, haiku and 3.x fail
    ("anthropic", "claude-opus-4-6", True),
    ("anthropic", "claude-opus-4-8", True),
    ("anthropic", "claude-sonnet-4-6", True),
    ("anthropic", "claude-opus-5", True),            # future-shaped
    ("anthropic", "claude-sonnet-5-2", True),        # future-shaped
    ("anthropic", "claude-fable-5", True),           # Mythos-class, live catalog
    ("anthropic", "claude-mythos-5", True),
    ("anthropic", "claude-haiku-4-5-20251001", False),
    ("anthropic", "claude-3-5-sonnet-20241022", False),
    # Google: id must contain computer-use
    ("google", "gemini-2.5-computer-use-preview-10-2025", True),
    ("google", "gemini-3-computer-use-preview", True),  # future-shaped
    ("google", "gemini-2.5-flash", False),
    ("google", "gemini-3.1-pro-preview", False),
    # OpenAI: gpt-5.5 carries the built-in computer tool (2026-06 contract);
    # the deprecated, access-gated computer-use-preview stays selectable.
    ("openai", "gpt-5.5", True),
    ("openai", "gpt-5.5-2026-04-23", True),       # dated snapshot
    ("openai", "gpt-5.5-pro", False),             # undocumented for computer use
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
    assert out["backends"] == {"anthropic": "live", "google": "live", "openai": "live"}


def test_cu_catalog_partial_vendor_failure(monkeypatch):
    """Vendors down -> still live; failed vendors contribute static backfill."""
    from Orchestrator.routes import admin_routes
    monkeypatch.setitem(admin_routes._FETCHERS, "anthropic",
        lambda: _mk("anthropic", ["claude-sonnet-4-6"]))
    monkeypatch.setitem(admin_routes._FETCHERS, "google",
        lambda: (_ for _ in ()).throw(RuntimeError("google down")))
    monkeypatch.setitem(admin_routes._FETCHERS, "openai", lambda: None)

    out = admin_routes.get_available_models("computer-use")
    assert out["source"] == "live"
    ids = {m["id"] for m in out["models"]}
    assert "claude-sonnet-4-6" in ids                            # anthropic live
    assert "gemini-2.5-computer-use-preview-10-2025" in ids      # google backfill
    assert "gpt-5.5" in ids                                      # openai backfill
    assert out["backends"] == {
        "anthropic": "live", "google": "error", "openai": "error"}


def test_cu_catalog_openai_backfill_when_filtered_empty(monkeypatch):
    """OpenAI live but its catalog has no CU model -> backfill from static list."""
    from Orchestrator.routes import admin_routes
    monkeypatch.setitem(admin_routes._FETCHERS, "anthropic",
        lambda: _mk("anthropic", ["claude-opus-4-8"]))
    monkeypatch.setitem(admin_routes._FETCHERS, "google",
        lambda: _mk("google", ["gemini-2.5-computer-use-preview-10-2025"]))
    monkeypatch.setitem(admin_routes._FETCHERS, "openai",
        lambda: _mk("openai", ["gpt-5.1"]))  # chat-only catalog, no CUA model

    out = admin_routes.get_available_models("computer-use")
    assert out["source"] == "live"
    openai_models = [m for m in out["models"] if m["backend"] == "openai"]
    assert [m["id"] for m in openai_models] == ["gpt-5.5"]
    assert out["backends"]["openai"] == "fallback"
    assert out["backends"]["anthropic"] == "live"
    assert out["backends"]["google"] == "live"


def test_cu_catalog_second_call_cached(monkeypatch):
    """Second request within the TTL is served from cache (no vendor refetch)."""
    from Orchestrator.routes import admin_routes
    calls = {"anthropic": 0}

    def _anthropic_counted():
        calls["anthropic"] += 1
        return _mk("anthropic", ["claude-opus-4-8"])

    monkeypatch.setitem(admin_routes._FETCHERS, "anthropic", _anthropic_counted)
    monkeypatch.setitem(admin_routes._FETCHERS, "google",
        lambda: _mk("google", ["gemini-2.5-computer-use-preview-10-2025"]))
    monkeypatch.setitem(admin_routes._FETCHERS, "openai",
        lambda: _mk("openai", ["computer-use-preview"]))

    first = admin_routes.get_available_models("computer-use")
    assert first["cached"] is False
    assert calls["anthropic"] == 1

    second = admin_routes.get_available_models("computer-use")
    assert second["cached"] is True
    assert calls["anthropic"] == 1, "cached call must not refetch vendors"


def test_cu_catalog_default_id_is_config_default(monkeypatch):
    from Orchestrator.routes import admin_routes
    monkeypatch.setitem(admin_routes._FETCHERS, "anthropic",
        lambda: _mk("anthropic", ["claude-opus-4-8"]))
    monkeypatch.setitem(admin_routes._FETCHERS, "google",
        lambda: _mk("google", ["gemini-2.5-computer-use-preview-10-2025"]))
    monkeypatch.setitem(admin_routes._FETCHERS, "openai",
        lambda: _mk("openai", ["computer-use-preview"]))

    out = admin_routes.get_available_models("computer-use")
    assert out["default_id"] == CU_MODEL_DEFAULT


def test_cu_catalog_all_down_falls_back(monkeypatch):
    from Orchestrator.routes import admin_routes
    for p in ("anthropic", "google", "openai"):
        monkeypatch.setitem(admin_routes._FETCHERS, p, lambda: None)
    out = admin_routes.get_available_models("computer-use")
    assert out["source"] == "fallback"
    assert out["models"], "static fallback must not be empty"
    assert all(m.get("backend") for m in out["models"])


# ---------------------------------------------------------------------------
# Single-source model defaults (plan task 3)
# ---------------------------------------------------------------------------

def test_no_scattered_cu_model_literals():
    """Defaults come from config; retired Gemini ids are gone."""
    from Orchestrator.browser import config as bconfig
    from Orchestrator.gemini_cu import config as gconfig

    # Value-equality (documents intent; cannot catch a re-hardcoded literal
    # while the config fallback happens to be the same string).
    assert bconfig.CU_MODEL == CU_MODEL_DEFAULT
    assert gconfig.DEFAULT_CU_MODEL == CU_GEMINI_MODEL_DEFAULT
    assert not hasattr(gconfig, "GEMINI_CU_MODEL_PRO"), "retired gemini-3-pro-preview ref must be deleted"
    assert not hasattr(gconfig, "GEMINI_CU_MODEL_FLASH")

    import inspect
    from Orchestrator.routes import chat_routes
    from Orchestrator.scheduler import executor

    # Source scans — these DO catch re-hardcoded literals.
    # Pattern covers assignments (= "..."), dict values (: "..."), and bare
    # returns (return "...") — the executor's two historical CU hardcodes were
    # a dict value and a return, which a plain `=`-only scan would miss.
    literal_rx = re.compile(r'(?:[=:]|return)\s*["\'](claude-|gemini-\d)')
    for mod in (bconfig, gconfig, executor):
        assert not literal_rx.search(inspect.getsource(mod)), (
            f"{mod.__name__} must not embed model-id literals; "
            "use Orchestrator.config *_MODEL_DEFAULT")

    src = inspect.getsource(chat_routes)
    assert 'model = "claude-opus-4-6"' not in src, "chat_routes must use CU_MODEL_DEFAULT"
    # Formatting-robust scan scoped to the CU (opus) family: the two
    # `model = "claude-sonnet-4-5"` literals remaining in chat_routes are the
    # anthropic *chat* provider defaults, out of CU-production-pass scope.
    assert not re.search(r'model\s*=\s*["\']claude-opus', src), (
        "chat_routes must not hardcode a CU model id; use CU_MODEL_DEFAULT")


def test_cu_streams_require_operator():
    """Operator must be caller-supplied — never a hard-coded seed default."""
    import inspect
    from Orchestrator.routes import chat_routes
    for fn in (chat_routes.stream_computer_use, chat_routes.stream_gemini_computer_use):
        p = inspect.signature(fn).parameters["operator"]
        assert p.default is inspect.Parameter.empty, (
            f"{fn.__name__} must not default operator")


def test_browser_prompt_no_unsatisfiable_tool():
    """Task 12: the legacy computer-tool-only loop is deleted; the headless
    runner's default prompt is the chat path's COMPUTER_USE_SYSTEM_PROMPT.
    Guard the same invariant in the new shape: if the prompt instructs a
    get_current_time 'first action', the (shared) Anthropic driver must be
    able to execute that tool — otherwise the instruction is unsatisfiable."""
    import inspect
    from Orchestrator.browser import driver_anthropic
    from Orchestrator.routes.chat_routes import COMPUTER_USE_SYSTEM_PROMPT
    if "get_current_time" in COMPUTER_USE_SYSTEM_PROMPT:
        src = inspect.getsource(driver_anthropic.run_anthropic_cu_loop)
        assert "get_current_time" in src, (
            "CU prompt instructs get_current_time but the driver cannot execute it")


# ---------------------------------------------------------------------------
# Backend dispatcher — catalog rules drive routing, not string sniffing
# (plan task 11)
# ---------------------------------------------------------------------------

def test_resolve_backend():
    from Orchestrator.browser.dispatch import resolve_backend
    assert resolve_backend("gemini-2.5-computer-use-preview-10-2025") == "google"
    assert resolve_backend("computer-use-preview") == "openai"
    assert resolve_backend("claude-opus-4-6") == "anthropic"
    assert resolve_backend("") == "anthropic"          # empty -> CU_MODEL_DEFAULT
    assert resolve_backend(None) == "anthropic"
    assert resolve_backend("totally-unknown-model") == "anthropic"  # fallthrough


def test_chat_routes_no_gemini_string_sniffing():
    """Dispatch AND session lookup (cu-status/cu-stop) must go through
    resolve_backend — no `\"gemini\" in <anything>` sniffing anywhere."""
    import inspect
    from Orchestrator.routes import chat_routes
    src = inspect.getsource(chat_routes)
    assert not re.search(r'["\']gemini["\'] in', src), (
        "chat_routes must route CU via Orchestrator.browser.dispatch.resolve_backend, "
        "not string-sniff model ids (includes /chat/cu-status and /chat/cu-stop)")


def test_cu_filter_patterns_disjoint():
    """Every catalog id must match EXACTLY ONE CU_MODEL_FILTERS pattern —
    pattern-disjointness insurance so resolve_backend is never ambiguous."""
    from Orchestrator.routes.admin_routes import _FALLBACK_MODELS
    for m in _FALLBACK_MODELS["computer-use"]:
        matches = [b for b, pat in CU_MODEL_FILTERS.items()
                   if re.match(pat, m["id"])]
        assert matches == [m["backend"]], (
            f"{m['id']!r} matched {matches}, expected exactly [{m['backend']!r}]")
