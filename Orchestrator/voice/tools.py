"""The voice tool ladder: one implementation for every live-voice session.

Gemini Live, OpenAI Realtime and Grok each carried a ~350-line copy of this
ladder (91% identical, audit O09-live-voice-providers-14), so a fix landed in
one copy. The routes now run tool calls through execute_voice_tool, and the
two tool-call wire formats (Gemini's toolCall/toolResponse, the OpenAI-style
function_call_arguments.done/function_call_output that OpenAI and xAI share)
are dispatched and answered here.

The voice-specific branches stay voice-specific: the media generators notify
the client with image_task/video_task/music_task frames, list_recent_snapshots
lets the system operator see every operator's snapshots (outbound-call context
handoff), and search_snapshots returns at most five whole snapshots. Every
other tool goes through BlackBoxToolExecutor.

Arguments are normalised first (coerce_voice_args): models stringify numbers,
arrays and booleans, and the inline branches read raw arguments, so
``min(args.get("k", 3), 5)`` raised TypeError on k="3" and the model was told
its search failed.
"""

import json
from typing import Any, Callable, Dict

from Orchestrator.config import VOL_PATH
from Orchestrator.fossils import format_snapshot_for_delivery, hybrid_retrieve
from Orchestrator.image_providers import IMAGE_TOOL_PROVIDERS
from Orchestrator.models import TaskType
from Orchestrator.routes.voice_ws_shared import (
    dispatch_voice_tool,
    safe_send,
    send_gemini_tool_error,
    send_openai_style_tool_error,
)
from Orchestrator.tasks import create_task
from Orchestrator.utils.async_helpers import run_blocking
from Orchestrator.web_tools import perform_web_fetch


# =============================================================================
# Arguments
# =============================================================================

def _parse_number(value: str, integer: bool):
    """A numeric string as an int (or float); None when it is not one."""
    text = value.strip()
    if not text:
        return None
    try:
        if integer:
            return int(text)
        number = float(text)
    except ValueError:
        if integer:
            try:
                number = float(text)
            except ValueError:
                return None
            return int(number) if number.is_integer() else None
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def coerce_voice_args(name: str, args):
    """Normalise a voice tool call's arguments against the tool's schema.

    Arrays, objects and booleans sent as strings are parsed back the way the
    executor does for every surface (_coerce_stringified_json_args); integer
    and number parameters sent as numeric strings become numbers. Anything
    else is left alone. Never raises.
    """
    if not isinstance(args, dict):
        return args
    try:
        from Orchestrator.tools.blackbox_tools import _coerce_stringified_json_args
        from Orchestrator.toolvault import registry
        spec = registry.get_tool(registry.resolve_alias(name)) or {}
    except Exception as e:
        print(f"[VOICE-TOOLS] no schema for {name}: {e}")
        return args
    args = _coerce_stringified_json_args(name, args, spec=spec)
    props = (spec.get("parameters") or {}).get("properties") or {}
    coerced = None
    for key, value in args.items():
        ptype = (props.get(key) or {}).get("type")
        if isinstance(value, str) and ptype in ("integer", "number"):
            number = _parse_number(value, integer=(ptype == "integer"))
            if number is not None:
                if coerced is None:
                    coerced = dict(args)
                coerced[key] = number
    return coerced if coerced is not None else args


def _int_arg(args: Dict, key: str, default: int) -> int:
    """An integer argument the ladder computes with (k, count): a number or a
    numeric string; anything else falls back to `default`."""
    value = args.get(key, default)
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        number = _parse_number(value, integer=True)
        if number is not None:
            return number
    return default


# =============================================================================
# Memory tools
# =============================================================================

