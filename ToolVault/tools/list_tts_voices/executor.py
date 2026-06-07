"""Executor for list_tts_voices (migrated from blackbox_tools._execute_list_tts_voices)."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """List available Google Cloud TTS voices via /tts/google/voices."""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{ctx.base_url}/tts/google/voices",
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                result = await resp.json()
                voices = result.get("voices", [])
                # Summarize to avoid overwhelming output
                summary_lines = [f"Found {len(voices)} Google Cloud TTS voices.\n"]
                # Group by language
                lang_counts = {}
                for v in voices:
                    for lc in v.get("languageCodes", []):
                        lang = lc.split("-")[0]
                        lang_counts[lang] = lang_counts.get(lang, 0) + 1
                summary_lines.append("Languages: " + ", ".join(f"{k}({v})" for k, v in sorted(lang_counts.items())))
                # Show first 20 English voices as examples
                en_voices = [v["name"] for v in voices if any("en-" in lc for lc in v.get("languageCodes", []))][:20]
                if en_voices:
                    summary_lines.append(f"\nSample English voices: {', '.join(en_voices)}")
                return ToolResult(True, "\n".join(summary_lines), data={"voice_count": len(voices)})
    except Exception as e:
        return ToolResult(False, f"List TTS voices error: {str(e)}")
