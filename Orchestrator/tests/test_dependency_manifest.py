"""The Python manifest, its hashed lock, and the import-vs-manifest checker.

Regression cover for audit rank 35 (X01-deps-python-02, -05, -06 and
O06-frontier-agent-17):
  - the manifest declared duckduckgo_search while web_tools imports ddgs, so a
    fresh box had no keyless web-search floor and every general search lost its
    DuckDuckGo fallback. The import sat behind `except ImportError`, so nothing
    failed loudly;
  - packages the code imports (websocket-client, PyYAML, huggingface_hub) were
    present only as someone else's transitive dependency, and packages nothing
    imports (mss, miniaudio, html2text, pytz) were installed on every box;
  - floors had no caps, so a fresh box resolved untested majors, and the
    requirements.lock.txt nothing installed was a stale `pip freeze`;
  - satisfied floors never moved, so existing boxes kept vulnerable transitive
    versions (aiohttp 3.13.2, urllib3 2.5.0, protobuf 5.29.5, ...).

The checker tests run on synthetic trees with the checker's static module map
only, so they do not depend on what this venv has installed. The repo tests
read the committed requirements.in, requirements.lock.txt and requirements.txt.
"""
import importlib.util
import re
import sys
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
CHECKER_PATH = ROOT / "scripts" / "check_imports_vs_manifest.py"
REQ_IN = ROOT / "requirements.in"
LOCK = ROOT / "requirements.lock.txt"
REQ_TXT = ROOT / "requirements.txt"


@pytest.fixture(scope="module")
def checker():
    assert CHECKER_PATH.is_file(), f"import-vs-manifest checker missing: {CHECKER_PATH}"
    spec = importlib.util.spec_from_file_location("check_imports_vs_manifest", CHECKER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses resolve their module by name
    spec.loader.exec_module(mod)
    return mod


def _tree(tmp_path, files, manifest):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    (tmp_path / "requirements.in").write_text(manifest)
    return tmp_path


def _check(checker, root, optional=None, paths=("pkg",)):
    return checker.check(
        root, root / "requirements.in", [root / p for p in paths],
        module_map=checker.build_module_map(use_installed=False),
        optional=optional or {},
    )


# ── the checker, on synthetic trees ─────────────────────────────────────

def test_guarded_import_of_undeclared_package_is_reported(checker, tmp_path):
    """The ddgs regression: an import behind `except ImportError` is still a
    dependency. The checker must name it AND name the declared package nothing
    imports, which is exactly how the rename went unnoticed."""
    root = _tree(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/web_tools.py": (
            "def search(q):\n"
            "    try:\n"
            "        from ddgs import DDGS\n"
            "    except ImportError:\n"
            "        return 'ddgs library not installed'\n"
            "    return DDGS().text(q, max_results=5)\n"),
    }, "duckduckgo_search>=8.1.1   # web search tool\n")
    report = _check(checker, root)
    assert set(report.undeclared) == {"ddgs"}
    assert report.unused == ["duckduckgo-search"]
    assert not report.ok


def test_import_names_resolve_to_distribution_names(checker, tmp_path):
    """Import names that differ from their distribution (yaml -> PyYAML,
    websocket -> websocket-client, the google.* namespace) resolve, so a
    correct manifest reports nothing."""
    root = _tree(tmp_path, {
        "pkg/a.py": (
            "import yaml\n"
            "import websocket\n"
            "from PIL import Image\n"
            "from bs4 import BeautifulSoup\n"
            "from dotenv import load_dotenv\n"
            "import google\n"
            "from google import genai\n"
            "import google.auth\n"
            "from google.oauth2 import service_account\n"
            "from google.cloud.speech_v2 import SpeechClient\n"
            "from googleapiclient.discovery import build\n"
            "import huggingface_hub\n"
            "import webrtcvad\n"),
    }, (
        "PyYAML>=6,<7\n"
        "websocket-client>=1.9,<2\n"
        "pillow>=12.3,<13\n"
        "beautifulsoup4>=4.14,<5\n"
        "python-dotenv>=1.2.2,<2\n"
        "google-genai>=1.64,<2\n"
        "google-auth>=2.48,<3\n"
        "google-cloud-speech>=2.27,<3\n"
        "google-api-python-client>=2.187,<3\n"
        "huggingface_hub>=1.21,<2\n"
        "webrtcvad-wheels>=2.0.14,<3\n"))
    report = _check(checker, root)
    assert report.undeclared == {}
    assert report.unused == []
    assert report.ok


