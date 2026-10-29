"""The OpenAI-compatible chat adapter: OpenAI, xAI and custom LAN servers.

Audit rank 74 (X05-architecture-11). stream_openai_with_reasoning,
stream_xai_with_reasoning and stream_custom_with_reasoning were three copies of
one chat.completions stream loop, and call_openai / call_xai / call_custom
three copies of its non-stream twin. Each pair is now one loop here, driven by
a CompatProvider that names what really differs: the key or server the call
goes to, whether an image URL is the server's to fetch (attachments are
otherwise encoded one way, on both paths), the per-family payload knobs, the one
retry each family has (OpenAI's reasoning_effort 400, an old llama.cpp's
stream_options 400), how an HTTP error is reported (billing classification for
the clouds, the learned context window for a llama.cpp server) and the
non-stream return shape. The chat_routes names stay as thin wrappers.

Transport: the official SDKs (openai 2.41) are deliberately not used here.
These loops' recorded behaviour (tests/golden/providers) is raw
chat.completions over httpx / requests with no client-side retries; the SDK
would add its own retry/backoff and error types and change the upstream
requests. W17 (the Responses API) is decided the same way: chat.completions
stays the wire format for every OpenAI-compatible family, since xAI and
llama.cpp speak only it and the one OpenAI case that wants Responses (gpt-5.6
reasoning with tools) is served by reasoning_effort="none" today.
"""
from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import httpx
import requests
from fastapi import HTTPException

from Orchestrator import provider_billing
from Orchestrator.onboarding import custom_servers
from Orchestrator.providers.common import (
    ProviderEnv,
    _ATTACHMENT_ERROR_NOTE,
    _last_user_msg,
    _tool_cap_events,
    _usage_into,
)
from Orchestrator.providers.tool_dispatch import _record_media_task, dispatch_tool, dispatch_tool_blocking
from Orchestrator.utils.paths import IMAGE_EXTS

# Fields an OpenAI-compatible server may carry native reasoning in, in priority
# order (llama.cpp always fills reasoning_content).
_REASONING_FIELDS = ("reasoning_content", "reasoning", "thinking", "thought")


@dataclass(frozen=True)
class CompatProvider:
    name: str                 # openai | xai | custom (dispatch loop: stream_<name>, call_<name>)
    stream_tag: str           # stream log prefix
    tool_tag: str             # stream tool-iteration log prefix
    call_tag: str             # non-stream log prefix
    tools_provider: str       # _get_tools provider key
    key_name: str = ""        # the "<KEY> not set" name ("" = custom: server registry)
    billing: Optional[str] = None   # provider_billing name (None = no billing classification)
    error_label: str = ""     # "<label> API error ..." (custom: alias-prefixed)
    executing_label: str = "Executing tools..."
    cap_note: bool = False    # tool cap ends with the note + done (else a plain done)
    fetches_image_urls: bool = False  # an image URL that is not an upload is the server's to fetch


OPENAI = CompatProvider(
    name="openai", stream_tag="[OPENAI]", tool_tag="[OPENAI-STREAM]", call_tag="[OPENAI]",
    tools_provider="openai", key_name="OPENAI_API_KEY", billing="openai",
    error_label="OpenAI API error", executing_label="Executing web tools...", cap_note=True,
    fetches_image_urls=True)
XAI = CompatProvider(
    name="xai", stream_tag="[XAI]", tool_tag="[XAI-STREAM]", call_tag="[XAI]",
    tools_provider="grok", key_name="XAI_API_KEY", billing="xai", error_label="xAI API error")
CUSTOM = CompatProvider(
    name="custom", stream_tag="[CUSTOM]", tool_tag="[CUSTOM-STREAM]", call_tag="[CUSTOM-CALL]",
    tools_provider="custom")


