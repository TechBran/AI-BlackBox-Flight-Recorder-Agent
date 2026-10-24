"""Guard: production code carries no dev-box identity (audit rank 36, O12-07).

Fails when shipped code contains
  * the dev box's operator name as a quoted literal ('Brandon', 'Brandon-DEV',
    'brandon-phone', any case) anywhere but a full-line comment; in shell
    scripts, unit, config and data files, Rust and extensionless scripts the
    bare word in any case too (an unquoted ${1:-Brandon} or
    Environment=DEFAULT_OPERATOR=Brandon routes just the same). A fresh box has
    no such operator, so the literal silently routes memory, Gmail tokens or
    pairing to a phantom (resolve the operator instead: config.current_default(),
    pairing_operator(), or require it);
  * the vendor's phone number or email in any format (any separators, Unicode
    dashes and spaces, fullwidth digits, URL- or HTML-encoded): it used to be
    seeded as every box's "self" contact. It is matched by SHA-256 so it never
    appears in this public repo, not even here;
  * in Python, a hard-coded loopback URL for the Orchestrator port (localhost,
    127.0.0.1, 0.0.0.0 or [::1]) or a bare 9091 port constant: use
    config.orchestrator_url() / config.ORCHESTRATOR_PORT, which follow
    ORCHESTRATOR_PORT.

Scope: Orchestrator/, ToolVault/, MCP/ and the install/ops scripts (Scripts/,
scripts/, installer/) on the server side; the Portal (modules, onboarding, the
page shell) and the Android app's src/main on the client side. Tests, docs (.md),
git-ignored files (Portal/uploads, build output) and vendored data are out of
scope. Files are listed with git; in a tree without git (a source export) the
scan walks the same roots instead, so the guard never passes by skipping.
A justified exception goes in ALLOWLIST with the checks it is exempt from and
why; an entry that no longer excuses anything fails.
"""
import ast
import hashlib
import html
import os
import re
import subprocess
import unicodedata
import warnings
from pathlib import Path
from urllib.parse import unquote

import pytest

REPO = Path(__file__).resolve().parents[2]
ANDROID_MAIN = ("AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/"
                "AI_BlackBox_Portal/app/src/main")
SERVER_ROOTS = ("Orchestrator", "ToolVault", "MCP", "Scripts", "scripts", "installer")
CLIENT_ROOTS = ("Portal", ANDROID_MAIN)
LOOPBACK_ROOTS = ("Orchestrator", "ToolVault")

# "" is an extensionless file: a shebang script, a sudoers or rc template.
SCAN_SUFFIXES = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".kt", ".kts", ".java", ".rs", ".html", ".sh",
    ".json", ".ini", ".toml", ".yaml", ".yml", ".xml", ".service", ".template", ".conf", "",
}
# No identifier syntax tells a name from a value in these, so the operator check
# matches the bare word there.
_WORD_SUFFIXES = {".sh", ".service", ".ini", ".conf", ".template", ".toml", ".yaml", ".yml",
                  ".json", ".rs", ""}
_TEST_DIRS = {"tests", "test", "androidTest", "testFixtures", "__pycache__",
              "node_modules", "venv", ".venv", "site-packages"}
# What .gitignore keeps out of the roots; the walk without git skips it too.
_IGNORED_DIRS = {".git", "uploads", "artifacts", "build", ".gradle", ".cxx", "captures",
                 "target", "profiles", "credentials", "secrets", "Secrets"}
_IGNORED_NAMES = {".env", ".env.local", "supervisor.env", "gateways.json", "local.properties"}

CHECKS = frozenset({"operator", "phone", "email", "loopback", "port"})