def test_namespace_import_is_not_satisfied_by_a_sibling_distribution(checker, tmp_path):
    """google.auth is google-auth, not 'anything under google'. Declaring only
    google-genai must not hide an undeclared google-auth."""
    root = _tree(tmp_path, {"pkg/a.py": "from google import genai\nimport google.auth\n"},
                 "google-genai>=1.64,<2\n")
    report = _check(checker, root)
    assert set(report.undeclared) == {"google-auth"}


def test_stdlib_first_party_relative_and_test_code_are_ignored(checker, tmp_path):
    root = _tree(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/a.py": (
            "import os, json, tomllib\n"
            "from . import b\n"
            "from .b import thing\n"
            "import pkg.b\n"
            "from b import thing\n"       # sibling import via sys.path insert
            "from __future__ import annotations\n"),
        "pkg/b.py": "thing = 1\n",
        "pkg/tests/test_a.py": "import pytest\nimport not_a_real_dist\n",
        "pkg/test_b.py": "import also_not_real\n",
        "pkg/conftest.py": "import pytest\n",
        "pkg/validation/matrix.py": "import pytest\nimport yet_another_unreal\n",
    }, "")
    report = _check(checker, root)
    assert report.undeclared == {}
    assert report.ok


def test_tagged_entries_may_be_unimported_untagged_may_not(checker, tmp_path):
    """[transitive] (a dependency's dependency declared for its floor or cap)
    and [runtime] (loaded by name, not by an import) are legitimate reasons to
    declare an unimported package. Anything else is dead weight on every box."""
    root = _tree(tmp_path, {"pkg/a.py": "import os\n"}, (
        "urllib3>=2.8.0,<3   # [transitive] requests' transport; CVE floor\n"
        "lxml>=6.1.3,<7      # [runtime] bs4 parser backend, selected by name\n"
        "mss>=10.1.0         # screen capture (Computer Use feature)\n"))
    report = _check(checker, root)
    assert report.unused == ["mss"]


def test_optional_allowlist_and_stale_entries(checker, tmp_path):
    """An optional import (installer-added or another venv's package) is
    allowed only by name with a reason, and an allowlist entry nothing imports
    any more is reported so the list cannot rot."""
    root = _tree(tmp_path, {
        "pkg/a.py": "try:\n    import greenswitch\nexcept ImportError:\n    greenswitch = None\n",
    }, "")
    assert _check(checker, root, optional={"greenswitch": "ESL client, optional"}).ok
    report = _check(checker, root, optional={"greenswitch": "optional",
                                             "torch": "installer-added"})
    assert report.stale_optional == ["torch"]
    assert not report.ok


def test_unknown_distribution_falls_back_to_the_import_name(checker, tmp_path):
    """An import the static map does not know is matched by its own name, so a
    plain `import requests` needs `requests` declared and nothing else."""
    root = _tree(tmp_path, {"pkg/a.py": "import requests\nimport frobnicate\n"},
                 "requests>=2.33,<3\n")
    report = _check(checker, root)
    assert set(report.undeclared) == {"frobnicate"}


def test_cli_exit_codes(checker, tmp_path, capsys):
    root = _tree(tmp_path, {"pkg/a.py": "import yaml\n"}, "PyYAML>=6,<7\n")
    argv = ["--root", str(root), "--manifest", str(root / "requirements.in"),
            "--no-installed-metadata", "--no-default-optional", "pkg"]
    assert checker.main(argv) == 0
    (root / "pkg" / "b.py").write_text("import ddgs\n")
    assert checker.main(argv) == 1
    out = capsys.readouterr().out
    assert "ddgs" in out and "pkg/b.py" in out


# ── the repo ────────────────────────────────────────────────────────────

def _manifest():
    reqs = {}
    for raw in REQ_IN.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            req = Requirement(line)
            reqs[canonicalize_name(req.name)] = (req, raw)
    return reqs


def _lock_pins(path):
    """name -> (version, [hashes]) for every requirement block of a pip
    requirements file (continuation lines joined)."""
    text = path.read_text(encoding="utf-8").replace("\\\n", " ")
    pins = {}
    for line in text.splitlines():
        if not line or line[0] in " #-":
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?==([^\s;]+)", line)
        assert m, f"unpinned line in {path.name}: {line[:80]}"
        pins[canonicalize_name(m.group(1))] = (
            Version(m.group(3)), re.findall(r"--hash=sha256:[0-9a-f]{64}", line))
    return pins


