"""dispatch_tool(): the one tool-execution path every chat provider loop uses.

Audit rank 74 (X05-architecture-11, O02-chat-routes-A-10, O03-chat-routes-B-07).
Each of the ten chat tool loops in routes/chat_routes.py (five streams, five
non-stream call_* twins) used to carry its own ~300-line ``elif tool_name ==``
table: web_fetch, the native image/video/music branches, the media lookups,
snapshot search, a dozen executor branches with their own progress labels and
the BlackBoxToolExecutor catch-all. Seven copies of the same table drifted, and
every fix had to be made seven times.

The table now lives here once. A loop hands over the tool call and gets back a
ToolOutcome: the text that goes back to the model, the SSE events a stream
yields (progress labels, {kind}_task placeholders, browser_task, cli_task), and
for a non-stream loop the media task it created. Packaging the result in the
provider's wire shape (an OpenAI ``tool`` message, an Anthropic ``tool_result``
block, a Gemini ``functionResponse``) stays with the loop.

The loops did not all behave alike, and what a client or a model sees is held
to recorded goldens (tests/golden/providers), so the differences that remain
are DATA in each loop's LoopProfile, not code: which executor tools have their
own labels, which result-text variant the native media branches return, whether
an empty catch-all result becomes a "(no output)" note, and the order of the
catch-all's events. Converging one of them is now a one-line profile change.
The differences that were bugs are gone (O02-chat-routes-A-10,
O03-chat-routes-B-07): no loop keeps an executor-only branch that bypasses the
catch-all's media placeholder, CLI event and empty-result note; call_gemini's
get_current_time is the ToolVault tool; the native branches coerce the model's
numeric arguments instead of crashing the turn on "2"; every stream's
image_task carries predicted_url and every stream's use_computer emits
browser_task.

The native branches read their helpers (create_task, web fetch, retrieval,
media lookups, upload resolution) from ToolServices, which chat_routes builds
from its own module namespace on every call, so a stub on chat_routes.<name>
reaches the dispatcher exactly as it reached the inline branches.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextvars
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from Orchestrator.fossils import format_snapshot_for_delivery
from Orchestrator.image_providers import IMAGE_TOOL_PROVIDERS
from Orchestrator.models import TaskType
from Orchestrator.utils.async_helpers import run_blocking
from Orchestrator.utils.paths import IMAGE_EXTS


# -----------------------------------------------------------------------------
# Media-task and agent-pill events (shared by every loop)
# -----------------------------------------------------------------------------
# Tool-name -> media kind. Covers the native-loop tools (generate_image via
# IMAGE_TOOL_PROVIDERS, generate_video, lyria_music) AND the ToolVault media
# tools that reach a loop's catch-all (which surface task_id in ToolResult.data).
_MEDIA_TOOL_KINDS = {
    "generate_image": "image",
    "gemini_image": "image",
    "openai_image": "image",
    "grok_image": "image",
    "local_image": "image",
    "generate_video": "video",
    "extend_video": "video",
    "lyria_music": "music",
    "elevenlabs_music": "music",
}


def _media_kind(tool_name: str):
    """Return 'image'|'video'|'music' for a known media tool, else None."""
    return _MEDIA_TOOL_KINDS.get(tool_name)


def _record_media_task(media_tasks: list, task_id, kind: str, prompt: str = "") -> None:
    """Append a {task_id,type,prompt} entry (dedup by task_id) if task_id truthy."""
    if not task_id or not kind:
        return
    tid = str(task_id)
    if any(m.get("task_id") == tid for m in media_tasks):
        return
    media_tasks.append({"task_id": tid, "type": kind, "prompt": (prompt or "")[:500]})


def _browser_task_event(cu_result, tool_input: Dict) -> Optional[Dict]:
    """The `browser_task` SSE event for a use_computer dispatch, or None.

    Emitted on SUCCESS only. The T2 executor returns the task_id in
    ToolResult.data on success (same shape the media catch-alls read) and, on an
    unresolvable class, mirrors its structured FAILURE payload into .data — which
    carries no "task_id" — so this returns None and the stream branch emits no
    browser_task. ToolResult.data is a dataclass field that defaults to None, so
    `.data or {}` is all the guarding needed. Keeping the emit DECISION in one
    tested helper means a future executor that nested task_id under a sub-key, or
    an inverted guard, fails a unit test instead of shipping silently."""
    task_id = (cu_result.data or {}).get("task_id")
    if not task_id:
        return None
    return {"type": "browser_task",
            "data": {"task_id": task_id, "prompt": tool_input.get("prompt", "")}}


_CLI_AGENT_TOOLS = ("claude_code_task", "gemini_cli_task", "codex_cli_task")


def _cli_task_event(tool_result, tool_name: str, tool_input) -> "Optional[Dict]":
    """The `cli_task` SSE event for a CLI-agent tool dispatch, or None (M3,
    2026-07-23). Sibling of _browser_task_event: the CLI launch surfaces
    task_id in ToolResult.data (cli_agent/tool_support.launch), so the in-chat
    agent pill can attach to the run. Emitted from every streaming catch-all
    (the CLI tools have no dedicated branch)."""
    if tool_name not in _CLI_AGENT_TOOLS:
        return None
    task_id = (getattr(tool_result, "data", None) or {}).get("task_id")
    if not task_id:
        return None
    return {"type": "cli_task",
            "data": {"task_id": str(task_id), "tool": tool_name,
                     "prompt": ((tool_input or {}).get("prompt", "") or "")[:200]}}


# -----------------------------------------------------------------------------
# What a loop hands over, and what it gets back
# -----------------------------------------------------------------------------
_CALL_TOOLS = frozenset({"send_sms", "make_phone_call", "make_voice_call"})
_EXECUTOR_LABELLED = _CALL_TOOLS | frozenset({
    "search_contacts", "save_contact", "create_cron_job", "edit_cron_job",
    "search_cron_jobs", "use_computer"})


@dataclass(frozen=True)
class LoopProfile:
    """How one provider loop dispatches tools: the differences between the old
    inline tables, kept so each loop's recorded behaviour is unchanged."""
    tag: str                                  # log prefix, e.g. "[ANTHROPIC-STREAM]"
    stream: bool                              # SSE events + helpers off the event loop
    native: bool = False                      # native media / snapshot / media-lookup branches
    labelled: frozenset = frozenset()         # executor tools with their own progress label
    native_text: str = "stream"               # media result-text variant: stream | anthropic_call | gemini_call
    full_call_labels: bool = False            # make_*_call labels untruncated ("Phone call: ...")
    empty_note: bool = True                   # catch-all: empty result -> "(no output)" note
    label_first: bool = False                 # catch-all: label before the cli/media events
    record_media: bool = False                # non-stream: report the media task it created