# path (or directory prefix ending in "/") -> (the checks it is exempt from, why).
ALLOWLIST = {
    "Orchestrator/tokenizers_vendored/": (
        CHECKS, "third-party tokenizer vocabularies: 'Brandon' is a vocabulary entry, "
                "and digit runs are token ids"),
    "Orchestrator/onboarding/validators.py": (
        {"loopback"}, "TODO(U17, owns validators.py): the Gmail OAuth probe's redirect_uris "
                      "literal should come from config.orchestrator_url()"),
    "Orchestrator/config.py": (
        {"port"}, "ORCHESTRATOR_PORT's built-in default, the one value every loopback "
                  "URL derives from"),
    "Orchestrator/utils/loopback.py": (
        {"port"}, "the stdlib-only loopback helper the lean MCP venv loads without "
                  "Orchestrator.config; config.ORCHESTRATOR_PORT wins whenever config is "
                  "loaded, and this is the same built-in default"),
    "Orchestrator/security/origin_guard.py": (
        {"port"}, "a constructor default; its one production caller (security/perimeter.py) "
                  "passes config.ORCHESTRATOR_PORT"),
    "Orchestrator/device_registry/registry.py": (
        {"port"}, "a reachability probe of a REMOTE device's ports (a peer box's Orchestrator "
                  "among them), not a URL of this box"),
    "Orchestrator/onboarding/tailscale_actuator.py": (
        {"port"}, "TODO(U18, installer): `tailscale serve --https=443 9091` must match the "
                  "root-owned sudoers rule's argv; take the port from ORCHESTRATOR_PORT "
                  "together with that rule"),
}

# The vendor's line (last 10 digits) and email, by SHA-256. The line's digest is
# also contacts.RETIRED_SEED_PHONE_SHA256's legacy entry.
VENDOR_PHONE_SHA256 = "6cbf6b02a2a8d8b079a48e45f81d6545cb54d489c8aada40c0a3090264c621a5"
VENDOR_EMAIL_SHA256 = "05a151a7cd22ed7a5c4ab11261158009370c579481ea4dd4a6bde56fdfbf4c3e"

# A quoted string that is the name, or starts with it as an id ('brandon-phone').
OPERATOR_LITERAL_RX = re.compile(r"""["'`]brandon(?:[-_][\w-]*)?["'`]""", re.I)
OPERATOR_WORD_RX = re.compile(r"(?<![A-Za-z0-9])brandon(?![A-Za-z0-9])", re.I)
_EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_LOOPBACK_RX = re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]):9091\b")
ORCHESTRATOR_DEFAULT_PORT = 9091


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _listed(roots, repo):
    """Repo-relative files under ``roots``: git's tracked and untracked files
    minus the ignored ones, or, where git cannot list them, a walk that skips
    the directories .gitignore keeps out."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *roots],
            cwd=repo, capture_output=True, check=True, timeout=60,
        ).stdout
        return sorted({p for p in out.decode("utf-8", "surrogateescape").split("\0") if p})
    except (OSError, subprocess.SubprocessError):
        pass
    found = set()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(Path(repo) / root):
            dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
            found.update((Path(dirpath) / name).relative_to(repo).as_posix()
                         for name in filenames if name not in _IGNORED_NAMES)
    return sorted(found)


def _scoped_paths(roots, repo=REPO):
    for rel in _listed(roots, repo):
        path = Path(rel)
        if path.suffix not in SCAN_SUFFIXES or _TEST_DIRS.intersection(path.parts):
            continue
        name = path.name
        if name.startswith("test_") or name.endswith(("_test.py", ".test.mjs", ".test.js", ".spec.js")):
            continue
        if (Path(repo) / rel).is_file():
            yield rel


def _read(rel, repo=REPO):
    return (Path(repo) / rel).read_text(encoding="utf-8", errors="replace")


def _exempt(rel: str, check: str) -> bool:
    return any((rel == key or (key.endswith("/") and rel.startswith(key))) and check in checks
               for key, (checks, _why) in ALLOWLIST.items())


def _decoded_lines(text: str):
    """Each line as a reader sees it: URL- and HTML-decoded and NFKC-normalized
    (fullwidth digits become ASCII, NBSP a space)."""
    for n, line in enumerate(text.splitlines(), 1):
        yield n, unicodedata.normalize("NFKC", html.unescape(unquote(line)))


def _phone_hits(text: str, digest: str = VENDOR_PHONE_SHA256):
    """Line numbers whose digits, read in order with everything else dropped,
    hold a 10-digit window that hashes to the vendor line."""
    hits = []
    for n, line in _decoded_lines(text):
        digits = "".join(ch for ch in line if "0" <= ch <= "9")
        if any(_sha(digits[i:i + 10]) == digest for i in range(len(digits) - 9)):
            hits.append(n)
    return hits


def _email_hits(text: str, digest: str = VENDOR_EMAIL_SHA256):
    return [n for n, line in _decoded_lines(text)
            if any(_sha(m.group().lower()) == digest for m in _EMAIL_RX.finditer(line))]


# Full-line comment openers by file type. A comment cannot route memory, tokens
# or a pairing anywhere, so a name in one (an example, a history note) is not an
# offender; one in code, a string, a docstring, markup or data is.
_HASH_COMMENT_SUFFIXES = {".py", ".sh", ".ini", ".toml", ".yaml", ".yml", ".conf",
                          ".service", ".template", ""}
_SLASH_COMMENT_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".kt", ".kts", ".java", ".rs"}


def _is_comment_line(suffix: str, line: str) -> bool:
    stripped = line.lstrip()
    if suffix in _HASH_COMMENT_SUFFIXES:
        return stripped.startswith("#")
    if suffix in _SLASH_COMMENT_SUFFIXES:
        return stripped.startswith(("//", "/*", "*"))
    if suffix in (".html", ".xml"):
        return stripped.startswith("<!--")
    return False


def _operator_hits(rel: str, text: str):
    """Line numbers of the dev-box operator name outside full-line comments."""
    suffix = Path(rel).suffix
    rx = OPERATOR_WORD_RX if suffix in _WORD_SUFFIXES else OPERATOR_LITERAL_RX
    return [n for n, line in enumerate(text.splitlines(), 1)
            if rx.search(line) and not _is_comment_line(suffix, line)]


def _python_constants(src: str):
    """(lineno, value) of every constant in ``src`` outside docstrings."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return sorted(((node.lineno, node.value) for node in ast.walk(tree)
                   if isinstance(node, ast.Constant) and id(node) not in docstrings),
                  key=lambda item: item[0])