# -----------------------------------------------------------------------------
# Custom servers: the learned context window
# -----------------------------------------------------------------------------
def _parse_exceed_context(body: str) -> int | None:
    """Extract a model's REAL context window from a llama.cpp over-window 400.

    llama.cpp rejects a too-long prompt with a 400 whose JSON body carries the
    model's true window:
      {"error": {"code": 400, "message": "request (18079 tokens) exceeds the
        available context size (16384 tokens), try increasing it",
        "type": "exceed_context_size_error", "n_prompt_tokens": 18079,
        "n_ctx": 16384}}
    Happens on multi-model hosts (llama-swap) where one server-wide
    context_tokens over-budgets a smaller model. Returns n_ctx as a positive
    int when the body parses to that shape; None otherwise (caller keeps
    today's raw-error behavior).
    """
    if not body:
        return None
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return None
    if err.get("type") != "exceed_context_size_error" and "n_ctx" not in err:
        return None
    n_ctx = err.get("n_ctx")
    if isinstance(n_ctx, bool) or not isinstance(n_ctx, int) or n_ctx <= 0:
        return None
    return n_ctx


def _learn_model_context(server: dict, bare_model: str, n_ctx: int) -> None:
    """Fail-soft persist a model's learned real window into the registry's
    model_context map so every subsequent window-guard computation fits it.
    A server that vanished between resolve and learn just doesn't learn —
    never masks the (friendly) error the caller is about to surface."""
    try:
        existing = server.get("model_context") or {}
        custom_servers.update_server(
            server["id"], {"model_context": {**existing, bare_model: n_ctx}})
        print(f"[CUSTOM] learned model_context: '{bare_model}' -> {n_ctx} tokens "
              f"on server '{server.get('alias', '')}'")
    except Exception as exc:
        print(f"[CUSTOM] could not persist learned context for '{bare_model}': {exc}")


def _exceed_context_reply(server: dict, bare_model: str, n_ctx: int) -> str:
    """Customer-facing replacement for the raw llama.cpp exceed-context JSON
    (shared by the stream + non-stream custom paths; raw body goes to the LOG)."""
    return (
        f"Model '{bare_model}' on server '{server.get('alias', '')}' has a "
        f"{n_ctx:,}-token context window (smaller than configured). I've learned "
        f"this limit — please resend your message; the context will now fit. "
        f"(To raise the window itself, increase -c for this model in the "
        f"llama-swap config on the server box.)"
    )


# -----------------------------------------------------------------------------
# Where a call goes
# -----------------------------------------------------------------------------
@dataclass
class _Target:
    url: str
    model: str                       # what the payload names
    api_key: str = ""
    server: Optional[dict] = None    # custom only
    bare_model: str = ""             # custom only


def _resolve(provider: CompatProvider, model: str, env: ProviderEnv) -> Tuple[Optional[_Target], str]:
    """(target, "") or (None, the error to report)."""
    if provider is not CUSTOM:
        if not env.api_key:
            return None, f"{provider.key_name} not set"
        return _Target(url=env.url, model=model, api_key=env.api_key), ""
    # Resolve the target server FRESH on every call (the registry is read from
    # disk; no import-time constants). The routes also resolve before context
    # building (Task 4.3); this is the backstop for direct callers.
    server, bare_model = custom_servers.resolve_model(model or "")
    if server is None:
        if custom_servers.list_servers(enabled_only=True):
            # Enabled servers exist -> the model was qualified with an alias
            # matching no ENABLED server (resolve_model fails fast by design).
            return None, custom_servers.MSG_UNKNOWN_ALIAS.format(model=model)
        return None, custom_servers.MSG_NO_SERVERS
    if not bare_model or bare_model.strip().lower() == "auto":
        last_models = server.get("last_models") or []
        if not last_models:
            return None, custom_servers.MSG_NO_MODELS.format(alias=server.get("alias", ""))
        bare_model = last_models[0]
    # base_url already ends in /v1 (normalized by the registry).
    return _Target(url=f"{server['base_url']}/chat/completions", model=bare_model,
                   api_key=server.get("api_key") or "", server=server, bare_model=bare_model), ""


