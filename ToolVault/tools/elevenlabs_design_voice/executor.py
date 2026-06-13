"""Executor for elevenlabs_design_voice — two-step Voice Design (preview -> save).

Calls ``Orchestrator.elevenlabs.voices`` DIRECTLY (in-process, no HTTP).

- No ``generated_voice_id``        -> step 1: design_previews (returns 3 candidates).
- ``generated_voice_id`` + ``name``-> step 2: design_save (persists the chosen one).
- ``generated_voice_id`` w/o name  -> error asking for a name.
"""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    voice_description = (params.get("voice_description") or "").strip()
    text = params.get("text")
    generated_voice_id = (params.get("generated_voice_id") or "").strip()
    name = (params.get("name") or "").strip()

    if not voice_description:
        return ToolResult(False, "voice_description is required.")

    from Orchestrator.elevenlabs import voices

    # ---- Step 2: save a chosen preview --------------------------------------
    if generated_voice_id:
        if not name:
            return ToolResult(False, "Provide a name to save the chosen preview.")
        try:
            result = voices.design_save(generated_voice_id, name, description=voice_description)
        except RuntimeError as exc:
            return ToolResult(False, str(exc))
        voice_id = result.get("voice_id")
        return ToolResult(
            True,
            f"Saved '{name}' (elevenlabs:{voice_id}).",
            data={"voice_id": voice_id},
        )

    # ---- Step 1: generate previews ------------------------------------------
    try:
        result = voices.design_previews(voice_description, text=text)
    except RuntimeError as exc:
        return ToolResult(False, str(exc))

    previews = result.get("previews") or []
    lines = []
    for i, p in enumerate(previews, start=1):
        dur = p.get("duration_secs")
        dur_str = f"{dur:.1f}s" if isinstance(dur, (int, float)) else "?s"
        lines.append(
            f"{i}. generated_voice_id={p.get('generated_voice_id')} "
            f"| {dur_str} | {p.get('audio_url')}"
        )
    listing = "\n".join(lines) if lines else "(no previews returned)"
    return ToolResult(
        True,
        f"Generated {len(previews)} preview voices:\n{listing}\n"
        "Pick one and call again with its generated_voice_id and a name to save it.",
        data={"previews": previews, "text": result.get("text")},
    )
