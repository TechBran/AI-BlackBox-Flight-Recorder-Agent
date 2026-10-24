"""The qwen-tts member's venv is pinned (audit O19-local-stack-11).

The installer runs `pip install --upgrade -r requirements.txt` into the
qwen-tts venv. With torch, numpy, fastapi, uvicorn and soundfile unpinned, a
fresh box pulled whatever was current — a torch whose bundled CUDA runtime may
not match the box's driver, a numpy major with a different ABI — and the
mismatch surfaced only at the first variant load inside llama-swap, after the
installer had reported success. The direct requirements are now exact, and
requirements.lock.txt (the set running on MS02, read from its venv) constrains
everything they pull in.
"""
import re
from pathlib import Path

SERVER = Path(__file__).resolve().parents[2] / "LocalModels" / "qwen_tts_server"
_PIN = re.compile(r"^(?P<name>[A-Za-z0-9_.\-]+)(\[[a-z,]+\])?==(?P<ver>[0-9][0-9A-Za-z.+\-]*)$")
_GIT = re.compile(r"^git\+https://\S+\.git@[0-9a-f]{40}$")


def _reqs(name):
    out = []
    for line in (SERVER / name).read_text(encoding="utf-8").splitlines():
        line = line.split(" #")[0].strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _norm(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def test_every_direct_requirement_is_an_exact_pin():
    for line in _reqs("requirements.txt"):
        assert _PIN.match(line) or _GIT.match(line), f"unpinned requirement: {line!r}"


def test_the_ones_the_audit_named_are_pinned():
    pinned = {_norm(m.group("name")): m.group("ver")
              for m in map(_PIN.match, _reqs("requirements.txt")) if m}
    for name in ("torch", "numpy", "fastapi", "uvicorn", "soundfile",
                 "python-multipart", "transformers", "accelerate"):
        assert name in pinned, name
    assert int(pinned["numpy"].split(".")[0]) < 3


def test_the_lock_is_exact_and_agrees_with_the_direct_pins():
    lock = {}
    for line in _reqs("requirements.lock.txt"):
        m = _PIN.match(line)
        assert m, f"not an exact pin in the lock: {line!r}"
        lock[_norm(m.group("name"))] = m.group("ver")
    for line in _reqs("requirements.txt"):
        m = _PIN.match(line)
        if m:
            assert lock.get(_norm(m.group("name"))) == m.group("ver"), line
    # what the fork and torch pull in is constrained too
    for name in ("torchaudio", "triton", "librosa", "onnxruntime", "huggingface-hub",
                 "tokenizers", "safetensors", "nvidia-cudnn-cu13"):
        assert name in lock, name
    # never the fork itself (it is a git pin in requirements.txt) nor pip's own tools
    assert not {"qwen-tts", "pip", "setuptools"} & set(lock)


def test_the_installer_installs_under_the_lock_and_checks_cuda():
    installer = (SERVER.parents[1] / "installer" / "templates" /
                 "blackbox-install-localstack.sh").read_text(encoding="utf-8")
    assert '-r "$QWEN_REQ" -c "$QWEN_LOCK"' in installer
    assert "torch.cuda.is_available()" in installer
