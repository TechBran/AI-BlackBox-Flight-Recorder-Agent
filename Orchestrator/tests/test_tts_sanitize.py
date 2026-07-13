"""Tests for sanitize_for_speech — the pre-TTS symbol scrubber."""
from Orchestrator.tts_sanitize import sanitize_for_speech as s


# --- the core complaint: asterisks must never be spoken -----------------------
def test_bold_unwrapped():
    assert s("Here is **important** stuff.") == "Here is important stuff."


def test_italic_unwrapped():
    assert s("That is *very* nice.") == "That is very nice."


def test_bold_italic_triple():
    assert s("***loud***") == "loud"


def test_standalone_asterisks_removed():
    assert s("Rating: ***") == "Rating:"


def test_leading_bullet_star_dropped():
    assert s("* first item") == "first item"


def test_times_asterisk_does_not_speak_symbol():
    # "2 * 3" has no closing pair → the lone star is dropped, not read aloud.
    assert s("2 * 3 = 6") == "2 3 6"


# --- other markdown structure -------------------------------------------------
def test_heading_hash_dropped():
    assert s("# Title") == "Title"


def test_blockquote_marker_dropped():
    assert s("> quoted line") == "quoted line"


def test_link_keeps_label_drops_url():
    assert s("See [Google](https://google.com) now") == "See Google now"


def test_image_keeps_alt():
    assert s("![a cat](cat.png)") == "a cat"


def test_inline_code_backticks_removed():
    assert s("Run `make test` please") == "Run make test please"


def test_table_pipes_become_spaces():
    # pipes → spaces, then runs collapse to single spaces.
    assert s("| a | b |") == "a b"


def test_strikethrough_unwrapped():
    assert s("~~old~~ new") == "old new"


def test_horizontal_rule_removed():
    assert s("above\n---\nbelow") == "above\n\nbelow"


def test_underscore_between_words_not_glued():
    # snake_case is not emphasis; underscore → space, words survive intact.
    assert s("open test_foo now") == "open test foo now"


def test_underscore_emphasis_unwrapped():
    assert s("that is _slick_ work") == "that is slick work"


# --- emoji / pictographs ------------------------------------------------------
def test_emoji_removed():
    assert s("Hello 😀 world 🚀") == "Hello world"


def test_arrow_symbol_removed():
    assert s("go → home") == "go home"


# --- MUST-PRESERVE guarantees -------------------------------------------------
def test_spanish_preserved():
    # No accented/inverted-punct casualties — this box speaks Spanish.
    assert s("¡Hola, señor! ¿Qué tal?") == "¡Hola, señor! ¿Qué tal?"


def test_gemini_emotional_cue_preserved():
    # Parenthetical cues drive Gemini TTS affect — leave them completely alone.
    assert s("(frantically) Morty! Get in here!") == "(frantically) Morty! Get in here!"


def test_speaker_label_preserved():
    assert s("Speaker 1: hello there") == "Speaker 1: hello there"


def test_normal_punctuation_preserved():
    assert s("Well... that's 50% off, right?") == "Well... that's 50% off, right?"


def test_word_symbols_preserved():
    # &, @, $, / map to real spoken words — keep them.
    assert s("R&D at $5/mo, email a@b.com") == "R&D at $5/mo, email a@b.com"


def test_em_dash_preserved():
    assert s("wait — really?") == "wait — really?"


# --- robustness ---------------------------------------------------------------
def test_empty_and_none():
    assert s("") == ""
    assert s(None) == ""


def test_non_string_returns_empty():
    assert s(12345) == ""  # type: ignore[arg-type]


def test_idempotent():
    once = s("# **Hi** there `code` 🚀 | end")
    assert s(once) == once


def test_only_symbols_becomes_empty():
    assert s("***") == ""
    assert s("`~|_") == ""
