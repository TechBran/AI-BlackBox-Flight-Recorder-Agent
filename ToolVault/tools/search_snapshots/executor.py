"""Executor for search_snapshots (migrated from blackbox_tools._execute_search_memory)."""
from Orchestrator.toolvault.context import ON_DEVICE_CALLER, ToolContext, ToolResult

# M8/WI-7a: result budget for the ON-DEVICE caller only. The phone's engine
# window is 6,144 tokens (device-proven GPU ceiling) — whole snapshots (corpus
# p99 ≈ 14.9k chars EACH) cannot fit its agent loop, so the local bridge gets
# a bounded result: ~8,000 chars total, split evenly across the requested
# result count and delivered as best-chunk windows (hybrid_retrieve
# window_budget_chars -> fossils.window_snapshot_text) rather than head
# truncations. A floor keeps each window useful when the model asks for many
# results. Cloud/MCP callers (caller != "local") stay WHOLE per WI-10/M7.
LOCAL_RESULT_BUDGET_CHARS = 8000
LOCAL_MIN_WINDOW_CHARS = 1000


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Search BlackBox snapshots for relevant information using hybrid retrieval."""
    query = params.get("query", "")
    limit = params.get("limit", params.get("k", 5))
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 5

    if not query:
        return ToolResult(False, "Search query is required")

    try:
        from Orchestrator.fossils import hybrid_retrieve
        from Orchestrator.volume import read_text_safe
        from Orchestrator.config import VOL_PATH

        vol_txt = read_text_safe(VOL_PATH)

        # WI-10 (M7): cloud/MCP callers get retrieved snapshots WHOLE (caps
        # exist only at the embedding layer; `limit` is the budget).
        # M8 (WI-7a): the ON-DEVICE caller is the one window-bound surface —
        # bound the result and window each snapshot on its best-matched chunk.
        window_budget = None
        if getattr(ctx, "caller", None) == ON_DEVICE_CALLER:
            window_budget = max(
                LOCAL_MIN_WINDOW_CHARS, LOCAL_RESULT_BUDGET_CHARS // limit
            )

        # Use hybrid retrieval (keyword + semantic)
        results = hybrid_retrieve(
            vol_txt, query, k=limit, operator=ctx.operator,
            window_budget_chars=window_budget,
        )

        if not results:
            return ToolResult(
                success=True,
                result=f"No memories found matching: {query}"
            )

        # Format results
        output_parts = [f"Found {len(results)} relevant memory(ies) for: {query}\n"]
        for i, snap_text in enumerate(results, 1):
            output_parts.append(f"--- Result {i} ---\n{snap_text}")

        return ToolResult(
            success=True,
            result="\n\n".join(output_parts),
            data={"count": len(results)}
        )

    except Exception as e:
        return ToolResult(False, f"Search error: {str(e)}")
