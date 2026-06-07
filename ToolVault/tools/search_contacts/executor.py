"""Executor for search_contacts (migrated from blackbox_tools._execute_search_contacts)."""
from Orchestrator.contacts import search_contacts as _search_contacts
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Search the contact book."""
    query = params.get("query", "")
    if not query:
        return ToolResult(False, "Search query is required")

    try:
        results = _search_contacts(query, ctx.operator)
        if not results:
            return ToolResult(
                success=True,
                result=f"No contacts found matching '{query}'.",
                data={"contacts": []}
            )

        summary_lines = [f"Found {len(results)} contact(s):"]
        for c in results:
            line = f"- {c['name']}"
            if c.get('phone'):
                line += f" | {c['phone']}"
            if c.get('email'):
                line += f" | {c['email']}"
            if c.get('relationship'):
                line += f" ({c['relationship']})"
            summary_lines.append(line)

        return ToolResult(
            success=True,
            result="\n".join(summary_lines),
            data={"contacts": results}
        )
    except Exception as e:
        return ToolResult(False, f"Contact search error: {str(e)}")
