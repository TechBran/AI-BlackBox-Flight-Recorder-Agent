"""M15.3: the AI-invoked SEARCH surfaces return reranked, BODY-ONLY results.

Both search surfaces already rerank (hybrid_retrieve / retrieve() run the
cross-encoder seam); M15.3 fixes the RESULT FORMATTING so the model/MCP client
sees content, not the ~1,000-char bookkeeping envelope:

  * ToolVault search_snapshots executor — each result is
    format_snapshot_for_delivery'd (attribution + provenance + session log);
    the on-device caller additionally bounds each BODY to its char budget
    (body FIRST, then cap).
  * /fossil/hybrid (the MCP search_snapshots / get_context path) — the 500-char
    `snippet` is the BODY head (extract_snapshot_content), not the START/BEACON
    envelope head.

The raw single-snapshot fetch (`/fossil/snapshot/{id}` -> get_snapshot_by_id)
is deliberately NOT touched — it legitimately returns the full envelope.
"""
import asyncio
import importlib.util
import types
from pathlib import Path

from Orchestrator.tests.test_fossils_body import FULL_SNAPSHOT


def _load_executor():
    mod_path = (
        Path(__file__).resolve().parents[2]
        / "ToolVault" / "tools" / "search_snapshots" / "executor.py"
    )
    spec = importlib.util.spec_from_file_location("m15_search_snapshots", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── executor: cloud/MCP caller -> whole body-only ────────────────────────────

def test_executor_result_is_body_only_with_attribution(monkeypatch):
    mod = _load_executor()
    import Orchestrator.fossils as fossils
    monkeypatch.setattr(fossils, "hybrid_retrieve",
                        lambda vol, q, k=5, operator="",
                        window_budget_chars=None: [FULL_SNAPSHOT])
    monkeypatch.setattr("Orchestrator.volume.read_text_safe", lambda p: "")

    ctx = types.SimpleNamespace(operator="Anna")  # caller-less -> cloud/box
    res = asyncio.run(mod.execute({"query": "reranker seam"}, ctx))
    assert res.success
    # attribution + session log delivered
    assert "[SNAP-20260704-7980" in res.result
    assert "operator: Anna" in res.result
    assert "how do I add a reranker seam?" in res.result
    assert "Context Provenance" in res.result
    # envelope stripped
    assert "CROSS-FILE BEACON" not in res.result
    assert "VOLUME TRACKER" not in res.result
    assert "GAUGES" not in res.result
    assert "Kernel Index" not in res.result
    # preamble preserved
    assert "relevant memory(ies) for: reranker seam" in res.result


def test_executor_on_device_is_body_only_and_within_budget(monkeypatch):
    from Orchestrator.toolvault.context import ON_DEVICE_CALLER

    mod = _load_executor()
    import Orchestrator.fossils as fossils
    # a big full-envelope snapshot: session log padded well past the budget
    big = FULL_SNAPSHOT.replace(
        "add a provider abstraction in rerank.py.",
        "add a provider abstraction in rerank.py. " + "detail " * 2000,
    )
    monkeypatch.setattr(fossils, "hybrid_retrieve",
                        lambda vol, q, k=5, operator="",
                        window_budget_chars=None: [big])
    monkeypatch.setattr("Orchestrator.volume.read_text_safe", lambda p: "")

    ctx = types.SimpleNamespace(operator="Anna", caller=ON_DEVICE_CALLER)
    res = asyncio.run(mod.execute({"query": "q", "limit": 5}, ctx))
    assert res.success
    assert "CROSS-FILE BEACON" not in res.result           # body-only
    budget = mod.LOCAL_RESULT_BUDGET_CHARS // 5
    # bounded to ~budget (framing + attribution slack), NOT the full padded body
    assert len(res.result) < budget + 400
    assert "detail detail detail" not in res.result or len(res.result) < budget + 400


# ── /fossil/hybrid: snippet is BODY head, not START/BEACON envelope ──────────

def test_fossil_hybrid_snippet_is_body_not_envelope(monkeypatch):
    import Orchestrator.routes.task_routes as tr

    sid = "SNAP-20260704-7980"
    monkeypatch.setattr(tr, "retrieve", lambda q, operator="", k=10: [(sid, 0.91)])
    monkeypatch.setattr(tr, "load_snapshot_index",
                        lambda: {sid: {"byte_start": 0,
                                       "byte_end": len(FULL_SNAPSHOT.encode()),
                                       "operator": "Anna",
                                       "timestamp": "2026-07-04T22:37:21Z",
                                       "type": "normal"}})
    monkeypatch.setattr(tr, "read_volume_bytes", lambda p: FULL_SNAPSHOT.encode())

    resp = tr.fossil_hybrid_search(q="reranker", operator="Anna", limit=10)
    assert resp["count"] == 1
    result = resp["results"][0]
    # ranked fields unchanged
    assert result["snap_id"] == sid
    assert result["similarity"] == 0.91
    snippet = result["snippet"]
    # body content, NOT the START/BEACON bookkeeping head
    assert "Raw Session Log" in snippet
    assert "how do I add a reranker seam?" in snippet
    assert "CROSS-FILE BEACON" not in snippet
    assert not snippet.startswith("=== START SNAPSHOT")
    assert "BYTES_AFTER_END" not in snippet
