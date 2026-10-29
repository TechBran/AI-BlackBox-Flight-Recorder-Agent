"""Golden contract harness for the chat provider loops (audit rank 74, M6 U38).

Every provider path a chat turn can take is replayed here from a RECORDED fake
provider response, and what the BlackBox does with it is captured:

  * stream paths go through the real POST /chat/stream route
    (chat_routes.chat_stream_post), so the capture is the exact client-facing
    SSE text: stream_start, the system_activity burst, every provider event
    (thinking, content, tool_start, tool_result, {kind}_task, browser_task,
    cli_task, usage, done, error + error_detail) and stream_end;
  * non-stream paths call the public call_<provider> functions that /chat,
    cron, SMS and MCP chat_with_context use, and capture the returned tuple or
    the raised HTTPException;
  * both capture the tool-call round trip: every request body sent upstream
    (so the assistant turn and tool results replayed to the provider are
    pinned), every tool executor call (name, args, session operator, origin
    device), every task created, every helper the native tool branches call.

One scenario = one JSON file under golden/providers/. The file holds the
scenario's INPUTS (client messages, the context the stubbed context builder
returns, the scripted upstream responses) and the OBSERVED capture. The test
(test_provider_goldens.py) replays the inputs through the current code and
asserts the capture is identical: SSE byte for byte, everything else value for
value. Regenerate after an intended behaviour change with

    BLACKBOX_REGEN_PROVIDER_GOLDENS=<dir outside the checkout> \
        pytest Orchestrator/tests/test_provider_goldens.py
    cp <dir>/*.json Orchestrator/tests/golden/providers/

(the suite's write guard keeps tests out of the checkout, so the run writes
the new files to that directory) and review the diff: it is exactly the
behaviour change.

Hermetic: no network (httpx.AsyncClient and requests.post are fakes), no live
store (the context builder, retrieval, media lookups and the tool executor are
recorders), the billing state, the custom-server registry and the uploads all
live under tmp_path, and the provider URLs and keys are fixed test values.
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any, Dict, List

GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "providers"
REGEN_ENV = "BLACKBOX_REGEN_PROVIDER_GOLDENS"

OPERATOR = "golden-op"
ORIGIN_DEVICE = "100.64.0.42"

# Fixed upstream endpoints and keys (never a real key shape: this repo is public).
URLS = {
    "OPENAI_URL": "https://openai.golden.test/v1/chat/completions",
    "ANTHROPIC_URL": "https://anthropic.golden.test/v1/messages",
    "XAI_URL": "https://xai.golden.test/v1/chat/completions",
    "GEMINI_BASE_URL": "https://gemini.golden.test/v1beta/models",
}
KEYS = {
    "openai": ("OPENAI_API_KEY", "test-openai-key"),
    "anthropic": ("ANTHROPIC_API_KEY", "test-anthropic-key"),
    "google": ("GOOGLE_API_KEY", "test-google-key"),
    "xai": ("XAI_API_KEY", "test-xai-key"),
}
CUSTOM_SERVER = {
    "id": "srv-golden01",
    "alias": "gbox",
    "base_url": "http://gbox.golden.test:8080/v1",
    "api_key": "test-custom-key",
    "context_tokens": 32768,
    "model_context": {},
    "enabled": True,
    "added_at": "2026-10-01T00:00:00+00:00",
    "validated_at": "2026-10-01T00:00:00+00:00",
    "last_models": ["gmodel-a", "gmodel-b"],
    "model_modalities": {},
    "audio": {},
}

MODELS = {
    "openai": "gpt-5.5",
    "anthropic": "claude-opus-5-5",
    "google": "gemini-3.1-pro-preview",
    "xai": "grok-3-mini",
    "custom": "gbox::gmodel-a",
}

# The call_* function and the stream route provider name per provider.
CALLS = {"openai": "call_openai", "anthropic": "call_anthropic", "google": "call_gemini",
         "xai": "call_xai", "custom": "call_custom"}

UPLOADS = {
    "photo.png": b"\x89PNG golden photo bytes",
    "notes.txt": "Golden notes: the launch is on Tuesday.\n".encode(),
    "clip.mp4": b"golden mp4 bytes",
    "voice.mp3": b"golden mp3 bytes",
    "ref.png": b"\x89PNG golden reference",
    "report.pdf": b"%PDF-1.4 golden pdf",
}

SNAPSHOTS = [
    "=== START SNAPSHOT — UTC 2026-10-01T10:00:00Z — SNAP-20261001-0001 ===\n"
    "OPERATOR: golden-op\n"
    "## Raw Session Log\n2026-10-01T10:00:00Z operator=golden-op user: ember backdrop?\n"
    "2026-10-01T10:00:01Z operator=golden-op assistant: The ember backdrop shipped.\n"
    "=== END SNAPSHOT — SNAP-20261001-0001 ===\n",
    "=== START SNAPSHOT — UTC 2026-10-02T10:00:00Z — SNAP-20261002-0002 ===\n"
    "OPERATOR: golden-op\n"
    "## Raw Session Log\n2026-10-02T10:00:00Z operator=golden-op user: particles?\n"
    "2026-10-02T10:00:01Z operator=golden-op assistant: Three particle modes.\n"
    "=== END SNAPSHOT — SNAP-20261002-0002 ===\n",
]

# Every tool branch a provider loop has (native, explicit-executor, catch-all,
# CLI, media catch-all, empty result), in one parallel tool round.
TOOLSET = [
    ("web_fetch", {"url": "https://example.com/a", "max_chars": 5000}),
    ("gemini_image", {"prompt": "a red fox in snow", "aspectRatio": "1:1", "numberOfImages": 2,
                      "reference_images": ["/ui/uploads/ref.png"]}),
    ("openai_image", {"prompt": "a lighthouse at dusk"}),
    ("generate_video", {"prompt": "waves at dusk", "duration": 6, "image_url": "/ui/uploads/ref.png"}),
    ("generate_video", {"prompt": "keep going", "video_url": "/ui/uploads/clip.mp4"}),
    ("lyria_music", {"prompt": "calm piano", "sampleCount": 2}),
    ("get_media", {"task_id": "task-old"}),
    ("list_media", {"media_type": "image", "limit": 5}),
    ("search_media", {"query": "fox"}),
    ("search_snapshots", {"query": "ember backdrop", "k": 4}),
    ("send_sms", {"phone_number": "+15550100", "message": "on my way"}),
    ("make_phone_call", {"phone_number": "+15550101", "greeting": "hello"}),
    ("make_voice_call", {"phone_number": "+15550102", "message": "reminder"}),
    ("search_contacts", {"query": "sam"}),
    ("save_contact", {"name": "Sam", "phone": "+15550103"}),
    ("create_cron_job", {"name": "daily", "schedule": "0 9 * * *", "prompt": "brief me"}),
    ("edit_cron_job", {"job_id": "job-1", "prompt": "brief me later"}),
    ("search_cron_jobs", {"query": "daily"}),
    ("use_computer", {"prompt": "open the docs"}),
    ("list_devices", {}),
    ("gmail_search", {"query": "invoice"}),
    ("get_task_status", {"task_id": "task-x"}),
    ("extend_video", {"video_url": "/ui/uploads/clip.mp4", "prompt": "longer"}),
    ("claude_code_task", {"prompt": "fix the bug"}),
    ("elevenlabs_music", {"prompt": "upbeat jingle"}),
    ("roll_dice", {"sides": 6}),
    ("quiet_tool", {}),
]

# Tools whose fake executor result carries a task_id in .data.
_TASK_DATA_TOOLS = {"gemini_image", "openai_image", "grok_image", "local_image", "generate_image",
                    "generate_video", "extend_video", "lyria_music", "elevenlabs_music",
                    "use_computer", "claude_code_task", "gemini_cli_task", "codex_cli_task"}


# --------------------------------------------------------------------------- #
# Provider-neutral round scripts -> each wire format                          #
# --------------------------------------------------------------------------- #

def _round(text=(), thinking=None, tools=(), stop=None, usage=(40, 12)):
    return {"text": list(text), "thinking": thinking, "tools": [list(t) for t in tools],
            "stop": stop, "usage": list(usage)}


def _tool_calls(tools):
    return [(f"call_{i:02d}", name, args) for i, (name, args) in enumerate(tools)]


def _split(s: str) -> List[str]:
    half = max(1, len(s) // 2)
    return [s[:half], s[half:]] if len(s) > 1 else [s]


def _sse(obj) -> List[str]:
    return [f"data: {json.dumps(obj)}", ""]


def openai_stream(r) -> Dict[str, Any]:
    lines: List[str] = []
    if r["thinking"]:
        lines += _sse({"id": "chatcmpl-g", "choices": [{"index": 0, "delta": {
            "role": "assistant", "reasoning_content": r["thinking"]}, "finish_reason": None}]})
    for chunk in r["text"]:
        lines += _sse({"id": "chatcmpl-g", "choices": [{"index": 0, "delta": {"content": chunk},
                                                         "finish_reason": None}]})
    for i, (tid, name, args) in enumerate(_tool_calls(r["tools"])):
        lines += _sse({"id": "chatcmpl-g", "choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": i, "id": tid, "type": "function",
            "function": {"name": name, "arguments": ""}}]}, "finish_reason": None}]})
        for part in _split(json.dumps(args)):
            lines += _sse({"id": "chatcmpl-g", "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": i, "function": {"arguments": part}}]}, "finish_reason": None}]})
    finish = "tool_calls" if r["tools"] else (r["stop"] or "stop")
    lines += _sse({"id": "chatcmpl-g", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    p, c = r["usage"]
    lines += _sse({"id": "chatcmpl-g", "choices": [],
                   "usage": {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}})
    lines += ["data: [DONE]", ""]
    return {"status": 200, "lines": lines}


def openai_json(r) -> Dict[str, Any]:
    message: Dict[str, Any] = {"role": "assistant", "content": "".join(r["text"]) or None}
    if r["thinking"]:
        message["reasoning_content"] = r["thinking"]
    if r["tools"]:
        message["tool_calls"] = [{"id": tid, "type": "function",
                                  "function": {"name": name, "arguments": json.dumps(args)}}
                                 for tid, name, args in _tool_calls(r["tools"])]
    finish = "tool_calls" if r["tools"] else (r["stop"] or "stop")
    p, c = r["usage"]
    return {"status": 200, "json": {
        "id": "chatcmpl-g", "object": "chat.completion",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}}}


def _anthropic_blocks(r, n):
    blocks = []
    if r["thinking"]:
        blocks.append({"type": "thinking", "thinking": r["thinking"], "signature": f"sig-{n}"})
    if r["text"]:
        blocks.append({"type": "text", "text": "".join(r["text"])})
    for tid, name, args in _tool_calls(r["tools"]):
        blocks.append({"type": "tool_use", "id": f"toolu_{tid}", "name": name, "input": args})
    return blocks


def anthropic_stream(r, n=0) -> Dict[str, Any]:
    events = [{"type": "message_start", "message": {
        "id": f"msg_g{n}", "type": "message", "role": "assistant", "model": MODELS["anthropic"],
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": r["usage"][0], "output_tokens": 1}}}]
    idx = 0
    if r["thinking"]:
        events += [
            {"type": "content_block_start", "index": idx,
             "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": idx,
             "delta": {"type": "thinking_delta", "thinking": r["thinking"]}},
            {"type": "content_block_delta", "index": idx,
             "delta": {"type": "signature_delta", "signature": f"sig-{n}"}},
            {"type": "content_block_stop", "index": idx}]
        idx += 1
    if r["text"]:
        events.append({"type": "content_block_start", "index": idx,
                       "content_block": {"type": "text", "text": ""}})
        events += [{"type": "content_block_delta", "index": idx,
                    "delta": {"type": "text_delta", "text": t}} for t in r["text"]]
        events.append({"type": "content_block_stop", "index": idx})
        idx += 1
    for tid, name, args in _tool_calls(r["tools"]):
        events.append({"type": "content_block_start", "index": idx, "content_block": {
            "type": "tool_use", "id": f"toolu_{tid}", "name": name, "input": {}}})
        events += [{"type": "content_block_delta", "index": idx,
                    "delta": {"type": "input_json_delta", "partial_json": part}}
                   for part in _split(json.dumps(args))]
        events.append({"type": "content_block_stop", "index": idx})
        idx += 1
    stop = "tool_use" if r["tools"] else (r["stop"] or "end_turn")
    delta: Dict[str, Any] = {"stop_reason": stop, "stop_sequence": None}
    if r.get("stop_details"):
        delta["stop_details"] = r["stop_details"]
    events += [{"type": "message_delta", "delta": delta, "usage": {"output_tokens": r["usage"][1]}},
               {"type": "message_stop"}]
    lines: List[str] = []
    for e in events:
        lines += [f"event: {e['type']}", f"data: {json.dumps(e)}", ""]
    return {"status": 200, "lines": lines}


def anthropic_json(r, n=0) -> Dict[str, Any]:
    stop = "tool_use" if r["tools"] else (r["stop"] or "end_turn")
    body = {"id": f"msg_g{n}", "type": "message", "role": "assistant", "model": MODELS["anthropic"],
            "content": _anthropic_blocks(r, n), "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": r["usage"][0], "output_tokens": r["usage"][1]}}
    if r.get("stop_details"):
        body["stop_details"] = r["stop_details"]
    return {"status": 200, "json": body}


def _gemini_parts(r, n):
    parts = []
    if r["thinking"]:
        parts.append({"text": r["thinking"], "thought": True})
    parts += [{"text": t} for t in r["text"]]
    for i, (_tid, name, args) in enumerate(_tool_calls(r["tools"])):
        part: Dict[str, Any] = {"functionCall": {"name": name, "args": args}}
        if i == 0:
            part["thoughtSignature"] = f"gsig-{n}"
        parts.append(part)
    return parts


def _gemini_usage(r):
    p, c = r["usage"]
    return {"promptTokenCount": p, "candidatesTokenCount": c, "totalTokenCount": p + c}


def gemini_stream(r, n=0) -> Dict[str, Any]:
    lines: List[str] = []
    parts = _gemini_parts(r, n)
    for i, part in enumerate(parts):
        chunk: Dict[str, Any] = {"candidates": [{"content": {"role": "model", "parts": [part]}}]}
        if i == len(parts) - 1:
            chunk["candidates"][0]["finishReason"] = "STOP"
            chunk["usageMetadata"] = _gemini_usage(r)
        lines += _sse(chunk)
    return {"status": 200, "lines": lines}


def gemini_json(r, n=0) -> Dict[str, Any]:
    return {"status": 200, "json": {
        "candidates": [{"content": {"role": "model", "parts": _gemini_parts(r, n)},
                        "finishReason": "STOP"}],
        "usageMetadata": _gemini_usage(r)}}


def encode(provider: str, path: str, rounds) -> List[Dict[str, Any]]:
    """The scripted upstream responses for ``rounds`` in ``provider``'s wire format."""
    out = []
    for n, r in enumerate(rounds):
        if "status" in r:          # an already-encoded (error) response
            out.append(r)
        elif provider in ("openai", "xai", "custom"):
            out.append(openai_stream(r) if path == "stream" else openai_json(r))
        elif provider == "anthropic":
            out.append(anthropic_stream(r, n) if path == "stream" else anthropic_json(r, n))
        else:
            out.append(gemini_stream(r, n) if path == "stream" else gemini_json(r, n))
    return out


