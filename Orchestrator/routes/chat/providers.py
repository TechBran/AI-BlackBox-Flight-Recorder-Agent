"""The chat provider entry points (stream_<provider>_*, call_<provider>): thin
wrappers over the wire-format adapters in Orchestrator/providers, plus
the tool lists and the per-call environment the adapters read.

Part of the chat routes (audit rank 74): see Orchestrator/routes/chat_routes.py,
the namespace every importer and test uses.
"""
from __future__ import annotations

from typing import Dict, Optional, List

from Orchestrator.utils.paths import GEMINI_INLINE_MAX_BYTES
from Orchestrator.config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_URL,
    GEMINI_BASE_URL,
    GOOGLE_API_KEY,
    OPENAI_API_KEY,
    OPENAI_URL,
    XAI_API_KEY,
    XAI_URL,
)
from Orchestrator.fossils import hybrid_retrieve
from Orchestrator.tasks import create_task, generate_prompt_slug
from Orchestrator.web_tools import perform_web_fetch
from Orchestrator.providers.tool_dispatch import (
    PROFILES as _TOOL_PROFILES,
    ToolContext,
    ToolServices,
)
from Orchestrator.providers import anthropic_messages, gemini_generate, openai_compat
from Orchestrator.providers.common import ProviderEnv
from Orchestrator.tools.tool_registry import (
    get_anthropic_tools,
    get_openai_rest_tools,
    get_gemini_rest_tools,
)
from Orchestrator.config import TOOLVAULT_ENABLED

from Orchestrator.routes.chat.context import (
    _ORIGIN_DEVICE_ID,
    _load_attachment,
    _resolve_attachment,
    _unreadable_attachment_parts,
)
from Orchestrator.routes.chat.media import (
    execute_get_media,
    execute_list_media,
    execute_search_media,
)


# Pre-built tool lists for each provider (static fallback)
CHAT_TOOLS_ANTHROPIC = get_anthropic_tools("chat")
CHAT_TOOLS_ANTHROPIC_CU = get_anthropic_tools("chat_cu")
CHAT_TOOLS_OPENAI = get_openai_rest_tools("chat")
CHAT_TOOLS_GEMINI = get_gemini_rest_tools("chat")
CHAT_TOOLS_XAI = CHAT_TOOLS_OPENAI  # xAI uses OpenAI format


# -----------------------------------------------------------------------------
# 2-media: native non-stream media-task surfacing.
#
# When a model natively calls generate_image/video/lyria_music (etc.) inside a
# call_* tool loop, that loop CREATES the background task but returns only
# predicted-URL text to the model -- it does NOT inject a UI loading-placeholder.
# The non-stream worker (tasks.py) needs the created (task_id, type, prompt) so it
# can append the EXACT existing placeholder <div> (so Portal's existing pollers
# fire) + a result_data['media_tasks'] array. Each call_* collects these into a
# list and surfaces it as the LAST return element (normalized by _unpack_call).
# -----------------------------------------------------------------------------
# The media-kind table and the media / browser / CLI event helpers live with the
# one tool dispatcher every loop shares (providers/tool_dispatch.py; chat_routes
# re-exports them). Each loop dispatches through _tool_context below.


def _tool_services() -> ToolServices:
    """The helpers the native tool branches call, read from this module on every
    call. A stub set on chat_routes.<name> (create_task, perform_web_fetch,
    hybrid_retrieve, the media lookups, _load_attachment) lands here too (the
    chat_routes namespace), so it reaches dispatch_tool exactly as it reached
    the inline branches."""
    return ToolServices(
        create_task=create_task,
        generate_prompt_slug=generate_prompt_slug,
        perform_web_fetch=perform_web_fetch,
        hybrid_retrieve=hybrid_retrieve,
        execute_get_media=execute_get_media,
        execute_list_media=execute_list_media,
        execute_search_media=execute_search_media,
        resolve_attachment=_resolve_attachment,
    )