def _loopback_literals(src: str):
    return [n for n, value in _python_constants(src)
            if isinstance(value, str) and _LOOPBACK_RX.search(value)]


def _port_literals(src: str):
    """A bare Orchestrator port: the int, or a string that is only the port
    (what "http://localhost:" + "9091" splits off)."""
    return [n for n, value in _python_constants(src)
            if (type(value) is int and value == ORCHESTRATOR_DEFAULT_PORT)
            or (isinstance(value, str) and value.strip().lstrip(":") == str(ORCHESTRATOR_DEFAULT_PORT))]


_MATCHERS = {
    "operator": _operator_hits,
    "phone": lambda rel, text: _phone_hits(text),
    "email": lambda rel, text: _email_hits(text),
    "loopback": lambda rel, text: (_loopback_literals(text)
                                   if rel.endswith(".py") and _LOOPBACK_RX.search(text) else []),
    "port": lambda rel, text: (_port_literals(text)
                               if rel.endswith(".py") and str(ORCHESTRATOR_DEFAULT_PORT) in text else []),
}
_LABELS = {"operator": "operator literal", "phone": "vendor phone number", "email": "vendor email",
           "loopback": "loopback URL literal", "port": "bare Orchestrator port"}


def _offenders(roots, checks=("operator", "phone", "email"), repo=REPO):
    found = []
    for rel in _scoped_paths(roots, repo):
        active = [check for check in checks if not _exempt(rel, check)]
        if not active:
            continue
        text = _read(rel, repo)
        for check in active:
            found += [f"{rel}:{n}: {_LABELS[check]}" for n in _MATCHERS[check](rel, text)]
    return found


# ── the matchers themselves ──────────────────────────────────────────────────

def test_phone_matcher_finds_every_format():
    digest = _sha("5555550177")   # a fixture line stands in for the vendor's
    for text in ('"+15555550177"', "+1 (555) 555-0177", "555.555.0177", "1-555-555-0177",
                 "x = 15555550177", "tel:+1 555 555 0177",
                 # a run of other digits first, and long digit-heavy lines
                 "v2.3.1 - 2026-10-17 - 555-555-0177", "10 20 30 40 50 60 70 555 555 0177",
                 # Unicode separators and digits
                 "+1 555 555 0177", "555–555–0177", "555—555‒0177",
                 "５５５５５５０１７７",
                 # slash, URL- and HTML-encoded
                 "555/555-0177", 'href="tel:+1%20555%20555%200177"', "tel:%2B15555550177",
                 "&#53;55-555-0177"):
        assert _phone_hits(text, digest) == [1], text
    assert _phone_hits("\n\n+1 555 555 0177", digest) == [3]
    assert _phone_hits("+15555550178\n2026-10-17 12:00", digest) == []


