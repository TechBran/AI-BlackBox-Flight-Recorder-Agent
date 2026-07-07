"""Portal updates-section embeddings card ↔ backend contract guard (Task 14).

The embeddings notification card in Portal/modules/updates-manager.js is
hand-bound to the backend contract (Orchestrator/routes/embeddings_routes.py):
it fetches GET /embeddings/status, POSTs /embeddings/migrate, reads the
watcher's health.successor_slug field, and deep-links [Manage] to the
onboarding wizard's embeddings step. It also fetches GET /rerank/status and
renders a read-only "Reranking: ON/OFF" line — reranker SELECTION lives in the
onboarding wizard now, not in the updates panel. There is no JS test infra, so
— mirroring test_onboarding_steps_parity.py — this is a deliberate source-text
test: it asserts the JS still references the real endpoints/fields (catching
accidental deletion or a typo'd route), AND that the removed selector surface
stays gone.

NOTE: keep these literals greppable in updates-manager.js (no string
concatenation for the URLs) or update this test alongside.
"""
from pathlib import Path

import pytest

UPDATES_MANAGER_JS = (
    Path(__file__).resolve().parents[2] / "Portal" / "modules" / "updates-manager.js"
)

# Every binding the card depends on: endpoints, the wizard deep-link, the
# health field that gates the [Update] button / carries the migrate target, and
# the read-only reranker status line (GET /rerank/status → "Reranking:"). The
# compute/placement card and reranker selector were removed (they moved to the
# onboarding wizard), so their endpoints/fields are NO LONGER required here.
REQUIRED_LITERALS = [
    "/embeddings/status",
    "/embeddings/migrate",
    "/onboarding/?step=embeddings",
    "successor_slug",
    "Reranking:",
    "/rerank/status",
]

# Tokens whose reappearance would mean a removed surface crept back. UNIQUE
# tokens only — never assert on the bare word "hardware" (it survives in
# prose/comments, so it would false-fail).
REMOVED_TOKENS = [
    "_computeCardHtml",        # the compute/placement card renderer
    "embeddings-rerank-btn",   # the reranker selector's "Use this reranker" button
    "/embeddings/placement",   # the placement toggle write endpoint
    "/rerank/select",          # the reranker selector's write endpoint
]


@pytest.mark.parametrize("literal", REQUIRED_LITERALS)
def test_updates_manager_references_embeddings_contract(literal):
    src = UPDATES_MANAGER_JS.read_text(encoding="utf-8")
    assert literal in src, (
        f"Portal/modules/updates-manager.js no longer references {literal!r} — "
        "the embeddings card has drifted from the backend contract "
        "(Orchestrator/routes/embeddings_routes.py / the wizard deep-link)."
    )


@pytest.mark.parametrize("token", REMOVED_TOKENS)
def test_updates_manager_removed_surfaces_stay_gone(token):
    src = UPDATES_MANAGER_JS.read_text(encoding="utf-8")
    assert token not in src, (
        f"Portal/modules/updates-manager.js still contains {token!r} — the "
        "reranker selector was moved to the onboarding wizard and must not "
        "reappear in the updates panel (status/action/progress only)."
    )