async def execute_search_snapshots(session, arguments: Dict, *, log_prefix: str) -> str:
    """search_snapshots: hybrid retrieval, at most five snapshots, delivered whole."""
    query = arguments.get("query", "")
    k = min(_int_arg(arguments, "k", 3), 5)  # Cap at 5 results

    if not query:
        return "Error: No search query provided."

    try:
        # Use hybrid retrieval for best results. It embeds, reranks and reads
        # the ledger, so it runs off the event loop every voice session shares
        # (its vol_txt argument is unused: no 58 MB volume read for it here).
        results = await run_blocking(hybrid_retrieve, "", query, k=k, operator=session.operator)

        if not results:
            return f"No snapshots found matching: {query}"

        # Format results
        output_parts = [f"Found {len(results)} relevant snapshot(s) for: {query}\n"]
        for i, snap_text in enumerate(results, 1):
            # WI-10 (M7): deliver retrieved snapshots WHOLE — no delivery truncation
            output_parts.append(f"--- Result {i} ---\n{format_snapshot_for_delivery(snap_text)}")

        return "\n\n".join(output_parts)

    except Exception as e:
        print(f"{log_prefix} Search error: {e}")
        return f"Search failed: {str(e)}"


async def _recent_snapshots(session, args: Dict, log_prefix: str) -> str:
    # The declared ToolVault name is list_recent_snapshots; the legacy
    # spelling (get_recent_snapshots -> list_recent_snapshots) stays a dispatch
    # alias, so this specialized handler (system-sees-all scoping for
    # outbound-call context handoff) serves both instead of the catch-all.
    count = min(_int_arg(args, "count", 3), 5)
    operator = session.operator or "system"

    # System operator sees ALL snapshots (for outbound calls/context handoff)
    # Regular operators only see their own snapshots (no context bleed)
    see_all = (operator == "system")

    print(f"{log_prefix} Getting {count} recent snapshots for {operator} (see_all={see_all})")
    try:
        from Orchestrator.fossils import load_snapshot_index, read_volume_bytes
        # The index parse and the whole-volume read block: off the event loop.
        index = await run_blocking(load_snapshot_index)

        if not index:
            return "No snapshots found in index."

        # Sort all snapshots by ID (date + sequence)
        def snapshot_sort_key(snap_id: str) -> tuple:
            try:
                parts = snap_id.split('-')
                date_part = int(parts[1]) if len(parts) > 1 else 0
                seq_part = int(parts[2]) if len(parts) > 2 else 0
                return (date_part, seq_part)
            except (ValueError, IndexError):
                return (0, 0)

        # Filter by operator unless system (which sees all)
        if see_all:
            matching = list(index.items())
        else:
            matching = [(sid, meta) for sid, meta in index.items()
                        if meta.get("operator") == operator]

        matching.sort(key=lambda x: snapshot_sort_key(x[0]), reverse=True)

        # Get the most recent N
        recent = matching[:count]

        if not recent:
            return f"No recent snapshots found for operator '{operator}'."

        vol_bytes = await run_blocking(read_volume_bytes, VOL_PATH)
        scope_desc = "all operators" if see_all else f"operator: {operator}"
        result_parts = [f"Found {len(recent)} recent snapshot(s) ({scope_desc}):\n"]

        for snap_id, meta in recent:
            start = meta.get("byte_start", 0)
            end = meta.get("byte_end", start + 5000)
            snap_bytes = vol_bytes[start:end]
            snap_text = snap_bytes.decode('utf-8', errors='replace')
            operator_name = meta.get("operator", "unknown")

            # WI-10 (M7): deliver recent snapshots WHOLE — no delivery truncation
            result_parts.append(f"--- {snap_id} (operator: {operator_name}) ---\n{snap_text}")

        print(f"{log_prefix} Retrieved {len(recent)} recent snapshots")
        return "\n\n".join(result_parts)
    except Exception as e:
        print(f"{log_prefix} Error getting recent snapshots: {e}")
        return f"Error retrieving recent snapshots: {str(e)}"


# =============================================================================
# Media generators (they notify the client of the task they start)
# =============================================================================

async def _notify(session, frame: Dict) -> None:
    if session.portal_ws:
        await safe_send(session.portal_ws, frame)