def _headers(provider: CompatProvider, target: _Target, stream: bool) -> Dict[str, str]:
    if provider is CUSTOM:
        headers = {"Content-Type": "application/json"}
        # Keyless LAN servers exist — only send the bearer when a key is set.
        if target.api_key:
            headers["Authorization"] = f"Bearer {target.api_key}"
        return headers
    if stream:
        return {"Authorization": f"Bearer {target.api_key}", "Content-Type": "application/json"}
    return {"Authorization": f"Bearer {target.api_key}"}


def _tool_args(raw) -> Dict:
    """A tool call's arguments string as a dict: "" (a zero-argument tool, as
    llama.cpp and Grok send it), malformed JSON, or JSON that is not an object
    ("null", "[]") is {} — the tool then runs and answers for itself instead of
    the whole turn failing. A server that sends the object itself keeps it."""
    if isinstance(raw, dict):
        return raw
    try:
        args = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return args if isinstance(args, dict) else {}


def _reasoning_of(message: Dict) -> str:
    for field in _REASONING_FIELDS:
        val = message.get(field)
        if val:
            return val if isinstance(val, str) else str(val)
    return ""


# -----------------------------------------------------------------------------
# Attachments
# -----------------------------------------------------------------------------
_IMAGE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif"}


def _base64_image_part(part: Dict, tag: str, verb: str) -> Optional[Dict]:
    """An Android-style Anthropic base64 image as an OpenAI data-URL part."""
    source = part.get("source", {})
    media_type = source.get("media_type", "image/png")
    data = source.get("data", "")
    if not data:
        return None
    print(f"{tag} {verb} ({len(data)} chars, {media_type})")
    return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}


def _image_url_parts(provider: CompatProvider, part: Dict, env: ProviderEnv, tag: str) -> List[Dict]:
    """An image_url part as the server can read it: a data URL passes, an
    upload becomes a data URL, an upload refused for its size or type is the
    refusal note; any other URL is OpenAI's to fetch, and a note for xAI and
    the custom servers (as is a missing upload, a relative URL no server can
    fetch)."""
    ref = part.get("image_url")
    url = ref.get("url") if isinstance(ref, dict) else None
    url = url if isinstance(url, str) else ""
    print(f"{tag} Processing image_url: {url[:80]}...")
    if url.startswith("data:"):
        return [part]
    try:
        # /ui/uploads URLs or absolute paths INSIDE Portal/uploads only.
        local_path, refused = env.load_attachment(url, IMAGE_EXTS)
        if local_path is not None:
            mime_type = _IMAGE_MIME.get(local_path.suffix.lower(), "image/jpeg")
            base64_image = base64.b64encode(local_path.read_bytes()).decode("utf-8")
            print(f"{tag} Converted image to base64 ({len(base64_image)} chars, {mime_type})")
            return [{"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64_image}"}}]
    except Exception as e:
        print(f"{tag} ERROR processing image: {e}")
        return [{"type": "text", "text": _ATTACHMENT_ERROR_NOTE}]
    if refused:
        return [{"type": "text", "text": refused}]
    if provider.fetches_image_urls and url.startswith(("http://", "https://")):
        return [part]
    print(f"{tag} WARNING: image not loaded: {url[:80]}")
    return [{"type": "text", "text": "[Image could not be loaded]"}]


def _encode_messages(provider: CompatProvider, messages: List[Dict], env: ProviderEnv, tag: str) -> List[Dict]:
    """The chat.completions encoding of ``messages``, the same on the stream
    and the non-stream path of every family (audit rank 74: the two copies
    disagreed, and the OpenAI stream sent uploads and base64 images raw).

    Images become data URLs (_image_url_parts); audio, video and document
    parts become what the model can read (a plain-text document inlined) or a
    note, via env.unreadable_parts: forwarded raw they fail far-end."""
    processed_messages = []
    for m in messages:
        content = m.get("content")
        if m.get("role") not in ("user", "assistant", "system") or not isinstance(content, list):
            processed_messages.append(m)
            continue
        new_content = []
        for part in content:
            if not isinstance(part, dict):
                new_content.append(part)
            # Direct base64 images (the Android app sends the Anthropic format)
            elif part.get("type") == "image" and part.get("source", {}).get("type") == "base64":
                converted = _base64_image_part(part, tag, "Converting Anthropic-format base64 image")
                if converted:
                    new_content.append(converted)
            elif part.get("type") == "image_url":
                new_content.extend(_image_url_parts(provider, part, env, tag))
            else:
                stand_in = env.unreadable_parts(part)
                new_content.extend(stand_in if stand_in is not None else [part])
        processed_messages.append({**m, "content": new_content})
    return processed_messages


