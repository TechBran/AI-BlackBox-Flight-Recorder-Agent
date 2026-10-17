# Orchestrator/tests/test_upload_path_ratchet.py
"""No client- or model-supplied URL is joined onto a Portal path by hand
(audit O04-tasks-retrieval-01, O02-chat-routes-A-05, O03-chat-routes-B-03,
O07-tts-voice-04; M3 U20 item 6).

M2 routed every loader through Orchestrator.utils.paths.resolve_upload_path,
which resolves under blackbox_root()/Portal/uploads and refuses '..',
absolute paths, symlink escapes and other schemes. The old idiom it replaced:

    clean_url.replace("/ui/", "")  ->  Path("Portal") / relative_path

followed every '/ui/../' out of Portal/ (to the repo-root .env) and depended
on the process CWD. This test scans the Python sources so the idiom cannot
come back in a new route or tool: a match must be rewritten to
resolve_upload_path (or, for a fixed literal, to utils.paths helpers).
"""
import io
import re
import tokenize
from pathlib import Path

import pytest

from Orchestrator.utils.paths import blackbox_root

ROOT = blackbox_root()
SCANNED = ("Orchestrator", "ToolVault", "MCP")
SKIP_PARTS = {"tests", "venv", "__pycache__", "node_modules"}
SKIP_FILES = {ROOT / "Orchestrator" / "utils" / "paths.py"}

FORBIDDEN = [
    # A CWD-relative Portal root joined with anything: Path("Portal") / x, P("Portal") / x
    (re.compile(r"""\b(?:Path|P|PurePath)\(\s*["']Portal["']\s*\)\s*/"""), "CWD-relative Path('Portal') join"),
    (re.compile(r"""os\.path\.join\(\s*["']Portal["']"""), "CWD-relative os.path.join('Portal', ...)"),
    # Deriving a filesystem path from a /ui/ URL by string surgery
    (re.compile(r"""\.(?:split|rsplit|partition|rpartition|replace|removeprefix|lstrip)\(\s*["']/ui/"""),
     "string surgery on a /ui/ URL"),
    (re.compile(r"""\[\s*len\(\s*["']/ui/"""), "slicing off a /ui/ prefix"),
]


def _code_only(src: str) -> str:
    """The source with every comment blanked in place (strings kept: the idiom
    lives in them; a comment may quote it)."""
    lines = src.splitlines(keepends=True)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                (row, col), (_, end) = tok.start, tok.end
                line = lines[row - 1]
                lines[row - 1] = line[:col] + " " * (end - col) + line[end:]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return "".join(lines)


def _violations(src: str):
    code = _code_only(src)
    return [why for rx, why in FORBIDDEN if rx.search(code)]


def _sources():
    for top in SCANNED:
        for p in sorted((ROOT / top).rglob("*.py")):
            if SKIP_PARTS & set(p.relative_to(ROOT).parts) or p.name.startswith("test_") or p in SKIP_FILES:
                continue
            yield p


# The scanner recognizes the idiom M2 removed (this is what made '/ui/../.env' work).
@pytest.mark.parametrize("snippet", [
    'relative_path = clean_url.replace("/ui/", "")\nfile_path = Path("Portal") / relative_path\n',
    'local_path = Path("Portal") / url.split("/ui/", 1)[-1]\n',
    'file_path = P("Portal") / clean_url\n',
    "p = os.path.join('Portal', rel)\n",
    'rel = url[len("/ui/"):]\n',
    'rel = url.removeprefix("/ui/")\n',
])
def test_scanner_catches_the_old_idiom(snippet):
    assert _violations(snippet)


@pytest.mark.parametrize("snippet", [
    '# used to join the value onto a CWD-relative Path("Portal") / x\n',
    'file_path = resolve_upload_path(url, allowed_exts=IMAGE_EXTS)\n',
    'LEGACY = _blackbox_root() / "Portal" / "tasks.db"\n',
    'url = f"/ui/uploads/{filename}"\n',
])
def test_scanner_ignores_comments_and_the_guarded_forms(snippet):
    assert _violations(snippet) == []


def test_no_source_joins_a_ui_url_onto_a_portal_path():
    offenders = {}
    for p in _sources():
        found = _violations(p.read_text(encoding="utf-8", errors="replace"))
        if found:
            offenders[str(p.relative_to(ROOT))] = found
    assert offenders == {}, ("use Orchestrator.utils.paths.resolve_upload_path for every "
                             f"client/model-supplied upload reference: {offenders}")


def test_scan_covers_the_known_loaders():
    names = {p.relative_to(ROOT).as_posix() for p in _sources()}
    for must in ("Orchestrator/routes/chat_routes.py", "Orchestrator/tasks.py",
                 "Orchestrator/routes/tts_routes.py", "MCP/blackbox_mcp_server.py",
                 "ToolVault/tools/generate_video/executor.py"):
        assert must in names, must