async def _generate_image(session, name: str, args: Dict, log_prefix: str) -> str:
    provider = IMAGE_TOOL_PROVIDERS[name]
    prompt = args.get("prompt", "")
    aspect_ratio = args.get("aspectRatio", "16:9")
    resolution = args.get("resolution", "1K")
    num_images = args.get("numberOfImages", 1)
    reference_images = args.get("reference_images", [])  # For image-to-image

    mode = "image-to-image" if reference_images else "text-to-image"
    print(f"{log_prefix} Executing image generation ({mode}): {prompt[:100]}... ({num_images} @ {resolution})")

    image_options = {
        "aspectRatio": aspect_ratio,
        "resolution": resolution,
        "numberOfImages": num_images,
        "provider": provider,
    }
    if args.get("size"):  # openai gpt-image / local z-image size
        image_options["size"] = args["size"]
    if reference_images:
        image_options["reference_images"] = reference_images

    task = create_task(
        TaskType.IMAGE_GENERATION,
        operator=session.operator or "system",
        prompt=prompt,
        result_data={"options": image_options}
    )

    img_count = num_images
    ref_desc = f" (using {len(reference_images)} reference image{'s' if len(reference_images) > 1 else ''})" if reference_images else ""
    result = f"Image generation started ({mode}){ref_desc}: {prompt[:100]}{'...' if len(prompt) > 100 else ''}. Creating {img_count} image{'s' if img_count > 1 else ''} at {resolution}."

    await _notify(session, {
        "type": "image_task",
        "data": {"task_id": task.task_id, "prompt": prompt, "count": num_images}
    })
    print(f"{log_prefix} Image generation task created: {task.task_id}")
    return result


async def _generate_video(session, args: Dict, log_prefix: str) -> str:
    prompt = args.get("prompt", "")
    aspect_ratio = args.get("aspectRatio", "16:9")
    duration = args.get("duration", 8)
    resolution = args.get("resolution", "720p")
    negative_prompt = args.get("negativePrompt", "")
    image_url = args.get("image_url")  # For image-to-video
    video_url = args.get("video_url")  # For video extension

    mode = "text-to-video"
    if image_url:
        mode = "image-to-video"
    elif video_url:
        mode = "video-extension"

    print(f"{log_prefix} Executing video generation ({mode}): {prompt[:100]}... ({duration}s @ {resolution})")

    video_options = {
        "aspectRatio": aspect_ratio,
        "duration": duration,
        "resolution": resolution,
    }
    if negative_prompt:
        video_options["negativePrompt"] = negative_prompt
    if image_url:
        video_options["image_url"] = image_url
    if video_url:
        video_options["video_url"] = video_url

    task = create_task(
        TaskType.VIDEO_GENERATION,
        operator=session.operator or "system",
        prompt=prompt,
        result_data={"options": video_options}
    )

    mode_desc = f" (animating image: {image_url})" if image_url else (f" (extending video: {video_url})" if video_url else "")
    result = f"Video generation started ({mode}){mode_desc}: {prompt[:100]}{'...' if len(prompt) > 100 else ''}. Creating {duration}s video at {resolution}. Takes 5-20 minutes."

    await _notify(session, {
        "type": "video_task",
        "data": {"task_id": task.task_id, "prompt": prompt, "duration": duration, "resolution": resolution}
    })
    print(f"{log_prefix} Video generation task created: {task.task_id}")
    return result


async def _generate_music(session, args: Dict, log_prefix: str) -> str:
    prompt = args.get("prompt", "")
    negative_prompt = args.get("negativePrompt", "")
    sample_count = args.get("sampleCount", 1)
    print(f"{log_prefix} Executing music generation: {prompt[:100]}... ({sample_count} variation(s))")

    music_options = {
        "prompt": prompt,
        "operator": session.operator or "system",
    }
    if negative_prompt:
        music_options["negative_prompt"] = negative_prompt
    if sample_count and sample_count > 1:
        music_options["sample_count"] = sample_count

    task = create_task(
        TaskType.LYRIA_MUSIC,
        operator=session.operator or "system",
        prompt=prompt,
        result_data=music_options
    )

    variations_text = f" ({sample_count} variations)" if sample_count > 1 else ""
    result = f"Music generation started for: {prompt[:100]}{'...' if len(prompt) > 100 else ''}{variations_text}. 30-second track will be ready in 20-60 seconds."

    await _notify(session, {
        "type": "music_task",
        "data": {"task_id": task.task_id, "prompt": prompt, "sample_count": sample_count}
    })
    print(f"{log_prefix} Music generation task created: {task.task_id}")
    return result


