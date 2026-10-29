"""The Gemini generateContent chat adapter: call() and stream() for Gemini.

Audit rank 74 (X05-architecture-11). call_gemini and stream_gemini_with_thinking
lived in routes/chat_routes.py with their own copies of the tool table; they
now live here, over the shared dispatch_tool() (providers/tool_dispatch.py),
with the thinking config they share. The chat_routes names stay as thin
wrappers.

Transport: raw REST generateContent / streamGenerateContent?alt=sse with the
key in the x-goog-api-key header, not the google-genai SDK (2.2): the recorded
contract (tests/golden/providers) is these exact requests, and the model turn
is replayed with its parts verbatim (thoughtSignature included) as JSON.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Dict, List

import httpx
import requests
from fastapi import HTTPException

from Orchestrator import provider_billing
from Orchestrator.providers.common import DeclinedReply, ProviderEnv, _ATTACHMENT_ERROR_NOTE, _last_user_msg
from Orchestrator.providers.tool_dispatch import _record_media_task, dispatch_tool, dispatch_tool_blocking
from Orchestrator.secret_redaction import error_message_from_body, redact, response_error
from Orchestrator.utils.paths import MEDIA_EXTS


def _thinking_config(model: str) -> Dict:
    """The generationConfig thinking fields for ``model`` (both paths).

    Gemini 3 uses thinkingLevel (low/high; thinking cannot be disabled on
    Gemini 3 Pro), Gemini 2.5 uses thinkingBudget (-1 = dynamic, 0 = disable on
    Flash only, or a token count). Both return thought summaries."""
    model_lower = model.lower()
    if "gemini-3" in model_lower:
        return {
            "thinkingConfig": {
                "thinkingLevel": "HIGH",  # Maximum reasoning depth
                "includeThoughts": True   # Return thought summaries
            }
        }
    if "gemini-2.5" in model_lower:
        return {
            "thinkingConfig": {
                "thinkingBudget": -1,     # Dynamic thinking
                "includeThoughts": True   # Return thought summaries
            }
        }
    return {}


# Inline-data MIME types by upload suffix (Gemini requires audio/aac for M4A).
_MIME_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    ".mp4": "video/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/aac",
    ".ogg": "audio/ogg", ".flac": "audio/flac",
    ".pdf": "application/pdf", ".txt": "text/plain",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _encode_parts(content: List[Dict], env: ProviderEnv, tag: str) -> List[Dict]:
    """A message's parts as Gemini parts, the same on both paths (audit rank
    74: the stream's copy dropped a base64 image and typed .webp as
    application/octet-stream). Text stays text; a base64 image (the Android
    app sends the Anthropic format) and every image, video, audio or document
    upload become inline data; an upload refused for its size or type is the
    refusal note."""
    parts = []
    for part in content:
        if part.get("type") == "text":
            parts.append({"text": part["text"]})
        elif part.get("type") == "image" and part.get("source", {}).get("type") == "base64":
            source = part.get("source", {})
            media_type = source.get("media_type", "image/png")
            data = source.get("data", "")
            if data:
                print(f"{tag} Processing direct base64 image ({len(data)} chars, {media_type})")
                parts.append({"inline_data": {"mime_type": media_type, "data": data}})
        elif part.get("type") in ("image_url", "video_url", "audio_url", "document_url"):
            media_key = part.get("type")
            url = part[media_key]["url"]
            try:
                local_path, refused = env.load_attachment(url, MEDIA_EXTS, env.inline_max_bytes)
                if refused:
                    parts.append({"text": refused})
                if local_path is not None:
                    media_bytes = local_path.read_bytes()
                    mime_type = _MIME_TYPES.get(local_path.suffix.lower(), "application/octet-stream")
                    parts.append({
                        "inline_data": {"mime_type": mime_type, "data": base64.b64encode(media_bytes).decode('utf-8')}
                    })
            except Exception as e:
                print(f"{tag} Could not process media for Gemini: {e}")
                parts.append({"text": _ATTACHMENT_ERROR_NOTE})
    return parts


def call(messages: List[Dict], model: str, operator: str, env: ProviderEnv):
    if not env.api_key: raise HTTPException(400, "GOOGLE_API_KEY not set")

    system_instruction = {"parts": []}
    system_text_parts = [m.get("content","") for m in messages if m.get("role") == "system"]
    if system_text_parts:
        system_instruction["parts"].append({"text": "\n".join(system_text_parts)})

    contents = []
    for m in messages:
        if m.get("role") in ("user", "assistant"):
            role = "model" if m.get("role") == "assistant" else "user"
            content_parts = []
            content = m.get("content", "")

            if isinstance(content, list): # Handle multimodal content
                content_parts.extend(_encode_parts(content, env, "[GEMINI]"))
            else: # Text-only messages
                content_parts.append({"text": str(content)})

            contents.append({"role": role, "parts": content_parts})

    thinking_config = _thinking_config(model)

    payload = {
        "contents": contents,
        "generationConfig": {
            **thinking_config
        },
        "tools": [{
            "google_search": {}  # Enable Google Search grounding
        }]
    }
    if system_instruction["parts"]:
        payload["system_instruction"] = system_instruction

    # Add web, image, video, music generation, snapshot search, and communication tools
    payload["tools"] = env.get_tools("gemini", _last_user_msg(messages))

    # Tool calling loop
    max_tool_calls = 30
    parts = []
    media_tasks = []  # 2-media: created (task_id,type,prompt) for the worker
    for iteration in range(max_tool_calls):
        # Key in the x-goog-api-key header, never the URL: requests copies the URL
        # into its exception text, which reached journald and tasks.db rows.
        url = f"{env.url}/{model}:generateContent"
        r = requests.post(url, headers={"x-goog-api-key": env.api_key}, json=payload, timeout=200)

        if r.status_code != 200:
            provider_billing.raise_if_exhausted("google", r.status_code, r.text, surface="chat")
            status, message = response_error(r)
            raise HTTPException(status, f"Gemini API error: {message}")
        provider_billing.note_ok("google")
        data = r.json()

        if not data.get("candidates"):
            # Check for safety ratings
            prompt_feedback = data.get("promptFeedback", {})
            if prompt_feedback.get("blockReason"):
                return DeclinedReply(f"(The model blocked the prompt. Reason: {prompt_feedback.get('blockReason')})"), {}, [], "", media_tasks
            return DeclinedReply("(The model did not provide a response. This may be due to the safety policy.)"), {}, [], "", media_tasks

        # Extract ALL parts from response (text + inline media)
        parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])

        # Check for function calls
        function_calls = [p for p in parts if "functionCall" in p]

        if function_calls:
            # Add model's response to conversation
            payload["contents"].append({
                "role": "model",
                "parts": parts
            })

            # Execute each function call
            function_responses = []
            for fc in function_calls:
                func_name = fc["functionCall"]["name"]
                func_args = fc["functionCall"].get("args", {})

                outcome = dispatch_tool_blocking(func_name, func_args, env.tool_context(operator, "call_gemini"))
                result = outcome.result
                if outcome.media_task:
                    _record_media_task(media_tasks, *outcome.media_task)

                function_responses.append({
                    "functionResponse": {
                        "name": func_name,
                        "response": {"result": result}
                    }
                })

            # Add function responses to conversation
            payload["contents"].append({
                "role": "user",
                "parts": function_responses
            })

            print(f"[GEMINI] Tool iteration {iteration + 1}, continuing...")
        else:
            # No more function calls, break out of loop
            break

    text_parts = []
    media_parts = []
    reasoning_parts = []

    for part in parts:
        # Extract text. Gemini marks thought-summary parts with `thought: true`
        # (alongside `text`); route those into reasoning so chain-of-thought never
        # leaks into the answer (2-reasoning). Only non-thought text is the answer.
        if "text" in part:
            if part.get("thought") is True:
                reasoning_parts.append(part["text"])
            else:
                text_parts.append(part["text"])

        # Extract inline media (images, audio, etc. generated by Gemini 3)
        elif "inlineData" in part or "inline_data" in part:
            inline_data = part.get("inlineData") or part.get("inline_data")
            if inline_data:
                media_parts.append({
                    "mime_type": inline_data.get("mimeType") or inline_data.get("mime_type"),
                    "data": inline_data.get("data")
                })

    # Combine all text parts (answer only) and thought summaries (reasoning).
    text = "\n".join(text_parts) if text_parts else ""
    reasoning = "\n".join(reasoning_parts) if reasoning_parts else ""

    # Usage metadata
    um = data.get("usageMetadata", {})
    usage = {
        "prompt_tokens": um.get("promptTokenCount", 0),
        "completion_tokens": um.get("candidatesTokenCount", 0),
        "total_tokens": um.get("totalTokenCount", um.get("promptTokenCount", 0) + um.get("candidatesTokenCount", 0))
    }

    return text, usage, media_parts, reasoning, media_tasks


async def stream(messages: List[Dict], model: str, operator: str, env: ProviderEnv):
    """Stream Google Gemini responses with thinking mode and tool support.

    Yields SSE events: {"type": "thinking"|"content"|"tool_start"|"tool_result"|"done"|"error", "data": "..."}
    """
    if not env.api_key:
        yield {"type": "error", "data": "GOOGLE_API_KEY not set"}
        return

    # Build Gemini-format messages
    system_instruction = {"parts": []}
    system_text_parts = [m.get("content", "") for m in messages if m.get("role") == "system"]
    if system_text_parts:
        system_instruction["parts"].append({"text": "\n".join(system_text_parts)})

    contents = []
    for m in messages:
        if m.get("role") in ("user", "assistant"):
            role = "model" if m.get("role") == "assistant" else "user"
            content_parts = []
            content = m.get("content", "")

            if isinstance(content, list):
                content_parts.extend(_encode_parts(content, env, "[GEMINI-STREAM]"))
            else:
                content_parts.append({"text": str(content)})

            contents.append({"role": role, "parts": content_parts})

    thinking_config = _thinking_config(model)

    payload = {
        "contents": contents,
        "generationConfig": {
            **thinking_config
        },
        "tools": await asyncio.to_thread(env.get_tools, "gemini", _last_user_msg(messages))
    }
    if system_instruction["parts"]:
        payload["system_instruction"] = system_instruction

    url = f"{env.url}/{model}:streamGenerateContent?alt=sse"
    gemini_headers = {"x-goog-api-key": env.api_key}

    # Tool execution loop (bounded by max_tool_iterations below)
    # Buffers persist across iterations so thinking from round 1 isn't lost
    thinking_buffer = ""
    content_buffer = ""
    max_tool_iterations = 30
    for iteration in range(max_tool_iterations):
        try:
            print(f"[GEMINI-STREAM] Starting iteration {iteration}, contents={len(payload.get('contents',[]))} messages", flush=True)
            if iteration > 0:
                # Dump contents structure for debugging
                for ci, c in enumerate(payload.get("contents", [])):
                    role = c.get("role", "?")
                    part_desc = []
                    for p in c.get("parts", []):
                        pk = list(p.keys())
                        if "text" in p:
                            part_desc.append(f"text({len(p['text'])})")
                        elif "functionCall" in p:
                            part_desc.append(f"funcCall({p['functionCall'].get('name','')})")
                        elif "functionResponse" in p:
                            part_desc.append(f"funcResp({p['functionResponse'].get('name','')})")
                        elif "thoughtSignature" in p:
                            part_desc.append("thoughtSig")
                        else:
                            part_desc.append(str(pk))
                    print(f"[GEMINI-STREAM]   [{ci}] {role}: {part_desc}", flush=True)
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream("POST", url, headers=gemini_headers, json=payload) as response:
                    print(f"[GEMINI-STREAM] Iter {iteration} response status: {response.status_code}", flush=True)
                    if response.status_code != 200:
                        # Status + Google's parsed message only, never the raw body.
                        error_text = await response.aread()
                        error_msg = redact(error_message_from_body(error_text) or "no error message")[:500]
                        print(f"[GEMINI-STREAM] API error {response.status_code} (iter {iteration}): {error_msg}", flush=True)
                        billing = provider_billing.check("google", response.status_code, error_text, surface="chat")
                        yield (billing.event() if billing else
                               {"type": "error", "data": f"Gemini API error ({response.status_code}): {error_msg}"})
                        # If we have content from previous iterations, still yield done
                        if content_buffer or thinking_buffer:
                            yield {"type": "done", "data": {"thinking": thinking_buffer, "content": content_buffer}}
                        return
                    provider_billing.note_ok("google")

                    response_parts = []
                    function_calls = []

                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue

                        data_str = line[6:]
                        try:
                            chunk = json.loads(data_str)
                            candidates = chunk.get("candidates", [])

                            for candidate in candidates:
                                content = candidate.get("content", {})
                                parts = content.get("parts", [])

                                for part in parts:
                                    # Collect all parts for potential tool execution
                                    response_parts.append(part)

                                    # Log every part for debugging
                                    part_keys = list(part.keys())
                                    if iteration > 0:
                                        print(f"[GEMINI-STREAM] Iter {iteration} part: {part_keys}, text={part.get('text','')[:80] if 'text' in part else 'N/A'}, thought={part.get('thought','')}", flush=True)

                                    # Handle thinking/thought content
                                    if part.get("thought"):
                                        thought_text = part.get("text", "")
                                        if thought_text:
                                            thinking_buffer += thought_text
                                            yield {"type": "thinking", "data": thought_text}

                                    # Handle regular text content
                                    elif "text" in part and not part.get("thoughtSignature"):
                                        text = part["text"]
                                        if text:
                                            content_buffer += text
                                            yield {"type": "content", "data": text}

                                    # Handle function calls
                                    elif "functionCall" in part:
                                        fc = part["functionCall"]
                                        function_calls.append(fc)
                                        yield {"type": "tool_start", "data": fc.get("name", "")}

                            # Check for usage metadata
                            usage_metadata = chunk.get("usageMetadata", {})
                            if usage_metadata:
                                yield {"type": "usage", "data": {
                                    "prompt_tokens": usage_metadata.get("promptTokenCount", 0),
                                    "completion_tokens": usage_metadata.get("candidatesTokenCount", 0)
                                }}

                        except json.JSONDecodeError:
                            continue

                    # Check if we need to execute tools
                    if function_calls:
                        yield {"type": "tool_executing", "data": "Executing web tools..."}

                        # Add model's response to conversation (includes thoughtSignature for Gemini 3.1+)
                        has_sig = any("thoughtSignature" in p for p in response_parts)
                        print(f"[GEMINI-STREAM] Tool loop iter {iteration}: {len(function_calls)} calls, {len(response_parts)} parts, thoughtSignature={has_sig}", flush=True)
                        print(f"[GEMINI-STREAM] Part types: {[list(p.keys()) for p in response_parts]}", flush=True)
                        payload["contents"].append({
                            "role": "model",
                            "parts": response_parts
                        })

                        # Execute each function call
                        function_responses = []
                        for fc in function_calls:
                            func_name = fc.get("name", "")
                            func_args = fc.get("args", {})

                            outcome = await dispatch_tool(func_name, func_args, env.tool_context(operator, "stream_gemini"))
                            function_responses.append({
                                "functionResponse": {
                                    "name": func_name,
                                    "response": {"result": outcome.result}
                                }
                            })
                            for event in outcome.events:
                                yield event

                        # Add function responses to conversation
                        payload["contents"].append({
                            "role": "user",
                            "parts": function_responses
                        })

                        print(f"[GEMINI-STREAM] Tool iteration {iteration + 1}, continuing...", flush=True)
                        # Heartbeat to keep SSE connection alive during tool execution
                        yield {"type": "heartbeat", "data": ""}
                        # Continue to next iteration
                        continue
                    else:
                        # No function calls, we're done
                        yield {"type": "done", "data": {"thinking": thinking_buffer, "content": content_buffer}}
                        return

        except Exception as e:
            yield {"type": "error", "data": f"Stream error: {redact(str(e))}"}
            return

    # If we exhaust iterations
    yield {"type": "done", "data": {"thinking": thinking_buffer, "content": content_buffer}}
