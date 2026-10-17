"""Guard: production code carries no dev-box identity (audit rank 36, O12-07).

Fails when shipped code contains
  * the dev box's operator name as a string literal ('Brandon', 'Brandon-DEV')
    — a fresh box has no such operator, so the literal silently routes memory,
    Gmail tokens or pairing to a phantom (resolve the operator instead:
    config.current_default(), pairing_operator(), or require it);
  * the vendor's phone number or email in any format — it used to be seeded as
    every box's "self" contact. It is matched by SHA-256 so it never appears
    in this public repo, not even here;
  * a hard-coded loopback URL for the Orchestrator port in Python (use
    config.orchestrator_url(), which follows ORCHESTRATOR_PORT).

Scope: Orchestrator/, ToolVault/, MCP/ (server) and Portal/modules + the Android
app's src/main (clients). Tests, docs (.md) and vendored data are out of scope.
A justified exception goes in ALLOWLIST with its reason; stale entries fail.
"""
import ast
import hashlib
import re
import subprocess
import warnings
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ANDROID_MAIN = ("AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/"
                "AI_BlackBox_Portal/app/src/main")
SERVER_ROOTS = ("Orchestrator", "ToolVault", "MCP")
CLIENT_ROOTS = ("Portal/modules", ANDROID_MAIN)

SCAN_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".kt", ".kts", ".java", ".html", ".sh",
    ".json", ".ini", ".toml", ".yaml", ".yml", ".xml", ".service", ".template", ".conf",
}
_TEST_DIRS = {"tests", "test", "androidTest", "testFixtures", "__pycache__",
              "node_modules", "venv", ".venv", "site-packages"}

# path (or directory prefix ending in "/") -> why it may contain a flagged literal.
ALLOWLIST = {
    "Orchestrator/tokenizers_vendored/":
        "third-party tokenizer vocabularies: 'Brandon' is a vocabulary entry, "
        "and digit runs are token ids",
    "Orchestrator/onboarding/validators.py":
        "TODO(U17, owns validators.py): the Gmail OAuth probe's redirect_uris "
        "literal should come from config.orchestrator_url()",
}

# The vendor's line (last 10 digits) and email, by SHA-256. The line's digest is
# also contacts.RETIRED_SEED_PHONE_SHA256's legacy entry.
VENDOR_PHONE_SHA256 = "6cbf6b02a2a8d8b079a48e45f81d6545cb54d489c8aada40c0a3090264c621a5"
VENDOR_EMAIL_SHA256 = "05a151a7cd22ed7a5c4ab11261158009370c579481ea4dd4a6bde56fdfbf4c3e"

OPERATOR_LITERAL_RX = re.compile(r"""["'`]Brandon(?:-DEV)?["'`]""")
# A phone-shaped run: digits with the usual separators, on one line.
_PHONE_RUN_RX = re.compile(r"\+?\(?\d[\d \t().\-]{8,24}\d")
_EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_LOOPBACK_RX = re.compile(r"(?:localhost|127\.0\.0\.1):9091\b")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _scoped_files(roots):
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *roots],
            cwd=REPO, capture_output=True, check=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is unavailable: cannot list the tracked production files")
    for rel in sorted({p for p in out.decode("utf-8", "surrogateescape").split("\0") if p}):
        path = Path(rel)
        if path.suffix not in SCAN_SUFFIXES or _TEST_DIRS.intersection(path.parts):
            continue
        name = path.name
        if name.startswith("test_") or name.endswith(("_test.py", ".test.mjs", ".test.js", ".spec.js")):
            continue
        if _allowlisted(rel):
            continue
        full = REPO / rel
        if full.is_file():
            yield rel, full.read_text(encoding="utf-8", errors="replace")


def _allowlisted(rel: str) -> bool:
    return any(rel == key or (key.endswith("/") and rel.startswith(key)) for key in ALLOWLIST)


def _phone_hits(text: str, digest: str = VENDOR_PHONE_SHA256):
    """Line numbers of digit runs whose 10-digit windows hash to the vendor line."""
    hits = []
    for m in _PHONE_RUN_RX.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if len(digits) < 10:
            continue
        if any(_sha(digits[i:i + 10]) == digest for i in range(len(digits) - 9)):
            hits.append(text.count("\n", 0, m.start()) + 1)
    return hits


