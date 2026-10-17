#!/usr/bin/env python3
"""Check the Orchestrator venv's manifest (requirements.in) against the code.

Reports, and exits 1 on, any of:
  undeclared  a third-party package the runtime code imports that
              requirements.in does not declare. Imports behind
              `except ImportError` count: that is how `from ddgs import DDGS`
              shipped against a manifest that only declared duckduckgo_search
              (audit X01-deps-python-06), so fresh boxes had no keyless search.
  unused      a requirements.in entry nothing imports, unless its comment
              carries a tag saying why it is declared anyway:
                [transitive]  a dependency's dependency, declared only for its
                              security floor or a compatibility cap
                [runtime]     loaded by name, not by an import statement
                              (bs4's "lxml" parser, starlette's form parser)
  stale       an OPTIONAL_IMPORTS entry the code no longer imports.

Scope: the code the Orchestrator venv runs (DEFAULT_PATHS), minus test code
(tests/ directories, test_*.py, *_test.py, conftest.py, and any module that
imports pytest), which runs with requirements.dev.txt on top. The MCP server
has its own venv and manifest.

Import names are mapped to distributions from the installed metadata of the
interpreter running this script (run it with a venv built from the lock), then
from STATIC_MODULE_MAP, then by the import's own name. Standard library,
first-party modules (anything defined in this repo) and relative imports are
ignored.

Usage:
  python scripts/check_imports_vs_manifest.py              # repo defaults
  python scripts/check_imports_vs_manifest.py --require-installed
      # also fail when an imported package is not importable here (CI runs
      # this inside a venv built from requirements.lock.txt)
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = "requirements.in"
DEFAULT_PATHS = ("Orchestrator", "ToolVault")

# Directories never scanned (for code, or for first-party module names).
SKIP_DIRS = {
    ".git", ".claude", "venv", ".venv", "node_modules", "__pycache__",
    "site-packages", "uploads", "Volumes", "Archive",
}

TAGS = ("[transitive]", "[runtime]")

# Imports the runtime code makes that are deliberately NOT in requirements.in.
# Each needs a reason; an entry nothing imports any more is reported as stale.
OPTIONAL_IMPORTS = {
    "greenswitch": "FreeSWITCH ESL client; Orchestrator/phone/sip_client.py "
                   "runs without it (legacy SIP path)",
    "hf_transfer": "optional faster Hugging Face downloads; "
                   "localstack_downloads.py uses it only when present",
    "sentence_transformers": "CPU reranker, installed on capable boxes by "
                             "installer/templates/blackbox-install-reranker-cpu.sh; "
                             "must stay out of the base install (test_rerank)",
    "mcp": "MCP server venv only (MCP/requirements.txt); the shared tool "
           "registry imports it lazily for the MCP format",
}

# Import name (dotted prefix) -> distribution, for names that differ from
# their distribution. Used when the package is not installed where this runs.
STATIC_MODULE_MAP = {
    "PIL": "pillow",
    "yaml": "pyyaml",
    "websocket": "websocket-client",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "docx": "python-docx",
    "serial": "pyserial",
    "webrtcvad": "webrtcvad-wheels",
    "imageio_ffmpeg": "imageio-ffmpeg",
    "huggingface_hub": "huggingface-hub",
    "python_multipart": "python-multipart",
    "multipart": "python-multipart",
    "googleapiclient": "google-api-python-client",
    "google_auth_oauthlib": "google-auth-oauthlib",
    "google.genai": "google-genai",
    "google.generativeai": "google-generativeai",
    "google.ai.generativelanguage": "google-ai-generativelanguage",
    "google.auth": "google-auth",
    "google.oauth2": "google-auth",
    "google.api_core": "google-api-core",
    "google.cloud.speech": "google-cloud-speech",
    "google.cloud.speech_v2": "google-cloud-speech",
    "google.protobuf": "protobuf",
    "google.rpc": "googleapis-common-protos",
    "dateutil": "python-dateutil",
    "jwt": "pyjwt",
    "attr": "attrs",
}


def canonical(name: str) -> str:
    """PEP 503 normalized distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


# ── scanning ────────────────────────────────────────────────────────────

