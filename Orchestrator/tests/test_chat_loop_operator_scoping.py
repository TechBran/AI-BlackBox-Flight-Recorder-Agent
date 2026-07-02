"""Operator scoping for chat/CU tool-loop memory search (WI-8, Task 1.1).

Seven `hybrid_retrieve` call sites live inside provider tool-loops and must pass
the session operator (with `operator=""` the semantic channel is UNSCOPED —
every operator's snapshots are visible — and the keyword channel is silently
EMPTY because no index entry has operator ""):

  Orchestrator/routes/chat_routes.py
    1. call_anthropic                  (search_snapshots branch)
    2. call_gemini                     (search_snapshots branch)
    3. stream_openai_with_reasoning    (search_snapshots branch)
    4. stream_anthropic_with_thinking  (search_snapshots branch)
    5. stream_gemini_with_thinking     (search_snapshots branch)
    6. stream_xai_with_reasoning       (search_snapshots branch)
  Orchestrator/browser/driver_anthropic.py
    7. run_anthropic_cu_loop           (search_snapshots branch)

Test seams (each loop INLINES the dispatch — there is no shared helper):

  * The two NON-STREAM loops (call_anthropic, call_gemini) use sync
    ``requests.post``, so they are behavior-driven here: a fake provider
    response requests the search_snapshots tool, a monkeypatched
    hybrid_retrieve records its kwargs, and we assert the session operator
    arrives (not the "" default).

  * The four STREAMING loops consume provider-specific httpx SSE streams;
    faking four SSE wire formats would exercise the fakes, not the plumbing.
    Their dispatch blocks are byte-for-byte the same shape as the non-stream
    ones, so they (and the CU loop, which additionally needs a live session
    object) are covered by the AST structural test below: EVERY
    hybrid_retrieve call site in the two files must pass ``operator=operator``
    — the session operator variable each loop holds under exactly that name,
    never a literal or some other variable (and the site count is pinned so a
    new unscoped site cannot slip in unseen).

  * One live-store integration test (skip-guarded, never fake-pass — pattern
    from test_retrieval_golden.py) proves a real operator gets results, gets
    ONLY its own snapshots, and gets a populated keyword channel.
"""
import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_CHAT_ROUTES = _REPO / "Orchestrator" / "routes" / "chat_routes.py"
_CU_DRIVER = _REPO / "Orchestrator" / "browser" / "driver_anthropic.py"


# --------------------------------------------------------------------------- #
# Behavior tests: the two non-stream loops                                     #
# --------------------------------------------------------------------------- #

