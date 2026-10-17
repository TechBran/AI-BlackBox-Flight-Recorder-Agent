"""Executor for list_tts_voices (migrated from blackbox_tools._execute_list_tts_voices)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult

# Voice ids shown per group in the text summary; the full list rides in data.
_SHOWN_PER_GROUP = 30


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """List the voices text_to_speech accepts, from GET /tts/catalog.

    /tts/catalog is the single source of truth the Portal and Android voice
    pickers read: OpenAI, Gemini Flash/Pro, ElevenLabs (premade + cloned), local
    and on-box groups, each with its availability, plus the box's default voice.
    """
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{ctx.base_url}/tts/catalog",
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                result = await resp.json()
    except Exception as e:
        return ToolResult(False, f"List TTS voices error: {str(e)}")

    groups = result.get("groups") or []
    default = result.get("default") or {}
    compact = []
    total = 0
    lines = []
    for g in groups:
        voices = [{"id": v.get("id"), "name": v.get("name", "")}
                  for v in (g.get("voices") or []) if v.get("id")]
        total += len(voices)
        available = g.get("available", True) is not False
        compact.append({"id": g.get("id"), "label": g.get("label"),
                        "available": available, "model": g.get("model", ""),
                        "voices": voices})
        status = "available" if available else \
            f"unavailable: {g.get('unavailable_reason') or 'not configured'}"
        ids = [v["id"] for v in voices]
        shown = ", ".join(ids[:_SHOWN_PER_GROUP])
        more = len(ids) - _SHOWN_PER_GROUP
        if more > 0:
            shown += f", ... and {more} more (full list in tool data)"
        lines.append(f"\n{g.get('label') or g.get('id')} [{g.get('id')}] "
                     f"({len(ids)} voices, {status}): {shown}")

    header = [f"{total} voices in {len(groups)} provider groups. Pass a voice id "
              "below as text_to_speech's voice; omit voice to use the default."]
    if default.get("id"):
        header.append(f"Default voice: {default['id']}" + (
            "" if default.get("available", True) else
            " (no text-to-speech provider is configured on this box)"))
    return ToolResult(True, "\n".join(header + lines),
                      data={"default": default, "groups": compact})
