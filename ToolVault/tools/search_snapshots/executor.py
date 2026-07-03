"""Executor for search_snapshots (migrated from blackbox_tools._execute_search_memory)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Search BlackBox snapshots for relevant information using hybrid retrieval."""
    query = params.get("query", "")
    limit = params.get("limit", params.get("k", 5))

    if not query:
        return ToolResult(False, "Search query is required")

    try:
        from Orchestrator.fossils import hybrid_retrieve
        from Orchestrator.volume import read_text_safe
        from Orchestrator.config import VOL_PATH

        vol_txt = read_text_safe(VOL_PATH)

        # Use hybrid retrieval (keyword + semantic)
        results = hybrid_retrieve(vol_txt, query, k=limit, operator=ctx.operator)

        if not results:
            return ToolResult(
                success=True,
                result=f"No memories found matching: {query}"
            )

        # Format results
        output_parts = [f"Found {len(results)} relevant memory(ies) for: {query}\n"]
        for i, snap_text in enumerate(results, 1):
            # WI-10 (M7): deliver retrieved snapshots WHOLE — the old 10k
            # per-result truncation is gone (caps exist only at the embedding
            # layer; the count knob `limit` is the budget).
            output_parts.append(f"--- Result {i} ---\n{snap_text}")

        return ToolResult(
            success=True,
            result="\n\n".join(output_parts),
            data={"count": len(results)}
        )

    except Exception as e:
        return ToolResult(False, f"Search error: {str(e)}")