def _error(status, message, typ="server_error"):
    return {"status": status, "body": json.dumps({"error": {"message": message, "type": typ}})}


_BILLING = {
    "openai": _error(429, "You exceeded your current quota.", "insufficient_quota"),
    "xai": _error(429, "Your team doesn't have any credits.", "invalid_request"),
    "anthropic": _error(400, "Your credit balance is too low to access the Anthropic API.",
                        "invalid_request_error"),
    "google": _error(429, "Your prepayment credits are depleted.", "RESOURCE_EXHAUSTED"),
}


# --------------------------------------------------------------------------- #
# Scenarios                                                                   #
# --------------------------------------------------------------------------- #

def _history(user_content):
    return [
        {"role": "user", "content": "What did we ship last week?"},
        {"role": "assistant", "content": "The ember backdrop and the particle modes."},
        {"role": "user", "content": user_content},
    ]


_ATTACHMENTS = [
    {"type": "text", "text": "Look at these attachments."},
    {"type": "image_url", "image_url": {"url": "/ui/uploads/photo.png"}},
    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QkFTRTY0"}},
    {"type": "image_url", "image_url": {"url": "/ui/uploads/missing.png"}},
    {"type": "document_url", "document_url": {"url": "/ui/uploads/notes.txt"}},
    {"type": "document_url", "document_url": {"url": "/ui/uploads/report.pdf"}},
    {"type": "video_url", "video_url": {"url": "/ui/uploads/clip.mp4"}},
    {"type": "audio_url", "audio_url": {"url": "/ui/uploads/voice.mp3"}},
]


