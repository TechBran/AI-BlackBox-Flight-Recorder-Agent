"""Executor for elevenlabs_list_voices — grouped account voice listing.

Calls ``Orchestrator.elevenlabs.catalog.get_voices`` DIRECTLY (in-process, no
HTTP). No key -> get_voices returns None -> we report nothing is available.
"""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    from Orchestrator.elevenlabs import catalog

    try:
        result = catalog.get_voices()
    except RuntimeError as exc:
        return ToolResult(False, str(exc))

    if result is None:
        return ToolResult(
            True,
            "No ElevenLabs voices available (no API key configured).",
            data={"my_voices": [], "premade": []},
        )

    my_voices = result.get("my_voices") or []
    premade = result.get("premade") or []

    if my_voices:
        mine = ", ".join(f"{v.get('name')} ({v.get('id')})" for v in my_voices)
    else:
        mine = "(none yet)"
    summary = f"My Voices: {mine} / Premade: {len(premade)} available"
    return ToolResult(True, summary, data={"my_voices": my_voices, "premade": premade})