def is_test_file(path: Path) -> bool:
    name = path.name
    return (name == "conftest.py" or name.startswith("test_")
            or name.endswith("_test.py")
            or any(part in ("tests", "test") for part in path.parts[:-1]))


def iter_python_files(paths):
    for base in paths:
        base = Path(base)
        if base.is_file():
            if base.suffix == ".py":
                yield base
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    yield Path(dirpath) / fn


def first_party_names(root: Path, paths) -> set[str]:
    """Module and package names this repo defines: its top-level entries, plus
    every module or package under the scanned trees. The code imports siblings
    through sys.path inserts (`from web_tools import ...`, `from config import
    ...`), so those count as first party too. Only names from Python files
    count, so a docs folder called `serial` cannot hide a real import."""
    names = {p.name for p in root.iterdir() if p.is_dir() and p.name not in SKIP_DIRS}
    names |= {p.stem for p in root.glob("*.py")}
    for path in iter_python_files(paths):
        names.add(path.stem)
        names.update(path.relative_to(root).parts[:-1])
    return names


def imports_in(source: str) -> set[str]:
    """Absolute dotted names a module imports. `from a.b import c` yields
    a.b.c (c may be a submodule, which matters for namespace packages like
    `from google import genai`); prefix matching resolves attributes."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "__future__":
                continue
            for alias in node.names:
                found.add(node.module if alias.name == "*" else f"{node.module}.{alias.name}")
    return found


def scan(paths, root: Path) -> dict[str, set[str]]:
    """Third-party dotted import name -> repo-relative files importing it."""
    stdlib = set(sys.stdlib_module_names)
    local = first_party_names(root, paths)
    out: dict[str, set[str]] = {}
    for path in iter_python_files(paths):
        if is_test_file(path.relative_to(root)):
            continue
        try:
            names = imports_in(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as e:
            raise SystemExit(f"cannot parse {path}: {e}")
        if any(n == "pytest" or n.startswith("pytest.") for n in names):
            continue  # a pytest module outside tests/ (e.g. a validation matrix)
        for name in names:
            top = name.split(".", 1)[0]
            if top in stdlib or top in local:
                continue
            out.setdefault(name, set()).add(path.relative_to(root).as_posix())
    return out


# ── import name -> distribution ─────────────────────────────────────────

def build_module_map(use_installed: bool = True) -> dict[str, frozenset[str]]:
    """Dotted module name -> distributions providing it. Installed metadata
    (every package and module path in each distribution's RECORD) wins over
    the static table, which wins over the import's own name."""
    mapping: dict[str, set[str]] = {}
    for mod, dist in STATIC_MODULE_MAP.items():
        mapping.setdefault(mod, set()).add(canonical(dist))
    if use_installed:
        from importlib import metadata
        installed: dict[str, set[str]] = {}
        for dist in metadata.distributions():
            dname = canonical(dist.metadata["Name"] or "")
            for f in dist.files or ():
                parts = Path(str(f)).parts
                if not parts or parts[0] in ("..", "bin") or parts[0].endswith(
                        (".dist-info", ".egg-info", ".data")):
                    continue
                if "__pycache__" in parts:
                    continue
                last = parts[-1]
                if last.endswith(".py") or ".cpython-" in last or last.endswith((".so", ".pyd")):
                    stem = last.split(".", 1)[0]
                    mods = list(parts[:-1]) + ([] if stem == "__init__" else [stem])
                else:
                    mods = list(parts[:-1])
                for i in range(1, len(mods) + 1):
                    installed.setdefault(".".join(mods[:i]), set()).add(dname)
        for mod, dists in installed.items():
            mapping[mod] = set(dists)  # metadata is the truth where present
    return {k: frozenset(v) for k, v in mapping.items()}


def resolve(name: str, module_map: dict[str, frozenset[str]]) -> frozenset[str]:
    """Distributions that may satisfy importing `name` (any one is enough).
    The longest mapped prefix wins. A namespace root (`google`) maps to every
    distribution under it, so a deeper prefix with a single owner is preferred
    and the ambiguous set is the answer only when nothing deeper is known."""
    parts = name.split(".")
    ambiguous = None
    for i in range(len(parts), 0, -1):
        hit = module_map.get(".".join(parts[:i]))
        if not hit:
            continue
        if len(hit) == 1 or i == len(parts):
            return hit
        ambiguous = ambiguous or hit
    if ambiguous:
        return ambiguous
    # Not mapped at all: a namespace root the static table only knows the
    # children of (`import google`), else the import's own name.
    if len(parts) == 1:
        children = {d for k, v in module_map.items() if k.startswith(name + ".") for d in v}
        if children:
            return frozenset(children)
    return frozenset({canonical(parts[0])})


# ── manifest ────────────────────────────────────────────────────────────

@dataclass
class Entry:
    name: str
    line: int
    tagged: bool


def parse_manifest(path: Path) -> dict[str, Entry]:
    entries = {}
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        req, _, comment = raw.partition("#")
        req = req.strip()
        if not req or req.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", req)
        if not m:
            raise SystemExit(f"{path}:{i}: cannot parse requirement {raw!r}")
        name = canonical(m.group(1))
        entries[name] = Entry(name, i, any(tag in comment for tag in TAGS))
    return entries


# ── check ───────────────────────────────────────────────────────────────

@dataclass
class Report:
    undeclared: dict[str, set[str]] = field(default_factory=dict)   # dist -> files
    unused: list[str] = field(default_factory=list)
    stale_optional: list[str] = field(default_factory=list)
    not_installed: dict[str, set[str]] = field(default_factory=dict)  # import -> files

    @property
    def ok(self) -> bool:
        return not (self.undeclared or self.unused or self.stale_optional
                    or self.not_installed)


def check(root, manifest, paths, module_map, optional=None,
          require_installed: bool = False) -> Report:
    root = Path(root)
    optional = OPTIONAL_IMPORTS if optional is None else optional
    declared = parse_manifest(Path(manifest))
    imported = scan(paths, root)
    report = Report()
    used: set[str] = set()
    optional_seen: set[str] = set()
    for name, files in sorted(imported.items()):
        top = name.split(".", 1)[0]
        if top in optional:
            optional_seen.add(top)
            continue
        dists = resolve(name, module_map)
        hit = dists & declared.keys()
        if hit:
            used |= hit
        else:
            key = "|".join(sorted(dists))
            report.undeclared.setdefault(key, set()).update(files)
        if require_installed and importlib.util.find_spec(top) is None:
            report.not_installed.setdefault(top, set()).update(files)
    report.unused = sorted(n for n, e in declared.items() if n not in used and not e.tagged)
    report.stale_optional = sorted(set(optional) - optional_seen)
    return report


def format_report(report: Report, root: Path) -> str:
    lines = []
    for dist, files in sorted(report.undeclared.items()):
        lines.append(f"UNDECLARED  {dist}: imported by {', '.join(sorted(files))}")
    for dist in report.unused:
        lines.append(f"UNUSED      {dist}: declared but never imported "
                     f"(remove it, or tag it {' / '.join(TAGS)} with a reason)")
    for mod in report.stale_optional:
        lines.append(f"STALE       {mod}: in OPTIONAL_IMPORTS but no longer imported")
    for mod, files in sorted(report.not_installed.items()):
        lines.append(f"MISSING     {mod}: not importable in {sys.executable} "
                     f"(imported by {', '.join(sorted(files))})")
    return "\n".join(lines) if lines else "OK: imports and manifest agree"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("paths", nargs="*", help=f"code to scan, relative to --root "
                    f"(default: {' '.join(DEFAULT_PATHS)})")
    ap.add_argument("--root", default=str(REPO_ROOT))
    ap.add_argument("--manifest", default=None,
                    help=f"default: <root>/{DEFAULT_MANIFEST}")
    ap.add_argument("--no-installed-metadata", action="store_true",
                    help="map import names with the static table only")
    ap.add_argument("--no-default-optional", action="store_true",
                    help="ignore OPTIONAL_IMPORTS (for checking other trees)")
    ap.add_argument("--require-installed", action="store_true",
                    help="also fail when an imported package is not importable here")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    manifest = Path(args.manifest) if args.manifest else root / DEFAULT_MANIFEST
    paths = [root / p for p in (args.paths or DEFAULT_PATHS)]
    report = check(root, manifest, paths,
                   module_map=build_module_map(not args.no_installed_metadata),
                   optional={} if args.no_default_optional else OPTIONAL_IMPORTS,
                   require_installed=args.require_installed)
    print(format_report(report, root))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