PROFILES: Dict[str, LoopProfile] = {
    "call_openai": LoopProfile(tag="[OPENAI]", stream=False),
    "call_xai": LoopProfile(tag="[XAI]", stream=False, record_media=True),
    "call_custom": LoopProfile(tag="[CUSTOM-CALL]", stream=False, record_media=True),
    "call_anthropic": LoopProfile(
        tag="[ANTHROPIC]", stream=False, native=True, labelled=_EXECUTOR_LABELLED,
        native_text="anthropic_call", record_media=True),
    "call_gemini": LoopProfile(
        tag="[GEMINI]", stream=False, native=True, labelled=_EXECUTOR_LABELLED,
        native_text="gemini_call", record_media=True),
    "stream_openai": LoopProfile(
        tag="[OPENAI-STREAM]", stream=True, native=True, labelled=_EXECUTOR_LABELLED,
        full_call_labels=True, empty_note=False, label_first=True),
    "stream_xai": LoopProfile(
        tag="[XAI-STREAM]", stream=True, native=True, labelled=_EXECUTOR_LABELLED - _CALL_TOOLS,
        empty_note=False, label_first=True),
    "stream_custom": LoopProfile(
        tag="[CUSTOM-STREAM]", stream=True, native=True, labelled=_EXECUTOR_LABELLED - _CALL_TOOLS,
        empty_note=False, label_first=True),
    "stream_anthropic": LoopProfile(
        tag="[ANTHROPIC-STREAM]", stream=True, native=True, labelled=_EXECUTOR_LABELLED),
    "stream_gemini": LoopProfile(
        tag="[GEMINI-STREAM]", stream=True, native=True, labelled=_EXECUTOR_LABELLED),
}


