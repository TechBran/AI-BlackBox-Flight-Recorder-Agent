#!/usr/bin/env python3
"""Write the /chat/stream SSE fixtures for the Android golden + reducer tests.

Each frame is built exactly as Orchestrator/routes/chat_routes.py chat_stream_post's
generate_sse() writes it:
    stream_start   f"event: stream_start\\ndata: {json.dumps({'provider','model','provenance'})}\\n\\n"
    retrieval      f"event: {_sa['type']}\\ndata: {json.dumps(_sa['data'])}\\n\\n"  (system_activity)
    provider evts  f"event: {event_type}\\ndata: {json.dumps(event_data)}\\n\\n"
    keepalive      ": keepalive\\n\\n"                                              (SSE_KEEPALIVE_FRAME)
    stream_end     f"event: stream_end\\ndata: {json.dumps({'status': 'complete'})}\\n\\n"
The event orders follow the provider streams and the 2026-07-02 provider-window probe
counts (docs/plans/artifacts/2026-07-02-provider-window-probes.json): gemini
stream_start/thinking/usage/content/done/stream_end, anthropic stream_start/content_start/
content x2/usage/done/stream_end, openai stream_start/usage x4/content/done/stream_end.

usage: gen_sse_fixtures.py <out-dir>
"""
import json
import os
import sys

KEEPALIVE = ": keepalive\n\n"


def ev(t, data):
    return f"event: {t}\ndata: {json.dumps(data)}\n\n"


def start(provider, model, provenance=None):
    return ev("stream_start", {"provider": provider, "model": model, "provenance": provenance})


def end(status="complete"):
    return ev("stream_end", {"status": status})


def activity(seq, stage, label, **detail):
    return ev("system_activity", {"stage": stage, "label": label, "detail": detail, "seq": seq})


PROV = {"recent": ["SNAP-20261029-0001", "SNAP-20261029-0002"], "keyword": ["SNAP-20261001-0100"],
        "semantic": ["SNAP-20260901-0042"], "checkpoint": []}