def test_repo_runtime_code_matches_requirements_in(checker):
    """Every third-party import in the code the Orchestrator venv runs is
    declared in requirements.in, and every entry there is imported or tagged."""
    assert REQ_IN.is_file(), "requirements.in (the human-edited manifest) is missing"
    report = checker.check(
        ROOT, REQ_IN, [ROOT / p for p in checker.DEFAULT_PATHS],
        module_map=checker.build_module_map(use_installed=True),
        optional=checker.OPTIONAL_IMPORTS,
    )
    assert report.ok, checker.format_report(report, ROOT)


def test_requirements_in_declares_ddgs_not_duckduckgo_search():
    reqs = _manifest()
    assert "duckduckgo-search" not in reqs
    req, _ = reqs["ddgs"]
    assert req.specifier.contains("9.16") and not req.specifier.contains("10.0"), \
        f"ddgs must be >=9.16,<10, got {req.specifier}"


@pytest.mark.parametrize("dist", ["websocket-client", "pyyaml", "huggingface-hub"])
def test_requirements_in_declares_directly_imported_packages(dist):
    assert dist in _manifest(), f"{dist} is imported directly but not declared"


@pytest.mark.parametrize("dist", ["mss", "miniaudio", "html2text", "pytz"])
def test_requirements_in_drops_packages_nothing_imports(dist):
    assert dist not in _manifest()


# Uncapped on purpose, each for a reason stated next to the entry:
# cryptography ships a new major several times a year and fixes land only on
# the newest one; certifi is calendar-versioned CA data with no API.
UNCAPPED_BY_DESIGN = {"cryptography", "certifi"}


def test_every_direct_dependency_is_capped():
    """'>=' alone let a fresh box install anthropic 1.x, openai 3.x,
    apscheduler 4.x (no AsyncIOScheduler: the service would not start), ..."""
    uncapped = {name for name, (req, _) in _manifest().items()
                if not any(s.operator in ("<", "==", "~=") for s in req.specifier)}
    assert uncapped == UNCAPPED_BY_DESIGN


# First release with the fix for every advisory pip-audit reported against
# the venv this lock replaces (requirements.in names the CVEs).
SECURITY_FLOORS = {
    "aiohttp": "3.14.3", "urllib3": "2.8.0", "protobuf": "5.29.6",
    "pillow": "13.0.0", "pyasn1": "0.6.4", "cryptography": "50.0.0",
    "requests": "2.33.0", "python-multipart": "0.0.31", "python-dotenv": "1.2.2",
    "lxml": "6.1.0", "idna": "3.15", "h11": "0.16.0", "anyio": "4.14.2",
    "httplib2": "0.32.0", "multidict": "6.9.1", "oauthlib": "4.0.0",
    "soupsieve": "2.9.0", "certifi": "2026.10.19",
}


@pytest.mark.parametrize("dist,minimum", sorted(SECURITY_FLOORS.items()))
def test_security_floor_is_declared_and_locked(dist, minimum):
    """A raised floor is what moves an EXISTING box off a vulnerable version
    (pip leaves a satisfied requirement alone), and the lock must honour it."""
    req, _ = _manifest()[dist]
    floors = [Version(s.version) for s in req.specifier
              if s.operator in (">=", ">", "==", "~=")]
    assert floors and max(floors) >= Version(minimum), \
        f"{dist} {req.specifier} still admits versions below {minimum}"
    locked, _ = _lock_pins(LOCK)[dist]
    assert locked >= Version(minimum), f"lock pins {dist}=={locked}, below {minimum}"


def test_protobuf_stays_below_6():
    """google-generativeai's google-ai-generativelanguage needs protobuf<6."""
    locked, _ = _lock_pins(LOCK)["protobuf"]
    assert locked < Version("6")


def test_lock_is_fully_pinned_and_hashed():
    assert LOCK.is_file()
    pins = _lock_pins(LOCK)
    assert len(pins) > 50, "lock looks truncated"
    unhashed = sorted(n for n, (_, hashes) in pins.items() if not hashes)
    assert not unhashed, f"lock entries without --hash: {unhashed}"
    for banned in ("pip", "setuptools", "pytest", "duckduckgo-search", "torch",
                   "sentence-transformers"):
        assert banned not in pins, f"{banned} must not be in the runtime lock"
    head = LOCK.read_text(encoding="utf-8").splitlines()[:3]
    assert any("scripts/compile_python_lock.sh" in line for line in head), \
        "lock header must name the script that regenerates it"


