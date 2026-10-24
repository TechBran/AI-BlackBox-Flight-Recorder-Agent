"""installer/templates/blackbox-build-reranker-gguf.sh — the reproducible build
of the on-box reranker GGUF (audit O19-local-stack-10).

Runs the real script with fakes for git, python3 (and the venv it creates)
and cmake first on PATH, so nothing is cloned, downloaded or compiled. The
fakes log what they were asked to do; the tests pin that the build uses the
SAME llama.cpp tag the installer builds llama-server from, the HF revision in
reranker-gguf-version, refuses a convert script without the Qwen3-Reranker
fix, refuses a result without the reranker head, and lands the file
atomically with its provenance.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT_REL = Path("installer/templates/blackbox-build-reranker-gguf.sh")
OUT_NAME = "Qwen3-Reranker-8B-Q8_0.gguf"


def _pins(name):
    lines = (REPO / "installer/templates" / name).read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


FAKE_GIT = r"""#!/usr/bin/env bash
echo "git $*" >> "$FAKE_LOG"
for last; do :; done
mkdir -p "$last/requirements" "$last/gguf-py"
: > "$last/requirements/requirements-convert_hf_to_gguf.txt"
if [[ "${FAKE_CONVERT_FIXED:-1}" == 1 ]]; then
    printf '# Qwen3ForRerank: extract cls.output.weight, pooling RANK\n' > "$last/convert_hf_to_gguf.py"
else
    printf '# an old convert script\n' > "$last/convert_hf_to_gguf.py"
fi
"""

# The venv python: the download and GGUF-check snippets arrive on stdin
# (`python - args`), the convert script as a path.
FAKE_VENV_PY = r"""#!/usr/bin/env bash
echo "venv-python $*" >> "$FAKE_LOG"
if [[ "${1:-}" == "-" ]]; then
    code="$(cat)"
    if grep -q snapshot_download <<< "$code"; then
        echo "download $2 $3" >> "$FAKE_LOG"; mkdir -p "$4"; echo '{}' > "$4/config.json"; exit 0
    fi
    if grep -q GGUFReader <<< "$code"; then
        echo "gguf-check PYTHONPATH=$PYTHONPATH" >> "$FAKE_LOG"; exit "${FAKE_HEAD_RC:-0}"
    fi
    exit 0