FIXTURES = {
    # Anthropic, a retrieval turn: the Signal burst, content_start, two deltas, usage, done.
    "anthropic_retrieval": ("anthropic", [
        start("anthropic", "claude-opus-5-5", PROV),
        activity(0, "resolve_model", "brain · claude-opus-5-5 · 200k window", provider="anthropic", model="claude-opus-5-5"),
        activity(1, "embed", "embed · gemini-embedding-001 · 3072d", model="gemini-embedding-001", dims=3072),
        activity(2, "search", "search · 4 snapshots", hits=4),
        KEEPALIVE,
        ev("content_start", ""),
        ev("content", "Hello"),
        ev("content", " — world.\nLine two with \"quotes\" and \\ backslash."),
        ev("usage", {"prompt_tokens": 35646, "completion_tokens": 12}),
        ev("done", {"content": "Hello — world.\nLine two with \"quotes\" and \\ backslash.", "reasoning": ""}),
        end(),
    ]),
    # Gemini with thinking: thinking deltas first, then content; done carries both.
    "gemini_thinking": ("gemini", [
        start("google", "gemini-3.1-pro-preview", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []}),
        ev("thinking_start", ""),
        ev("thinking", "Considering the question. "),
        ev("thinking", "Checking memory."),
        ev("thinking_end", ""),
        ev("usage", {"prompt_tokens": 22339, "completion_tokens": 1}),
        ev("content", "OK"),
        ev("usage", {"prompt_tokens": 22339, "completion_tokens": 3}),
        ev("done", {"thinking": "Considering the question. Checking memory.", "content": "OK"}),
        end(),
    ]),
    # OpenAI with a tool round and an image task: tool_start/heartbeat/tool_result, usage x2.
    "openai_tool_image": ("openai", [
        start("openai", "gpt-5.5", None),
        ev("usage", {"prompt_tokens": 20785, "completion_tokens": 0}),
        ev("tool_start", "generate_image"),
        ev("heartbeat", ""),
        ev("tool_result", "generate_image: queued"),
        ev("image_task", {"task_id": "img-task-1", "prompt": "a red fox", "count": 1,
                          "predicted_url": "/ui/uploads/a_red_fox_img-task-1.png"}),
        ev("content", "Here is your fox."),
        ev("usage", {"prompt_tokens": 20785, "completion_tokens": 10}),
        ev("done", {"content": "Here is your fox.", "reasoning": ""}),
        end(),
    ]),
    # An object-shaped tool_start (the planned structured tool events) and an unknown event.
    "xai_structured_tool": ("xai", [
        start("xai", "grok-4.3", None),
        ev("tool_start", {"name": "web_search", "args": {"q": "weather"}}),
        ev("tool_executing", {"name": "web_search"}),
        ev("tool_result", {"name": "web_search", "ok": True}),
        ev("content", "Sunny."),
        ev("done", {"content": "Sunny."}),
        end(),
    ]),
    # Video and music tasks, the second as a bare JSON string (the plain-string branch).
    "media_tasks": ("gemini", [
        start("google", "gemini-3.1-pro-preview", None),
        ev("video_task", {"task_id": "vid-task-9", "prompt": "waves"}),
        ev("music_task", "mus-task-3"),
        ev("content", "Rendering a video and a track."),
        ev("done", {"content": "Rendering a video and a track."}),
        end(),
    ]),
    # Only an error arrives: a failed turn (no save, the user turn offers retry).
    "error_only": ("anthropic", [
        start("anthropic", "claude-opus-5-5", None),
        ev("error_detail", {"kind": "credits_exhausted", "provider": "anthropic", "reason": "billing",
                            "message": "Anthropic credits are exhausted."}),
        ev("error", "Anthropic credits are exhausted."),
        end(),
    ]),
    # A transient error mid-stream, then the real reply: not a failure; the error is shown,
    # never saved.
    "error_then_content": ("openai", [
        start("openai", "gpt-5.5", None),
        ev("content", "Partial answer"),
        ev("error", "upstream hiccup, retrying"),
        ev("content", " and the rest."),
        ev("done", {"content": "Partial answer and the rest."}),
        end(),
    ]),
    # done is longer than what streamed: its content and reasoning replace the buffers.
    "done_fills_gaps": ("anthropic", [
        start("anthropic", "claude-sonnet-5-5", None),
        ev("content", "Hel"),
        ev("thinking", "short"),
        ev("done", {"content": "Hello, the full reply.", "reasoning": "a much longer chain of reasoning"}),
        end(),
    ]),
    # An empty completion: nothing to save.
    "empty_completion": ("gemini", [
        start("google", "gemini-3.1-pro-preview", None),
        ev("done", {"content": ""}),
        end(),
    ]),
    # Computer use: session, steps, screenshot, action, bash and file edits; never saved by
    # the phone (the box's CU loop mints it), the Signal stays dark.
    "computer_use": ("computer-use", [
        start("computer-use", "claude-opus-5-5", None),
        ev("cu_session", {"session_id": "cu-sess-1", "device_id": "blackbox"}),
        ev("cu_step", {"step": 1, "total": 5}),
        ev("cu_screenshot", {"url": "/ui/uploads/cu_step1.png", "step": 1}),
        ev("tool_start", "computer"),
        ev("cu_action", {"action": "left_click (640, 360)", "step": 2}),
        ev("cu_bash_output", {"command": "ls /tmp", "output": "a.txt\nb.txt"}),
        ev("cu_file_edit", {"command": "create", "path": "/tmp/c.txt"}),
        ev("heartbeat", {"step": 3}),
        ev("content", "\nDone clicking."),
        ev("done", {"content": "Done clicking."}),
        end(),
    ]),
    # A CU prompt queued behind a running task: the bubble is dropped, nothing saved.
    "cu_queued": ("computer-use", [
        start("computer-use", "claude-opus-5-5", None),
        ev("cu_queued", {"position": 2, "prompt": "then open the browser"}),
        end(),
    ]),
    # The running CU task picks up the next queued prompt in this stream: done must not
    # replace the two replies.
    "cu_queue_next": ("computer-use", [
        start("computer-use", "claude-opus-5-5", None),
        ev("content", "First task done."),
        ev("cu_queue_next", {"prompt": "now check email", "remaining": 0}),
        ev("content", "Inbox has 3 messages."),
        ev("cu_stopped", {"reason": "user"}),
        ev("done", {"content": "Inbox has 3 messages."}),
        end(),
    ]),
    # Robotics ER: a mission with frames, reasoning, actions, nav, an error and a stop.
    "robotics_mission": ("robotics", [
        start("robotics", "gemini-robotics-er-1.5-preview", None),
        ev("er_session", {"session_id": "er-1"}),
        ev("er_mission_start", {"goal": "find the cup"}),
        ev("er_frame", {"image": "data:image/jpeg;base64,AAAA", "camera": "front"}),
        ev("er_reasoning", {"text": "The cup is on the left table."}),
        ev("er_action", {"tool": "move", "args": {"x": 1.5, "y": 0}, "status": "ok"}),
        ev("er_nav_status", {"goal_status": "ACTIVE"}),
        ev("er_queued", {"position": 1}),
        ev("er_prompt_injected", {"text": "faster"}),
        ev("er_status", {"state": "searching"}),
        ev("er_error", {"error": "bumper pressed"}),
        ev("er_stopped", {}),
        ev("content", "Mission ended."),
        ev("done", {"content": "Mission ended."}),
        end(),
    ]),
}


def main():
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    for name, (provider, frames) in FIXTURES.items():
        with open(os.path.join(out, f"{name}.sse"), "w", newline="\n") as f:
            f.write("".join(frames))
        with open(os.path.join(out, f"{name}.provider"), "w", newline="\n") as f:
            f.write(provider + "\n")
    print(f"wrote {len(FIXTURES)} fixtures to {out}")


if __name__ == "__main__":
    main()
