"""The transcription step must offer the on-box STT option (M8): an 'onbox'
PROVIDERS entry so a user can pin STT_PROVIDER=onbox. Source-text test — the
wizard has no JS test infra (mirrors test_onboarding_steps_parity.py)."""
import re
from pathlib import Path

TRANSCRIPTION_JS = (
    Path(__file__).resolve().parents[2]
    / "Portal" / "onboarding" / "steps" / "transcription.js"
)


def test_transcription_offers_onbox_provider():
    src = TRANSCRIPTION_JS.read_text(encoding="utf-8")
    m = re.search(r"const PROVIDERS\s*=\s*\[(.*?)\];", src, re.DOTALL)
    assert m, "could not find `const PROVIDERS = [...]` in transcription.js"
    ids = re.findall(r'id:\s*"([a-z0-9_]+)"', m.group(1))
    assert "onbox" in ids, (
        "transcription.js PROVIDERS must include an 'onbox' (on-box local STT) "
        f"option; found {ids}"
    )
    # The distinct on-box token must not be conflated with the custom-server
    # 'local' token (spec §5.3 — they route to different backends).
    assert "local" in ids and "onbox" in ids
