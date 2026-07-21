"""speech_to_text's provider enum must accept the custom-server `local` token
and the on-box `onbox` token (spec §5.3 STT routing fix). Reads the shipping
schema.json directly via registry.TOOLS_DIR so it tracks the real module.
"""

import json

from Orchestrator.toolvault import registry


def _provider_enum() -> list:
    schema_path = registry.TOOLS_DIR / "speech_to_text" / "schema.json"
    data = json.loads(schema_path.read_text())
    return data["parameters"]["properties"]["provider"]["enum"]


def test_provider_enum_includes_local_and_onbox():
    enum = _provider_enum()
    assert "local" in enum, f"custom-server 'local' STT token missing: {enum}"
    assert "onbox" in enum, f"on-box 'onbox' STT token missing: {enum}"


def test_provider_enum_keeps_existing_cloud_providers():
    # Regression floor: the fix is additive, not a rewrite.
    enum = _provider_enum()
    for p in ("openai", "google", "elevenlabs"):
        assert p in enum, f"existing provider {p!r} was dropped: {enum}"