def test_lock_covers_every_requirements_in_entry():
    pins = _lock_pins(LOCK)
    for name, (req, _) in _manifest().items():
        assert name in pins, f"{name} declared in requirements.in but not locked"
        assert req.specifier.contains(pins[name][0], prereleases=True), \
            f"lock pins {name}=={pins[name][0]}, outside {req.specifier}"


@pytest.mark.parametrize("dist,version", [
    ("fastapi", "0.143.5"), ("starlette", "1.9.0"),
    ("uvicorn", "0.54.0"), ("pydantic", "2.14.1"),
])
def test_web_stack_held_at_validated_versions(dist, version):
    """The web stack moved as one unit (audit rank 70: fastapi 0.118 ->
    0.143.5, starlette 0.48 -> 1.9.0, uvicorn 0.30 -> 0.54.0, pydantic 2.9 ->
    2.14.1), validated by the full suite on a venv built from the lock. The
    lock must not move them again without that."""
    assert _lock_pins(LOCK)[dist][0] == Version(version)


def test_requirements_txt_is_the_hash_free_rendering_of_the_lock():
    """requirements.txt carries the lock's pins without hashes, so
    `pip install -r requirements.txt -r requirements.dev.txt` works and an
    updater that predates the lock still installs exactly the locked set."""
    txt = REQ_TXT.read_text(encoding="utf-8")
    assert "--hash" not in txt
    lock = {n: v for n, (v, _) in _lock_pins(LOCK).items()}
    pinned = {n: v for n, (v, _) in _lock_pins(REQ_TXT).items()}
    assert pinned == lock


WORKFLOW = ROOT / ".github" / "workflows" / "supply-chain.yml"


def test_supply_chain_workflow_pins_actions_and_tools():
    """The gate itself must not pull moving code: actions by commit SHA, the
    tools from a hashed lock, and the same uv the lock script demands."""
    wf = WORKFLOW.read_text(encoding="utf-8")
    uses = re.findall(r"uses:\s*(\S+)", wf)
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u) for u in uses), uses
    uv_version = re.search(r'^UV_VERSION="([^"]+)"',
                           (ROOT / "scripts" / "compile_python_lock.sh").read_text(),
                           re.M).group(1)
    tools = _lock_pins(ROOT / ".github" / "supply-chain-tools.lock.txt")
    assert tools["uv"][0] == Version(uv_version)
    assert all(hashes for _, hashes in tools.values())
    assert "--require-hashes -r .github/supply-chain-tools.lock.txt" in wf
    assert "scripts/compile_python_lock.sh --check" in wf
    assert "scripts/check_imports_vs_manifest.py --require-installed" in wf
    assert "--dry-run --quiet\n          --disable-pip-version-check -c requirements.txt sentence-transformers" in wf


def test_pip_audit_exceptions_are_documented_in_requirements_in():
    """Every advisory the gate ignores is named, with its reason, next to the
    package it belongs to; an undocumented exception is a silenced alarm. (It
    ignores none since the rank-70 web-stack upgrade closed the six Starlette
    0.48 ones: test_web_stack_upgrade.py.)"""
    steps = "\n".join(line for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
                      if not line.lstrip().startswith("#"))
    ignored = re.findall(r"--ignore-vuln\s+(\S+)", steps)
    documented = REQ_IN.read_text(encoding="utf-8")
    for vuln in ignored:
        short = "-" + vuln.split("-", 2)[2]  # CVE-2026-48817 is listed as -48817
        assert vuln in documented or short in documented, f"{vuln} ignored but not documented"


def test_orchestrator_requirements_txt_is_gone():
    """A second, partial manifest nothing installs from only drifts."""
    assert not (ROOT / "Orchestrator" / "requirements.txt").exists()


# ── the lock sync gate checks hashes, not only versions (U14 review) ─────
# compile_python_lock.sh --check seeded uv with the committed HASHED lock, and
# uv keeps a seeded pin's --hash lines as they are. So a lock with an extra
# hash (pip would then accept a file nobody locked), a swapped one or a removed
# one still came out "in sync". The fake uv below reproduces that behaviour
# (observed on uv 0.13.1); the gate must catch the edit anyway.