# -----------------------------------------------------------------------------
# The stream loop
# -----------------------------------------------------------------------------
def _stream_knobs(provider: CompatProvider, payload: Dict, model: str) -> None:
    """The per-family request knobs of a stream."""
    model_lower = model.lower()
    if provider is OPENAI:
        # O-series: o1, o1-mini, o1-pro, o3, o3-mini, o4-mini; GPT-5+: gpt-5, gpt-5.1, ...
        is_o_series = any(model_lower.startswith(x) or f"-{x}" in model_lower for x in ['o1', 'o3', 'o4']) and 'gpt' not in model_lower
        is_gpt5_plus = 'gpt-5' in model_lower or 'gpt5' in model_lower
        # Only O-series models support the "reasoning" parameter; GPT-5 streams
        # normally but does not expose its reasoning.
        if is_o_series:
            payload["reasoning"] = {"effort": "medium"}  # low, medium, high
            print(f"[OPENAI] Requesting reasoning for O-series model: {model}")
        elif is_gpt5_plus:
            print(f"[OPENAI] GPT-5 model: {model} - no reasoning parameter (not supported)")
        # gpt-5.6 chat/completions rejects function tools unless reasoning_effort
        # is explicitly 'none' (OpenAI applies a non-none server-side default);
        # reasoning+tools requires the /v1/responses API (W17: not migrated).
        if "gpt-5.6" in model_lower:
            payload["reasoning_effort"] = "none"
            print(f"[OPENAI] gpt-5.6 family ({model}): sending reasoning_effort='none' (function-tool compat)")
    elif provider is XAI:
        # Only grok-3-mini supports reasoning_effort and returns visible
        # reasoning_content; grok-4-*-reasoning reason internally.
        if "grok-3-mini" in model_lower:
            payload["reasoning_effort"] = "low"  # "low" or "high"
            print(f"[XAI] Requesting reasoning for grok-3-mini model: {model}")
        else:
            print(f"[XAI] Model {model} - no visible reasoning (only grok-3-mini exposes thinking)")
    # custom: no max_tokens — llama-server generates until EOS/ctx; reasoning
    # models need the headroom (INTEGRATION.md §6).


def _retry_once(provider: CompatProvider, status: int, body: str, payload: Dict) -> bool:
    """Apply the family's one compatibility retry to ``payload``; True to resend.

    OpenAI: a FIRST-attempt 400 that mentions reasoning_effort when none was
    sent means the server-side default rejected the function tools — retry
    with an explicit 'none' (self-heals families the gpt-5.6 gate does not yet
    name). Custom: an older llama.cpp build 400s on stream_options — drop it."""
    if status != 400:
        return False
    if provider is OPENAI and "reasoning_effort" in body and "reasoning_effort" not in payload:
        payload["reasoning_effort"] = "none"
        print("[OPENAI] 400 mentions reasoning_effort — retrying once with reasoning_effort='none'")
        return True
    if provider is CUSTOM and "stream_options" in body and "stream_options" in payload:
        payload.pop("stream_options", None)
        print("[CUSTOM] 400 mentions stream_options — retrying once without it")
        return True
    return False


