# Orchestrator/tests/test_agent_ansi_escape.py
"""ansi_to_html escapes PTY text before wrapping it in spans (audit M3 U20 item 4).

Terminal output is whatever the agent, a tool or a fetched file printed. It
was turned into HTML by swapping ANSI SGR codes for <span class=ansi-*> and
passing everything else through, so markup in the output arrived as markup.
The Portal sanitizes it, but the server must not hand out HTML it never meant
to write: the text is escaped first, then only the known codes become spans.
"""
import pytest

from Orchestrator.routes.agent_routes import ansi_to_html


@pytest.mark.parametrize("hostile", [
    "<img src=x onerror=alert(1)>",
    "<script>fetch('/agent/apps')</script>",
    '<a href="javascript:alert(1)">x</a>',
    "</span><svg onload=alert(1)>",
])
def test_markup_in_terminal_output_stays_text(hostile):
    out = ansi_to_html(hostile)
    assert "<" not in out.replace('<span class="ansi-', "").replace("</span>", "")
    assert "&lt;" in out


def test_colors_still_become_spans_around_escaped_text():
    out = ansi_to_html("\x1b[31m<b>red</b>\x1b[0m done")
    assert out == '<span class="ansi-red">&lt;b&gt;red&lt;/b&gt;</span> done'


def test_quotes_and_ampersands_escaped():
    assert ansi_to_html('a & b " c \' d') == "a &amp; b &quot; c &#x27; d"


def test_bold_reset_and_unknown_codes():
    assert ansi_to_html("\x1b[1mB\x1b[m \x1b[38;5;196mx\x1b[0m") == \
        '<span class="ansi-bold">B</span> x</span>'


def test_osc_hyperlink_carrying_markup_is_dropped():
    out = ansi_to_html("\x1b]8;;http://x/?q=<b>\x07link\x1b]8;;\x07 end")
    assert out == "link end"


def test_plain_text_unchanged():
    assert ansi_to_html("hello world\n") == "hello world\n"
