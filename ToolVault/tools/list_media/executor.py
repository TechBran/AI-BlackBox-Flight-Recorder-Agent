"""Executor for list_media (migrated from blackbox_tools._execute_list_media)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """List media files in uploads folder."""
    media_type = params.get("media_type")
    limit = params.get("limit", 20)

    try:
        from Orchestrator.routes.chat_routes import execute_list_media
        result = execute_list_media(media_type, limit)

        # Function returns "media" not "files"
        media = result.get("media", [])
        if not media:
            return ToolResult(
                success=True,
                result="No media files found in uploads folder.",
                data=result
            )

        # Format output for voice/text
        summary = f"Found {len(media)} media file(s):\n"
        for m in media[:15]:  # Show first 15
            desc = m.get('description', '')[:50] if m.get('description') else 'no description'
            summary += f"- {m.get('url', 'unknown')} ({m.get('type', 'unknown')}) - {desc}\n"
        if len(media) > 15:
            summary += f"... and {len(media) - 15} more"

        # Add usage hint
        summary += f"\n\n{result.get('usage_hint', '')}"

        return ToolResult(success=True, result=summary, data=result)
    except Exception as e:
        return ToolResult(False, f"List media error: {str(e)}")