def _tool_context(operator: str, loop: str) -> ToolContext:
    """One tool call's context for provider loop ``loop`` (a PROFILES key): the
    session operator, this request's originating device (M3) and the helpers."""
    return ToolContext(operator=operator, origin_device_id=_ORIGIN_DEVICE_ID.get(),
                       profile=_TOOL_PROFILES[loop], services=_tool_services())


def _provider_env(api_key: str, url: str, inline_max_bytes: Optional[int] = None) -> ProviderEnv:
    """What a provider adapter reads from this module, built on every call: the
    key and endpoint the wrapper passes, the tool lists, the upload loaders and
    the tool-context builder. A stub set on chat_routes.<name> (_get_tools,
    _load_attachment, a key) lands here too, so it reaches the adapter as it
    reached the inline loop."""
    return ProviderEnv(api_key=api_key, url=url, get_tools=_get_tools,
                       load_attachment=_load_attachment,
                       unreadable_parts=_unreadable_attachment_parts,
                       tool_context=_tool_context, inline_max_bytes=inline_max_bytes)


# Cache the last injection result so _get_tools can reuse a single search
# pass (tool names / tools) within the same request.
_inject_cache = {"prompt": "", "provider": "", "group": "", "tools": None, "tool_names": set()}


def _extract_tool_names(tools: list) -> set:
    """Extract tool names from a provider-formatted tools list.

    Handles all four formats:
      Anthropic: [{"name": "x", ...}]
      OpenAI/xAI: [{"type": "function", "function": {"name": "x", ...}}]
      Gemini: [{"functionDeclarations": [{"name": "x", ...}]}]
    """
    names = set()
    if not tools:
        return names
    for t in tools:
        if not isinstance(t, dict):
            continue
        # Anthropic
        if "name" in t and isinstance(t["name"], str):
            names.add(t["name"])
        # OpenAI / xAI
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            n = t["function"].get("name")
            if isinstance(n, str):
                names.add(n)
        # Gemini
        if "functionDeclarations" in t and isinstance(t["functionDeclarations"], list):
            for fd in t["functionDeclarations"]:
                if isinstance(fd, dict):
                    n = fd.get("name")
                    if isinstance(n, str):
                        names.add(n)
    return names


def _get_tools(provider: str, prompt: str = "", group: str = "chat") -> list:
    """Get tools for an API call — dynamic (ToolVault) or static (legacy).

    When TOOLVAULT_ENABLED=true, uses semantic search to inject only
    the tools relevant to this specific prompt.
    """
    if TOOLVAULT_ENABLED and prompt.strip():
        from Orchestrator.toolvault.injector import inject_for_prompt
        tools, _instructions = inject_for_prompt(prompt, provider, group)
        _inject_cache["prompt"] = prompt
        _inject_cache["provider"] = provider
        _inject_cache["group"] = group
        _inject_cache["tools"] = tools
        _inject_cache["tool_names"] = _extract_tool_names(tools)
        return tools

    # Static fallback
    if provider == "anthropic":
        tools = CHAT_TOOLS_ANTHROPIC_CU if group == "chat_cu" else CHAT_TOOLS_ANTHROPIC
    elif provider in ("openai", "openai_rest", "xai", "grok", "custom"):
        tools = CHAT_TOOLS_OPENAI
    elif provider in ("gemini", "gemini_rest", "google"):
        tools = CHAT_TOOLS_GEMINI
    else:
        tools = CHAT_TOOLS_OPENAI
    _inject_cache["tool_names"] = _extract_tool_names(tools)
    return tools

def call_openai(messages: List[Dict], model: str, operator: str):
    """One non-stream OpenAI chat.completions turn with its tool loop:
    (text, usage) — answer-only by design (see providers/openai_compat)."""
    return openai_compat.call(openai_compat.OPENAI, messages, model, operator,
                              _provider_env(OPENAI_API_KEY, OPENAI_URL))




