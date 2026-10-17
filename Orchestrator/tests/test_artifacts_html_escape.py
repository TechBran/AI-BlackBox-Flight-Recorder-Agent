"""Hostile-input tests for the HTML artifacts.py builds (audit O05-ledger-persistence-13).

parse_and_process_artifacts_with_meta() turns a model-emitted
[ARTIFACT:<filename>:<type>]...[/ARTIFACT] tag into download-button HTML that
the Portal renders and the conversation history keeps. The filename, the type
and the generator's error text are all model/attacker-controlled (a
prompt-injected web page or email can make the model emit the tag), so every
one of them must be HTML-escaped where it is interpolated. The filename is
also reduced to a safe basename first (audit rank 16, utils.paths.safe_filename),
so the download name is the REDUCED name, escaped.
"""

import re
from html.parser import HTMLParser

import pytest

import Orchestrator.artifacts as artifacts_mod
from Orchestrator.artifacts import parse_and_process_artifacts_with_meta
from Orchestrator.utils.paths import safe_filename


@pytest.fixture(autouse=True)
def _isolate_artifacts_dir(monkeypatch, tmp_path):
    """Write artifacts to a tmp dir, never the real Portal/artifacts/."""
    monkeypatch.setattr(artifacts_mod, "ARTIFACTS_DIR", tmp_path)


class _Collector(HTMLParser):
    """Collects every start tag and its attributes, plus the visible text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def _parse(fragment):
    collector = _Collector()
    collector.feed(fragment)
    collector.close()
    return collector


def _assert_no_script_vectors(fragment):
    parsed = _parse(fragment)
    for tag, attrs in parsed.tags:
        assert tag in {"div", "a"}, f"unexpected <{tag}> in {fragment!r}"
        for name in attrs:
            assert not name.startswith("on"), f"event handler {name} in {fragment!r}"
    return parsed


HOSTILE_NAME = 'x" onmouseover="alert(1)<img src=y onerror=alert(2)>.txt'


def test_hostile_filename_is_escaped_in_attribute_and_text():
    reply = f"[ARTIFACT:{HOSTILE_NAME}:text]hello[/ARTIFACT]"
    modified, artifacts = parse_and_process_artifacts_with_meta(reply, "tester")

    parsed = _assert_no_script_vectors(modified)
    anchors = [attrs for tag, attrs in parsed.tags if tag == "a"]
    assert len(anchors) == 1
    # The browser sees the reduced safe basename as the download name...
    reduced = safe_filename(HOSTILE_NAME, fallback="artifact.txt")
    assert anchors[0]["download"] == reduced
    assert anchors[0]["href"] == artifacts[0]["url"]
    # ...and as text, not markup.
    assert reduced in "".join(parsed.text)
    assert "<img" not in modified


def test_hostile_unsupported_type_is_escaped():
    reply = "[ARTIFACT:a.txt:<img src=x onerror=alert(1)>]x[/ARTIFACT]"
    modified, artifacts = parse_and_process_artifacts_with_meta(reply, "tester")

    assert artifacts == []
    parsed = _assert_no_script_vectors(modified)
    assert "<img" not in modified
    assert "Unsupported artifact type: <img src=x onerror=alert(1)>" in "".join(parsed.text)


def test_hostile_error_text_and_filename_are_escaped(monkeypatch):
    def _boom(content, output_path):
        raise RuntimeError('<script>alert("err")</script>')

    monkeypatch.setattr(artifacts_mod, "generate_pdf", _boom)
    name = "<b>r</b>.pdf"
    reply = f"[ARTIFACT:{name}:pdf]body[/ARTIFACT]"
    modified, artifacts = parse_and_process_artifacts_with_meta(reply, "tester")

    assert artifacts == []
    parsed = _assert_no_script_vectors(modified)
    assert "<script" not in modified and "<b>" not in modified
    text = "".join(parsed.text)
    assert f"Failed to generate {safe_filename(name, fallback='artifact.txt')}" in text
    assert '<script>alert("err")</script>' in text


def test_ordinary_filename_html_is_unchanged():
    """Escaping is the identity for ordinary names: the Portal's HTML contract holds."""
    modified, artifacts = parse_and_process_artifacts_with_meta(
        "[ARTIFACT:notes.txt:text]Hello world content[/ARTIFACT]", "tester")
    normalized = re.sub(r"tester_[0-9a-f]{8}_", "tester_XXXXXXXX_", modified)
    assert normalized == (
        '<div class="artifact-download"><a href="/artifacts/tester_XXXXXXXX_notes.txt" '
        'download="notes.txt" class="btn artifact-btn">📥 Download notes.txt (0.0 KB)</a></div>'
    )
    assert artifacts[0]["url"].startswith("/artifacts/tester_")