def scenario_specs() -> List[Dict[str, Any]]:
    """Every scenario: provider x path x case (inputs only)."""
    specs = []
    tool_round = _round(text=["Working ", "on it."], thinking="I will use every tool.",
                        tools=TOOLSET, usage=(120, 30))
    final_round = _round(text=["All ", "done."], thinking="Summarize the results.", usage=(400, 20))
    for provider in ("openai", "anthropic", "google", "xai", "custom"):
        for path in ("stream", "nonstream"):
            def add(case, rounds, user=None, repeat_last=False, record="full"):
                specs.append({
                    "name": f"{provider}-{path}-{case}", "provider": provider, "path": path,
                    "case": case, "model": MODELS[provider],
                    "messages": _history(user or "Please answer briefly."),
                    "upstream": encode(provider, path, rounds),
                    "repeat_last": repeat_last, "record_upstream": record,
                })

            add("text", [_round(text=["Hello ", "there."], thinking="A short greeting.")])
            add("tools", [tool_round, final_round], user="Do all the things.")
            add("attachments", [_round(text=["I see them."])], user=_ATTACHMENTS)
            add("http_error", [_error(500, "upstream exploded")])
            if provider != "custom":
                add("billing", [_BILLING[provider]])
            add("tool_cap", [_round(text=["Again."], tools=[("roll_dice", {"sides": 6})])],
                repeat_last=True, record="count")
            if provider == "openai":
                add("reasoning_effort_retry",
                    [_error(400, "Unsupported value: reasoning_effort default with tools"),
                     _round(text=["Retried fine."])])
            if provider == "anthropic":
                add("max_tokens", [_round(text=["A long answer that"], stop="max_tokens")])
                refusal = _round(text=["Sure, the first"], stop="refusal")
                refusal["stop_details"] = {"type": "refusal", "category": "cyber",
                                           "explanation": "Declined by a classifier."}
                add("refusal", [refusal])
            if provider == "custom":
                add("exceed_context", [{"status": 400, "body": json.dumps({"error": {
                    "code": 400, "message": "request (18079 tokens) exceeds the available "
                    "context size (16384 tokens)", "type": "exceed_context_size_error",
                    "n_prompt_tokens": 18079, "n_ctx": 16384}})}])
                if path == "stream":
                    add("stream_options_retry",
                        [_error(400, "unknown field stream_options"), _round(text=["Old build ok."])])
            if provider == "google":
                add("error_after_tools", [tool_round, _error(503, "model overloaded")],
                    user="Do all the things.")
                if path == "nonstream":
                    specs.append({
                        "name": "google-nonstream-blocked", "provider": provider, "path": path,
                        "case": "blocked", "model": MODELS[provider],
                        "messages": _history("Please answer briefly."),
                        "upstream": [{"status": 200, "json": {
                            "promptFeedback": {"blockReason": "SAFETY"}}}],
                        "repeat_last": False, "record_upstream": "full"})
    return specs


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #

class _Capture:
    def __init__(self, spec):
        self.spec = spec
        self.responses = copy.deepcopy(spec["upstream"])
        self.calls = 0
        self.upstream: List[Dict[str, Any]] = []
        self.tool_executor: List[Dict[str, Any]] = []
        self.tasks: List[Dict[str, Any]] = []
        self.helpers: List[Dict[str, Any]] = []
        self.get_tools: List[Dict[str, Any]] = []
        self.context_builds: List[Dict[str, Any]] = []

    def next_response(self, method, url, headers, body, timeout):
        self.calls += 1
        self.upstream.append({"method": method, "url": url, "headers": dict(headers or {}),
                              "json": copy.deepcopy(body), "timeout": repr(timeout)})
        if self.calls <= len(self.responses):
            return self.responses[self.calls - 1]
        if self.spec["repeat_last"]:
            return self.responses[-1]
        raise RuntimeError("golden script exhausted: an unexpected extra upstream request")


def _httpx_factory(cap: _Capture):
    class _Resp:
        def __init__(self, r):
            self.status_code = r["status"]
            self._r = r

        async def aread(self):
            return self._r.get("body", "").encode()

        async def aiter_lines(self):
            for line in self._r.get("lines", []):
                yield line

    class _Ctx:
        def __init__(self, r):
            self._r = r

        async def __aenter__(self):
            return _Resp(self._r)

        async def __aexit__(self, *exc):
            return False

    class _Client:
        def __init__(self, *args, **kwargs):
            self._timeout = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, headers=None, json=None, **kw):
            return _Ctx(cap.next_response(method, url, headers, json, self._timeout))

    return _Client