def _stream_http_error(provider: CompatProvider, target: _Target, status: int, error_msg: str) -> Dict:
    """The error event for a non-200 stream response."""
    if provider is CUSTOM:
        # Over-window 400 (llama.cpp exceed_context_size_error): learn the
        # model's REAL window and surface a friendly error; the raw body stays
        # in the log. Checked after the stream_options retry (a 400 mentioning
        # stream_options is not a context error).
        n_ctx = _parse_exceed_context(error_msg) if status == 400 else None
        if n_ctx:
            _learn_model_context(target.server, target.bare_model, n_ctx)
            print(f"[CUSTOM] exceed_context 400 (model {target.bare_model}, "
                  f"n_ctx {n_ctx}): {error_msg}")
            return {"type": "error", "data": _exceed_context_reply(target.server, target.bare_model, n_ctx)}
        print(f"[CUSTOM] API Error: {error_msg}")
        # Alias-prefixed so multi-server fleets can tell WHICH box failed.
        return {"type": "error", "data": f"Custom server '{target.server.get('alias', '')}' API error ({status}): {error_msg}"}
    print(f"{provider.stream_tag} API Error: {error_msg}")
    billing = provider_billing.check(provider.billing, status, error_msg, surface="chat")
    if billing:
        return billing.event()
    return {"type": "error", "data": f"{provider.error_label} ({status}): {error_msg}"}


