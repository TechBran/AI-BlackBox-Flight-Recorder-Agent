"""Executor for search_snapshots (migrated from blackbox_tools._execute_search_memory)."""
from Orchestrator.toolvault.context import ON_DEVICE_CALLER, ToolContext, ToolResult

# Result budget for the ON-DEVICE caller only. The phone's engine window is
# 6,144 tokens (device-proven GPU ceiling) — whole snapshots (corpus p99 ≈
# 14.9k chars EACH) cannot fit its agent loop, so the local bridge gets a
# bounded result: ~8,000 chars total, split evenly across the requested result
# count, with a floor to keep each result useful when the model asks for many.
# Cloud/MCP callers (caller != "local") stay WHOLE per WI-10/M7.
LOCAL_RESULT_BUDGET_CHARS = 8000
LOCAL_MIN_WINDOW_CHARS = 1000


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Search BlackBox snapshots for relevant information using hybrid retrieval.

    Results are RERANKED (hybrid_retrieve -> retrieve() incl. the cross-encoder
    rerank seam) and delivered BODY-ONLY (M15.3): each result is stripped of the
    ~1,000-char bookkeeping envelope (START/BEACON/VOLUME-TRACKER/GAUGES/
    Kernel-Index) the model can't use, keeping a compact [SNAP-id · date ·
    operator] attribution + Context Provenance + the session log.
    """
    query = params.get("query", "")
    limit = params.get("limit", params.get("k", 5))
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 5

    if not query:
        return ToolResult(False, "Search query is required")

    try:
        from Orchestrator.fossils import (
            cap_chars,
            format_snapshot_for_delivery,
            hybrid_retrieve,
        )
        from Orchestrator.volume import read_text_safe
        from Orchestrator.config import VOL_PATH

        vol_txt = read_text_safe(VOL_PATH)

        # Fetch WHOLE snapshots (window_budget_chars=None); the reranked ranking
        # is identical for every caller. Formatting/bounding happens below.
        results = hybrid_retrieve(vol_txt, query, k=limit, operator=ctx.operator)

        if not results:
            return ToolResult(
                success=True,
                result=f"No memories found matching: {query}"
            )

        # M15.3: strip the envelope -> body-only for the model. Compose body
        # FIRST, then (on-device only) window: the phone's engine window can't
        # hold whole snapshots, so bound each BODY to its per-result char budget.
        # Cloud/MCP callers keep whole body-only snapshots (WI-10/M7). Now that
        # the boilerplate head is gone, a body head-cap keeps real content (the
        # old best-chunk windowing existed to dodge that envelope head).
        formatted = [format_snapshot_for_delivery(snap) for snap in results]
        if getattr(ctx, "caller", None) == ON_DEVICE_CALLER:
            window_budget = max(
                LOCAL_MIN_WINDOW_CHARS, LOCAL_RESULT_BUDGET_CHARS // limit
            )
            formatted = [
                cap_chars(snap, window_budget) if len(snap) > window_budget else snap
                for snap in formatted
            ]

        # Format results
        output_parts = [f"Found {len(formatted)} relevant memory(ies) for: {query}\n"]
        for i, snap_text in enumerate(formatted, 1):
            output_parts.append(f"--- Result {i} ---\n{snap_text}")

        return ToolResult(
            success=True,
            result="\n\n".join(output_parts),
            data={"count": len(formatted)}
        )

    except Exception as e:
        return ToolResult(False, f"Search error: {str(e)}")