def _email_hits(text: str):
    return [text.count("\n", 0, m.start()) + 1 for m in _EMAIL_RX.finditer(text)
            if _sha(m.group().lower()) == VENDOR_EMAIL_SHA256]


def _offenders(roots):
    found = []
    for rel, text in _scoped_files(roots):
        for m in OPERATOR_LITERAL_RX.finditer(text):
            found.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}: operator literal {m.group()}")
        found += [f"{rel}:{n}: vendor phone number" for n in _phone_hits(text)]
        found += [f"{rel}:{n}: vendor email" for n in _email_hits(text)]
    return found


# ── the matchers themselves ──────────────────────────────────────────────────

def test_phone_matcher_finds_every_format():
    digest = _sha("5555550177")   # a fixture line stands in for the vendor's
    for text in ('"+15555550177"', "+1 (555) 555-0177", "555.555.0177", "1-555-555-0177",
                 "x = 15555550177", "tel:+1 555 555 0177"):
        assert _phone_hits(text, digest) == [1], text
    assert _phone_hits("\n\n+1 555 555 0177", digest) == [3]
    assert _phone_hits("+15555550178\n2026-10-17 12:00", digest) == []


def test_operator_matcher():
    assert OPERATOR_LITERAL_RX.search('os.getenv("DEFAULT_OPERATOR", "Brandon")')
    assert OPERATOR_LITERAL_RX.search("operator || 'Brandon'")
    assert OPERATOR_LITERAL_RX.search('body.get("operator", "Brandon-DEV")')
    assert not OPERATOR_LITERAL_RX.search("# Brandon's directive 2026-05-20")
    assert not OPERATOR_LITERAL_RX.search('"Brandon WALKS the catalogue"')


def test_vendor_phone_digest_is_the_retired_seed_digest():
    from Orchestrator import contacts
    assert VENDOR_PHONE_SHA256 in contacts.RETIRED_SEED_PHONE_SHA256


def test_allowlist_entries_exist_and_say_why():
    for key, reason in ALLOWLIST.items():
        assert (REPO / key).exists(), f"stale ALLOWLIST entry: {key}"
        assert len(reason) > 20, f"ALLOWLIST entry {key} needs a real reason"


# ── the guards ───────────────────────────────────────────────────────────────

def test_server_code_has_no_dev_box_identity():
    offenders = _offenders(SERVER_ROOTS)
    assert not offenders, (
        "Dev-box identity in production code — resolve the operator "
        "(config.current_default() / pairing_operator() / a required field) and "
        "take numbers from the telephony config:\n  " + "\n  ".join(offenders))


@pytest.mark.xfail(strict=False, reason=(
    "TODO(U16): the Portal modules and the Android app still default the operator "
    "to the dev-box name; U16 (client operator + models) removes them in parallel. "
    "Drop this mark once it is merged."))
def test_client_code_has_no_dev_box_identity():
    offenders = _offenders(CLIENT_ROOTS)
    assert not offenders, "Dev-box identity in client code:\n  " + "\n  ".join(offenders)


def test_client_code_has_no_vendor_number_or_email():
    # Unlike the operator literal, the vendor's contact details are already gone
    # from the clients; keep it that way without waiting for U16.
    found = []
    for rel, text in _scoped_files(CLIENT_ROOTS):
        found += [f"{rel}:{n}: vendor phone number" for n in _phone_hits(text)]
        found += [f"{rel}:{n}: vendor email" for n in _email_hits(text)]
    assert not found, "\n  ".join(found)


def _loopback_literals(src: str):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings and _LOOPBACK_RX.search(node.value)]


def test_loopback_matcher_skips_docstrings():
    src = ('"""Talks to http://localhost:9091."""\n'
           'URL = "http://localhost:9091/chat"\n'
           'WS = f"ws://127.0.0.1:9091{path}"\n'
           '# http://localhost:9091 in a comment\n')
    assert _loopback_literals(src) == [2, 3]


def test_server_python_derives_loopback_urls_from_the_port():
    offenders = []
    for rel, text in _scoped_files(("Orchestrator", "ToolVault")):
        if rel.endswith(".py") and _LOOPBACK_RX.search(text):
            offenders += [f"{rel}:{n}" for n in _loopback_literals(text)]
    assert not offenders, (
        "Hard-coded Orchestrator loopback URL — use config.orchestrator_url() so a box "
        "on another ORCHESTRATOR_PORT reaches itself:\n  " + "\n  ".join(offenders))
