"""Hermetic tests for the ElevenLabs Voice Lab ToolVault executors (Task 23).

Each executor is loaded directly from its file (same mechanism the registry uses)
and exercised with a ToolContext. The executors call
``Orchestrator.elevenlabs.voices`` / ``.catalog`` directly (in-process), imported
INSIDE the function body, so we monkeypatch the functions on those real modules.

Critical coverage (both consent/confirm gates):
  * clone WITHOUT confirm_consent -> success False AND clone_instant NEVER called
  * clone WITH confirm_consent (+ existing files) -> calls clone_instant, returns id
  * design step 1 (no generated_voice_id) -> previews listed
  * design step 2 (generated_voice_id + name) -> design_save called
  * design generated_voice_id WITHOUT name -> blocked
  * delete WITHOUT confirm -> blocked, delete_voice NEVER called
  * delete WITH confirm -> calls delete_voice, in-use warning surfaced
  * list -> readable summary from catalog.get_voices
"""
import asyncio
import importlib.util
from pathlib import Path

import pytest

from Orchestrator.elevenlabs import catalog as cat
from Orchestrator.elevenlabs import voices as vox
from Orchestrator.toolvault.context import ToolContext, ToolResult

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "ToolVault" / "tools"


def _load_executor(tool_name: str):
    path = _TOOLS_DIR / tool_name / "executor.py"
    spec = importlib.util.spec_from_file_location(f"{tool_name}_executor", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


clone_exec = _load_executor("elevenlabs_clone_voice")
design_exec = _load_executor("elevenlabs_design_voice")
list_exec = _load_executor("elevenlabs_list_voices")
delete_exec = _load_executor("elevenlabs_delete_voice")


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ctx():
    return ToolContext(operator="system")


@pytest.fixture
def audio_file(tmp_path):
    """A real file on disk so the executor's exists() check passes."""
    p = tmp_path / "sample.mp3"
    p.write_bytes(b"ID3fakeaudio")
    return str(p)


# =============================================================================
# elevenlabs_clone_voice — the consent gate
# =============================================================================

def test_clone_without_confirm_consent_is_blocked_and_never_calls_provider(monkeypatch, ctx, audio_file):
    """No confirm_consent -> ToolResult success False AND clone_instant NOT called."""
    monkeypatch.setattr(
        vox, "clone_instant",
        lambda *a, **k: pytest.fail("clone_instant called without consent"),
    )
    result = _run(clone_exec.execute(
        {"name": "Test", "audio_paths": [audio_file], "confirm_consent": False}, ctx
    ))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "confirm" in result.result.lower()


def test_clone_with_confirm_consent_calls_clone_instant(monkeypatch, ctx, audio_file):
    seen = {}

    def fake_clone(name, file_paths, *, description=None, **kwargs):
        seen.update(name=name, file_paths=file_paths, description=description)
        return {"voice_id": "cloned-abc", "requires_verification": False}

    monkeypatch.setattr(vox, "clone_instant", fake_clone)

    result = _run(clone_exec.execute(
        {"name": "My Narrator", "audio_paths": [audio_file],
         "confirm_consent": True, "description": "warm"},
        ctx,
    ))
    assert result.success is True
    assert "elevenlabs:cloned-abc" in result.result
    assert result.data["voice_id"] == "cloned-abc"
    assert seen["name"] == "My Narrator"
    assert seen["file_paths"] == [audio_file]
    assert seen["description"] == "warm"


def test_clone_missing_audio_file_blocked(monkeypatch, ctx):
    """A nonexistent path -> failure BEFORE any provider call, even with consent."""
    monkeypatch.setattr(
        vox, "clone_instant",
        lambda *a, **k: pytest.fail("clone_instant called with a missing file"),
    )
    result = _run(clone_exec.execute(
        {"name": "X", "audio_paths": ["/nope/missing.mp3"], "confirm_consent": True}, ctx
    ))
    assert result.success is False
    assert "not found" in result.result.lower()


def test_clone_runtime_error_returns_failure(monkeypatch, ctx, audio_file):
    def boom(*a, **k):
        raise RuntimeError("ElevenLabs quota exceeded - add credits or upgrade plan")

    monkeypatch.setattr(vox, "clone_instant", boom)
    result = _run(clone_exec.execute(
        {"name": "X", "audio_paths": [audio_file], "confirm_consent": True}, ctx
    ))
    assert result.success is False
    assert "quota exceeded" in result.result


# =============================================================================
# elevenlabs_design_voice — two-step
# =============================================================================

def test_design_step1_previews(monkeypatch, ctx):
    """No generated_voice_id -> design_previews; readable list of candidates."""
    def fake_previews(voice_description, *, text=None, **kwargs):
        return {
            "text": "sample",
            "previews": [
                {"generated_voice_id": "gen-1", "audio_url": "/ui/uploads/a.mp3",
                 "duration_secs": 3.2, "language": "en"},
                {"generated_voice_id": "gen-2", "audio_url": "/ui/uploads/b.mp3",
                 "duration_secs": 2.8, "language": "en"},
                {"generated_voice_id": "gen-3", "audio_url": "/ui/uploads/c.mp3",
                 "duration_secs": 3.0, "language": "en"},
            ],
        }

    monkeypatch.setattr(vox, "design_previews", fake_previews)

    result = _run(design_exec.execute({"voice_description": "a gravelly wizard"}, ctx))
    assert result.success is True
    assert "3 preview" in result.result
    assert "gen-1" in result.result and "gen-2" in result.result and "gen-3" in result.result
    assert len(result.data["previews"]) == 3


def test_design_step2_saves(monkeypatch, ctx):
    """generated_voice_id + name -> design_save."""
    seen = {}

    def fake_save(generated_voice_id, name, description):
        seen.update(generated_voice_id=generated_voice_id, name=name, description=description)
        return {"voice_id": "saved-xyz"}

    monkeypatch.setattr(vox, "design_save", fake_save)

    result = _run(design_exec.execute(
        {"voice_description": "a gravelly wizard", "generated_voice_id": "gen-1", "name": "Gandalf"},
        ctx,
    ))
    assert result.success is True
    assert "elevenlabs:saved-xyz" in result.result
    assert result.data["voice_id"] == "saved-xyz"
    assert seen == {"generated_voice_id": "gen-1", "name": "Gandalf",
                    "description": "a gravelly wizard"}


def test_design_generated_id_without_name_blocked(monkeypatch, ctx):
    monkeypatch.setattr(
        vox, "design_save",
        lambda *a, **k: pytest.fail("design_save called without a name"),
    )
    result = _run(design_exec.execute(
        {"voice_description": "x", "generated_voice_id": "gen-1"}, ctx
    ))
    assert result.success is False
    assert "name" in result.result.lower()


# =============================================================================
# elevenlabs_list_voices
# =============================================================================

def test_list_voices_summary(monkeypatch, ctx):
    monkeypatch.setattr(cat, "get_voices", lambda *a, **k: {
        "my_voices": [{"id": "elevenlabs:mine-1", "name": "Mine"}],
        "premade": [{"id": "elevenlabs:pm-1", "name": "Rachel"},
                    {"id": "elevenlabs:pm-2", "name": "Adam"}],
    })
    result = _run(list_exec.execute({}, ctx))
    assert result.success is True
    assert "My Voices" in result.result
    assert "Mine (elevenlabs:mine-1)" in result.result
    assert "Premade: 2 available" in result.result
    assert len(result.data["premade"]) == 2


def test_list_voices_no_key(monkeypatch, ctx):
    monkeypatch.setattr(cat, "get_voices", lambda *a, **k: None)
    result = _run(list_exec.execute({}, ctx))
    assert result.success is True
    assert "no api key" in result.result.lower()
    assert result.data == {"my_voices": [], "premade": []}


# =============================================================================
# elevenlabs_delete_voice — confirm gate
# =============================================================================

def test_delete_without_confirm_blocked_and_never_calls_provider(monkeypatch, ctx):
    monkeypatch.setattr(
        vox, "delete_voice",
        lambda *a, **k: pytest.fail("delete_voice called without confirm"),
    )
    result = _run(delete_exec.execute({"voice_id": "elevenlabs:abc", "confirm": False}, ctx))
    assert result.success is False
    assert "confirm=true" in result.result


def test_delete_with_confirm_calls_provider_and_warns_in_use(monkeypatch, ctx):
    calls = {}
    monkeypatch.setattr(vox, "voice_in_use", lambda vid: ["Brandon"])

    def fake_delete(vid):
        calls["deleted"] = vid
        return {"ok": True}

    monkeypatch.setattr(vox, "delete_voice", fake_delete)

    result = _run(delete_exec.execute({"voice_id": "elevenlabs:abc123", "confirm": True}, ctx))
    assert result.success is True
    assert "Deleted elevenlabs:abc123" in result.result
    assert "WARNING" in result.result and "Brandon" in result.result
    assert calls["deleted"] == "elevenlabs:abc123"
    assert result.data == {"ok": True, "in_use": ["Brandon"]}


def test_delete_with_confirm_no_warning_when_unused(monkeypatch, ctx):
    monkeypatch.setattr(vox, "voice_in_use", lambda vid: [])
    monkeypatch.setattr(vox, "delete_voice", lambda vid: {"ok": True})
    result = _run(delete_exec.execute({"voice_id": "abc123", "confirm": True}, ctx))
    assert result.success is True
    assert "WARNING" not in result.result
