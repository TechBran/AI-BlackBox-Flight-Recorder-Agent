"""A PDF artifact must never fail on the model's inline markup.

MS02 production logs, 2026-10-17..23: generate_pdf turned markdown emphasis
into reportlab <b>/<i> tags with independent regexes, so overlapping emphasis
in the model's text ("**bold *italic** tail*") or an identifier with
underscores inside bold ("**config_value** and other_value") produced
interleaved tags such as '<b><i></b>', reportlab raised "Parse error: saw </b>
instead of expected </i>", and the WHOLE artifact failed: the user got an
error box instead of a document.

Now a paragraph whose markup does not parse is rendered as plain text, a
document that still fails to build is rebuilt from plain paragraphs, and
underscores inside a word are not emphasis.

Hermetic: artifacts are written under tmp_path.
"""
import pytest

import Orchestrator.artifacts as artifacts_mod
from Orchestrator.artifacts import generate_pdf, parse_and_process_artifacts_with_meta

HOSTILE = (
    "**bold *italic** tail*",
    "<b><i></b>",
    "__init__ and **config_value** and other_value",
    "Use `my_func_name` with *care_",
    "A & B < C > D",
)


@pytest.fixture(autouse=True)
def _isolate_artifacts_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(artifacts_mod, "ARTIFACTS_DIR", tmp_path)


@pytest.fixture
def built(monkeypatch):
    """Record the flowables of every build (and still build the real PDF)."""
    from reportlab.platypus import SimpleDocTemplate

    stories = []
    real_build = SimpleDocTemplate.build

    def recording_build(self, flowables, *a, **kw):
        stories.append(list(flowables))
        return real_build(self, flowables, *a, **kw)

    monkeypatch.setattr(SimpleDocTemplate, "build", recording_build)
    return stories


def _texts(story):
    return [f.getPlainText() for f in story if hasattr(f, "getPlainText")]


@pytest.mark.parametrize("fragment", HOSTILE)
def test_hostile_inline_markup_still_makes_a_pdf(tmp_path, built, fragment):
    out = tmp_path / "doc.pdf"
    content = f"# Report\n\n{fragment}\n- {fragment}\n1. {fragment}\n## {fragment}"
    generate_pdf(content, out)
    assert out.read_bytes().startswith(b"%PDF")
    # No text is lost: every line's words survive (with or without formatting).
    words = [w for w in fragment.replace("*", " ").replace("`", " ").split() if w.isalpha()]
    joined = " ".join(_texts(built[-1]))
    for word in words:
        assert word in joined, (word, joined)


def test_overlapping_emphasis_falls_back_to_plain_text(tmp_path, built):
    generate_pdf("**bold *italic** tail*", tmp_path / "doc.pdf")
    [para] = [f for f in built[-1] if hasattr(f, "getPlainText")]
    assert para.getPlainText() == "**bold *italic** tail*"


def test_snake_case_inside_bold_keeps_the_bold(tmp_path, built):
    generate_pdf("**config_value** and other_value", tmp_path / "doc.pdf")
    [para] = [f for f in built[-1] if hasattr(f, "getPlainText")]
    assert para.getPlainText() == "config_value and other_value"
    assert para.text == "<b>config_value</b> and other_value"


def test_well_formed_markdown_is_unchanged(tmp_path, built):
    generate_pdf("Some **bold**, *italic* and `code`.", tmp_path / "doc.pdf")
    [para] = [f for f in built[-1] if hasattr(f, "getPlainText")]
    assert para.getPlainText() == "Some bold, italic and code."
    assert para.text == 'Some <b>bold</b>, <i>italic</i> and <font face="Courier">code</font>.'


def test_a_failed_build_is_rebuilt_from_plain_paragraphs(tmp_path, monkeypatch):
    from reportlab.platypus import SimpleDocTemplate

    real_build = SimpleDocTemplate.build
    calls = []

    def flaky_build(self, flowables, *a, **kw):
        calls.append(len(flowables))
        if len(calls) == 1:
            raise ValueError("layout failed")
        return real_build(self, flowables, *a, **kw)

    monkeypatch.setattr(SimpleDocTemplate, "build", flaky_build)
    out = tmp_path / "doc.pdf"
    generate_pdf("# Title\nbody text", out)
    assert len(calls) == 2
    assert out.read_bytes().startswith(b"%PDF")


def test_the_artifact_tag_with_the_exact_hostile_fragment_succeeds():
    reply = "Here you go:\n[ARTIFACT:notes.pdf:pdf]\n# Notes\n<b><i></b>\n**bold *italic** tail*\n[/ARTIFACT]"
    html, artifacts = parse_and_process_artifacts_with_meta(reply, "test-op")
    assert "artifact-error" not in html, html
    assert "artifact-download" in html
    assert [a["type"] for a in artifacts] == ["pdf"]