async def stream(provider: CompatProvider, messages: List[Dict], model: str, operator: str,
                 env: ProviderEnv):
    """One chat.completions stream turn with its tool loop.

    Yields SSE events: {"type": "thinking"|"content"|"tool_start"|"tool_result"|
    "usage"|"done"|"error", "data": ...}
    """
    tag = provider.stream_tag
    target, error = _resolve(provider, model, env)
    if target is None:
        yield {"type": "error", "data": error}
        return

    processed_messages = _encode_messages(provider, messages, env, provider.tool_tag)

    payload = {
        "model": target.model,
        "messages": processed_messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "tools": await asyncio.to_thread(env.get_tools, provider.tools_provider, _last_user_msg(messages))
    }
    _stream_knobs(provider, payload, model)
    if provider is CUSTOM:
        print(f"[CUSTOM] Server '{target.server.get('alias')}' at {target.server['base_url']}, model {target.bare_model}")
    headers = _headers(provider, target, stream=True)

    retried = False  # the family's one compatibility retry (see _retry_once)
    loop = f"stream_{provider.name}"

    # Tool execution loop (bounded by max_tool_iterations below)
    max_tool_iterations = 30
    for iteration in range(max_tool_iterations):
        try:
            print(f"{tag} Starting stream request to {target.url} with model {target.model}")
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream("POST", target.url, headers=headers, json=payload) as response:
                    print(f"{tag} Response status: {response.status_code}")
                    if response.status_code != 200:
                        error_text = await response.aread()
                        error_msg = error_text.decode()
                        if not retried and _retry_once(provider, response.status_code, error_msg, payload):
                            retried = True
                            continue
                        yield _stream_http_error(provider, target, response.status_code, error_msg)
                        return
                    if provider.billing:
                        provider_billing.note_ok(provider.billing)

                    reasoning_buffer = ""
                    content_buffer = ""
                    tool_calls = []
                    current_tool_call = None
                    finish_reason = None

                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue

                        data_str = line[6:]  # Remove "data: " prefix
                        if data_str == "[DONE]":
                            break

                        try:
                            chunk = json.loads(data_str)
                            choices = chunk.get("choices", [])
                            if not choices:
                                # Chunks with no choices (e.g. the usage info at the end)
                                if "usage" in chunk:
                                    yield {"type": "usage", "data": chunk["usage"]}
                                continue

                            choice = choices[0]
                            delta = choice.get("delta", {})
                            finish_reason = choice.get("finish_reason")

                            # Reasoning content: models use different field names
                            reasoning_text = None
                            for field in _REASONING_FIELDS:
                                if field in delta and delta[field]:
                                    reasoning_text = delta[field]
                                    break

                            if reasoning_text:
                                reasoning_buffer += reasoning_text
                                yield {"type": "thinking", "data": reasoning_text}

                            # Regular content
                            if "content" in delta:
                                content_text = delta["content"]
                                if content_text:
                                    content_buffer += content_text
                                    yield {"type": "content", "data": content_text}

                            # Tool calls arrive as indexed fragments
                            if "tool_calls" in delta:
                                for tc_delta in delta["tool_calls"]:
                                    index = tc_delta.get("index", 0)

                                    # Initialize tool call if new
                                    if len(tool_calls) <= index:
                                        tool_calls.append({
                                            "id": "",
                                            "type": "function",
                                            "function": {"name": "", "arguments": ""}
                                        })
                                    current_tool_call = tool_calls[index]

                                    # Update tool call fields
                                    if "id" in tc_delta:
                                        current_tool_call["id"] = tc_delta["id"]
                                    if "type" in tc_delta:
                                        current_tool_call["type"] = tc_delta["type"]
                                    if "function" in tc_delta:
                                        func_delta = tc_delta["function"]
                                        if "name" in func_delta:
                                            current_tool_call["function"]["name"] = func_delta["name"]
                                            yield {"type": "tool_start", "data": func_delta["name"]}
                                        if "arguments" in func_delta:
                                            current_tool_call["function"]["arguments"] += func_delta["arguments"]

                            # Usage info at the end
                            if "usage" in chunk:
                                yield {"type": "usage", "data": chunk["usage"]}

                        except json.JSONDecodeError:
                            continue

                    # Check if we need to execute tools
                    if finish_reason == "tool_calls" and tool_calls:
                        yield {"type": "tool_executing", "data": provider.executing_label}

                        # Add assistant message with tool calls
                        assistant_message = {"role": "assistant", "content": content_buffer or None}
                        if tool_calls:
                            assistant_message["tool_calls"] = tool_calls
                        payload["messages"].append(assistant_message)

                        # Execute each tool call
                        for tool_call in tool_calls:
                            func_name = tool_call["function"]["name"]
                            func_args = _tool_args(tool_call["function"]["arguments"])

                            outcome = await dispatch_tool(func_name, func_args, env.tool_context(operator, loop))
                            for event in outcome.events:
                                yield event

                            # Add tool result message
                            payload["messages"].append({
                                "role": "tool",
                                "tool_call_id": tool_call["id"],
                                "content": outcome.result or "Tool executed successfully"
                            })

                        print(f"{provider.tool_tag} Tool iteration {iteration + 1}, continuing...")
                        continue
                    # No tool calls, we're done
                    yield {"type": "done", "data": {"reasoning": reasoning_buffer, "content": content_buffer}}
                    print(f"{tag} Stream complete. Reasoning: {len(reasoning_buffer)} chars, Content: {len(content_buffer)} chars")
                    return

        except Exception as e:
            import traceback
            print(f"{tag} Stream exception: {str(e)}")
            print(f"{tag} Traceback: {traceback.format_exc()}")
            yield {"type": "error", "data": f"Stream error: {str(e)}"}
            return

    # Every round ended in tool calls.
    if provider.cap_note:
        # Close the turn with its text, a note and done (the same terminal
        # events as the Anthropic stream).
        for event in _tool_cap_events(content_buffer, "reasoning", reasoning_buffer, max_tool_iterations):
            yield event
    else:
        yield {"type": "done", "data": {"reasoning": reasoning_buffer, "content": content_buffer}}


