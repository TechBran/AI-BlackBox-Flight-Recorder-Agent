#!/usr/bin/env python3
"""tts_sanitize.py — scrub non-spoken symbols out of text before it reaches a TTS model.

Every TTS provider (OpenAI, ElevenLabs, Kokoro/local, Gemini, Google) will happily
read raw markdown and stray symbols out loud: ``**bold**`` becomes "asterisk asterisk
bold asterisk asterisk", a ``|`` in a table becomes "vertical bar", an emoji becomes
its CLDR name. The model is told not to emit markdown (behavioral_core), but it still
does, so this is the deterministic safety net applied at the TTS boundary.

Design — SUBTRACTIVE, not allow-listed:
  * We DELETE a fixed set of symbols that are spoken as noise, and UNWRAP markdown
    emphasis (keeping the words). Everything else — including accented/non-Latin
    letters (``ñ``, ``¡``, ``¿``, CJK, …) and real punctuation — is preserved. An
    allow-list of ASCII would silently mangle every Spanish/Unicode reply.
  * Parentheses and their contents are LEFT ALONE so Gemini emotional cues
    ``(frantically) Morty!`` keep working. Speaker labels ``Speaker 1:`` survive too.

``sanitize_for_speech`` is a pure function: same input → same output, no I/O.
"""
from __future__ import annotations

import re

# Fenced code markers (```lang ... ```): drop the fence + language tag, keep the
# code text itself (reading code aloud is governed elsewhere; here we only kill symbols).
_FENCE = re.compile(r"```[A-Za-z0-9_+-]*")

# Markdown image / link: keep the human label, drop the URL + brackets.
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")

# Emphasis wrappers — UNWRAP (keep the inner words):
#   **bold**, ***b***, *italic*  → inner
_EMPH_STAR = re.compile(r"\*{1,3}([^*\n]+?)\*{1,3}")
#   ~~strike~~ → inner
_STRIKE = re.compile(r"~~([^~\n]+?)~~")
#   _italic_ → inner, but ONLY when the underscores are on word boundaries, so
#   identifiers like ``test_foo`` are not treated as emphasis.
_EMPH_UNDER = re.compile(r"(?<![A-Za-z0-9])_{1,3}([^_\n]+?)_{1,3}(?![A-Za-z0-9])")

# Line-leading structural markdown (multiline): ATX headings, blockquotes, bullets.
_HEADING = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]*")
_BLOCKQUOTE = re.compile(r"(?m)^[ \t]*>[ \t]?")
_BULLET = re.compile(r"(?m)^[ \t]*[-*+][ \t]+")
# Horizontal rules: a line of only ---, ***, ___, ===, ~~~ (3+).
_HRULE = re.compile(r"(?m)^[ \t]*([-*_=~])\1{2,}[ \t]*$")

# Emoji / pictographs / dingbats / arrows / regional indicators / VS16 / ZWJ.
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # symbols & pictographs, emoticons, transport, supplemental
    "\U00002600-\U000027BF"  # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"  # regional indicators (flags)
    "\U00002190-\U000021FF"  # arrows
    "\U00002B00-\U00002BFF"  # misc symbols and arrows
    "\U00002460-\U000024FF"  # enclosed alphanumerics (①②…)
    "️"                 # variation selector-16
    "‍"                 # zero-width joiner
    "]"
)

# Residual noise symbols spoken aloud by TTS engines. NOTE: this set deliberately
# EXCLUDES parentheses, sentence punctuation (. , ! ? : ; ' " -), the em/en dash,
# ¡ ¿ …, and word-mapped symbols kept on purpose (& @ % $ + /). ``|`` and ``_`` are
# handled separately (→ space) so words are not glued together.
_NOISE = re.compile(r"[*#`~=<>\[\]{}\\^•·▪▸►◦‣※¶§]")


def sanitize_for_speech(text: str | None) -> str:
    """Return ``text`` with non-spoken symbols removed, markdown unwrapped.

    Idempotent and safe on empty / non-string input. Preserves letters of any
    script, real punctuation, and parenthetical content (Gemini cues).
    """
    if not isinstance(text, str) or not text:
        return ""

    t = text
    # 1) Fenced-code markers and inline backticks (keep the enclosed text).
    t = _FENCE.sub(" ", t)
    t = t.replace("`", "")
    # 2) Images/links → label only.
    t = _IMAGE.sub(r"\1", t)
    t = _LINK.sub(r"\1", t)
    # 3) Unwrap emphasis (repeat the star pass so nested/adjacent runs settle).
    t = _STRIKE.sub(r"\1", t)
    for _ in range(3):
        new = _EMPH_STAR.sub(r"\1", t)
        if new == t:
            break
        t = new
    t = _EMPH_UNDER.sub(r"\1", t)
    # 4) Line-leading structure + horizontal rules.
    t = _HRULE.sub("", t)
    t = _HEADING.sub("", t)
    t = _BLOCKQUOTE.sub("", t)
    t = _BULLET.sub("", t)
    # 5) Separators that must not glue words together → space.
    t = t.replace("|", " ").replace("_", " ")
    # 6) Emoji / pictographs → gone.
    t = _EMOJI.sub("", t)
    # 7) Any remaining noise symbols → gone.
    t = _NOISE.sub("", t)
    # 8) Tidy whitespace (collapse runs, cap blank lines, drop spaces before
    #    punctuation left behind by removals).
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r" +([.,!?;:])", r"\1", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    return t.strip()