def test_email_matcher_decodes_the_line():
    digest = _sha("seed-owner@example.invalid")
    for text in ("seed-owner@example.invalid", "mailto:Seed-Owner@Example.invalid",
                 "mailto:seed-owner%40example.invalid", "seed-owner&#64;example.invalid"):
        assert _email_hits(text, digest) == [1], text
    assert _email_hits("other@example.invalid", digest) == []


def test_operator_matcher():
    assert OPERATOR_LITERAL_RX.search('os.getenv("DEFAULT_OPERATOR", "Brandon")')
    assert OPERATOR_LITERAL_RX.search("operator || 'Brandon'")
    assert OPERATOR_LITERAL_RX.search('body.get("operator", "Brandon-DEV")')
    assert OPERATOR_LITERAL_RX.search("device_id = 'brandon-phone'")
    assert OPERATOR_LITERAL_RX.search('op = "brandon"')
    assert not OPERATOR_LITERAL_RX.search("# Brandon's directive 2026-05-20")
    assert not OPERATOR_LITERAL_RX.search('"Brandon WALKS the catalogue"')


@pytest.mark.parametrize("rel, src, lines", [
    # A full-line comment cannot route anything; code, strings and markup can.
    ("a.kt", ' * returns {"operators":["Brandon","Anna"]}\nval op = "Brandon"\n', [2]),
    ("a.js", '    // e.g. "Brandon-DEV"\nconst op = x || \'Brandon\';\n', [2]),
    ("a.js", 'class A {\n    #op = "Brandon";\n}\n', [2]),  # a private field, not a comment
    ("a.py", '# e.g. "Brandon"\nOP = "Brandon"\n', [2]),
    ("a.py", 'def f():\n    """Run as "Brandon"."""\n', [2]),  # a docstring is still code text
    ("a.html", '<!-- "Brandon" -->\n<option>"Brandon"</option>\n', [2]),
    ("a.kt", '# not a Kotlin comment: "Brandon"\n', [1]),
])
def test_operator_hits_skip_full_line_comments(rel, src, lines):
    assert _operator_hits(rel, src) == lines


@pytest.mark.parametrize("rel, src, lines", [
    # Shell, unit, config, data, Rust and extensionless scripts: the bare word, any case.
    ("Scripts/x.sh", '# Brandon 2026-05-17: why\nOPERATOR="${1:-Brandon}"\n', [2]),
    ("installer/a.service", "Environment=DEFAULT_OPERATOR=Brandon\n", [1]),
    ("installer/a.ini", "[users]\nlist = Brandon\n", [2]),
    ("installer/a.yaml", "operator: brandon\n", [1]),
    ("ToolVault/tools/t/schema.json", '{"description": "e.g. brandon_phone"}\n', [1]),
    ("installer/src/lib.rs", "// Brandon's E6 ask\nlet op = String::from(\"brandon\");\n", [2]),
    ("Scripts/shim", "#!/bin/sh\n# Brandon's note\nOP=brandon-dev\n", [3]),
    ("installer/templates/sudoers", "# Brandon\nbrandon ALL=(root) NOPASSWD: /bin/true\n", [2]),
    # ... but not a longer word, and code languages keep the quoted rule.
    ("Scripts/x.sh", "USER=brandonsmith\n", []),
    ("a.py", "x = brandon_default()\n", []),
])
def test_operator_word_rule_for_scripts_config_and_data(rel, src, lines):
    assert _operator_hits(rel, src) == lines


def test_loopback_and_port_matchers():
    src = ('"""Talks to http://localhost:9091."""\n'
           'URL = "http://localhost:9091/chat"\n'
           'WS = f"ws://127.0.0.1:9091{path}"\n'
           '# http://localhost:9091 in a comment\n'
           'ANY = "http://0.0.0.0:9091/chat"\n'
           'V6 = "http://[::1]:9091/chat"\n')
    assert _loopback_literals(src) == [2, 3, 5, 6]
    split = ('A = "http://localhost:" + "9091"\n'
             'B = f"http://localhost:{9091}/chat"\n'
             'C = ("127.0.0.1", 9091)\n'
             'D = 19091\n'
             'E = True\n')
    assert _port_literals(split) == [1, 2, 3]