# =============================================================================
# The ladder
# =============================================================================

# Executor tools whose answer carries the executor's structured data
# (ToolResult.rich_result) and tools answered with its plain text.
_RICH_EXECUTOR_TOOLS = ("create_cron_job", "edit_cron_job", "search_cron_jobs")
_PLAIN_EXECUTOR_TOOLS = ("use_computer", "list_devices", "control_android_device")


def _executor(session):
    from Orchestrator.tools import BlackBoxToolExecutor
    return BlackBoxToolExecutor(operator=session.operator or "system")


async def execute_voice_tool(session, name: str, args: Dict, *, log_prefix: str,
                             created_by: str) -> str:
    """Run ONE voice tool call and return the text the model is answered with.

    Runs as a task tracked on the session (voice_ws_shared.dispatch_voice_tool),
    never inside a listener, which used to sit on a tool for its whole run.
    `created_by` labels the contacts this session saves.
    """
    args = coerce_voice_args(name, args)

    if name == "search_snapshots":
        return await execute_search_snapshots(session, args, log_prefix=log_prefix)
    if name in ("get_recent_snapshots", "list_recent_snapshots"):
        return await _recent_snapshots(session, args, log_prefix)
    if name == "web_fetch":
        url = args.get("url", "")
        max_chars = args.get("max_chars", 80000)
        print(f"{log_prefix} Executing web fetch: {url}")
        # Off the event loop: a fetch can take its whole time budget.
        result = await run_blocking(perform_web_fetch, url, max_chars)
        print(f"{log_prefix} Web fetch result length: {len(result)} chars")
        return result
    if name in IMAGE_TOOL_PROVIDERS:
        return await _generate_image(session, name, args, log_prefix)
    if name == "generate_video":
        return await _generate_video(session, args, log_prefix)
    if name == "lyria_music":
        return await _generate_music(session, args, log_prefix)
    if name == "get_media":
        from Orchestrator.routes.chat_routes import execute_get_media
        url = args.get("url")
        task_id_param = args.get("task_id")
        print(f"{log_prefix} Executing get_media: url={url}, task_id={task_id_param}")
        media_result = await run_blocking(execute_get_media, url=url, task_id=task_id_param)
        return json.dumps(media_result, indent=2)
    if name == "list_media":
        from Orchestrator.routes.chat_routes import execute_list_media
        media_type = args.get("media_type")
        limit = args.get("limit", 20)
        print(f"{log_prefix} Executing list_media: type={media_type}, limit={limit}")
        list_result = await run_blocking(execute_list_media, media_type=media_type, limit=limit)
        return json.dumps(list_result, indent=2)
    if name == "search_media":
        from Orchestrator.routes.chat_routes import execute_search_media
        query = args.get("query", "")
        media_type = args.get("media_type")
        limit = args.get("limit", 10)
        print(f"{log_prefix} Executing search_media: query='{query}', type={media_type}")
        search_result = await run_blocking(execute_search_media, query=query, media_type=media_type, limit=limit)
        return json.dumps(search_result, indent=2)
    if name == "send_sms":
        phone_number = args.get("phone_number", "")
        message = args.get("message", "")
        print(f"{log_prefix} Executing send_sms to {phone_number}: {message[:50]}...")
        tool_result = await _executor(session).execute(
            "send_sms", {"phone_number": phone_number, "message": message})
        result = tool_result.rich_result()
        print(f"{log_prefix} SMS result: {result}")
        return result
    if name == "make_phone_call":
        # Outbound call through the unified tool executor: the box's call route
        # (Asterisk/TG200 or Twilio), an E.164 number, and a 200 {"error": ...}
        # counted as a failure (Orchestrator.telephony).
        phone_number = args.get("phone_number", "")
        print(f"{log_prefix} Executing make_phone_call to {phone_number}")
        tool_result = await _executor(session).execute("make_phone_call", {
            "phone_number": phone_number,
            "greeting": args.get("greeting", ""),
            "role": args.get("role", ""),
            "backend": args.get("backend", "openai_realtime"),
        })
        result = tool_result.rich_result()
        print(f"{log_prefix} Call result: {result}")
        return result
    if name == "make_voice_call":
        # Call with a pre-generated TTS message
        phone_number = args.get("phone_number", "")
        message = args.get("message", "")
        voice = args.get("voice", "onyx")
        print(f"{log_prefix} Executing make_voice_call to {phone_number}: {message[:50]}...")
        tool_result = await _executor(session).execute("make_voice_call", {
            "phone_number": phone_number,
            "message": message,
            "voice": voice
        })
        result = tool_result.rich_result()
        print(f"{log_prefix} Voice call result: {result}")
        return result
    if name == "search_contacts":
        from Orchestrator.contacts import search_contacts
        query = args.get("query", "")
        operator = session.operator or "system"
        print(f"{log_prefix} Executing search_contacts: query='{query}', operator={operator}")
        contacts = search_contacts(query, operator)
        print(f"{log_prefix} search_contacts result: {len(contacts)} contacts found")
        if contacts:
            return json.dumps(contacts, indent=2)
        return f"No contacts found matching '{query}'."
    if name == "save_contact":
        from Orchestrator.contacts import upsert_contact
        operator = session.operator or "system"
        contact_name = args.get("name", "")
        print(f"{log_prefix} Executing save_contact: name='{contact_name}', operator={operator}")
        saved = upsert_contact(
            name=contact_name, notes=args.get("notes", ""), tags=args.get("tags", []),
            operator=operator, created_by=created_by,
            phone=args.get("phone"), email=args.get("email"),
            relationship=args.get("relationship")
        )
        print(f"{log_prefix} save_contact result: saved '{contact_name}'")
        return f"Contact saved: {json.dumps(saved, indent=2)}"
    if name in _RICH_EXECUTOR_TOOLS:
        tool_result = await _executor(session).execute(name, args)
        result = tool_result.rich_result()
        print(f"{log_prefix} {name} result: {result}")
        return result
    if name in _PLAIN_EXECUTOR_TOOLS:
        tool_result = await _executor(session).execute(name, args)
        result = tool_result.result
        print(f"{log_prefix} {name}: {result[:100]}")
        return result
    if name == "get_current_time":
        from datetime import datetime
        now = datetime.now()
        result = f"Current date and time: {now.strftime('%A, %B %d, %Y at %I:%M %p')}"
        print(f"{log_prefix} get_current_time: {result}")
        return result

    # Catch-all: route ANY other tool (incl. the per-provider web search tools
    # and dynamically-injected ToolVault tools) through BlackBoxToolExecutor
    # instead of reporting "Unknown tool".
    tool_result = await _executor(session).execute(name, args)
    result = tool_result.result if hasattr(tool_result, 'result') else str(tool_result)
    if not result:
        result = f"Tool '{name}' executed successfully (no output)."
    print(f"{log_prefix} {name} (catch-all): {result[:100]}")
    return result