fi
if [[ "$1" == */convert_hf_to_gguf.py ]]; then
    while [[ $# -gt 0 ]]; do [[ "$1" == --outfile ]] && out="$2"; shift; done
    echo F16 > "$out"; exit 0
fi
exit 0
"""

FAKE_PY = r"""#!/usr/bin/env bash
if [[ "${1:-}" == -m && "${2:-}" == venv ]]; then
    echo "python -m venv $3" >> "$FAKE_LOG"
    mkdir -p "$3/bin"
    cp "$FAKE_BIN/.venv-python" "$3/bin/python"
    printf '#!/usr/bin/env bash\necho "pip $*" >> "$FAKE_LOG"\n' > "$3/bin/pip"
    chmod +x "$3/bin/python" "$3/bin/pip"
    exit 0
fi
exec "$REAL_PYTHON" "$@"
"""

FAKE_CMAKE = r"""#!/usr/bin/env bash
echo "cmake $*" >> "$FAKE_LOG"
if [[ "${1:-}" == --build ]]; then
    mkdir -p "$2/bin"
    printf '#!/usr/bin/env bash\necho "quantize $*" >> "$FAKE_LOG"\n{ cat "$1"; echo Q8_0; } > "$2"\n' > "$2/bin/llama-quantize"
    chmod +x "$2/bin/llama-quantize"
fi
"""


@pytest.fixture
def sandbox(tmp_path):
    """A copy of the script + its pins under a sandbox root (so a test can
    edit the pins), a fake-tools dir, and a models dir."""
    root = tmp_path / "root"
    (root / "installer/templates").mkdir(parents=True)
    for name in ("blackbox-build-reranker-gguf.sh", "llamacpp-version", "reranker-gguf-version"):
        shutil.copy(REPO / "installer/templates" / name, root / "installer/templates" / name)
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "git").write_text(FAKE_GIT)
    (fake / ".venv-python").write_text(FAKE_VENV_PY)
    (fake / "python3").write_text(FAKE_PY)
    (fake / "python3.12").write_text(FAKE_PY)
    (fake / "cmake").write_text(FAKE_CMAKE)
    for f in fake.iterdir():
        f.chmod(0o755)
    models = tmp_path / "models"
    log = tmp_path / "fake.log"
    log.touch()

    def run(*args, **env):
        e = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", FAKE_LOG=str(log),
                 FAKE_BIN=str(fake), REAL_PYTHON=shutil.which("python3"),
                 REQUIRED_FREE_GB="0", HOME=str(tmp_path / "home"))
        e.update({k: str(v) for k, v in env.items()})
        r = subprocess.run(["bash", str(root / SCRIPT_REL), "--models-dir", str(models), *args],
                           env=e, capture_output=True, text=True, timeout=120)
        return r, log.read_text()
    return root, models, run


def test_happy_path_builds_at_the_pinned_revisions_and_lands_atomically(sandbox):
    root, models, run = sandbox
    r, log = run()
    assert r.returncode == 0, r.stdout + r.stderr
    lc_tag = _pins("llamacpp-version")[0]
    hf_repo, hf_rev, ref_sha = _pins("reranker-gguf-version")
    # the SAME llama.cpp release the installer builds llama-server from
    assert f"git clone --depth 1 --branch {lc_tag} https://github.com/ggml-org/llama.cpp" in log
    assert f"download {hf_repo} {hf_rev}" in log
    assert "requirements-convert_hf_to_gguf.txt" in log         # the tag's own pins
    assert "--target llama-quantize" in log
    assert " Q8_0" in log.split("quantize ", 2)[-1]
    assert "gguf-check PYTHONPATH=" in log and "/llama.cpp/gguf-py" in log
    out = models / OUT_NAME
    assert out.read_text() == "F16\nQ8_0\n"
    prov = json.loads((models / f"{OUT_NAME}.provenance.json").read_text())
    assert prov["llama_cpp"] == lc_tag and prov["hf_revision"] == hf_rev
    assert prov["matches_g2_reference"] is False      # fake bytes != the MS02 artifact
    assert "Run the G2 gate" in r.stdout
    assert not (models / ".reranker-gguf-build").exists()   # scratch cleaned up
    assert not list(models.glob("*.part"))


def test_an_existing_gguf_is_kept_without_force(sandbox):
    root, models, run = sandbox
    models.mkdir()
    (models / OUT_NAME).write_text("validated")
    r, log = run()
    assert r.returncode == 0 and "already exists" in r.stdout
    assert log == "" and (models / OUT_NAME).read_text() == "validated"


def test_force_rebuilds(sandbox):
    root, models, run = sandbox
    models.mkdir()
    (models / OUT_NAME).write_text("old")
    r, _log = run("--force")
    assert r.returncode == 0
    assert (models / OUT_NAME).read_text() == "F16\nQ8_0\n"


def test_a_convert_script_without_the_reranker_fix_is_refused(sandbox):
    """Too old a llama.cpp writes a GGUF that scores ~1e-28: stop before any
    download, and leave nothing behind."""
    root, models, run = sandbox
    r, log = run(FAKE_CONVERT_FIXED=0)
    assert r.returncode == 3
    assert "#16407" in r.stderr
    assert "download" not in log and "venv" not in log
    assert not (models / OUT_NAME).exists()


def test_a_result_without_the_reranker_head_is_not_installed(sandbox):
    root, models, run = sandbox
    r, _log = run(FAKE_HEAD_RC=1)
    assert r.returncode == 3
    assert "cls.output.weight" in r.stderr
    assert not (models / OUT_NAME).exists()


def test_an_unpinned_revision_is_refused(sandbox):
    root, models, run = sandbox
    pins = root / "installer/templates/reranker-gguf-version"
    pins.write_text(pins.read_text().replace(_pins("reranker-gguf-version")[1], "main"))
    r, log = run()
    assert r.returncode == 2 and "40-hex" in r.stderr
    assert log == ""


def test_not_enough_scratch_space_is_refused_up_front(sandbox):
    root, models, run = sandbox
    r, log = run(REQUIRED_FREE_GB=10 ** 9)
    assert r.returncode == 2 and "GB free" in r.stderr
    assert log == ""


def test_pins_are_real_pins():
    hf_repo, hf_rev, ref_sha = _pins("reranker-gguf-version")
    assert hf_repo == "Qwen/Qwen3-Reranker-8B"
    assert len(hf_rev) == 40 and all(c in "0123456789abcdef" for c in hf_rev)
    assert len(ref_sha) == 64 and all(c in "0123456789abcdef" for c in ref_sha)