class _PostResp:
    reason = None

    def __init__(self, r):
        self.status_code = r["status"]
        self._r = r
        self.text = r["body"] if "body" in r else json.dumps(r.get("json"))

    def json(self):
        if "json" in self._r:
            return copy.deepcopy(self._r["json"])
        return json.loads(self._r["body"])


def _executor_class(cap: _Capture):
    from Orchestrator.toolvault.context import ToolResult

    class _GoldenExecutor:
        def __init__(self, operator="system", base_url=None, origin_device_id=None, caller=None):
            self.operator = operator
            self.origin_device_id = origin_device_id

        async def execute(self, tool_name, tool_input):
            cap.tool_executor.append({"name": tool_name, "args": copy.deepcopy(tool_input),
                                      "operator": self.operator,
                                      "origin_device_id": self.origin_device_id})
            if tool_name == "quiet_tool":
                return ToolResult(success=True, result="")
            data = {"task_id": f"exec-{tool_name}"} if tool_name in _TASK_DATA_TOOLS else None
            result = f"[{tool_name}] ok: {json.dumps(tool_input, sort_keys=True)}"
            return ToolResult(success=True, result=result, data=data)

    return _GoldenExecutor


class _Task:
    def __init__(self, task_id):
        self.task_id = task_id


def install(spec, monkeypatch, tmp_path) -> _Capture:
    """Point chat_routes at the fakes for ``spec``; returns the capture."""
    import httpx
    import requests

    import Orchestrator.routes.chat_routes as cr
    import Orchestrator.tools as tools_pkg
    from Orchestrator import provider_billing as pb
    from Orchestrator.onboarding import custom_servers as cs
    from Orchestrator.utils import paths

    cap = _Capture(spec)
    provider = spec["provider"]

    for name, value in URLS.items():
        monkeypatch.setattr(cr, name, value)
    for prov, (attr, value) in KEYS.items():
        monkeypatch.setattr(cr, attr, value if prov == provider else "")

    monkeypatch.setattr(httpx, "AsyncClient", _httpx_factory(cap))

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        return _PostResp(cap.next_response("POST", url, headers, json, timeout))

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(tools_pkg, "BlackBoxToolExecutor", _executor_class(cap))

    def fake_get_tools(prov, prompt="", group="chat"):
        cap.get_tools.append({"provider": prov, "prompt": prompt, "group": group})
        return [{"golden_tools_for": prov, "group": group}]

    monkeypatch.setattr(cr, "_get_tools", fake_get_tools)

    def fake_create_task(task_type, operator=None, **kwargs):
        task_id = f"task-{len(cap.tasks) + 1:03d}"
        cap.tasks.append({"task_id": task_id, "type": getattr(task_type, "name", str(task_type)),
                          "operator": operator, **copy.deepcopy(kwargs)})
        return _Task(task_id)

    monkeypatch.setattr(cr, "create_task", fake_create_task)

    def fake_web_fetch(url, max_chars=80000):
        cap.helpers.append({"fn": "perform_web_fetch", "url": url, "max_chars": max_chars})
        return f"PAGE TEXT OF {url}"

    def fake_hybrid(vol_txt, query, k=3, operator=""):
        cap.helpers.append({"fn": "hybrid_retrieve", "vol_txt": vol_txt, "query": query, "k": k,
                            "operator": operator})
        return list(SNAPSHOTS)

    def fake_get_media(url=None, task_id=None):
        cap.helpers.append({"fn": "execute_get_media", "url": url, "task_id": task_id})
        return {"success": True, "url": url or f"/ui/uploads/{task_id}.png", "type": "image"}

    def fake_list_media(media_type=None, limit=20):
        cap.helpers.append({"fn": "execute_list_media", "media_type": media_type, "limit": limit})
        return {"success": True, "count": 2, "files": [{"url": "/ui/uploads/a.png"},
                                                       {"url": "/ui/uploads/b.png"}]}

    def fake_search_media(query, media_type=None, limit=10):
        cap.helpers.append({"fn": "execute_search_media", "query": query,
                            "media_type": media_type, "limit": limit})
        return {"success": True, "count": 1, "results": [{"url": "/ui/uploads/fox.png"}]}

    monkeypatch.setattr(cr, "perform_web_fetch", fake_web_fetch)
    monkeypatch.setattr(cr, "hybrid_retrieve", fake_hybrid)
    monkeypatch.setattr(cr, "execute_get_media", fake_get_media)
    monkeypatch.setattr(cr, "execute_list_media", fake_list_media)
    monkeypatch.setattr(cr, "execute_search_media", fake_search_media)
    monkeypatch.setattr(cr, "read_text_safe", lambda *a, **k: "")

    def fake_build_context(messages, operator, provider="openai", window_guard_tokens=None):
        cap.context_builds.append({"operator": operator, "provider": provider,
                                   "window_guard_tokens": window_guard_tokens})
        ctx = [{"role": "system", "content": "GOLDEN SYSTEM PROMPT"},
               {"role": "system", "content": "GOLDEN FOSSILS"}] + list(messages)
        provenance = {"recent": ["SNAP-20261001-0001"], "keyword": ["SNAP-20261002-0002"],
                      "semantic": [], "checkpoint": []}
        return ctx, provenance, {"embed_model": "golden-embed", "embed_dims": 8}

    monkeypatch.setattr(cr, "build_streaming_context", fake_build_context)

    # Billing state and pushes stay in tmp; the custom registry is one fixed server.
    monkeypatch.setattr(pb, "STATE_PATH", tmp_path / "provider_billing_state.json")
    monkeypatch.setattr(pb, "_push", lambda *a, **k: False)
    pb._reset_cache()
    registry = tmp_path / "custom_models.json"
    registry.write_text(json.dumps({"servers": [CUSTOM_SERVER]}))
    monkeypatch.setattr(cs, "REGISTRY_PATH", registry)

    # Uploads the attachment and tool-argument loaders resolve.
    root = tmp_path / "box"
    uploads = root / "Portal" / "uploads"
    uploads.mkdir(parents=True)
    for name, data in UPLOADS.items():
        (uploads / name).write_bytes(data)
    monkeypatch.setenv("BLACKBOX_ROOT", str(root))
    paths.blackbox_root.cache_clear()
    return cap


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