class _FakeResp:
    """Minimal stand-in for a requests.Response (same as test_phase2_reasoning)."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = "fake"

    def json(self):
        return self._payload


@pytest.fixture
def _cr(monkeypatch):
    """chat_routes with provider-key guards satisfied and heavy deps stubbed."""
    import Orchestrator.routes.chat_routes as cr

    monkeypatch.setattr(cr, "ANTHROPIC_API_KEY", "test-anthropic", raising=False)
    monkeypatch.setattr(cr, "GOOGLE_API_KEY", "test-google", raising=False)
    # _get_tools makes a ToolVault round-trip; read_text_safe reads the full volume.
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(cr, "read_text_safe", lambda *a, **k: "")
    return cr


@pytest.fixture
def _recorded(monkeypatch, _cr):
    """Monkeypatch chat_routes.hybrid_retrieve with a kwargs recorder."""
    calls = []

    def fake_hybrid_retrieve(vol_txt, query, k=3, operator=""):
        calls.append({"query": query, "k": k, "operator": operator})
        return ["SNAP-TEST fake snapshot text"]

    monkeypatch.setattr(_cr, "hybrid_retrieve", fake_hybrid_retrieve)
    return calls


def test_call_anthropic_scopes_retrieval_to_session_operator(monkeypatch, _cr, _recorded):
    """call_anthropic's search_snapshots dispatch passes the session operator."""
    responses = iter([
        _FakeResp({
            "content": [{
                "type": "tool_use", "id": "tu_1", "name": "search_snapshots",
                "input": {"query": "ember backdrop", "k": 3},
            }],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }),
        _FakeResp({
            "content": [{"type": "text", "text": "done"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }),
    ])
    monkeypatch.setattr(_cr.requests, "post", lambda *a, **k: next(responses))

    _cr.call_anthropic(
        [{"role": "user", "content": "find the ember work"}],
        "claude-test-model",
        operator="scoped-op",
    )

    assert _recorded, "search_snapshots dispatch never reached hybrid_retrieve"
    assert _recorded[0]["operator"] == "scoped-op", (
        f"hybrid_retrieve got operator={_recorded[0]['operator']!r} — memory "
        f"search is not scoped to the session operator"
    )


def test_call_gemini_scopes_retrieval_to_session_operator(monkeypatch, _cr, _recorded):
    """call_gemini's search_snapshots dispatch passes the session operator."""
    responses = iter([
        _FakeResp({
            "candidates": [{"content": {"parts": [{
                "functionCall": {"name": "search_snapshots",
                                 "args": {"query": "ember backdrop", "k": 3}},
            }]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1,
                              "totalTokenCount": 2},
        }),
        _FakeResp({
            "candidates": [{"content": {"parts": [{"text": "done"}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1,
                              "totalTokenCount": 2},
        }),
    ])
    monkeypatch.setattr(_cr.requests, "post", lambda *a, **k: next(responses))

    _cr.call_gemini(
        [{"role": "user", "content": "find the ember work"}],
        "gemini-test-model",
        operator="scoped-op",
    )

    assert _recorded, "search_snapshots dispatch never reached hybrid_retrieve"
    assert _recorded[0]["operator"] == "scoped-op", (
        f"hybrid_retrieve got operator={_recorded[0]['operator']!r} — memory "
        f"search is not scoped to the session operator"
    )


# --------------------------------------------------------------------------- #
# AST structural test: ALL 7 call sites (covers the 4 SSE loops + CU driver)   #
# --------------------------------------------------------------------------- #

def _refs_hybrid_retrieve(node: ast.AST) -> bool:
    """Name or Attribute reference to hybrid_retrieve (bare or dotted)."""
    return (isinstance(node, ast.Name) and node.id == "hybrid_retrieve") or (
        isinstance(node, ast.Attribute) and node.attr == "hybrid_retrieve"
    )


def _hybrid_retrieve_call_sites(path: Path):
    """All hybrid_retrieve invocations: any ast.Call whose func OR any
    positional argument references hybrid_retrieve. This generically covers
    direct calls, dotted calls (fossils.hybrid_retrieve), and every
    function-as-argument dispatch shape (run_blocking, asyncio.to_thread,
    functools.partial, ...) — a new site cannot dodge the gate by changing
    dispatch shape."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            _refs_hybrid_retrieve(node.func)
            or any(_refs_hybrid_retrieve(arg) for arg in node.args)
        )
    ]


@pytest.mark.parametrize("path,expected_count", [
    (_CHAT_ROUTES, 6),   # the six provider tool-loops
    (_CU_DRIVER, 1),     # run_anthropic_cu_loop
])
def test_every_hybrid_retrieval_site_passes_operator(path, expected_count):
    """Every tool-loop hybrid_retrieve site passes operator=<variable>.

    Pinned counts: adding a new call site must consciously update this test
    (and pass the operator), so an unscoped site can't slip in unseen.
    """
    sites = _hybrid_retrieve_call_sites(path)
    assert len(sites) == expected_count, (
        f"{path.name}: expected {expected_count} hybrid_retrieve call site(s), "
        f"found {len(sites)} at lines {[s.lineno for s in sites]} — if a site was "
        f"added/removed on purpose, update this test AND scope it to the operator"
    )
    for site in sites:
        kw = {k.arg: k.value for k in site.keywords}
        assert "operator" in kw, (
            f"{path.name}:{site.lineno} hybrid_retrieve call missing operator= "
            f"(memory search is unscoped: all operators' snapshots visible, "
            f"keyword channel silently empty)"
        )
        assert isinstance(kw["operator"], ast.Name) and kw["operator"].id == "operator", (
            f"{path.name}:{site.lineno} operator= must be the session `operator` "
            f"variable (every loop holds it under exactly that name), got "
            f"{ast.dump(kw['operator'])}"
        )


def test_no_hardcoded_operator_defaults_in_retrieval_route_signatures():
    """No chat_routes function may default an operator param to a person-name
    string literal (portable-build rule: no hardcoded operator names).

    With scoping live, a fresh box whose caller omits operator would scope
    retrieval to a nonexistent operator -> empty allowed_ids -> ZERO semantic
    results (worse than the old unscoped fallback). Operator params must be
    required (no default), default to "" (the explicit unscoped sentinel) or
    "system", or reference a config constant — any other plain string-literal
    default fails.
    """
    tree = ast.parse(_CHAT_ROUTES.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = node.args
        positional = a.posonlyargs + a.args
        paired = list(zip(positional[len(positional) - len(a.defaults):], a.defaults))
        paired += [(arg, d) for arg, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
        for arg, default in paired:
            if "operator" not in arg.arg:
                continue
            if (
                isinstance(default, ast.Constant)
                and isinstance(default.value, str)
                and default.value not in ("", "system")
            ):
                offenders.append(
                    f"{node.name}:{node.lineno} {arg.arg}={default.value!r}"
                )
    assert not offenders, (
        "hardcoded operator signature default(s) in chat_routes.py — make the "
        "param required or use a config-driven default (config.USERS_DEFAULT), "
        f"never a literal name: {offenders}"
    )


# --------------------------------------------------------------------------- #
# Live-store integration (skip-guarded — pattern from test_retrieval_golden)   #
# --------------------------------------------------------------------------- #

def _require_live_store():
    """Skip (don't fake-pass) when the active store / embedding provider is down."""
    try:
        from Orchestrator.embeddings.search import get_active_store
        store = get_active_store()
    except Exception as e:  # noqa: BLE001 - provider/store unavailable in test env
        pytest.skip(f"active store/provider unavailable: {e}")
    if store.count == 0:
        pytest.skip("active store empty")


def test_hybrid_retrieval_live_scoped_to_operator():
    """With a real operator: results come back, ONLY that operator's snapshots
    rank, and the keyword channel is populated (it is silently empty for '').

    The operator is picked from the live index (most frequent), NOT hardcoded —
    this must pass on a fresh box with any operator set (portable-build rule).
    """
    _require_live_store()
    from collections import Counter

    from Orchestrator.fossils import (
        hybrid_retrieve,
        keyword_retrieve_ids_for_operator,
        load_snapshot_index,
    )
    from Orchestrator.retrieval import retrieve

    # Most frequent scoped operator in the live index. "system" is excluded:
    # it is the see-everything sentinel, so the per-id scoping assertion below
    # would not hold for it.
    index = load_snapshot_index()
    ops = Counter(
        m.get("operator") for m in index.values()
        if m.get("operator") and m.get("operator") != "system"
    )
    if not ops:
        pytest.skip("no operator-attributed snapshots in index")
    op = ops.most_common(1)[0][0]

    query = "BlackBox memory snapshots"

    texts = hybrid_retrieve("", query, k=5, operator=op)
    if not texts:
        pytest.skip(
            f"hybrid_retrieve returned nothing for {query!r} under operator {op!r} "
            f"(embed provider down, or no semantically/keyword-matching snapshots "
            f"for this operator)"
        )
    assert len(texts) >= 1

    # Scoping: every ranked id belongs to the operator. This embeds the query
    # a SECOND time (hybrid_retrieve above embedded it once) — a provider blip
    # between the two calls is not a scoping regression, so skip, don't fail
    # (pattern from test_retrieval_golden.py).
    ranked = retrieve(query, operator=op, k=5)
    if not ranked:
        pytest.skip(f"retrieve returned nothing for {query!r} (query embed unavailable)")
    for sid, _score in ranked:
        assert index.get(sid, {}).get("operator") == op, (
            f"{sid} leaked into {op!r}-scoped results "
            f"(operator={index.get(sid, {}).get('operator')!r})"
        )

    # Keyword channel contributes for a real operator (it matches nothing for "").
    kw_ids = keyword_retrieve_ids_for_operator("", query, 40, op)
    assert kw_ids, f"keyword channel empty for operator {op!r} with a live index"
