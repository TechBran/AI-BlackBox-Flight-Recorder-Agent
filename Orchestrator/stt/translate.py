"""Any-target text translation as a standalone generative step.

Independent of the STT provider: Cloud Speech translation only covers limited
language pairs, so we translate with whichever generative key is available,
preferring Gemini (GOOGLE_API_KEY / GEMINI_API_KEY) and falling back to OpenAI.

The Gemini call follows the same inline `requests` -> generateContent pattern
already used elsewhere in the codebase (e.g. Orchestrator/tasks.py audio
analysis); there is no reusable text-only Gemini helper to import.
"""
from __future__ import annotations

import requests

from Orchestrator import config


def translate_text(text: str, target_lang: str) -> str:
    """Translate ``text`` into ``target_lang`` using the available provider.

    Prefers Gemini when a Google/Gemini key is set, else OpenAI. Empty input
    short-circuits to "". Raises RuntimeError if no provider is configured.
    """
    if not text.strip():
        return ""

    if config.GOOGLE_API_KEY or config.GEMINI_API_KEY:
        return _gemini_translate(text, target_lang).strip()
    if config.OPENAI_API_KEY:
        return _openai_translate(text, target_lang).strip()
    raise RuntimeError(
        "no translation provider configured (set GOOGLE_API_KEY or OPENAI_API_KEY)"
    )


def _gemini_translate(text: str, target_lang: str) -> str:
    """Translate via Gemini generateContent (inline requests pattern)."""
    api_key = config.GOOGLE_API_KEY or config.GEMINI_API_KEY
    model = getattr(config, "GEMINI_MODEL_DEFAULT", None) or "gemini-2.5-flash"
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    prompt = (
        f"Translate the following text into {target_lang}. "
        f"Output ONLY the translation, no preamble, no quotes.\n\n{text}"
    )
    resp = requests.post(
        url,
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _openai_translate(text: str, target_lang: str) -> str:
    """Translate via OpenAI chat completions (gpt-4o)."""
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {config.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a translation engine. Translate the user's text "
                        f"into {target_lang}. Output only the translation."
                    ),
                },
                {"role": "user", "content": text},
            ],
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]
