"""A DOCX artifact must never fail on control characters in the model's text.

python-docx (lxml) refuses any string that XML 1.0 cannot carry: "All strings
must be XML compatible: Unicode or ASCII, no NULL bytes or control
characters". Model text that quotes terminal or CLI-agent output carries ANSI
escapes (\\x1b[31m) and sometimes NUL bytes, so the WHOLE .docx artifact failed
with an error box, while the PDF path set the same text fine (rank 58 review,
the sibling of the PDF markup fix).

Now generate_docx drops ANSI escape sequences and the characters XML cannot
carry before the text reaches python-docx; tabs, newlines and every printable
character are kept.

Hermetic: artifacts are written under tmp_path.
"""
import pytest
from docx import Document

import Orchestrator.artifacts as artifacts_mod
from Orchestrator.artifacts import generate_docx, parse_and_process_artifacts_with_meta

HOSTILE = (
    "ANSI \x1b[31mred\x1b[0m output",
    "a NUL \x00 byte",
    "bell \x07 and form feed \x0c and vertical tab \x0b",
    "a lone surrogate \ud800 here",
    "noncharacters ￾￿ end",
    "**bold \x1b[1m*italic** tail*",
)


@pytest.fixture(autouse=True)
def _isolate_artifacts_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(artifacts_mod, "ARTIFACTS_DIR", tmp_path)


def _paragraphs(path):
    return [p.text for p in Document(str(path)).paragraphs]


@pytest.mark.parametrize("fragment", HOSTILE)
def test_control_characters_still_make_a_docx(tmp_path, fragment):
    out = tmp_path / "notes.docx"
    generate_docx(f"# {fragment}\n\n{fragment}\n- {fragment}\n1. {fragment}", out)
    text = "\n".join(_paragraphs(out))
    for bad in ("\x1b", "\x00", "\x07", "\x0b", "\x0c", "\ud800", "￾", "￿"):
        assert bad not in text


def test_ansi_sequences_are_dropped_whole(tmp_path):
    out = tmp_path / "notes.docx"
    generate_docx("ANSI \x1b[31mred\x1b[0m output", out)
    assert _paragraphs(out) == ["ANSI red output"]


def test_printable_text_tabs_and_unicode_are_kept(tmp_path):
    out = tmp_path / "notes.docx"
    generate_docx("# Café — résumé\ncol1\tcol2 \U0001F600\n**bold** and *it*", out)
    assert _paragraphs(out) == ["Café — résumé", "col1\tcol2 \U0001F600", "bold and it"]


def test_the_artifact_tag_with_terminal_output_succeeds():
    reply = ("Here is the log:\n[ARTIFACT:notes.docx:docx]\n# Build log\n"
             "\x1b[32mPASS\x1b[0m 12 tests\nnul \x00 here\n[/ARTIFACT]")
    html, artifacts = parse_and_process_artifacts_with_meta(reply, "test-op")
    assert "artifact-error" not in html, html
    assert [a["type"] for a in artifacts] == ["docx"]