# =============================================================================
# Tool-call wire formats
# =============================================================================

def _live_upstream(session, ws_attr: str, upstream):
    """The socket a call came on, while it is still the session's upstream."""
    return upstream if (upstream is not None and getattr(session, ws_attr, None) is upstream) else None


async def dispatch_openai_style_call(session, event: Dict, *, ws_attr: str, log_prefix: str,
                                     execute: Callable[[str, Dict], Any]) -> None:
    """Handle response.function_call_arguments.done (OpenAI Realtime and xAI).

    Malformed arguments are answered with a parse error instead of running the
    tool with {} (which masked the real cause). The call itself runs OFF the
    listener, so the listener keeps draining provider events while the tool
    works (audit O08-stt-realtime-02); its answer goes back on the socket the
    call came from, never on a reconnected one that has no such call id.
    ``execute(name, arguments)`` runs the tool.
    """
    call_id = event.get("call_id", "")
    name = event.get("name", "")
    arguments_str = event.get("arguments", "{}")

    try:
        arguments = json.loads(arguments_str)
    except json.JSONDecodeError as parse_err:
        print(f"{log_prefix} Malformed tool arguments for {name}: {parse_err}")
        await send_openai_style_tool_error(
            getattr(session, ws_attr, None), session.portal_ws, event,
            ValueError(f"Malformed tool-call arguments JSON: {parse_err}. "
                       f"Raw arguments: {arguments_str[:200]}"),
        )
        return

    print(f"{log_prefix} Tool call: {name} with args: {arguments}")

    if session.portal_ws:
        await safe_send(session.portal_ws, {
            "type": "tool_call",
            "data": {"name": name, "arguments": arguments}
        })

    upstream = getattr(session, ws_attr, None)
    dispatch_voice_tool(
        session,
        lambda: execute(name, arguments),
        lambda result: deliver_openai_style_result(session, ws_attr, upstream, call_id, name,
                                                   result, log_prefix=log_prefix),
        lambda exc: send_openai_style_tool_error(_live_upstream(session, ws_attr, upstream),
                                                 session.portal_ws, event, exc),
        label=f"{log_prefix} {name} (call_id={call_id})",
        call_id=call_id,
    )