def test_vendor_phone_digest_is_the_retired_seed_digest():
    from Orchestrator import contacts
    assert VENDOR_PHONE_SHA256 in contacts.RETIRED_SEED_PHONE_SHA256


def _fake_repo(tmp_path, files):
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def test_an_allowlist_entry_exempts_only_its_own_checks(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path, {"Orchestrator/x.py": 'URL = "http://localhost:9091/a"\nOP = "Brandon"\n'})
    monkeypatch.setattr(subprocess, "run", _no_git)
    monkeypatch.setitem(ALLOWLIST, "Orchestrator/x.py", ({"loopback"}, "a fixture entry"))
    assert _offenders(("Orchestrator",), ("operator", "loopback"), repo=repo) == [
        "Orchestrator/x.py:2: operator literal"]
    monkeypatch.setitem(ALLOWLIST, "Orchestrator/x.py", ({"loopback", "operator"}, "a fixture entry"))
    assert _offenders(("Orchestrator",), ("operator", "loopback"), repo=repo) == []


def _no_git(*a, **k):
    raise FileNotFoundError("git")


def test_without_git_the_guard_walks_the_tree_instead_of_skipping(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path, {
        "Orchestrator/bad.py": 'OP = os.getenv("X", "Brandon")\n',
        "Orchestrator/tests/test_x.py": 'OP = "Brandon"\n',           # tests are out of scope
        "Portal/uploads/note.json": '{"who": "Brandon"}\n',           # git-ignored user content
        "Portal/modules/ok.js": "const op = current();\n",
    })
    monkeypatch.setattr(subprocess, "run", _no_git)
    assert _offenders(("Orchestrator", "Portal"), repo=repo) == ["Orchestrator/bad.py:1: operator literal"]
    # Not a git checkout at all (git itself present): the same walk.
    monkeypatch.undo()
    assert _offenders(("Orchestrator", "Portal"), repo=repo) == ["Orchestrator/bad.py:1: operator literal"]


def test_allowlist_entries_exist_and_say_why():
    for key, (checks, reason) in ALLOWLIST.items():
        assert (REPO / key).exists(), f"stale ALLOWLIST entry: {key}"
        assert len(reason) > 20, f"ALLOWLIST entry {key} needs a real reason"
        assert checks and set(checks) <= CHECKS, f"ALLOWLIST entry {key} names no known check"
        if not key.endswith("/"):
            # A file entry must still hold what it excuses; drop the check once fixed.
            text = _read(key)
            for check in checks:
                assert _MATCHERS[check](key, text), f"stale ALLOWLIST entry: {key} ({check})"


# ── the guards ───────────────────────────────────────────────────────────────

def test_server_code_has_no_dev_box_identity():
    offenders = _offenders(SERVER_ROOTS)
    assert not offenders, (
        "Dev-box identity in production code — resolve the operator "
        "(config.current_default() / pairing_operator() / a required field) and "
        "take numbers from the telephony config:\n  " + "\n  ".join(offenders))


@pytest.mark.xfail(strict=False, reason=(
    "TODO(U16): the Portal modules and the Android app still default the operator "
    "to the dev-box name; U16 (client operator + models) removes them in parallel "
    "(its branch scans clean). Drop this mark once it is merged."))
def test_client_code_has_no_dev_box_identity():
    offenders = _offenders(CLIENT_ROOTS)
    assert not offenders, "Dev-box identity in client code:\n  " + "\n  ".join(offenders)


def test_client_code_has_no_vendor_number_or_email():
    # Unlike the operator literal, the vendor's contact details are already gone
    # from the clients; keep it that way without waiting for U16.
    found = _offenders(CLIENT_ROOTS, ("phone", "email"))
    assert not found, "\n  ".join(found)


def test_server_python_derives_loopback_urls_from_the_port():
    offenders = _offenders(LOOPBACK_ROOTS, ("loopback", "port"))
    assert not offenders, (
        "Hard-coded Orchestrator loopback URL or port — use config.orchestrator_url() / "
        "config.ORCHESTRATOR_PORT so a box on another ORCHESTRATOR_PORT reaches itself:\n  "
        + "\n  ".join(offenders))
