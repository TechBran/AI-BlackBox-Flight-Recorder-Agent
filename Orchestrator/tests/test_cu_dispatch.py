"""M1-T1 — browser/dispatch.py capability gates + class-alias model resolution.

Two units under test:
  * CU_MODEL_FILTERS as CAPABILITY GATES (not version pins): the OpenAI filter
    must answer "can this gpt class drive the built-in computer tool?" — so
    gpt-5.6 (and any future 5.x >= 5.5 minor) passes, while gpt-5.1 and the
    -pro variants stay excluded. Anthropic/Google gates unchanged.
  * resolve_model_class(class_or_id) — stable CLASS names resolve to the newest
    concrete id in the live catalog; concrete gate-passing ids pass verbatim;
    unresolvable input raises a class-naming error (never a silent default).
"""
import re

import pytest

from Orchestrator.config import CU_MODEL_FILTERS


# ---------------------------------------------------------------------------
# Capability gates — strict: which backend (if any) a raw id is CU-capable for.
# resolve_backend() has an anthropic FALLTHROUGH for unknown ids, so we check
# the filters directly here to answer "CU-capable at all?" honestly.
# ---------------------------------------------------------------------------

def _gate(model_id: str):
    """Backend whose CU_MODEL_FILTERS pattern matches, else None (no fallthrough)."""
    return next((b for b, p in CU_MODEL_FILTERS.items() if re.match(p, model_id)), None)


@pytest.mark.parametrize("model_id,expected", [
    # OpenAI: capability gate, NOT a version pin.
    ("gpt-5.6", "openai"),                 # THE BUG this task fixes
    ("gpt-5.6-sol", "openai"),             # named snapshot of 5.6
    ("gpt-5.5", "openai"),                 # must not regress
    ("gpt-5.5-2026-04-23", "openai"),      # dated snapshot
    ("gpt-5.9", "openai"),                 # future 5.x minor
    ("gpt-5.12", "openai"),                # future double-digit minor
    ("computer-use-preview", "openai"),    # legacy, kept
    ("computer-use-preview-2025-03-11", "openai"),
    # Future MAJORS are assumed CU-capable (fail-loud-at-runtime beats a silent
    # gate gap that needs a regex edit months later).
    ("gpt-6", "openai"),                   # bare major, no minor
    ("gpt-6.0", "openai"),
    ("gpt-6.5", "openai"),
    ("gpt-7.1", "openai"),
    ("gpt-12.3", "openai"),                # double-digit major
    ("gpt-6-2027-01-01", "openai"),        # future-major dated snapshot
    # Known-incapable / excluded.
    ("gpt-5.5-pro", None),                 # -pro excluded (undocumented for CU)
    ("gpt-5.5-pro-2026-01-01", None),      # dated -pro still excluded
    ("gpt-6-pro", None),                   # -pro excluded on future majors too
    ("gpt-5.6-pro", None),                 # -pro excluded at any minor
    ("gpt-5.1", None),                     # minor < 5.5: no built-in computer tool
    ("gpt-5.4", None),
    ("gpt-4o", None),
    ("gpt-4", None),                       # major < 5
    # Issue 2: -pro must be BOUNDARY-anchored — only the exact -pro segment is
    # rejected; longer words that merely start with "pro" still match.
    ("gpt-5.5-professional", "openai"),
    ("gpt-5.5-prometheus", "openai"),
    ("gpt-5.5-proto", "openai"),
    # Anthropic: class + open version tail (the GOOD pattern, unchanged).
    ("claude-opus-4-8", "anthropic"),
    ("claude-sonnet-4-6", "anthropic"),
    ("claude-fable-5", "anthropic"),
    ("claude-mythos-5", "anthropic"),
    ("claude-opus-5", "anthropic"),
    ("claude-haiku-4-5-20251001", None),   # haiku has NO CU support
    ("claude-3-5-sonnet-20241022", None),  # 3.x too old
    # Google: must contain computer-use; -pro / flash excluded.
    ("gemini-2.5-computer-use-preview-10-2025", "google"),
    ("gemini-3-computer-use-preview", "google"),
    ("gemini-3.1-pro", None),
    ("gemini-3.1-pro-preview", None),
    ("gemini-flash", None),
    ("gemini-2.5-flash", None),
])
def test_capability_gate(model_id, expected):
    assert _gate(model_id) == expected


def test_gates_are_disjoint():
    """No id may match two vendor patterns (resolve_backend must be unambiguous)."""
    ids = ["gpt-5.6", "gpt-5.5", "computer-use-preview", "claude-opus-4-8",
           "claude-fable-5", "gemini-2.5-computer-use-preview-10-2025"]
    for mid in ids:
        matches = [b for b, p in CU_MODEL_FILTERS.items() if re.match(p, mid)]
        assert len(matches) == 1, f"{mid!r} matched {matches}"


# ---------------------------------------------------------------------------
# resolve_backend keeps its contract AND now routes gpt-5.6 -> openai
# ---------------------------------------------------------------------------

