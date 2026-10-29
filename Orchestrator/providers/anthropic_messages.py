"""The Anthropic Messages chat adapter: call() and stream() for Claude.

Audit rank 74 (X05-architecture-11). call_anthropic and
stream_anthropic_with_thinking lived in routes/chat_routes.py with their own
copy of the tool table; they now live here, over the shared dispatch_tool()
(providers/tool_dispatch.py), with the Claude-specific pieces they share:
alternation-safe message preparation, the anthropic_caps request fields, the
refusal / truncation / tool-cap endings and the timeouts. The chat_routes names
stay as thin wrappers.

Transport: raw Messages API over requests (non-stream) and httpx SSE (stream),
not the official SDK (anthropic 0.102): the recorded contract
(tests/golden/providers) is these exact requests with no client-side retries,
and the thinking / redacted_thinking blocks are replayed verbatim as JSON.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Dict, List

import httpx
import requests
from fastapi import HTTPException

from Orchestrator import provider_billing
from Orchestrator.config import anthropic_caps
from Orchestrator.providers.common import (
    DeclinedReply,
    ProviderEnv,
    _ATTACHMENT_ERROR_NOTE,
    _TOOL_CAP_NOTE,
    _last_user_msg,
    _tool_cap_events,
    _with_note,
)
from Orchestrator.providers.tool_dispatch import _record_media_task, dispatch_tool, dispatch_tool_blocking
from Orchestrator.utils.paths import DOC_EXTS, IMAGE_EXTS


def _as_block_list(content) -> List[Dict]:
    """Coerce a message's content into a list of content blocks.

    A plain str becomes a single ``{"type": "text", "text": ...}`` block; a
    list is returned as-is (already block-shaped); anything else is wrapped as
    a text block of its str() so nothing is silently dropped.
    """
    if isinstance(content, list):
        return list(content)
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if content is None:
        return []
    return [{"type": "text", "text": str(content)}]


def _normalize_alternation(messages: List[Dict]) -> List[Dict]:
    """Merge consecutive same-role user/assistant turns so roles alternate.

    The Anthropic Messages API 400s ("messages: roles must alternate") on two
    adjacent same-role turns. Any caller (SMS peer threads, future batchers,
    etc.) that emits consecutive same-role messages would trip it. This pure
    pass collapses each run of adjacent same-role user/assistant turns into a
    single turn, concatenating their content:

      - str + str  -> joined with a newline.
      - list + list -> block lists concatenated (no blocks dropped).
      - str + list / list + str -> both coerced to block lists and concatenated
        (the str becomes a ``{"type": "text", ...}`` block).

    System rows are left untouched and never merged across (system is handled
    separately by call_anthropic). A correctly-alternating list is returned
    unchanged. The first conversation turn keeps whatever role the input
    started with — no turns are invented.
    """
    out: List[Dict] = []
    for m in messages:
        role = m.get("role")
        if out and role in ("user", "assistant") and out[-1].get("role") == role:
            prev = out[-1]
            prev_content = prev.get("content")
            cur_content = m.get("content")
            if isinstance(prev_content, str) and isinstance(cur_content, str):
                prev["content"] = prev_content + "\n" + cur_content
            else:
                # At least one side is a block list (multimodal); coerce both to
                # block lists and concatenate so no content blocks are dropped.
                prev["content"] = _as_block_list(prev_content) + _as_block_list(cur_content)
            continue
        # Shallow-copy so we never mutate the caller's dict when merging.
        out.append(dict(m))
    return out


def _prepare_anthropic_messages(messages: List[Dict]):
    """Split system text out and return an alternation-safe conversation.

    Returns ``(system_text, convo)`` where ``system_text`` is the joined
    content of every system row (extracted from the ORIGINAL list, unchanged)
    and ``convo`` is the user/assistant-only conversation with consecutive
    same-role turns merged so roles strictly alternate.

    System is extracted and filtered out *before* normalizing. This matters:
    a system row interleaved between two user turns (e.g. ``[user, system,
    user]``) would otherwise break list-adjacency so the two user turns never
    merge, and then system extraction drops the row -- leaving ``[user, user]``,
    the exact Anthropic 400 ("roles must alternate") this guard exists to
    prevent. Filtering system out first lets the normalizer see the real
    user/assistant adjacency.
    """
    system_text = "\n\n".join([m.get("content", "") for m in messages if m.get("role") == "system"])
    convo = _normalize_alternation([m for m in messages if m.get("role") in ("user", "assistant")])
    return system_text, convo


# -----------------------------------------------------------------------------
# Shared by call_anthropic and stream_anthropic_with_thinking (audit rank 58).
# -----------------------------------------------------------------------------
# (connect, read) for the non-stream Claude call. A non-stream response sends
# nothing until the whole message is done, so the read timeout is the longest
# turn it may take: 16K output tokens at xhigh effort could outrun the old
# flat 200 s (W21). The stream's 300 s is a gap BETWEEN chunks, which a live
# stream never reaches, so this matches the SDK's 10-minute ceiling instead.
# MCP chat_with_context polls a /chat task at least this long
# (BLACKBOX_MCP_CHAT_TIMEOUT in MCP/blackbox_mcp_server.py).
ANTHROPIC_CONNECT_TIMEOUT_S = 30
ANTHROPIC_NONSTREAM_TIMEOUT = (ANTHROPIC_CONNECT_TIMEOUT_S, 600)
ANTHROPIC_STREAM_READ_TIMEOUT_S = 300

# A Claude turn that stopped on its output limit (or the context window) is
# cut off mid-answer; say so instead of passing it off as complete.
_ANTHROPIC_TRUNCATED_STOPS = ("max_tokens", "model_context_window_exceeded")
_TRUNCATION_NOTE = "[Response truncated: the model reached its output limit.]"


def _apply_anthropic_caps(payload: Dict, model: str, temperature: float) -> None:
    """Add the thinking / effort / sampling fields ``model`` accepts (anthropic_caps)."""
    caps = anthropic_caps(model)
    if caps.thinking:
        payload["thinking"] = caps.thinking
        if caps.effort:
            payload["output_config"] = {"effort": caps.effort}
    # temperature: removed on Opus 4.7+ (400 "deprecated"), and on the models
    # that still accept it, it must stay at the default 1 whenever thinking is
    # on (400 "may only be set to 1 when thinking is enabled"). So: thinking-off
    # legacy only.
    elif caps.sampling:
        payload["temperature"] = temperature


def _anthropic_refusal(stop_details) -> Dict:
    """A stop_reason "refusal" as ``{"message", "category"}``. The 5.x safety
    classifiers decline with HTTP 200 and empty (or partial) content, so the
    old paths rendered a blank or half-written reply as the answer."""
    details = stop_details if isinstance(stop_details, dict) else {}
    category = details.get("category") or ""
    message = "Claude declined to answer this request"
    if category:
        message += f" (category: {category})"
    message += "."
    explanation = (details.get("explanation") or "").strip()
    if explanation:
        message += f" {explanation}"
    return {"message": message, "category": category}


def call(messages: List[Dict], model: str, operator: str, env: ProviderEnv):
    if not env.api_key: raise HTTPException(400, "ANTHROPIC_API_KEY not set")
    # Defense-in-depth: extract system text, then collapse any consecutive
    # same-role turns so the conversation array alternates (Anthropic 400s
    # otherwise). System is pulled out BEFORE normalizing so an interleaved
    # system row can't break adjacency and reintroduce consecutive roles.
    system_text, convo = _prepare_anthropic_messages(messages)
    amsgs = []
    for m in convo:
        if m.get("role") in ("user", "assistant"):
            content = m.get("content")
            if isinstance(content, list): # Handle multimodal content
                new_content = []
                for part in content:
                    # Handle direct base64 images (from Android app)
                    if part.get("type") == "image" and part.get("source", {}).get("type") == "base64":
                        # Already in Anthropic format - pass through directly
                        source = part.get("source", {})
                        media_type = source.get("media_type", "image/png")
                        data = source.get("data", "")
                        if data:
                            print(f"[ANTHROPIC] Processing direct base64 image ({len(data)} chars, {media_type})")
                            new_content.append({
                                "type": "image",
                                "source": { "type": "base64", "media_type": media_type, "data": data }
                            })
                    elif part.get("type") == "image_url":
                        url = part["image_url"]["url"]
                        try:
                            local_path, refused = env.load_attachment(url, IMAGE_EXTS)
                            if local_path is not None:
                                media_type = "image/jpeg" if local_path.suffix.lower() in [".jpg", ".jpeg"] else "image/png"
                                img_bytes = local_path.read_bytes()
                                base64_image = base64.b64encode(img_bytes).decode("utf-8")
                                new_content.append({
                                    "type": "image",
                                    "source": { "type": "base64", "media_type": media_type, "data": base64_image }
                                })
                            elif refused:
                                new_content.append({"type": "text", "text": refused})
                        except Exception as e:
                            print(f"Could not process local image for Anthropic: {e}")
                            new_content.append({"type": "text", "text": _ATTACHMENT_ERROR_NOTE})
                    elif part.get("type") == "video_url":
                        # Handle videos - Anthropic doesn't support video directly, provide a note
                        url = part["video_url"]["url"]
                        new_content.append({
                            "type": "text",
                            "text": f"[Note: Video file attached: {url}. Anthropic Claude models don't support direct video analysis. Please use Google Gemini for video analysis.]"
                        })
                    elif part.get("type") == "audio_url":
                        # A turn media_auto_route kept here (no Google key)
                        new_content.extend(env.unreadable_parts(part))
                    elif part.get("type") == "document_url":
                        # Anthropic supports PDF documents
                        url = part["document_url"]["url"]
                        try:
                            local_path, refused = env.load_attachment(url, DOC_EXTS)
                            if refused:
                                new_content.append({"type": "text", "text": refused})
                            if local_path is not None:
                                suffix = local_path.suffix.lower()
                                if suffix == ".pdf":
                                    doc_bytes = local_path.read_bytes()
                                    base64_doc = base64.b64encode(doc_bytes).decode("utf-8")
                                    new_content.append({
                                        "type": "document",
                                        "source": { "type": "base64", "media_type": "application/pdf", "data": base64_doc }
                                    })
                                elif suffix == ".txt":
                                    # For text files, just read and add as text
                                    text_content = local_path.read_text(encoding='utf-8')
                                    new_content.append({
                                        "type": "text",
                                        "text": f"[Document content: {local_path.name}]\n\n{text_content}"
                                    })
                        except Exception as e:
                            print(f"Could not process document for Anthropic: {e}")
                            new_content.append({"type": "text", "text": _ATTACHMENT_ERROR_NOTE})
                    elif part.get("type") == "text":
                        new_content.append(part)
                amsgs.append({"role": m["role"], "content": new_content})
            else: # Original text-only logic
                amsgs.append({"role": m["role"], "content": content})

    headers = {"x-api-key": env.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}

    # Include web search, image generation, and video generation tools for Claude
    payload = {
        "model": model,
        "max_tokens": 16000,
        "messages": amsgs,
        "tools": env.get_tools("anthropic", _last_user_msg(messages))
    }
    # Adaptive thinking, display and effort per model, or temperature for a
    # thinking-off legacy model (anthropic_caps; same rule as the stream path).
    _apply_anthropic_caps(payload, model, temperature=0.2)
    if system_text:
        payload["system"] = system_text

    # Tool calling loop - continue until model stops using tools
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    media_tasks = []  # 2-media: created (task_id,type,prompt) for the worker
    max_tool_calls = 30  # Prevent infinite loops
    tool_cap_hit = False

    for iteration in range(max_tool_calls):
        r = requests.post(env.url, headers=headers, json=payload, timeout=ANTHROPIC_NONSTREAM_TIMEOUT)
        if r.status_code != 200:
            provider_billing.raise_if_exhausted("anthropic", r.status_code, r.text, surface="chat")
            raise HTTPException(r.status_code, r.text)
        provider_billing.note_ok("anthropic")

        data = r.json()
        blocks = data.get("content", [])
        stop_reason = data.get("stop_reason", "")

        # Accumulate usage
        u = data.get("usage", {})
        total_usage["prompt_tokens"] += u.get("input_tokens", 0)
        total_usage["completion_tokens"] += u.get("output_tokens", 0)
        total_usage["total_tokens"] = total_usage["prompt_tokens"] + total_usage["completion_tokens"]

        # Check if model wants to use tools
        if stop_reason == "tool_use":
            # Find tool use blocks
            tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
            if not tool_uses:
                break  # No tool uses found, exit loop

            # Add assistant's response (with tool use) to messages
            payload["messages"].append({"role": "assistant", "content": blocks})

            # Process each tool use and add results
            tool_results = []
            for tool_use in tool_uses:
                tool_name = tool_use.get("name", "")
                tool_id = tool_use.get("id", "")
                tool_input = tool_use.get("input", {})

                outcome = dispatch_tool_blocking(tool_name, tool_input, env.tool_context(operator, "call_anthropic"))
                if outcome.media_task:
                    _record_media_task(media_tasks, *outcome.media_task)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": outcome.result
                })

            # Add tool results to messages
            payload["messages"].append({"role": "user", "content": tool_results})
            print(f"[ANTHROPIC] Tool iteration {iteration + 1}, continuing...")
        else:
            # No more tool calls, extract final text
            break
    else:
        # Every round ended in tool calls: the model never gave a final answer.
        tool_cap_hit = True

    # A refusal (HTTP 200, stop_reason "refusal") carries no answer: a non-stream
    # decline omits the declined partial, and whatever text is there must not
    # pass for one. Same parenthesized-notice shape as call_gemini's block.
    if stop_reason == "refusal":
        refusal = _anthropic_refusal(data.get("stop_details"))
        print(f"[ANTHROPIC] Refusal ({refusal['category'] or 'no category'})")
        return DeclinedReply(f"({refusal['message']})"), total_usage, "", media_tasks

    # Extract final text response (answer) and native reasoning (thinking blocks).
    # Anthropic returns reasoning in `thinking` / `redacted_thinking` blocks and the
    # user-facing answer in `text` blocks; keep them separate so reasoning never
    # leaks into the reply (2-reasoning).
    text = ""
    reasoning_parts = []
    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            text += block.get("text", "")
        elif btype == "thinking":
            t = block.get("thinking", "") or block.get("text", "")
            if t:
                reasoning_parts.append(t)
        elif btype == "redacted_thinking":
            reasoning_parts.append("[redacted thinking]")
    reasoning = "\n".join(reasoning_parts)

    if tool_cap_hit:
        text = _with_note(text, _TOOL_CAP_NOTE.format(cap=max_tool_calls))
    elif stop_reason in _ANTHROPIC_TRUNCATED_STOPS:
        text = _with_note(text, _TRUNCATION_NOTE)

    return text, total_usage, reasoning, media_tasks


def _guard_anthropic_history(amsgs: List[Dict], system_text: str):
    """WI-10 (M7) window guard for the anthropic stream path's HISTORY.

    Replaces the old blanket 15,000-char per-message truncation
    (truncate_large_content) — a delivery cap missing from the original WI-10
    inventory (M3 audit §1/§8) that silently mutilated long pasted user
    content and long assistant turns. Semantics now match the fossil-context
    guard: messages are delivered WHOLE; only if the estimated payload
    (system + history text) exceeds the verified anthropic window budget are
    the OLDEST whole messages dropped (never truncated mid-message). The
    final (current-turn) message is never dropped, and the surviving list
    still starts with a user turn (Anthropic requires it).

    The token estimate counts system + text parts only — image blocks are
    metered by the provider separately, and base64 payloads would explode a
    chars/2 floor estimate (the caller strips inline base64 blobs first).

    Returns (guarded_messages, dropped_count). Never mutates the input list.
    """
    from Orchestrator.context_builder import window_guard_budget_tokens
    from Orchestrator.tokenization import estimate_tokens

    def _text_tokens(m) -> int:
        c = m.get("content")
        if isinstance(c, str):
            return estimate_tokens(c)
        total = 0
        for part in c or []:
            if isinstance(part, dict) and part.get("type") == "text":
                total += estimate_tokens(part.get("text", ""))
        return total

    budget = window_guard_budget_tokens("anthropic")
    est = estimate_tokens(system_text) + sum(_text_tokens(m) for m in amsgs)
    if est <= budget:
        return amsgs, 0

    guarded = list(amsgs)
    dropped = 0
    while len(guarded) > 1 and est > budget:
        victim = guarded.pop(0)
        est -= _text_tokens(victim)
        dropped += 1
    # Anthropic requires the first message to be a user turn.
    while len(guarded) > 1 and guarded[0].get("role") != "user":
        est -= _text_tokens(guarded[0])
        guarded.pop(0)
        dropped += 1
    print(
        f"[CONTEXT] window guard dropped {dropped} oldest whole history "
        f"message(s) on the anthropic path (est {est:,} tokens vs budget {budget:,})"
    )
    return guarded, dropped


async def stream(messages: List[Dict], model: str, operator: str, env: ProviderEnv):
    """Stream Anthropic Claude responses with tools enabled.

    Adaptive thinking is enabled for every model anthropic_caps says takes it
    (4.6 and later) — these models interleave thinking with tool calls
    automatically, so the old "thinking vs tools" conflict no longer applies.
    Thinking content streams as `thinking` SSE events; Portal and Android
    already handle these event names.

    Yields SSE events: {"type": "thinking"|"content"|"done"|"error", "data": "..."}
    Every turn ends in exactly one ``done`` or ``error``: a refusal, a mid-stream
    error event or a stream cut off before message_delta is an error, never a
    done; a max_tokens stop and the tool-round cap end in done with a note in
    the content. The clients build the reply from the content events, so text
    that streamed BEFORE such an error is still saved by them, with the error
    (Portal chat-send.js, Android ChatViewModel); only an error with no content
    before it keeps the turn out of the ledger.
    """
    if not env.api_key:
        yield {"type": "error", "data": "ANTHROPIC_API_KEY not set"}
        return

    # Extract system message
    system_text = "\n\n".join([m.get("content", "") for m in messages if m.get("role") == "system"])

    # WI-10 (M7): the old truncate_large_content 15,000-char per-message cut
    # is GONE — history messages are delivered WHOLE. Its window-safety role
    # moved to _guard_anthropic_history below (drops OLDEST whole messages
    # only if the verified 1M-window budget would be exceeded — never a
    # mid-message truncation). What remains here is blob-stripping: inline
    # base64 data-URLs are junk in a text channel (media rides in proper
    # image blocks) and would bloat both the payload and the token estimate.
    def strip_media_blobs(text: str) -> str:
        if not isinstance(text, str):
            return text
        cleaned = re.sub(
            r'data:(image|audio|video)/[^;]+;base64,[A-Za-z0-9+/=\s]{1000,}',
            '[MEDIA DATA REMOVED]',
            text
        )
        if len(cleaned) != len(text):
            print(f"[ANTHROPIC] Stripped inline base64 media: {len(text):,} -> {len(cleaned):,} chars")
        return cleaned

    # Build Anthropic-format messages — filter out empty content (Anthropic requires non-empty)
    amsgs = []
    for m in messages:
        if m.get("role") in ("user", "assistant"):
            content = m.get("content")
            if isinstance(content, list):
                new_content = []
                for part in content:
                    if part.get("type") == "image_url":
                        url = part["image_url"]["url"]
                        try:
                            local_path, refused = env.load_attachment(url, IMAGE_EXTS)
                            if local_path is not None:
                                media_type = "image/jpeg" if local_path.suffix.lower() in [".jpg", ".jpeg"] else "image/png"
                                img_bytes = local_path.read_bytes()
                                base64_image = base64.b64encode(img_bytes).decode("utf-8")
                                new_content.append({
                                    "type": "image",
                                    "source": {"type": "base64", "media_type": media_type, "data": base64_image}
                                })
                            elif refused:
                                new_content.append({"type": "text", "text": refused})
                        except Exception as e:
                            print(f"Could not process image for Anthropic streaming: {e}")
                            new_content.append({"type": "text", "text": _ATTACHMENT_ERROR_NOTE})
                    elif part.get("type") == "text":
                        cleaned_text = strip_media_blobs(part.get("text", ""))
                        if cleaned_text:  # Skip empty text parts
                            new_content.append({"type": "text", "text": cleaned_text})
                    else:
                        # Audio, video and documents used to vanish here; Claude
                        # reads a PDF itself and gets a note for the rest.
                        new_content.extend(env.unreadable_parts(part, pdf_block=True) or [])
                if new_content:  # Skip messages with no content parts
                    amsgs.append({"role": m["role"], "content": new_content})
            else:
                cleaned_content = strip_media_blobs(content) if isinstance(content, str) else content
                # Skip empty/None content — Anthropic requires non-empty
                if cleaned_content:
                    amsgs.append({"role": m["role"], "content": cleaned_content})

    # Dropping empty and image-only-but-missing turns above can leave two
    # same-role turns adjacent, which the API 400s ("roles must alternate");
    # merge them like call_anthropic does (O02-chat-routes-A-14). The window
    # guard below only drops from the front, so the result still alternates.
    amsgs = _normalize_alternation(amsgs)

    # WI-10 (M7) window guard: history delivered whole; oldest whole messages
    # drop only if the verified anthropic window budget would be exceeded.
    amsgs, _hist_dropped = _guard_anthropic_history(amsgs, system_text)

    # Tools mode: always enabled for reliable tool calling
    print("[ANTHROPIC] Using TOOLS mode (all tools enabled)")
    headers = {
        "x-api-key": env.api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    payload = {
        "model": model,
        # Streaming keeps HTTP timeouts out of the way, so give the model room:
        # adaptive thinking at xhigh effort spends output tokens before the
        # answer, and 32K cut long turns off (every model this path serves
        # takes 64K or more).
        "max_tokens": 64000,
        "stream": True,
        "messages": amsgs,
        "tools": await asyncio.to_thread(env.get_tools, "anthropic", _last_user_msg(messages))
    }
    # Adaptive thinking, display and effort per model, or temperature for a
    # thinking-off legacy model (anthropic_caps; same rule as call_anthropic).
    _apply_anthropic_caps(payload, model, temperature=0.7)

    if system_text:
        payload["system"] = system_text

    # Debug: Log payload size to identify token bloat
    import json as json_module
    payload_json = json_module.dumps(payload)
    payload_size_chars = len(payload_json)
    system_size = len(system_text) if system_text else 0
    msgs_count = len(amsgs)
    print(f"[ANTHROPIC DEBUG] Payload size: {payload_size_chars:,} chars, System: {system_size:,} chars, Messages: {msgs_count}")

    # Log individual message sizes to find the culprit
    for i, msg in enumerate(amsgs):
        msg_json = json_module.dumps(msg)
        print(f"[ANTHROPIC DEBUG] Message {i} ({msg.get('role')}): {len(msg_json):,} chars")

    # Tool use tracking
    tool_uses = []
    current_tool_use = None
    max_tool_iterations = 30
    thinking_buffer = ""
    content_buffer = ""
    timeout = httpx.Timeout(ANTHROPIC_STREAM_READ_TIMEOUT_S, connect=ANTHROPIC_CONNECT_TIMEOUT_S)

    try:
        for iteration in range(max_tool_iterations):
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", env.url, headers=headers, json=payload) as response:
                    if response.status_code != 200:
                        error_text = await response.aread()
                        billing = provider_billing.check("anthropic", response.status_code, error_text, surface="chat")
                        if billing:
                            yield billing.event()
                            return
                        yield {"type": "error", "data": f"Anthropic API error: {error_text.decode()}"}
                        return
                    provider_billing.note_ok("anthropic")

                    thinking_buffer = ""
                    content_buffer = ""
                    current_block_type = None
                    stop_reason = None
                    stop_details = None
                    stream_error = None
                    # The assistant turn as the API produced it, in stream
                    # order: replayed verbatim after a tool call. Thinking and
                    # redacted_thinking blocks must come back unchanged, with
                    # their signatures, or the model loses its reasoning between
                    # tool calls and the next request can 400 on the signature
                    # (O02-chat-routes-A-01; the CU driver carries them the same way).
                    response_content_blocks = []
                    block_thinking = ""
                    block_signature = ""
                    block_text = ""
                    redacted_block = None
                    tool_uses = []
                    current_tool_use = None

                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue

                        data_str = line[6:]
                        try:
                            event = json.loads(data_str)
                            event_type = event.get("type")

                            # Track content block type
                            if event_type == "content_block_start":
                                block = event.get("content_block", {})
                                current_block_type = block.get("type")
                                if current_block_type == "thinking":
                                    # Per-block buffers: a response can carry several
                                    # thinking blocks, each under its own signature.
                                    block_thinking = block.get("thinking", "") or ""
                                    block_signature = block.get("signature", "") or ""
                                    yield {"type": "thinking_start", "data": ""}
                                elif current_block_type == "redacted_thinking":
                                    # Arrives whole in the start event; replayed verbatim.
                                    redacted_block = {"type": "redacted_thinking", "data": block.get("data", "")}
                                elif current_block_type == "text":
                                    block_text = ""
                                    yield {"type": "content_start", "data": ""}
                                elif current_block_type == "tool_use":
                                    # Start tracking a tool use
                                    current_tool_use = {
                                        "type": "tool_use",
                                        "id": block.get("id", ""),
                                        "name": block.get("name", ""),
                                        "input": {}
                                    }
                                    yield {"type": "tool_start", "data": block.get("name", "")}

                            # Handle deltas
                            elif event_type == "content_block_delta":
                                delta = event.get("delta", {})
                                delta_type = delta.get("type")

                                if delta_type == "thinking_delta":
                                    thinking_text = delta.get("thinking", "")
                                    if thinking_text:
                                        block_thinking += thinking_text
                                        thinking_buffer += thinking_text
                                        yield {"type": "thinking", "data": thinking_text}

                                elif delta_type == "signature_delta":
                                    block_signature += delta.get("signature", "")

                                elif delta_type == "text_delta":
                                    content_text = delta.get("text", "")
                                    if content_text:
                                        block_text += content_text
                                        content_buffer += content_text
                                        yield {"type": "content", "data": content_text}

                                elif delta_type == "input_json_delta":
                                    # Accumulate tool input JSON
                                    if current_tool_use is not None:
                                        partial_json = delta.get("partial_json", "")
                                        if '_input_json' not in current_tool_use:
                                            current_tool_use['_input_json'] = ""
                                        current_tool_use['_input_json'] += partial_json
                                        print(f"[TOOL-DEBUG] Accumulated JSON: {current_tool_use['_input_json']}")

                            elif event_type == "content_block_stop":
                                if current_block_type == "thinking":
                                    yield {"type": "thinking_end", "data": ""}
                                    # An unsigned thinking block cannot be replayed
                                    # (the API rejects it); its text still streamed.
                                    # An EMPTY signed one (display "omitted") is kept.
                                    if block_signature:
                                        response_content_blocks.append({
                                            "type": "thinking", "thinking": block_thinking,
                                            "signature": block_signature,
                                        })
                                elif current_block_type == "redacted_thinking" and redacted_block:
                                    response_content_blocks.append(redacted_block)
                                    redacted_block = None
                                elif current_block_type == "text":
                                    if block_text:  # the API refuses empty text blocks
                                        response_content_blocks.append({"type": "text", "text": block_text})
                                elif current_block_type == "tool_use" and current_tool_use:
                                    # Parse the accumulated JSON input
                                    try:
                                        input_json = current_tool_use.get('_input_json', '{}')
                                        print(f"[TOOL-DEBUG] Final JSON string: '{input_json}'")
                                        current_tool_use['input'] = json.loads(input_json) if input_json else {}
                                        print(f"[TOOL-DEBUG] Parsed input: {current_tool_use['input']}")
                                    except json.JSONDecodeError as e:
                                        print(f"[TOOL-DEBUG] JSON parse error: {e}")
                                        current_tool_use['input'] = {}
                                    if '_input_json' in current_tool_use:
                                        del current_tool_use['_input_json']
                                    tool_uses.append(current_tool_use)
                                    response_content_blocks.append(current_tool_use)
                                    print(f"[TOOL-DEBUG] Tool use recorded: {current_tool_use}")
                                    current_tool_use = None
                                current_block_type = None

                            elif event_type == "message_delta":
                                delta = event.get("delta", {})
                                stop_reason = delta.get("stop_reason")
                                stop_details = delta.get("stop_details") or event.get("stop_details")
                                usage = event.get("usage", {})
                                if usage:
                                    yield {"type": "usage", "data": {
                                        "prompt_tokens": usage.get("input_tokens", 0),
                                        "completion_tokens": usage.get("output_tokens", 0)
                                    }}

                            elif event_type == "message_stop":
                                pass  # Will check stop_reason after loop

                            elif event_type == "error":
                                # A mid-stream API error (overloaded_error, ...).
                                stream_error = event.get("error") or {}
                                break

                        except json.JSONDecodeError:
                            continue

                    if stream_error is not None:
                        detail = stream_error.get("message") or stream_error.get("type") or "unknown error"
                        yield {"type": "error", "data": f"Anthropic stream error: {detail}"}
                        return
                    if stop_reason is None:
                        # The connection ended before message_delta: whatever
                        # streamed is a fragment, so this is no done.
                        yield {"type": "error", "data": "Anthropic stream ended before the response finished."}
                        return
                    if stop_reason == "refusal":
                        # HTTP 200 with a declined request: report it as an
                        # error, never a done for the partial text.
                        refusal = _anthropic_refusal(stop_details)
                        print(f"[ANTHROPIC-STREAM] Refusal ({refusal['category'] or 'no category'})")
                        yield {"type": "error", "data": refusal["message"],
                               "error_detail": {"kind": "refusal", "provider": "anthropic",
                                                "reason": refusal["category"],
                                                "message": refusal["message"]}}
                        return

                    # Check if we need to handle tool use
                    if stop_reason == "tool_use" and tool_uses:
                        yield {"type": "tool_executing", "data": "Searching the web..."}

                        # Add assistant message with tool uses to conversation
                        payload["messages"].append({"role": "assistant", "content": response_content_blocks})

                        # Execute each tool and collect results
                        tool_results = []
                        for tool_use in tool_uses:
                            tool_name = tool_use.get("name", "")
                            tool_id = tool_use.get("id", "")
                            tool_input = tool_use.get("input", {})

                            outcome = await dispatch_tool(tool_name, tool_input, env.tool_context(operator, "stream_anthropic"))
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": outcome.result
                            })
                            for event in outcome.events:
                                yield event

                        # Add tool results to conversation
                        payload["messages"].append({"role": "user", "content": tool_results})

                        # Reset for next iteration
                        tool_uses = []
                        response_content_blocks = []
                        print(f"[ANTHROPIC-STREAM] Tool iteration {iteration + 1}, continuing...")
                        continue  # Continue to next iteration
                    else:
                        # No more tools, we're done. A turn cut off by its
                        # output limit says so (O02-chat-routes-A-09); a
                        # tool_use stop on a truncated block lands here too and
                        # never runs the half-written call.
                        if stop_reason in _ANTHROPIC_TRUNCATED_STOPS:
                            marker = ("\n\n" if content_buffer else "") + _TRUNCATION_NOTE
                            content_buffer += marker
                            yield {"type": "content", "data": marker}
                        yield {"type": "done", "data": {"thinking": thinking_buffer, "content": content_buffer}}
                        return

        # Every round ended in tool calls: still close the turn with its text
        # and done, which the client saves (O02-chat-routes-A-11).
        for event in _tool_cap_events(content_buffer, "thinking", thinking_buffer, max_tool_iterations):
            yield event

    except Exception as e:
        import traceback
        print(f"[ANTHROPIC-STREAM] Stream exception: {e}\n{traceback.format_exc()}")
        yield {"type": "error", "data": f"Stream error: {str(e)}"}