@dataclass(frozen=True)
class ToolServices:
    """The helpers the native branches call (chat_routes' own names, per call)."""
    create_task: Callable
    generate_prompt_slug: Callable
    perform_web_fetch: Callable
    hybrid_retrieve: Callable
    execute_get_media: Callable
    execute_list_media: Callable
    execute_search_media: Callable
    resolve_attachment: Callable


@dataclass(frozen=True)
class ToolContext:
    """One tool call's session: who runs it, from which device, in which loop."""
    operator: str
    origin_device_id: Optional[str]
    profile: LoopProfile
    services: ToolServices


@dataclass
class ToolOutcome:
    result: Any                               # what goes back to the model
    events: List[Dict] = field(default_factory=list)   # SSE events (stream loops)
    media_task: Optional[Tuple[Any, str, str]] = None  # (task_id, kind, prompt), non-stream


# -----------------------------------------------------------------------------
# Dispatch
# -----------------------------------------------------------------------------
async def dispatch_tool(name: str, args: Dict, ctx: ToolContext) -> ToolOutcome:
    """Run one tool call the way ``ctx.profile``'s loop always has."""
    profile = ctx.profile
    if name == "web_fetch":
        return await _web_fetch(args, ctx)
    if profile.native:
        if name in IMAGE_TOOL_PROVIDERS:
            return await _image(name, args, ctx)
        handler = _NATIVE.get(name)
        if handler is not None:
            return await handler(name, args, ctx)
    if name in profile.labelled:
        return await _labelled(name, args, ctx)
    # Everything else, the ToolVault executor tools included, is the catch-all:
    # it alone emits the media placeholder, the cli_task event and the
    # empty-result note (O02-chat-routes-A-10).
    return await _catch_all(name, args, ctx)


