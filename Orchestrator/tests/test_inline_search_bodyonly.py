"""M15.4: the LIVE inline cloud+voice search_snapshots handlers deliver
body-only results.

These are the AI-facing SEARCH surfaces that build results inline (not via the
ToolVault executor / context_builder): the 7 chat-provider tool loops (anthropic,
gemini, openai, xai, custom + the two non-stream call_* paths), the Anthropic CU
driver, and the 3 voice-agent routes. Each appends
`--- Result {i} ---\\n{snap_text}` — M15.4 wraps every such site in
`format_snapshot_for_delivery(...)` so the model sees content, not the
~1,000-char bookkeeping envelope.

The structural test is an AST gate (mirrors M1's operator-scoping gate): it
walks each source file, finds every `--- Result {i} ---` f-string, asserts the
formatted value is `format_snapshot_for_delivery(snap_text)` (never a bare
`snap_text`), and PINS the site count at 11 so a new unformatted site — or a
removed one — fails loudly.

Deliberately NOT matched (different surfaces, correctly full-envelope):
  * `--- {snap_id} (operator: …) ---` browse handlers (grok/gemini/realtime) —
    get-by-id / list-recent, not semantic search (like get_snapshot).
  * `=== CHECKPOINT #{i} ===` context block (chat_routes) — already body-only
    via M15.2.
"""
import ast
import asyncio
import types
from pathlib import Path

from Orchestrator.tests.test_fossils_body import FULL_SNAPSHOT

ROOT = Path(__file__).resolve().parents[2]

INLINE_SEARCH_FILES = [
    "Orchestrator/routes/chat_routes.py",
    "Orchestrator/browser/driver_anthropic.py",
    "Orchestrator/routes/grok_live_routes.py",
    "Orchestrator/routes/gemini_live_routes.py",
    "Orchestrator/routes/realtime_routes.py",
]
EXPECTED_RESULT_SITES = 11  # 7 chat loops + 1 CU driver + 3 voice routes


def _result_fstrings(tree):
    """Every f-string whose literal parts contain the '--- Result ' marker."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            literal = "".join(
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
            if "--- Result " in literal:
                out.append(node)
    return out


def _wraps_snaptext(js):
    """True iff a formatted value is format_snapshot_for_delivery(snap_text)
    AND no formatted value is a bare `snap_text` Name (unformatted delivery)."""
    has_wrap = False
    for v in js.values:
        if not isinstance(v, ast.FormattedValue):
            continue
        val = v.value
        if isinstance(val, ast.Name) and val.id == "snap_text":
            return False  # bare {snap_text} -> raw envelope leaks to the model
        if (isinstance(val, ast.Call)
                and isinstance(val.func, ast.Name)
                and val.func.id == "format_snapshot_for_delivery"
                and any(isinstance(a, ast.Name) and a.id == "snap_text"
                        for a in val.args)):
            has_wrap = True
    return has_wrap


def test_every_inline_result_site_is_body_only_and_count_pinned():
    total = 0
    for rel in INLINE_SEARCH_FILES:
        tree = ast.parse((ROOT / rel).read_text())
        for js in _result_fstrings(tree):
            assert _wraps_snaptext(js), (
                f"UNFORMATTED inline search site in {rel} line {js.lineno}: "
                f"'--- Result {{i}} ---' must wrap snap_text in "
                f"format_snapshot_for_delivery(...) (M15.4)"
            )
            total += 1
    assert total == EXPECTED_RESULT_SITES, (
        f"expected {EXPECTED_RESULT_SITES} inline '--- Result' search sites, "
        f"found {total} — a new site must be body-only (or a removed one "
        f"re-pinned)."
    )


def test_browse_and_checkpoint_sites_are_left_full_envelope():
    """Guard the intentional NON-targets: the get-by-id browse handlers and the
    checkpoint context block must NOT be wrapped by this change."""
    for rel in ("Orchestrator/routes/grok_live_routes.py",
                "Orchestrator/routes/gemini_live_routes.py",
                "Orchestrator/routes/realtime_routes.py"):
        src = (ROOT / rel).read_text()
        # browse handler keeps the raw snap_text (full envelope, like get_snapshot)
        assert "(operator: {operator_name}) ---\\n{snap_text}\")" in src


# ── behavior test: the grok voice search handler returns body-only ───────────

def test_grok_search_handler_returns_body_only(monkeypatch):
    import Orchestrator.routes.grok_live_routes as glr

    monkeypatch.setattr(glr, "read_text_safe", lambda p: "")
    monkeypatch.setattr(glr, "hybrid_retrieve",
                        lambda vol, q, k=3, operator="": [FULL_SNAPSHOT])

    session = types.SimpleNamespace(operator="Anna")
    out = asyncio.run(glr.execute_grok_search_snapshots(session, {"query": "rerank"}))

    # attribution + body delivered
    assert "[SNAP-20260704-7980" in out
    assert "how do I add a reranker seam?" in out
    # envelope stripped
    assert "CROSS-FILE BEACON" not in out
    assert "VOLUME TRACKER" not in out
    assert "GAUGES" not in out