def call_anthropic(messages: List[Dict], model: str, operator: str):
    """One non-stream Claude turn with its tool loop: (text, usage, reasoning,
    media_tasks); a refusal is a DeclinedReply (see providers/anthropic_messages)."""
    return anthropic_messages.call(messages, model, operator,
                                   _provider_env(ANTHROPIC_API_KEY, ANTHROPIC_URL))


def call_gemini(messages: List[Dict], model: str, operator: str):
    """One non-stream Gemini turn with its tool loop: (text, usage, media_parts,
    reasoning, media_tasks); a safety block is a DeclinedReply
    (see providers/gemini_generate)."""
    return gemini_generate.call(messages, model, operator,
                                _provider_env(GOOGLE_API_KEY, GEMINI_BASE_URL, GEMINI_INLINE_MAX_BYTES))


def call_xai(messages: List[Dict], model: str, operator: str):
    """Call xAI Grok API - OpenAI-compatible format: (text, usage, reasoning,
    media_tasks) (see providers/openai_compat)."""
    return openai_compat.call(openai_compat.XAI, messages, model, operator,
                              _provider_env(XAI_API_KEY, XAI_URL))


def call_custom(messages: List[Dict], model: str, operator: str):
    """Call a user-registered OpenAI-compatible server (provider 'custom') — non-stream.

    The server is resolved FRESH from the registry on every call; the payload
    names the bare model, the bearer is sent only when the server has a key, and
    there is no max_tokens. Serves POST /chat, the MCP chat_with_context tool,
    and the cron executor via tasks.process_chat_task. Returns (text, usage,
    reasoning, media_tasks) (see providers/openai_compat).
    """
    return openai_compat.call(openai_compat.CUSTOM, messages, model, operator,
                              _provider_env("", ""))


# -----------------------------------------------------------------------------
# Streaming Chat with Thinking/Reasoning Support
# -----------------------------------------------------------------------------

def stream_openai_with_reasoning(messages: List[Dict], model: str, operator: str):
    """Stream OpenAI responses with reasoning support for o1/o3/o4 and GPT-5+ models.

    Yields SSE events: {"type": "thinking"|"content"|"done"|"error", "data": "..."}
    (the async generator of providers/openai_compat.stream).
    """
    return openai_compat.stream(openai_compat.OPENAI, messages, model, operator,
                                _provider_env(OPENAI_API_KEY, OPENAI_URL))


def _detect_tool_intent(messages: List[Dict]) -> bool:
    """Detect if the user's message suggests they want to use tools (web search, image/video/music generation).

    Returns True if tools should be enabled (thinking mode disabled for Anthropic).
    """
    # Web search keywords
    search_keywords = [
        "search the web", "search online", "look up", "find online",
        "google", "search for", "web search", "latest news",
        "current", "recent", "today's", "what's happening",
        "search internet", "look online"
    ]

    # Image generation keywords
    image_keywords = [
        "generate an image", "generate image", "create an image", "create image",
        "make an image", "make image", "draw", "generate a picture",
        "create a picture", "make a picture", "visualize", "illustration",
        "generate artwork", "create artwork", "make me an image",
        "show me an image", "picture of", "image of"
    ]

    # Video generation keywords
    video_keywords = [
        "generate a video", "generate video", "create a video", "create video",
        "make a video", "make video", "animate", "animation",
        "video of", "make me a video", "show me a video",
        "generate an animation", "create an animation"
    ]

    # Music generation keywords
    music_keywords = [
        "generate music", "create music", "make music", "compose",
        "generate a song", "create a song", "make a song",
        "generate audio", "create audio", "make some music",
        "play some music", "music for", "make me music",
        "some music", "song for", "tune for", "soundtrack",
        "instrumental", "beat", "melody", "music that",
        "compose a", "write music", "write a song"
    ]

    # Snapshot search keywords
    snapshot_keywords = [
        "search snapshots", "search memory", "search history",
        "find in snapshots", "look in memory", "recall",
        "what did we discuss", "what did we talk about",
        "previous conversation", "past conversation",
        "remember when", "do you remember", "check history",
        "search blackbox", "find in history"
    ]

    all_keywords = search_keywords + image_keywords + video_keywords + music_keywords + snapshot_keywords

    # Check the last user message
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
            content_lower = content.lower()
            for keyword in all_keywords:
                if keyword in content_lower:
                    if keyword in search_keywords:
                        print(f"[ANTHROPIC] Web search intent detected: '{keyword}'")
                    elif keyword in image_keywords:
                        print(f"[ANTHROPIC] Image generation intent detected: '{keyword}'")
                    elif keyword in video_keywords:
                        print(f"[ANTHROPIC] Video generation intent detected: '{keyword}'")
                    elif keyword in music_keywords:
                        print(f"[ANTHROPIC] Music generation intent detected: '{keyword}'")
                    elif keyword in snapshot_keywords:
                        print(f"[ANTHROPIC] Snapshot search intent detected: '{keyword}'")
                    return True
            break
    return False