def test_resolve_backend_routes_new_gpt():
    from Orchestrator.browser.dispatch import resolve_backend
    assert resolve_backend("gpt-5.6") == "openai"
    assert resolve_backend("gpt-5.6-sol") == "openai"
    assert resolve_backend("gemini-2.5-computer-use-preview-10-2025") == "google"
    assert resolve_backend("claude-opus-4-8") == "anthropic"
    assert resolve_backend("") == "anthropic"
    assert resolve_backend(None) == "anthropic"


# ---------------------------------------------------------------------------
# _is_preview — the GA/preview trap. -preview- (with a trailing segment) is a
# dated preview; a bare -preview suffix (OpenAI's legacy id) is NOT.
# ---------------------------------------------------------------------------

def test_is_preview_semantics():
    from Orchestrator.browser.dispatch import _is_preview
    assert _is_preview("gemini-2.5-computer-use-preview-10-2025") is True
    assert _is_preview("gpt-5.6-preview-2027-01-01") is True
    # Trap cases — a bare -preview suffix is NOT a dated preview:
    assert _is_preview("computer-use-preview") is False
    assert _is_preview("gemini-3-computer-use-preview") is False
    assert _is_preview("gpt-5.6") is False


# ---------------------------------------------------------------------------
# resolve_model_class — catalog injected so tests are hermetic (no network).
# ---------------------------------------------------------------------------

CATALOG = [
    {"id": "claude-opus-4-8", "backend": "anthropic"},
    {"id": "claude-opus-4-7", "backend": "anthropic"},
    {"id": "claude-sonnet-4-6", "backend": "anthropic"},
    {"id": "gemini-2.5-computer-use-preview-10-2025", "backend": "google"},
    {"id": "gpt-5.6", "backend": "openai"},
    {"id": "gpt-5.5", "backend": "openai"},
    {"id": "computer-use-preview", "backend": "openai"},
]


@pytest.mark.parametrize("class_name,expected", [
    ("opus", "claude-opus-4-8"),     # newest opus
    ("sonnet", "claude-sonnet-4-6"),
    ("gpt", "gpt-5.6"),              # newest gpt (NOT legacy computer-use-preview)
    ("gemini", "gemini-2.5-computer-use-preview-10-2025"),
])
def test_resolve_class_alias_newest(class_name, expected):
    from Orchestrator.browser.dispatch import resolve_model_class
    assert resolve_model_class(class_name, catalog=CATALOG) == expected


def test_empty_and_none_default_to_opus():
    from Orchestrator.browser.dispatch import resolve_model_class
    assert resolve_model_class("", catalog=CATALOG) == "claude-opus-4-8"
    assert resolve_model_class(None, catalog=CATALOG) == "claude-opus-4-8"
    assert resolve_model_class("  ", catalog=CATALOG) == "claude-opus-4-8"


def test_concrete_gate_passing_id_verbatim():
    """Rule 1: a concrete id that passes the gate is returned as-is — it need
    not even be present in the catalog (works during a vendor outage)."""
    from Orchestrator.browser.dispatch import resolve_model_class
    assert resolve_model_class("claude-opus-4-8", catalog=CATALOG) == "claude-opus-4-8"
    assert resolve_model_class("gpt-5.6-sol", catalog=CATALOG) == "gpt-5.6-sol"
    assert resolve_model_class(
        "gemini-2.5-computer-use-preview-10-2025", catalog=CATALOG
    ) == "gemini-2.5-computer-use-preview-10-2025"


def test_preview_only_class_still_resolves():
    """GA/preview TRAP: a class whose only member is a preview must resolve to
    that preview, NOT raise/return nothing (else the whole Google backend dies)."""
    from Orchestrator.browser.dispatch import resolve_model_class
    google_only = [{"id": "gemini-2.5-computer-use-preview-10-2025", "backend": "google"}]
    assert resolve_model_class("gemini", catalog=google_only) == \
        "gemini-2.5-computer-use-preview-10-2025"


def test_ga_preferred_over_newer_preview():
    """When a class has both, GA wins over a numerically-newer dated preview."""
    from Orchestrator.browser.dispatch import resolve_model_class
    mixed = [
        {"id": "gpt-5.5", "backend": "openai"},                    # GA, older
        {"id": "gpt-5.6-preview-2027-09-09", "backend": "openai"}, # preview, newer
    ]
    assert resolve_model_class("gpt", catalog=mixed) == "gpt-5.5"


def test_future_major_class_resolves_newest():
    """A future gpt major outranks a current 5.x minor within the gpt class."""
    from Orchestrator.browser.dispatch import resolve_model_class
    cat = [
        {"id": "gpt-5.6", "backend": "openai"},
        {"id": "gpt-6.0", "backend": "openai"},
    ]
    assert resolve_model_class("gpt", catalog=cat) == "gpt-6.0"


# ---------------------------------------------------------------------------
# Issue 3 — 'newest' precedence is DETERMINISTIC (never depends on catalog
# order). Precedence: rolling alias (evergreen production pointer) outranks a
# pinned dated/named snapshot of the SAME version.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("order", [
    ["gpt-5.5", "gpt-5.5-2026-04-23"],
    ["gpt-5.5-2026-04-23", "gpt-5.5"],   # reversed: result must not change
])
def test_rolling_alias_beats_dated_snapshot_regardless_of_order(order):
    from Orchestrator.browser.dispatch import resolve_model_class
    cat = [{"id": i, "backend": "openai"} for i in order]
    assert resolve_model_class("gpt", catalog=cat) == "gpt-5.5"


