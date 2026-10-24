"""The phone bridge's Claude Code backend reads the CLI through the shared transport
(audit O10-telephony-twilio-06, O06-frontier-agent-13).

It used to flip the CLI's stdout to O_NONBLOCK and read it with select() +
readline(): a stream-json event bigger than one pipe write came back in two
halves, both failed json.loads and were dropped, so a long answer was spoken as
silence. Every turn after the first also ran `claude --continue`, which resumes
the most recent conversation in the shared working directory (possibly another
operator's). Hermetic: the CLI is a Python stand-in under tmp_path.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from types import SimpleNamespace

import pytest

import Orchestrator.routes.agent_routes as ar
import Orchestrator.state as state
from Orchestrator.phone.bridge import PhoneAIBridge

pytestmark = pytest.mark.timeout(60)

LONG_ANSWER = "The forecast for tomorrow is clear. " * 2500   # ~90 KB, several pipe writes

FAKE_CLAUDE = r'''
import json, os, sys, time
d = os.environ["FAKE_CLI_DIR"]
argv = sys.argv[1:]
with open(os.path.join(d, "argv.jsonl"), "a") as fh:
    fh.write(json.dumps(argv) + "\n")
answer = open(os.path.join(d, "answer.txt")).read()
sid = next(argv[i + 1] for i, a in enumerate(argv) if a in ("--session-id", "--resume"))
def emit_split(obj):
    line = json.dumps(obj) + "\n"
    half = len(line) // 2
    sys.stdout.write(line[:half]); sys.stdout.flush()
    time.sleep(0.3)
    sys.stdout.write(line[half:]); sys.stdout.flush()
print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
emit_split({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "get_current_time", "input": {}},
    {"type": "text", "text": answer}]}})
emit_split({"type": "result", "result": answer, "session_id": sid})
'''


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    d = tmp_path / "fakecli"
    d.mkdir()
    (d / "answer.txt").write_text(LONG_ANSWER)
    fake = d / "claude"
    fake.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
    fake.chmod(0o755)
    monkeypatch.setenv("FAKE_CLI_DIR", str(d))
    monkeypatch.setattr(ar, "_claude_path", lambda: str(fake))
    monkeypatch.setattr(state, "PERSISTED_AGENT_SESSIONS", {})
    monkeypatch.setattr(state, "save_operator_state", lambda: None)

    b = PhoneAIBridge.__new__(PhoneAIBridge)
    b.phone_session = SimpleNamespace(operator="Alice", session_id="phone-test",
                                      caller_id="+15550000001")
    b._claude_message_count = 0
    b._resume_from_disk = False
    b._claude_session_id = str(uuid.uuid4())
    b._claude_phone_context = "(phone context)"
    b._phone_disconnected = False
    b._sent_first_message_this_call = False
    b.fake_dir = d
    return b


def _argvs(d):
    return [json.loads(line) for line in (d / "argv.jsonl").read_text().splitlines()]


def test_a_long_answer_split_across_pipe_writes_is_spoken_whole(bridge):
    answer = asyncio.run(bridge._claude_respond("what's the weather"))
    assert answer == LONG_ANSWER.strip()


def test_every_phone_turn_resumes_this_conversation_by_id(bridge):
    conversation = bridge._claude_session_id
    asyncio.run(bridge._claude_respond("first"))
    asyncio.run(bridge._claude_respond("second"))
    first, second = _argvs(bridge.fake_dir)
    assert first[first.index("--session-id") + 1] == conversation
    assert "--continue" not in second
    assert second[second.index("--resume") + 1] == conversation