FAKE_UV = '''#!{python}
import os, sys

args = sys.argv[1:]
if args == ["--version"]:
    print("uv 0.13.1")
    sys.exit(0)
out = args[args.index("-o") + 1]
seed = open(out).read() if os.path.exists(out) else ""
open(os.environ["FAKE_UV_SEED_LOG"], "w").write(seed)


def blocks(text):
    head, found, cur = [], {{}}, None
    for line in text.splitlines(keepends=True):
        if line[:1].isalnum():
            cur = line.split("==")[0]
            found[cur] = [line]
        elif cur is None:
            head.append(line)
        else:
            found[cur].append(line)
    return head, found


head, index = blocks(open(os.environ["FAKE_UV_INDEX"]).read())
_, seeded = blocks(seed)
with open(out, "w") as f:
    f.writelines(head)
    for name, block in index.items():
        kept = seeded.get(name)
        if kept and kept[0].split()[0] == block[0].split()[0] and any(
                "--hash=" in line for line in kept):
            block = kept  # uv carries a seeded pin's hashes through
        f.writelines(block)
'''

INDEX_LOCK = (
    "# This file was autogenerated by uv via the following command:\n"
    "#    scripts/compile_python_lock.sh\n"
    "aiohttp==3.14.5 \\\n"
    "    --hash=sha256:" + "1" * 64 + " \\\n"
    "    --hash=sha256:" + "2" * 64 + "\n"
    "    # via -r requirements.in\n"
)


@pytest.fixture
def lock_tree(tmp_path):
    import shutil
    import stat
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "compile_python_lock.sh", tmp_path / "scripts")
    (tmp_path / "requirements.in").write_text("aiohttp>=3.14.3,<4\n")
    uv = tmp_path / "fake-uv"
    uv.write_text(FAKE_UV.format(python=sys.executable))
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "index.lock").write_text(INDEX_LOCK)
    env = {"PATH": "/usr/bin:/bin", "UV": str(uv),
           "FAKE_UV_INDEX": str(tmp_path / "index.lock"),
           "FAKE_UV_SEED_LOG": str(tmp_path / "seed.log")}

    def run(*args):
        import subprocess
        return subprocess.run(["bash", str(tmp_path / "scripts" / "compile_python_lock.sh"), *args],
                              env=env, capture_output=True, text=True, timeout=60)

    first = run()
    assert first.returncode == 0, first.stderr
    assert (tmp_path / "requirements.lock.txt").read_text() == INDEX_LOCK
    return tmp_path, run


def test_lock_check_passes_an_untouched_lock(lock_tree):
    root, run = lock_tree
    res = run("--check")
    assert res.returncode == 0, res.stderr
    assert "OK" in res.stdout


@pytest.mark.parametrize("edit", [
    lambda t: t.replace("1" * 64, "d" * 64),                          # swapped
    lambda t: t.replace("2" * 64 + "\n", "2" * 64 + " \\\n    --hash=sha256:" + "e" * 64 + "\n"),  # added
    lambda t: t.replace(" \\\n    --hash=sha256:" + "2" * 64, ""),     # removed
], ids=["swapped", "added", "removed"])
def test_lock_check_catches_an_edited_hash(lock_tree, edit):
    root, run = lock_tree
    lock = root / "requirements.lock.txt"
    edited = edit(lock.read_text())
    assert edited != INDEX_LOCK
    lock.write_text(edited)
    res = run("--check")
    assert res.returncode == 1, res.stdout
    assert "requirements.lock.txt is out of date" in res.stderr


def test_lock_compile_seeds_uv_with_versions_only(lock_tree):
    """Every hash comes from the index on every compile; the committed lock
    only says which versions to prefer."""
    root, run = lock_tree
    assert run("--check").returncode == 0
    seed = (root / "seed.log").read_text()
    assert "aiohttp==3.14.5" in seed
    assert "--hash" not in seed


# ── installer-added packages must fit the lock (U14 review) ─────────────
# installer/templates/blackbox-install-reranker-cpu.sh installs the in-process
# CPU reranker on MID-tier boxes under the lock's pins (-c requirements.txt).
# transformers 5.20 needs huggingface-hub>=1.31 and tokenizers>=0.23.1,<0.24,
# and sentence-transformers 6.1 needs huggingface-hub<2. A lock outside that
# makes the reranker uninstallable there, or (before -c) let it move hub up and
# the next lock install move hub back under transformers.
CPU_RERANKER_NEEDS = {
    "huggingface-hub": ">=1.31.0,<2",
    "tokenizers": ">=0.23.1,<0.24",
}


@pytest.mark.parametrize("dist,spec", sorted(CPU_RERANKER_NEEDS.items()))
def test_lock_admits_the_cpu_reranker_stack(dist, spec):
    from packaging.specifiers import SpecifierSet
    locked, _ = _lock_pins(LOCK)[dist]
    assert SpecifierSet(spec).contains(locked), \
        f"lock pins {dist}=={locked}; the CPU reranker stack needs {spec}"
