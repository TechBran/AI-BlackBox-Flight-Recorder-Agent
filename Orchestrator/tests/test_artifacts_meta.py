"""Tests for the structured artifacts[] surface (Phase 6a).

parse_and_process_artifacts_with_meta(ui_reply, operator) returns
(modified_string, [{filename, type, url, size_kb}]). The modified_string is
byte-identical to the legacy parse_and_process_artifacts output (Portal depends
on it); the list lets native clients (Android Phase 6b) render download chips.
"""

import re

from Orchestrator.artifacts import (
    parse_and_process_artifacts,
    parse_and_process_artifacts_with_meta,
)
import pytest
import Orchestrator.artifacts as _artifacts_mod


@pytest.fixture(autouse=True)
def _isolate_artifacts_dir(monkeypatch, tmp_path):
    """Redirect artifact file writes to a tmp dir so tests never pollute the
    non-gitignored Portal/artifacts/ (stray tester_* files could be swept into a commit)."""
    monkeypatch.setattr(_artifacts_mod, "ARTIFACTS_DIR", tmp_path)


def _href_urls(modified: str):
    """Pull every /artifacts/<id> href out of the modified HTML string."""
    return re.findall(r'href="(/artifacts/[^"]+)"', modified)


def test_single_text_artifact_meta():
    reply = (
        "Here is your file:\n"
        "[ARTIFACT:notes.txt:text]Hello world content[/ARTIFACT]\n"
        "Done."
    )
    modified, artifacts = parse_and_process_artifacts_with_meta(reply, "tester")

    # Modified string carries a download link, no raw [ARTIFACT] tag remains.
    assert '<a href="/artifacts/' in modified
    assert "[ARTIFACT:" not in modified

    # Exactly one structured entry with the required keys.
    assert len(artifacts) == 1
    art = artifacts[0]
    assert art["filename"] == "notes.txt"
    assert art["type"] == "text"
    assert art["url"].startswith("/artifacts/")
    assert isinstance(art["size_kb"], float)

    # The url in the list matches the href in the modified string.
    assert art["url"] in _href_urls(modified)


def test_no_artifacts_returns_unchanged_and_empty_list():
    reply = "Just a normal reply with no artifact blocks at all."
    modified, artifacts = parse_and_process_artifacts_with_meta(reply, "tester")
    assert modified == reply
    assert artifacts == []


def test_two_artifacts_both_listed_and_linked():
    reply = (
        "First:\n"
        "[ARTIFACT:report.txt:text]alpha beta gamma[/ARTIFACT]\n"
        "Second:\n"
        "[ARTIFACT:data.csv:csv]a,b,c\n1,2,3[/ARTIFACT]\n"
    )
    modified, artifacts = parse_and_process_artifacts_with_meta(reply, "tester")

    assert len(artifacts) == 2
    filenames = {a["filename"] for a in artifacts}
    assert filenames == {"report.txt", "data.csv"}
    types = {a["type"] for a in artifacts}
    assert types == {"text", "csv"}

    # Both urls appear as hrefs in the modified string.
    hrefs = _href_urls(modified)
    for a in artifacts:
        assert a["url"] in hrefs

    # Both backing files were actually created on disk.
    from Orchestrator.artifacts import ARTIFACTS_DIR
    for a in artifacts:
        artifact_id = a["url"].split("/artifacts/", 1)[1]
        assert (ARTIFACTS_DIR / artifact_id).exists()


def test_image_placeholder_not_in_list_and_no_link():
    reply = "[ARTIFACT:pic.png:image_placeholder]ignored[/ARTIFACT]"
    modified, artifacts = parse_and_process_artifacts_with_meta(reply, "tester")
    # image_placeholder produces no file, no download link, no list entry.
    assert artifacts == []
    assert "artifact-download" not in modified
    assert "<a href=" not in modified


def test_backcompat_wrapper_matches_with_meta_string():
    reply = (
        "Wrapper parity check:\n"
        "[ARTIFACT:wrap.txt:text]same content both ways[/ARTIFACT]\n"
    )
    # Two independent runs differ only by the random uuid in the artifact id;
    # normalize the uuid hex segment so we compare the structural HTML output.
    wrapper_only = parse_and_process_artifacts(reply, "tester")
    modified, _artifacts = parse_and_process_artifacts_with_meta(reply, "tester")

    uuid_rx = re.compile(r"(tester)_[0-9a-f]{8}_")
    norm_wrapper = uuid_rx.sub(r"\1_XXXXXXXX_", wrapper_only)
    norm_meta = uuid_rx.sub(r"\1_XXXXXXXX_", modified)
    assert norm_wrapper == norm_meta


def test_chat_save_response_carries_artifacts():
    """Hermetic check of the exact dict-construction logic chat_save runs:
    when the reply had an artifact, the response includes a structured
    'artifacts' key (and modified_response with the download link)."""
    assistant_response = "[ARTIFACT:save.txt:text]content via save path[/ARTIFACT]"

    has_artifacts = False
    artifacts_meta = []
    if "[ARTIFACT:" in assistant_response:
        assistant_response, artifacts_meta = parse_and_process_artifacts_with_meta(
            assistant_response, "tester"
        )
        has_artifacts = True

    media_tasks = []
    response = {
        "success": True,
        "operator": "tester",
        "media_tasks": media_tasks,
        "modified_response": assistant_response if (media_tasks or has_artifacts) else None,
        "artifacts": artifacts_meta,
    }

    assert response["artifacts"], "artifacts[] should be populated for an artifact reply"
    assert response["artifacts"][0]["filename"] == "save.txt"
    assert response["artifacts"][0]["url"] in _href_urls(response["modified_response"])


def test_chat_save_response_empty_artifacts_when_none():
    """No artifact in the reply -> response carries artifacts == []."""
    assistant_response = "plain reply, nothing to download"

    has_artifacts = False
    artifacts_meta = []
    if "[ARTIFACT:" in assistant_response:
        assistant_response, artifacts_meta = parse_and_process_artifacts_with_meta(
            assistant_response, "tester"
        )
        has_artifacts = True

    response = {"artifacts": artifacts_meta}
    assert response["artifacts"] == []