def _detect_web_search_intent(messages: List[Dict]) -> bool:
    """Legacy function - now calls _detect_tool_intent for backwards compatibility."""
    return _detect_tool_intent(messages)


def stream_anthropic_with_thinking(messages: List[Dict], model: str, operator: str, force_tools: bool = False):
    """Stream Anthropic Claude responses with tools enabled.

    Adaptive thinking is enabled for every model anthropic_caps says takes it
    (4.6 and later) — these models interleave thinking with tool calls
    automatically, so the old "thinking vs tools" conflict no longer applies
    (``force_tools`` is accepted for old callers and ignored). Every turn ends in
    exactly one ``done`` or ``error`` (see providers/anthropic_messages.stream,
    whose async generator this returns).
    """
    return anthropic_messages.stream(messages, model, operator,
                                     _provider_env(ANTHROPIC_API_KEY, ANTHROPIC_URL))


def stream_gemini_with_thinking(messages: List[Dict], model: str, operator: str):
    """Stream Google Gemini responses with thinking mode and tool support.

    Yields SSE events: {"type": "thinking"|"content"|"tool_start"|"tool_result"|"done"|"error", "data": "..."}
    (the async generator of providers/gemini_generate.stream).
    """
    return gemini_generate.stream(messages, model, operator,
                                  _provider_env(GOOGLE_API_KEY, GEMINI_BASE_URL, GEMINI_INLINE_MAX_BYTES))


def stream_xai_with_reasoning(messages: List[Dict], model: str, operator: str):
    """Stream xAI Grok responses with reasoning support.

    Note: Only grok-3-mini-beta returns visible reasoning_content.
    Other reasoning models (grok-4-*-reasoning) reason internally but don't expose it.

    Yields SSE events: {"type": "thinking"|"content"|"done"|"error", "data": "..."}
    (the async generator of providers/openai_compat.stream).
    """
    return openai_compat.stream(openai_compat.XAI, messages, model, operator,
                                _provider_env(XAI_API_KEY, XAI_URL))


def stream_custom_with_reasoning(messages: List[Dict], model: str, operator: str):
    """Stream responses from a user-registered OpenAI-compatible server (provider 'custom').

    Servers (llama.cpp / llama-swap / vLLM ...) come from the onboarding-wizard
    registry; `model` may be alias-qualified ("alias::model") or bare.
    llama.cpp surfaces reasoning via `reasoning_content` (first in the priority list).

    Yields SSE events: {"type": "thinking"|"content"|"done"|"error", "data": "..."}
    (the async generator of providers/openai_compat.stream).
    """
    return openai_compat.stream(openai_compat.CUSTOM, messages, model, operator,
                                _provider_env("", ""))
