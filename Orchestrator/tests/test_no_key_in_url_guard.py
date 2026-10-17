"""CI guard: no source file builds an API key into a URL query string
(audit rank 23 — the "CI grep rejecting '?key={'").

requests/httpx/websockets copy the full URL into exception text, so a key in
the query string ends up in logs, HTTP error details and stored task errors.
Google accepts the key in the x-goog-api-key header on every REST and Live
endpoint the box uses; send it there.

The scan covers every tracked Python/Kotlin/Java/JS/TS source and fails on
``?key=`` / ``&key=`` (also api_key/apikey) followed by an interpolation, and
on a requests ``params={"key": ...}`` dict. EXCLUDED lists the only paths that
may keep the shape, each with its reason; add to it only for a genuinely
non-secret use.
"""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

SOURCE_SUFFIXES = {".py", ".kt", ".kts", ".java", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}

# path prefix -> why it may keep a key-in-URL shape
EXCLUDED = {
    "docs/": "archived reference copies (UGV sub-project); never run by the box",
    "Apps/apparition/lib/talkinghead.mjs": "vendored third-party library; the key is the user's own TTS option",
}

_Q = r"[?&](?:key|api_key|apikey)="
URL_KEY_INTERPOLATION = re.compile(
    _Q + r"(?:\{|\$|%s|%\(|[\"']\s*\+)"     # f"...?key={k}", `${k}`, "$k", "%s", "?key=" + k
    r"|[\"']" + _Q + r"[\"']\s*\+"           # "?key=" + k  (the literal ends right after '=')
    r"|params\s*=\s*\{\s*[\"']key[\"']\s*:"  # requests.get(url, params={"key": k})
)


def _tracked_sources():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True,
                             text=True, check=True).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        out = [str(p.relative_to(REPO)) for p in REPO.rglob("*") if p.is_file()]
    for rel in out:
        if Path(rel).suffix not in SOURCE_SUFFIXES:
            continue
        if "node_modules/" in rel or any(rel.startswith(p) for p in EXCLUDED):
            continue
        yield rel


def test_guard_pattern_catches_the_shapes_it_targets():
    bad = [
        'url = f"{BASE}/{model}:generateContent?key={GOOGLE_API_KEY}"',
        'url = f"{BASE}:stream?alt=sse&key={key}"',
        "fetch(`${base}?key=${apiKey}`)",
        'val url = "$base?key=$apiKey"',
        'fetch(endpoint + "?key=" + apiKey)',
        'requests.get(url, params={"key": GOOGLE_API_KEY})',
    ]
    good = [
        'headers = {"x-goog-api-key": GOOGLE_API_KEY}',
        'url = f"{BASE}/{model}:streamGenerateContent?alt=sse"',
        'assert "key=[REDACTED]" in out',
        'sorted(items, key=lambda x: x[0])',
    ]
    assert all(URL_KEY_INTERPOLATION.search(s) for s in bad)
    assert not any(URL_KEY_INTERPOLATION.search(s) for s in good)


def test_no_tracked_source_puts_a_key_in_a_url():
    this_file = str(Path(__file__).resolve().relative_to(REPO))
    hits = []
    for rel in _tracked_sources():
        if rel == this_file:
            continue
        try:
            text = (REPO / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if URL_KEY_INTERPOLATION.search(line):
                hits.append(f"{rel}:{n}: {line.strip()[:120]}")
    assert not hits, (
        "API key interpolated into a URL; send it in a header instead "
        "(Google: x-goog-api-key):\n" + "\n".join(hits))
