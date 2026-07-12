"""Voice-probe CLI.

Single probe:
    python -m diagnostics.voice_probes.run --provider openai --model gpt-realtime-2.1
    python -m diagnostics.voice_probes.run --provider xai            # no ?model= (default resolution)
    python -m diagnostics.voice_probes.run --provider gemini --model gemini-3.1-flash-live-preview

Canned suites (each writes diagnostics/voice_probes/results/<date>-<suite>.json):
    python -m diagnostics.voice_probes.run --suite openai-models
    python -m diagnostics.voice_probes.run --suite xai
    python -m diagnostics.voice_probes.run --suite gemini-tools     # AFTER P1.1 schema fix
    python -m diagnostics.voice_probes.run --suite translate

Exit code is 0 as long as the harness ran — a rejected model is a RECORDED
finding, not a failure. Run from the repo root with the Orchestrator venv.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import List

from diagnostics.voice_probes.harness import ProbeResult, write_results
from diagnostics.voice_probes.probes import probe_gemini, probe_openai, probe_xai

ASSETS = Path(__file__).resolve().parent / "assets"

# The backend's exact server_vad knobs (Orchestrator/routes/grok_live_routes.py:441-446)
# — the round-trip probe checks whether xAI echoes them back in session.updated
# or silently drops them (docs don't list threshold/padding on the xAI schema).
SERVER_VAD_KNOBS = {
    "turn_detection": {
        "type": "server_vad",
        "threshold": 0.7,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 900,
    }
}

# audio.input probes (P2 gates). Both are session.update round-trips through
# _probe_openai_style: ok=True iff a session.updated event arrives (patch
# ACCEPTED); the echoed session object inside the recorded session.updated
# event is the finding itself (extracted in P0.4 Step 4).
#   - input_rate_16k: does xAI accept audio.input.format rate=16000?
#     Gates the P2.15 Branch A/B choice.
#   - transcription_shape: is an explicit audio.input.transcription opt-in
#     accepted, and what shape does the server echo back? Gates P2.11.
INPUT_16K_PATCH = {
    "audio": {"input": {"format": {"type": "audio/pcm", "rate": 16000}}}
}
TRANSCRIPTION_PATCH = {
    "audio": {"input": {"transcription": {}}}
}


async def suite_openai_models(args) -> List[ProbeResult]:
    # gpt-realtime-2.1 / -mini: expected OK (GA 2026-07-06, research-confirmed).
    # gpt-realtime-2025-08-28: docs say valid, our May test saw close-4000 — re-probe.
    # gpt-live-1 / -mini: ChatGPT-only per docs — expect rejection; RECORD the exact shape.
    models = [
        "gpt-realtime-2.1",
        "gpt-realtime-2.1-mini",
        "gpt-realtime-2025-08-28",
        "gpt-live-1",
        "gpt-live-1-mini",
    ]
    return [await probe_openai(m) for m in models]


async def suite_xai(args) -> List[ProbeResult]:
    results = [await probe_xai("", probe="default_model_resolution")]
    results.append(await probe_xai("grok-voice-latest"))
    results.append(await probe_xai("grok-voice-think-fast-1.0"))
    results.append(await probe_xai(
        "grok-voice-latest", probe="server_vad_roundtrip",
        session_patch=SERVER_VAD_KNOBS,
    ))
    # 16 kHz input-format round-trip (P2.15 Branch A/B gate): ok=True means
    # xAI accepted audio.input.format.rate=16000; the echoed format lives in
    # the recorded session.updated event.
    results.append(await probe_xai(
        "grok-voice-latest", probe="input_rate_16k",
        session_patch=INPUT_16K_PATCH,
    ))
    # Explicit input-transcription opt-in (P2.11 gate): ok=True means the bare
    # {} shape is accepted; the echoed audio.input.transcription object in the
    # recorded session.updated event is the accepted shape P2.11 mirrors.
    results.append(await probe_xai(
        "grok-voice-latest", probe="transcription_shape",
        session_patch=TRANSCRIPTION_PATCH,
    ))
    # Transcription-by-default: NO transcription opt-in in the session; send real
    # speech and record whether any input-transcription events arrive unprompted.
    asset = Path(args.speech_asset)
    audio = asset.read_bytes() if asset.exists() else None
    r = await probe_xai(
        "grok-voice-latest", probe="transcription_default",
        session_patch=SERVER_VAD_KNOBS, audio_pcm=audio, listen_s=12.0,
    )
    if audio is None:
        r.notes = (
            f"no speech asset at {asset} — transcription-by-default UNRESOLVED "
            "(run: python -m diagnostics.voice_probes.make_speech_asset); " + r.notes
        )
    results.append(r)
    return results


async def suite_gemini_tools(args) -> List[ProbeResult]:
    # DEPENDS ON P1.1 (update_sheet_values schema fix). Pre-fix this records the
    # known 1007 at properties[values].items.items. Requires the repo venv
    # (imports Orchestrator.tools.tool_registry) run from the repo root.
    from Orchestrator.tools.tool_registry import get_gemini_live_tools
    tools = get_gemini_live_tools("gemini_live")
    results = [await probe_gemini("gemini-3.1-flash-live-preview", probe="bare")]
    for m in ("gemini-3.1-flash-live-preview", "gemini-2.5-flash-native-audio-latest"):
        results.append(await probe_gemini(m, probe="full_tools", tools=tools))
    return results


async def suite_translate(args) -> List[ProbeResult]:
    # Session shapes for the translation voice mode (Workstream 5 gate).
    # session.created's session object reveals the translate model's default
    # config fields; Gemini translate probed with AUDIO and with server-default
    # modalities (3.1 rejects TEXT — the translate model's tolerance is unknown).
    results = [await probe_openai("gpt-realtime-translate", probe="translate_handshake")]
    results.append(await probe_gemini(
        "gemini-3.5-live-translate-preview", probe="translate_minimal_audio",
    ))
    results.append(await probe_gemini(
        "gemini-3.5-live-translate-preview", probe="translate_no_modalities",
        response_modalities=None,
    ))
    return results


SUITES = {
    "openai-models": suite_openai_models,
    "xai": suite_xai,
    "gemini-tools": suite_gemini_tools,
    "translate": suite_translate,
}


async def _single(args) -> List[ProbeResult]:
    if args.provider == "openai":
        return [await probe_openai(args.model or "gpt-realtime-2.1")]
    if args.provider == "xai":
        return [await probe_xai(args.model)]
    return [await probe_gemini(
        args.model or "gemini-3.1-flash-live-preview", api_version=args.api_version,
    )]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m diagnostics.voice_probes.run")
    parser.add_argument("--provider", choices=["openai", "xai", "gemini"])
    parser.add_argument("--model", default="")
    parser.add_argument("--suite", choices=sorted(SUITES))
    parser.add_argument("--out", default="", help="results file name stem override")
    parser.add_argument("--api-version", default="v1beta",
                        help="gemini only: v1beta | v1alpha")
    parser.add_argument("--speech-asset", default=str(ASSETS / "speech_24k.pcm"))
    args = parser.parse_args(argv)

    if args.suite:
        results = asyncio.run(SUITES[args.suite](args))
        name = args.out or args.suite
    elif args.provider:
        results = asyncio.run(_single(args))
        name = args.out or (
            f"{args.provider}-{(args.model or 'default').replace('/', '_')}-adhoc"
        )
    else:
        parser.error("--provider or --suite required")

    for r in results:
        print(r.summary())
    print(f"results: {write_results(name, results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