async def _drain(resp) -> str:
    chunks = []
    async for chunk in resp.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
    return "".join(chunks)


def _jsonable(value):
    """A JSON-safe copy of a call_* return value, keeping str subclasses visible."""
    if isinstance(value, tuple):
        value = list(value)
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, str) and type(value) is not str:
        return {"__str_type__": type(value).__name__, "value": str(value)}
    return value


def run(spec, monkeypatch, tmp_path) -> Dict[str, Any]:
    """Replay ``spec`` through the current code; returns the observed capture."""
    import Orchestrator.routes.chat_routes as cr
    from Orchestrator.utils import paths

    cap = install(spec, monkeypatch, tmp_path)
    observed: Dict[str, Any] = {}
    try:
        if spec["path"] == "stream":
            body = {"messages": copy.deepcopy(spec["messages"]), "provider": spec["provider"],
                    "model": spec["model"], "operator": OPERATOR, "origin_device_id": ORIGIN_DEVICE}

            async def go():
                resp = await cr.chat_stream_post(_Req(body))
                return await _drain(resp)

            observed["sse"] = asyncio.run(go())
            observed["context_builds"] = cap.context_builds
        else:
            ctx = [{"role": "system", "content": "GOLDEN SYSTEM PROMPT"},
                   {"role": "system", "content": "GOLDEN FOSSILS"}] + copy.deepcopy(spec["messages"])
            fn = getattr(cr, CALLS[spec["provider"]])
            token = cr._ORIGIN_DEVICE_ID.set(ORIGIN_DEVICE)
            try:
                observed["result"] = _jsonable(fn(ctx, spec["model"], operator=OPERATOR))
            except Exception as exc:  # noqa: BLE001 - the raised error IS the contract here
                observed["raised"] = {"type": type(exc).__name__,
                                      "status_code": getattr(exc, "status_code", None),
                                      "detail": getattr(exc, "detail", str(exc))}
            finally:
                cr._ORIGIN_DEVICE_ID.reset(token)
    finally:
        paths.blackbox_root.cache_clear()
    if spec["record_upstream"] == "count":
        observed["upstream"] = {"count": len(cap.upstream)}
    else:
        observed["upstream"] = cap.upstream
    observed["tool_executor"] = cap.tool_executor
    observed["tasks"] = cap.tasks
    observed["helpers"] = cap.helpers
    observed["get_tools"] = cap.get_tools
    return json.loads(json.dumps(observed))


def golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.json"


def write_golden(spec, observed, out_dir) -> None:
    """Write one regenerated golden into ``out_dir`` (outside the checkout)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{spec['name']}.json").write_text(
        json.dumps({"scenario": spec, "observed": observed}, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8")


def read_golden(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