def dispatch_tool_blocking(name: str, args: Dict, ctx: ToolContext) -> ToolOutcome:
    """dispatch_tool for the non-stream call_* loops, which run on a worker
    thread with no event loop: the call gets its own loop (asyncio.run), as
    the executor branches always did. A caller already inside a running loop
    gets one on a helper thread instead of asyncio.run's RuntimeError; the
    copied context keeps the request's origin device and tool-outcome log."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(dispatch_tool(name, args, ctx))
    run = contextvars.copy_context().run
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(run, asyncio.run, dispatch_tool(name, args, ctx)).result()


async def _blocking(ctx: ToolContext, fn: Callable, *args, **kwargs):
    """A blocking helper: off the event loop in a stream, inline otherwise."""
    if ctx.profile.stream:
        return await run_blocking(fn, *args, **kwargs)
    return fn(*args, **kwargs)


async def _run_executor(ctx: ToolContext, name: str, args):
    from Orchestrator.tools import BlackBoxToolExecutor
    executor = BlackBoxToolExecutor(operator=ctx.operator, origin_device_id=ctx.origin_device_id)
    return await executor.execute(name, args)


def _int_arg(args: Dict, keys, default: int, lo: Optional[int] = None,
             hi: Optional[int] = None) -> int:
    """A numeric tool argument as the model sent it: an int, an integral float
    or a numeric string ("2", " 2 ", "2.0") all count, anything else is
    ``default``; then clamped to [lo, hi]. The first of ``keys`` present wins.
    Models stringify numbers, and range("2") used to kill the whole turn
    (O03-chat-routes-B-07)."""
    for key in ((keys,) if isinstance(keys, str) else keys):
        if key in args:
            raw = args[key]
            break
    else:
        raw = None
    value = default
    if isinstance(raw, bool):
        pass
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, float) and raw.is_integer():
        value = int(raw)
    elif isinstance(raw, str):
        try:
            number = float(raw.strip())
            if number.is_integer():
                value = int(number)
        except ValueError:
            pass
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _list_arg(args: Dict, key: str) -> List:
    """A list argument, also when the model sent it as a JSON string."""
    raw = args.get(key, [])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip().startswith("[") else [raw]
        except ValueError:
            raw = []
    return raw if isinstance(raw, list) else []


def _read_b64(path) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _ellipsis(prompt: str) -> str:
    return f"{prompt[:100]}{'...' if len(prompt) > 100 else ''}"


async def _web_fetch(args, ctx: ToolContext) -> ToolOutcome:
    url = args.get("url", "")
    max_chars = args.get("max_chars", 80000)
    print(f"{ctx.profile.tag} Executing web fetch: {url}")
    result = await _blocking(ctx, ctx.services.perform_web_fetch, url, max_chars)
    return ToolOutcome(result, [{"type": "tool_result", "data": f"Fetched content from: {url}"}])


async def _image(name, args, ctx: ToolContext) -> ToolOutcome:
    profile, operator = ctx.profile, ctx.operator
    create_task = ctx.services.create_task
    provider = IMAGE_TOOL_PROVIDERS[name]
    prompt = args.get("prompt", "")
    aspect_ratio = args.get("aspectRatio", "16:9")
    resolution = args.get("resolution", "1K")
    num_images = _int_arg(args, "numberOfImages", 1, lo=1, hi=4)  # the schema's 1..4
    reference_images = _list_arg(args, "reference_images")
    mode = "image-to-image" if reference_images else "text-to-image"
    print(f"{profile.tag} Executing {mode} generation: {prompt[:100]}... ({num_images} @ {resolution}, {len(reference_images)} refs)")

    image_options = {
        "aspectRatio": aspect_ratio,
        "resolution": resolution,
        "numberOfImages": num_images,
        "provider": provider,
    }
    # Reference images for image-to-image generation (max 14).
    reference_image_data = []
    if reference_images:
        for img_url in reference_images[:14]:
            file_path = ctx.services.resolve_attachment(img_url, IMAGE_EXTS)
            if file_path is not None:
                img_b64 = await _blocking(ctx, _read_b64, file_path)
                ext = file_path.suffix.lower()
                mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
                reference_image_data.append({
                    "data": img_b64,
                    "mimeType": mime_map.get(ext, "image/jpeg")
                })
        if reference_image_data:
            image_options["referenceImages"] = reference_image_data

    task = create_task(
        TaskType.IMAGE_GENERATION,
        operator=operator,
        prompt=prompt,
        result_data={"options": image_options}
    )

    # Predicted URL (same logic as process_image_generation)
    slug = ctx.services.generate_prompt_slug(prompt)
    if num_images == 1:
        predicted_url = f"/ui/uploads/{slug}_{task.task_id}.png"
        url_info = f"Image will be saved to: {predicted_url}"
    else:
        predicted_urls = [f"/ui/uploads/{slug}_{task.task_id}_{i+1}.png" for i in range(num_images)]
        url_info = f"Images will be saved to: {', '.join(predicted_urls)}"

    img_count = num_images
    plural = 's' if img_count > 1 else ''
    ref_info = f" Using {len(reference_image_data)} reference image(s) for style guidance." if reference_image_data else ""
    tail = "To use this image for video generation later, pass the URL to generate_video with image_url parameter."
    if profile.native_text == "anthropic_call":
        result = f"Image generation started for: {_ellipsis(prompt)}. Creating {img_count} image{plural} at {resolution} resolution ({aspect_ratio} aspect ratio). {url_info}. {tail}"
    elif profile.native_text == "gemini_call":
        result = f"Image generation started for: {_ellipsis(prompt)}. Creating {img_count} image{plural} at {resolution} resolution.{ref_info} {url_info}. {tail}"
    else:
        result = f"Image generation started for: {_ellipsis(prompt)}. Creating {img_count} image{plural} at {resolution} resolution ({aspect_ratio} aspect ratio).{ref_info} {url_info}. {tail}"

    if not profile.stream:
        return ToolOutcome(result, media_task=(task.task_id, "image", prompt))
    print(f"[STREAM] Sending image_task event: task_id={task.task_id}, prompt={prompt[:50]}, count={num_images}")
    return ToolOutcome(result, [
        {"type": "tool_result", "data": f"Image generation task created: {task.task_id}"},
        {"type": "image_task", "data": {"task_id": task.task_id, "prompt": prompt, "count": num_images,
                                        "predicted_url": predicted_url if num_images == 1 else predicted_urls[0]}},
    ])


async def _video(name, args, ctx: ToolContext) -> ToolOutcome:
    profile, operator = ctx.profile, ctx.operator
    create_task = ctx.services.create_task
    call_text = profile.native_text != "stream"
    prompt = args.get("prompt", "")
    # The call_* loops read a missing url as None, the streams as "".
    video_url = args.get("video_url") if call_text else args.get("video_url", "")
    image_url = args.get("image_url") if call_text else args.get("image_url", "")
    aspect_ratio = args.get("aspectRatio", "16:9")
    duration = _int_arg(args, "duration", 8)
    resolution = args.get("resolution", "720p")
    negative_prompt = args.get("negativePrompt", "")

    # Determine mode: video extension takes priority
    if video_url:
        mode = "video-extension"
    elif image_url:
        mode = "image-to-video"
    else:
        mode = "text-to-video"
    print(f"{profile.tag} Executing video generation ({mode}): {prompt[:100]}... ({duration}s @ {resolution})")

    slug = ctx.services.generate_prompt_slug(prompt) if prompt else "video"

    if video_url:
        # VIDEO EXTENSION MODE - create task with url for extension
        video_data = {
            "url": video_url,
            "prompt": prompt
        }
        task = create_task(
            TaskType.VIDEO_GENERATION,
            operator=operator,
            prompt=prompt if call_text else (prompt or "Extend video"),
            result_data=video_data
        )
        predicted_url = f"/ui/uploads/{slug}_{task.task_id}.mp4"
        if profile.native_text == "anthropic_call":
            result = f"Video EXTENSION started. Extending video at {video_url} with prompt: {_ellipsis(prompt)}. The new clip will continue from where the original ended. Video will be saved to: {predicted_url}. Takes 5-20 minutes."
        elif profile.native_text == "gemini_call":
            result = f"Video EXTENSION started. Extending video at {video_url}. Video will be saved to: {predicted_url}. Takes 5-20 minutes."
        else:
            result = f"Video EXTENSION started. Extending video at {video_url} with prompt: {_ellipsis(prompt)}. The new clip will continue from where the original ended. Video generation takes 5-20 minutes. When complete, the extended video will be available at: {predicted_url}"
    else:
        # TEXT-TO-VIDEO or IMAGE-TO-VIDEO
        video_options = {
            "aspectRatio": aspect_ratio,
            "duration": duration,
            "resolution": resolution,
        }
        if negative_prompt:
            video_options["negativePrompt"] = negative_prompt
        if image_url:
            video_options["image_url"] = image_url

        if call_text:
            # The call_* loops also hand the source image to the task as base64.
            image_data = None
            if image_url:
                file_path = ctx.services.resolve_attachment(image_url, IMAGE_EXTS)
                if file_path is not None:
                    image_b64 = await _blocking(ctx, _read_b64, file_path)
                    ext = file_path.suffix.lower()
                    mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
                    image_data = {"data": image_b64, "mime_type": mime_map.get(ext, "image/jpeg")}
            task = create_task(
                TaskType.VIDEO_GENERATION,
                operator=operator,
                prompt=prompt,
                image_data=image_data,
                result_data={"options": video_options}
            )
        else:
            task = create_task(
                TaskType.VIDEO_GENERATION,
                operator=operator,
                prompt=prompt,
                result_data={"options": video_options}
            )
        predicted_url = f"/ui/uploads/{slug}_{task.task_id}.mp4"
        if profile.native_text == "anthropic_call":
            result = f"Video generation ({mode}) started for: {_ellipsis(prompt)}. Creating {duration}s video at {resolution} resolution. Video will be saved to: {predicted_url}. Takes 5-20 minutes."
        elif profile.native_text == "gemini_call":
            result = f"Video generation ({mode}) started. Creating {duration}s video at {resolution}. Video will be saved to: {predicted_url}. Takes 5-20 minutes."
            if image_url:
                result += f" Source image: {image_url}"
        elif image_url:
            result = f"Image-to-video generation started. Animating image at {image_url} with prompt: {_ellipsis(prompt)}. Creating {duration}s video at {resolution} resolution ({aspect_ratio} aspect ratio). Video generation takes 5-20 minutes. When complete, the video will be available at: {predicted_url}"
        else:
            result = f"Video generation started for: {_ellipsis(prompt)}. Creating {duration}s video at {resolution} resolution ({aspect_ratio} aspect ratio). Video generation takes 5-20 minutes. When complete, the video will be available at: {predicted_url}"
    if profile.native_text == "anthropic_call" and image_url:
        result += f" Source image: {image_url}"

    if not profile.stream:
        return ToolOutcome(result, media_task=(task.task_id, "video", prompt))
    print(f"[STREAM] Sending video_task event: task_id={task.task_id}, prompt={prompt[:50]}, mode={mode}, predicted_url={predicted_url}")
    return ToolOutcome(result, [
        {"type": "tool_result", "data": f"Video generation task created: {task.task_id}"},
        {"type": "video_task", "data": {"task_id": task.task_id, "prompt": prompt, "duration": duration,
                                        "resolution": resolution, "mode": mode,
                                        "predicted_url": predicted_url}},
    ])


async def _music(name, args, ctx: ToolContext) -> ToolOutcome:
    profile, operator = ctx.profile, ctx.operator
    create_task = ctx.services.create_task
    prompt = args.get("prompt", "")
    negative_prompt = args.get("negativePrompt", "")
    sample_count = _int_arg(args, "sampleCount", 1, lo=1)
    print(f"{profile.tag} Executing music generation: {prompt[:100]}... ({sample_count} variation(s))")

    music_options = {
        "prompt": prompt,
        "operator": operator,
    }
    if negative_prompt:
        music_options["negative_prompt"] = negative_prompt
    if sample_count and sample_count > 1:
        music_options["sample_count"] = sample_count

    task = create_task(
        TaskType.LYRIA_MUSIC,
        operator=operator,
        prompt=prompt,
        result_data=music_options
    )

    variations_text = f" ({sample_count} variations)" if sample_count > 1 else ""
    result = f"Music generation started for: {_ellipsis(prompt)}{variations_text}. 30-second track will be ready in 20-60 seconds."
    if not profile.stream:
        return ToolOutcome(result, media_task=(task.task_id, "music", prompt))
    print(f"[STREAM] Sending music_task event: task_id={task.task_id}, prompt={prompt[:50]}")
    return ToolOutcome(result, [
        {"type": "tool_result", "data": f"Music generation task created: {task.task_id}"},
        {"type": "music_task", "data": {"task_id": task.task_id, "prompt": prompt, "sample_count": sample_count}},
    ])


async def _get_media(name, args, ctx: ToolContext) -> ToolOutcome:
    # The call_* loops read a missing url / task_id as None, the streams as "".
    default = "" if ctx.profile.stream else None
    media_url = args.get("url", default)
    media_task_id = args.get("task_id", default)
    print(f"{ctx.profile.tag} Executing get_media: url={media_url}, task_id={media_task_id}")
    found = await _blocking(ctx, ctx.services.execute_get_media, url=media_url, task_id=media_task_id)
    return ToolOutcome(json.dumps(found, indent=2),
                       [{"type": "tool_result", "data": f"Retrieved media: {media_url or media_task_id}"}])


async def _list_media(name, args, ctx: ToolContext) -> ToolOutcome:
    media_type = args.get("media_type")
    limit = _int_arg(args, "limit", 20)
    print(f"{ctx.profile.tag} Executing list_media: type={media_type}, limit={limit}")
    listed = await _blocking(ctx, ctx.services.execute_list_media, media_type=media_type, limit=limit)
    return ToolOutcome(json.dumps(listed, indent=2),
                       [{"type": "tool_result", "data": f"Listed {listed.get('count', 0)} media files"}])


async def _search_media(name, args, ctx: ToolContext) -> ToolOutcome:
    query = args.get("query", "")
    media_type = args.get("media_type")
    limit = _int_arg(args, "limit", 10)
    print(f"{ctx.profile.tag} Executing search_media: query='{query}', type={media_type}")
    found = await _blocking(ctx, ctx.services.execute_search_media, query=query, media_type=media_type, limit=limit)
    return ToolOutcome(json.dumps(found, indent=2),
                       [{"type": "tool_result", "data": f"Found {found.get('count', 0)} matching media"}])


async def _search_snapshots(name, args, ctx: ToolContext) -> ToolOutcome:
    tag, operator = ctx.profile.tag, ctx.operator
    query = args.get("query", "")
    # The schema's "limit" (older prompts say "k"); 3 by default, capped at 5.
    k = _int_arg(args, ("limit", "k"), 3, lo=1, hi=5)
    print(f"{tag} Executing snapshot search: '{query}' (k={k})")

    if not query:
        result = "Error: No search query provided."
    else:
        try:
            search_results = await _blocking(ctx, ctx.services.hybrid_retrieve, "", query, k=k, operator=operator)

            if not search_results:
                result = f"No snapshots found matching: {query}"
            else:
                output_parts = [f"Found {len(search_results)} relevant snapshot(s) for: {query}\n"]
                for i, snap_text in enumerate(search_results, 1):
                    # WI-10 (M7): deliver retrieved snapshots WHOLE — no delivery truncation
                    output_parts.append(f"--- Result {i} ---\n{format_snapshot_for_delivery(snap_text)}")
                result = "\n\n".join(output_parts)
                print(f"{tag} Snapshot search returned {len(search_results)} results")
        except Exception as e:
            print(f"{tag} Snapshot search error: {e}")
            result = f"Search failed: {str(e)}"
    return ToolOutcome(result, [{"type": "tool_result", "data": "Snapshot search completed"}])


_NATIVE = {
    "generate_video": _video,
    "get_media": _get_media,
    "list_media": _list_media,
    "search_media": _search_media,
    "lyria_music": _music,
    "search_snapshots": _search_snapshots,
}


# The executor tools with their own progress label: the arguments they pass
# (the call tools rebuild theirs with defaults) and the label.
def _call_args(args) -> Dict:
    return {
        "phone_number": args.get("phone_number", ""),
        "greeting": args.get("greeting", ""),
        "role": args.get("role", ""),
        "backend": args.get("backend", "openai_realtime"),
    }


_LABELLED_ARGS: Dict[str, Callable[[Dict], Dict]] = {
    "send_sms": lambda a: {"phone_number": a.get("phone_number", ""), "message": a.get("message", "")},
    "make_phone_call": _call_args,
    "make_voice_call": lambda a: {"phone_number": a.get("phone_number", ""),
                                  "message": a.get("message", ""),
                                  "voice": a.get("voice", "onyx")},
}


# The event a labelled tool adds after its label in a stream.
_LABELLED_EVENTS: Dict[str, Callable] = {"use_computer": _browser_task_event}


def _label(name: str, result: str, profile: LoopProfile) -> str:
    if name == "send_sms":
        return f"SMS: {result[:100]}"
    if name == "make_phone_call":
        return f"Phone call: {result}" if profile.full_call_labels else f"Call: {result[:100]}"
    if name == "make_voice_call":
        return f"Voice call: {result}" if profile.full_call_labels else f"Voice call: {result[:100]}"
    if name == "search_contacts":
        return "Contact search completed"
    if name == "save_contact":
        return f"Contact: {result[:100]}"
    if name in ("create_cron_job", "edit_cron_job"):
        return f"Cron job: {result[:100]}"
    if name == "search_cron_jobs":
        return f"Cron jobs: {result[:100]}"
    return f"Computer Use: {result[:80]}"   # use_computer


async def _labelled(name, args, ctx: ToolContext) -> ToolOutcome:
    rebuild = _LABELLED_ARGS.get(name)
    tool_result = await _run_executor(ctx, name, rebuild(args) if rebuild else args)
    result = tool_result.result
    print(f"{ctx.profile.tag} {name}: {str(result)[:100]}")
    if not ctx.profile.stream:
        return ToolOutcome(result)
    events = [{"type": "tool_result", "data": _label(name, result, ctx.profile)}]
    follow_up = _LABELLED_EVENTS.get(name)
    if follow_up:
        # use_computer's browser_task, on SUCCESS only (the guard lives in the
        # tested _browser_task_event helper).
        event = follow_up(tool_result, args)
        if event:
            events.append(event)
    return ToolOutcome(result, events)


async def _catch_all(name, args, ctx: ToolContext) -> ToolOutcome:
    """Any other tool (ToolVault tools, the per-provider web search tools, CLI
    agents) through BlackBoxToolExecutor, never an "Unknown tool" reply."""
    profile = ctx.profile
    tool_result = await _run_executor(ctx, name, args)
    result = tool_result.result if hasattr(tool_result, 'result') else str(tool_result)
    if profile.empty_note and not result:
        result = f"Tool '{name}' executed successfully (no output)."
    print(f"\033[33m[TOOLVAULT-EXEC] {profile.tag} {name} (catch-all): {(result or '')[:120]}\033[0m")
    kind = _media_kind(name)
    data = getattr(tool_result, "data", None)
    if not profile.stream:
        # 2-media: ToolVault media executors surface task_id in .data; report
        # it like the native branches so the worker injects the placeholder.
        if profile.record_media and kind and data:
            return ToolOutcome(result, media_task=(data.get("task_id"), kind, args.get("prompt", "")))
        return ToolOutcome(result)

    extra = []
    cli = _cli_task_event(tool_result, name, args)
    if cli:
        extra.append(cli)
    if kind and data:
        task_id = data.get("task_id")
        if task_id:
            # Blanket media placeholder: a media tool reaching the catch-all
            # (e.g. elevenlabs_music) emits the same {kind}_task event as the
            # native branches, so the UI shows and fills a placeholder.
            print(f"{profile.tag} Sending {kind}_task (catch-all): task_id={task_id}")
            extra.append({"type": f"{kind}_task", "data": {"task_id": str(task_id), "prompt": args.get("prompt", "")}})
    if profile.label_first:
        return ToolOutcome(result, [{"type": "tool_result", "data": f"{name}: {(result or '')[:80]}"}] + extra)
    return ToolOutcome(result, extra + [{"type": "tool_result", "data": f"{name}: {result[:80]}"}])
