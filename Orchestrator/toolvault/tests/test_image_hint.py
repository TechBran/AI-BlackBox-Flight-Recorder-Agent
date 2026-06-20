from Orchestrator.toolvault import availability as av
from Orchestrator.toolvault import injector


def test_no_image_tool_no_hint():
    assert av.default_provider_hint(["roll_dice", "web_fetch"], "image") == ""


def test_prefers_default_when_injected(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {"IMAGE_DEFAULT": "gemini"})
    hint = av.default_provider_hint(["gemini_image", "openai_image"], "image")
    assert "gemini_image" in hint
    assert "prefer" in hint.lower()


def test_compare_when_multiple(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {"IMAGE_DEFAULT": "gemini"})
    hint = av.default_provider_hint(["gemini_image", "openai_image"], "image")
    assert "compare" in hint.lower()


def test_default_unset_is_graceful(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {})
    hint = av.default_provider_hint(["gemini_image"], "image")
    assert hint  # non-empty, no crash
    assert "prefer" not in hint.lower()  # no default -> no "prefer X"


def test_build_tool_instructions_appends_image_hint(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {"IMAGE_DEFAULT": "gemini"})
    out = injector.build_tool_instructions(["gemini_image"])
    assert "IMAGE GENERATION GUIDANCE" in out
    assert "gemini_image" in out


def test_build_tool_instructions_no_image_hint_without_image_tool():
    out = injector.build_tool_instructions(["roll_dice"])
    assert "IMAGE GENERATION GUIDANCE" not in out