@pytest.mark.parametrize("order", [
    ["gpt-5.6", "gpt-5.6-sol"],
    ["gpt-5.6-sol", "gpt-5.6"],          # reversed: result must not change
])
def test_rolling_alias_beats_named_snapshot_regardless_of_order(order):
    from Orchestrator.browser.dispatch import resolve_model_class
    cat = [{"id": i, "backend": "openai"} for i in order]
    assert resolve_model_class("gpt", catalog=cat) == "gpt-5.6"


def test_version_key_total_order_no_ties():
    """Distinct ids must never produce equal keys (else max() is order-dependent)."""
    from Orchestrator.browser.dispatch import _version_key
    ids = ["gpt-5.6", "gpt-5.6-sol", "gpt-5.6-abc", "gpt-5.5",
           "gpt-5.5-2026-04-23", "gpt-6.0"]
    keys = [_version_key(i) for i in ids]
    assert len(set(keys)) == len(ids), "version keys must be unique per id"


def test_higher_version_still_wins_over_rolling_preference():
    """The rolling-alias tie-break only applies WITHIN a version; a higher
    version always wins first (5.6 > a dated 5.5)."""
    from Orchestrator.browser.dispatch import resolve_model_class
    cat = [
        {"id": "gpt-5.5", "backend": "openai"},
        {"id": "gpt-5.6-2027-01-01", "backend": "openai"},
    ]
    assert resolve_model_class("gpt", catalog=cat) == "gpt-5.6-2027-01-01"


def test_haiku_is_not_cu_capable_raises():
    """A haiku id passes no gate and is no class alias -> hard error."""
    from Orchestrator.browser.dispatch import resolve_model_class
    with pytest.raises(ValueError):
        resolve_model_class("claude-haiku-4-5-20251001", catalog=CATALOG)


def test_pro_excluded_raises():
    from Orchestrator.browser.dispatch import resolve_model_class
    with pytest.raises(ValueError):
        resolve_model_class("gpt-5.5-pro", catalog=CATALOG)


def test_unknown_class_raises_and_names_available():
    """Rule 4: never silently default. The error must name the classes the live
    catalog can currently satisfy so an LLM caller can retry with a valid one."""
    from Orchestrator.browser.dispatch import resolve_model_class
    with pytest.raises(ValueError) as ei:
        resolve_model_class("turbo", catalog=CATALOG)
    msg = str(ei.value)
    for cls in ("opus", "sonnet", "gpt", "gemini"):
        assert cls in msg, f"error message must name available class {cls!r}: {msg!r}"


def test_known_class_with_no_catalog_candidates_raises():
    """'fable' is a valid class name but absent from this catalog -> raise
    (naming what IS available), rather than resolve to nothing."""
    from Orchestrator.browser.dispatch import resolve_model_class
    with pytest.raises(ValueError):
        resolve_model_class("fable", catalog=CATALOG)


def test_resolve_model_class_uses_live_catalog_when_not_injected(monkeypatch):
    """Seam: with no injected catalog, resolution reuses the live
    /models/computer-use catalog builder (get_available_models)."""
    from Orchestrator.routes import admin_routes
    monkeypatch.setattr(
        admin_routes, "get_available_models",
        lambda provider: {"models": [{"id": "claude-opus-4-8", "backend": "anthropic"}]},
    )
    from Orchestrator.browser.dispatch import resolve_model_class
    assert resolve_model_class("opus") == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# resolve_cu_model now lives in dispatch (hoisted from scheduler/executor,
# renamed public). Scheduler still imports it; behavior is unchanged.
# ---------------------------------------------------------------------------

def test_resolve_cu_model_hoisted_to_dispatch():
    from Orchestrator.browser.dispatch import resolve_cu_model
    from Orchestrator.config import CU_MODEL_DEFAULT, CU_GEMINI_MODEL_DEFAULT
    assert resolve_cu_model("") == CU_MODEL_DEFAULT
    assert resolve_cu_model(None) == CU_MODEL_DEFAULT
    assert resolve_cu_model("computer-use") == CU_MODEL_DEFAULT
    assert resolve_cu_model("cu") == CU_MODEL_DEFAULT
    assert resolve_cu_model("totally-not-a-cu-model") == CU_MODEL_DEFAULT
    assert resolve_cu_model(CU_GEMINI_MODEL_DEFAULT) == CU_GEMINI_MODEL_DEFAULT
    assert resolve_cu_model("gpt-5.6") == "gpt-5.6"  # now gate-passing


def test_scheduler_reexports_resolve_cu_model():
    """executor must import the hoisted helper (no divergent copy)."""
    from Orchestrator.scheduler import executor
    from Orchestrator.browser.dispatch import resolve_cu_model as canonical
    assert executor.resolve_cu_model is canonical
