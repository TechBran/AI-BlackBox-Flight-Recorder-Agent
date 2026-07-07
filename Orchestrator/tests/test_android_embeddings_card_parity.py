"""Android UpdatesScreen embeddings card ↔ backend contract guard (Task 15).

The Android updates screen mirrors the Portal's embeddings notification card
(Portal/modules/updates-manager.js): it fetches GET /embeddings/status, POSTs
/embeddings/migrate from the [Update] affordance, maps the watcher's
``successor_slug`` / ``cancel_requested`` JSON fields via @SerialName, and
deep-links [Manage] to the onboarding wizard's embeddings step. There is no
Android unit-test infra for this screen, so — mirroring
test_portal_embeddings_card_parity.py — this is a deliberate source-text
test: it asserts the Kotlin sources still reference the real endpoints/fields,
catching accidental deletion or a typo'd route during future refactors.

NOTE: keep these literals greppable in the Kotlin files (no string
concatenation for the URLs/field names) or update this test alongside.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The Android app base directory contains spaces and parentheses — build the
# path from parts, never from a shell-style string.
ANDROID_SRC = (
    REPO_ROOT
    / "AI_BlackBox_Portal_Android_MVP (2)"
    / "AI_BlackBox_Portal_Android_MVP"
    / "AI_BlackBox_Portal"
    / "app" / "src" / "main" / "java" / "com" / "aiblackbox" / "portal"
)

KOTLIN_FILES = [
    ANDROID_SRC / "ui" / "updates" / "UpdatesScreen.kt",
    ANDROID_SRC / "ui" / "updates" / "UpdatesViewModel.kt",
    ANDROID_SRC / "data" / "repository" / "UpdateRepository.kt",
    ANDROID_SRC / "data" / "model" / "EmbeddingsStatus.kt",
]

# Every binding the card depends on: endpoints, the wizard deep-link, and the
# snake_case JSON fields the DTO must keep mapping (@SerialName).
REQUIRED_LITERALS = [
    "/embeddings/status",
    "/embeddings/migrate",
    "/onboarding/?step=embeddings",
    '"successor_slug"',
    '"cancel_requested"',
]

# The read-only reranker status line replaced the deleted selector card: the
# status endpoint, the composable that renders it, and the "Reranking:" copy
# must stay greppable in the Kotlin sources.
RERANK_STATUS_LINE_LITERALS = [
    "/rerank/status",       # the read-only status endpoint (UpdateRepository)
    "RerankStatusLine",     # the composable that renders the line
    "Reranking:",           # the status-line copy
]

# The reranker SELECTOR write path moved to the onboarding wizard — none of
# these selector symbols may survive in the updates-screen sources.
REMOVED_RERANK_SELECTOR_SYMBOLS = [
    "selectRerank",         # the selector write call (ViewModel/Repository)
    "/rerank/select",       # the selector write endpoint
    "RerankCard",           # the deleted selector composable
    "RerankOptionRow",      # the deleted per-model selector row
]

# Copy the spec requires VERBATIM in both the Portal card and the Android
# card: the superseded-state explanation, minus the interpolated successor
# label ("...transfer embeddings to <successor label> in the background...").
SHARED_SUPERSEDED_TAIL = (
    "in the background. Search keeps working the whole time; the switch "
    "happens automatically when it finishes and survives restarts."
)


def _kotlin_blob() -> str:
    missing = [str(p) for p in KOTLIN_FILES if not p.is_file()]
    assert not missing, f"expected Kotlin sources missing: {missing}"
    return "\n".join(p.read_text(encoding="utf-8") for p in KOTLIN_FILES)


@pytest.mark.parametrize("literal", REQUIRED_LITERALS)
def test_android_card_references_embeddings_contract(literal):
    blob = _kotlin_blob()
    assert literal in blob, (
        f"Android updates screen no longer references {literal!r} — the "
        "embeddings card has drifted from the backend contract "
        "(Orchestrator/routes/embeddings_routes.py / the wizard deep-link). "
        f"Checked: {[p.name for p in KOTLIN_FILES]}"
    )


@pytest.mark.parametrize("literal", RERANK_STATUS_LINE_LITERALS)
def test_android_renders_readonly_rerank_status_line(literal):
    """The updates screen keeps a read-only reranker status line (fed by
    GET /rerank/status) after the selector moved to the wizard."""
    blob = _kotlin_blob()
    assert literal in blob, (
        f"Android updates screen no longer references {literal!r} — the "
        "read-only reranker status line (RerankStatusLine, fed by "
        "GET /rerank/status) has drifted or was removed. "
        f"Checked: {[p.name for p in KOTLIN_FILES]}"
    )


@pytest.mark.parametrize("symbol", REMOVED_RERANK_SELECTOR_SYMBOLS)
def test_android_reranker_selector_is_gone(symbol):
    """The tier/key-gated reranker SELECTOR was removed from the updates screen
    (selection lives in the onboarding wizard); only the status line remains."""
    blob = _kotlin_blob()
    assert symbol not in blob, (
        f"Android updates screen still references {symbol!r} — the reranker "
        "SELECTOR was removed (selection lives in the onboarding wizard); keep "
        "only the read-only status line. "
        f"Checked: {[p.name for p in KOTLIN_FILES]}"
    )


def test_superseded_copy_verbatim_with_portal():
    """The superseded card copy must stay word-for-word identical between the
    Portal card and the Android card (both interpolate the successor label,
    so the assertion targets the shared tail after it)."""
    portal_src = (
        REPO_ROOT / "Portal" / "modules" / "updates-manager.js"
    ).read_text(encoding="utf-8")
    kotlin_src = (
        ANDROID_SRC / "ui" / "updates" / "UpdatesScreen.kt"
    ).read_text(encoding="utf-8")
    # Kotlin wraps the sentence across concatenated string literals; collapse
    # the `" + "` seams (quote, +, quote — any whitespace) before comparing.
    kotlin_collapsed = re.sub(r'"\s*\+\s*"', "", kotlin_src)
    for src, name in ((portal_src, "Portal"), (kotlin_collapsed, "Android")):
        assert SHARED_SUPERSEDED_TAIL in src, (
            f"{name} superseded-card copy drifted from the agreed verbatim text: "
            f"{SHARED_SUPERSEDED_TAIL!r}"
        )


def test_progress_line_copy_matches_portal():
    """Both cards render migration progress as 'Re-embedding N/M…' with a
    ' (cancelling…)' suffix when cancel was requested."""
    portal_src = (
        REPO_ROOT / "Portal" / "modules" / "updates-manager.js"
    ).read_text(encoding="utf-8")
    blob = _kotlin_blob()
    for needle in ("Re-embedding ", "(cancelling…)"):
        assert needle in portal_src, f"Portal lost progress copy {needle!r}"
        assert needle in blob, f"Android lost progress copy {needle!r}"


def test_android_embeddings_job_has_progress_bar():
    """The running embeddings job renders a visual progress bar (not just the
    'Re-embedding N/M…' text) beside the progress line."""
    kotlin_src = (
        ANDROID_SRC / "ui" / "updates" / "UpdatesScreen.kt"
    ).read_text(encoding="utf-8")
    assert "LinearProgressIndicator" in kotlin_src, (
        "UpdatesScreen.kt no longer renders a LinearProgressIndicator for the "
        "embeddings migration job — the visual progress bar was removed."
    )
