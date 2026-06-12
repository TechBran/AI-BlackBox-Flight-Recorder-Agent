"""Portal updates-section embeddings card ↔ backend contract guard (Task 14).

The embeddings notification card in Portal/modules/updates-manager.js is
hand-bound to the backend contract (Orchestrator/routes/embeddings_routes.py):
it fetches GET /embeddings/status, POSTs /embeddings/migrate, reads the
watcher's health.successor_slug field, and deep-links [Manage] to the
onboarding wizard's embeddings step. There is no JS test infra, so — mirroring
test_onboarding_steps_parity.py — this is a deliberate source-text test: it
asserts the JS still references the real endpoints/fields, catching accidental
deletion or a typo'd route during future refactors.

NOTE: keep these literals greppable in updates-manager.js (no string
concatenation for the URLs) or update this test alongside.
"""
from pathlib import Path

import pytest

UPDATES_MANAGER_JS = (
    Path(__file__).resolve().parents[2] / "Portal" / "modules" / "updates-manager.js"
)

# Every binding the card depends on: endpoints, the wizard deep-link, and the
# health field that gates the [Update] button / carries the migrate target.
REQUIRED_LITERALS = [
    "/embeddings/status",
    "/embeddings/migrate",
    "/onboarding/?step=embeddings",
    "successor_slug",
]


@pytest.mark.parametrize("literal", REQUIRED_LITERALS)
def test_updates_manager_references_embeddings_contract(literal):
    src = UPDATES_MANAGER_JS.read_text(encoding="utf-8")
    assert literal in src, (
        f"Portal/modules/updates-manager.js no longer references {literal!r} — "
        "the embeddings card has drifted from the backend contract "
        "(Orchestrator/routes/embeddings_routes.py / the wizard deep-link)."
    )
