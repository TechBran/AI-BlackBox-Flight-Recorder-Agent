"""Source-text guard for the M10.0 Voyage + Cohere cards in the API-Keys step.

There is no JS test infra in Portal (see test_onboarding_steps_parity.py), so —
matching the house pattern — we parse Portal/onboarding/steps/api_keys.js and
assert the two reranker providers render the identical paste/reveal/Validate
card the other providers use, with honest "optional reranker upgrade" copy, and
that the card's Validate posts to /onboarding/validate.
"""
import re
from pathlib import Path

API_KEYS_JS = (
    Path(__file__).resolve().parents[2]
    / "Portal" / "onboarding" / "steps" / "api_keys.js"
)


def _src() -> str:
    return API_KEYS_JS.read_text(encoding="utf-8")


def _providers_block() -> str:
    src = _src()
    m = re.search(r"const PROVIDERS\s*=\s*\[(.*?)\];", src, re.DOTALL)
    assert m, "could not find `const PROVIDERS = [...]` in api_keys.js"
    return m.group(1)


def test_voyage_provider_entry_present():
    block = _providers_block()
    assert '"voyage"' in block or "id: \"voyage\"" in block
    assert "VOYAGE_API_KEY" in block


def test_cohere_provider_entry_present():
    block = _providers_block()
    assert '"cohere"' in block or "id: \"cohere\"" in block
    assert "COHERE_API_KEY" in block


def test_reranker_cards_have_honest_optional_copy():
    """Both frame themselves as optional reranker upgrades — memory works
    without them (Brandon's honesty directive)."""
    block = _providers_block().lower()
    # A description field exists and frames the upgrade honestly.
    assert "description" in block
    assert "rerank" in block
    assert "without" in block  # "...work without it/them"


def test_reranker_description_is_rendered_in_the_card():
    """A description field is useless if the card never renders it — assert the
    provider-card renderer surfaces p.description."""
    src = _src()
    assert "p.description" in src


def test_card_validate_posts_to_onboarding_validate():
    """The shared card (used by every provider incl. voyage/cohere) validates
    against the standard endpoint."""
    assert '"/onboarding/validate"' in _src()