# -----------------------------------------------------------------------------
# The non-stream loop
# -----------------------------------------------------------------------------
def _call_http_error(provider: CompatProvider, target: _Target, r) -> HTTPException:
    """The exception for a non-200 non-stream response (a billing refusal raises itself)."""
    if provider is CUSTOM:
        # Over-window 400 (llama.cpp exceed_context_size_error): learn the
        # model's REAL window, then surface a friendly actionable error — the
        # raw JSON body stays in the log line only.
        n_ctx = _parse_exceed_context(r.text) if r.status_code == 400 else None
        if n_ctx:
            _learn_model_context(target.server, target.bare_model, n_ctx)
            print(f"[CUSTOM-CALL] exceed_context 400 (model {target.bare_model}, "
                  f"n_ctx {n_ctx}): {r.text}")
            return HTTPException(400, _exceed_context_reply(target.server, target.bare_model, n_ctx))
        # Alias-prefixed so multi-server fleets can tell WHICH box failed.
        return HTTPException(r.status_code, f"Custom server '{target.server.get('alias', '')}' API error: {r.text}")
    provider_billing.raise_if_exhausted(provider.billing, r.status_code, r.text, surface="chat")
    return HTTPException(r.status_code, r.text)


def call(provider: CompatProvider, messages: List[Dict], model: str, operator: str,
         env: ProviderEnv):
    """One non-stream chat.completions turn with its tool loop.

    Returns (text, usage) for OpenAI (answer-only by design), and
    (text, usage, reasoning, media_tasks) for xAI and custom servers.
    """
    target, error = _resolve(provider, model, env)
    if target is None:
        raise HTTPException(400, error)

    processed_messages = _encode_messages(provider, messages, env, provider.call_tag)
    headers = _headers(provider, target, stream=False)
    payload = {
        "model": target.model,
        "messages": processed_messages,
        "tools": env.get_tools(provider.tools_provider, _last_user_msg(messages))
    }
    if provider is OPENAI and "gpt-5.6" in model.lower():
        # gpt-5.6 chat/completions rejects function tools unless reasoning_effort
        # is explicitly 'none' (see _stream_knobs).
        payload["reasoning_effort"] = "none"
    if provider is CUSTOM:
        # No max_tokens (llama-server generates until EOS/ctx) and no
        # stream_options: this is the non-stream payload.
        print(f"[CUSTOM-CALL] Server '{target.server.get('alias')}' at {target.server['base_url']}, model {target.bare_model}")

    retried = False  # OpenAI's one reasoning_effort retry (see _retry_once)
    loop = f"call_{provider.name}"

    # Tool calling loop
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    media_tasks = []  # 2-media: created (task_id,type,prompt) for the worker
    max_tool_calls = 30

    for iteration in range(max_tool_calls):
        r = requests.post(target.url, headers=headers, json=payload, timeout=200)
        if r.status_code != 200:
            if provider is OPENAI and not retried and _retry_once(provider, r.status_code, r.text, payload):
                retried = True
                continue
            raise _call_http_error(provider, target, r)
        if provider.billing:
            provider_billing.note_ok(provider.billing)

        data = r.json()
        message = data["choices"][0]["message"]
        finish_reason = data["choices"][0].get("finish_reason", "")

        u = data.get("usage", {})
        _usage_into(total_usage, u.get("prompt_tokens", 0), u.get("completion_tokens", 0))

        # Check if model wants to use tools
        if finish_reason == "tool_calls" and message.get("tool_calls"):
            # Add assistant message to conversation
            payload["messages"].append(message)

            # Execute each tool call
            for tool_call in message["tool_calls"]:
                func = tool_call["function"]
                tool_name = func["name"]
                tool_id = tool_call["id"]
                args = _tool_args(func["arguments"])

                outcome = dispatch_tool_blocking(tool_name, args, env.tool_context(operator, loop))
                if outcome.media_task:
                    _record_media_task(media_tasks, *outcome.media_task)

                # Add tool result to messages
                payload["messages"].append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": outcome.result
                })

            print(f"{provider.call_tag} Tool iteration {iteration + 1}, continuing...")
        else:
            # No more tool calls, extract final text
            break

    if provider is OPENAI:
        # Answer-only: OpenAI's chat.completions carries no reasoning text.
        return message.get("content", ""), total_usage
    # OpenAI-compatible: `content` is the answer, `reasoning_content` (or its
    # aliases) carries native reasoning; keep them separate (2-reasoning).
    return message.get("content", "") or "", total_usage, _reasoning_of(message), media_tasks