async def deliver_openai_style_result(session, ws_attr: str, upstream, call_id: str, name: str,
                                      result: str, *, log_prefix: str) -> None:
    """Answer the model with a tool result, on the upstream the call came from."""
    if _live_upstream(session, ws_attr, upstream) is not None:
        print(f"{log_prefix} Sending tool response for {name}, call_id: {call_id}")
        await upstream.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": result
            }
        }))
        # Tool results don't trigger a response by themselves: ask for one.
        await upstream.send(json.dumps({"type": "response.create"}))
    else:
        print(f"{log_prefix} Dropping the {name} result (call_id: {call_id}): the upstream "
              f"it was called on is gone")

    if session.portal_ws:
        await safe_send(session.portal_ws, {
            "type": "tool_result",
            "data": {"name": name, "result_length": len(result)}
        })


async def dispatch_gemini_calls(session, event: Dict, *, log_prefix: str,
                                execute: Callable[[str, Dict], Any]) -> None:
    """Handle a Gemini toolCall: every functionCall runs as its own session
    task, answered on the socket it came from with a toolResponse."""
    function_calls = event["toolCall"].get("functionCalls", [])

    # The client may use this to discard the reply it saw before the call.
    session.pending_tool_call = True
    print(f"{log_prefix} Tool call detected")

    # Per-event answered-id tracking: the listener-level error responder uses
    # it to answer ONLY dangling ids after a mid-loop crash.
    session.answered_tool_call_ids = set()
    upstream = session.gemini_ws

    for fc in function_calls:
        call_id = fc.get("id", "")
        name = fc.get("name", "")
        args = fc.get("args", {})

        print(f"{log_prefix} Tool call: {name} with args: {args}")

        if session.portal_ws:
            await safe_send(session.portal_ws, {
                "type": "tool_call",
                "data": {"name": name, "arguments": args}
            })

        # A dispatched id counts as answered for the listener-level error
        # responder: its task always answers it, with the result or an error.
        session.answered_tool_call_ids.add(call_id)
        dispatch_voice_tool(
            session,
            lambda name=name, args=args: execute(name, args),
            lambda result, call_id=call_id, name=name: deliver_gemini_result(
                session, upstream, call_id, name, result, log_prefix=log_prefix),
            lambda exc, fc=fc: send_gemini_tool_error(
                _live_upstream(session, "gemini_ws", upstream), session.portal_ws,
                {"toolCall": {"functionCalls": [fc]}}, exc),
            label=f"{log_prefix} {name} (id={call_id})",
            call_id=call_id,
        )


async def deliver_gemini_result(session, upstream, call_id: str, name: str, result: str, *,
                                log_prefix: str) -> None:
    """Answer Gemini with a functionResponse, on the upstream the call came from."""
    if _live_upstream(session, "gemini_ws", upstream) is not None:
        await upstream.send(json.dumps({
            "toolResponse": {
                "functionResponses": [{
                    "id": call_id,
                    "name": name,
                    "response": {"result": result}
                }]
            }
        }))
        print(f"{log_prefix} Sent tool response for {name}: {len(result)} chars")
    else:
        print(f"{log_prefix} Dropping the {name} result (id: {call_id}): the upstream "
              f"it was called on is gone")

    if session.portal_ws:
        await safe_send(session.portal_ws, {
            "type": "tool_result",
            "data": {"name": name, "result_length": len(result)}
        })
