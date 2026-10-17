"""install_prompts.py installs the tracked IVR prompts where Asterisk plays them.

blackbox.service cannot write Asterisk's sound dir (ProtectSystem=strict), so a
root-run step outside the service installs Orchestrator/asterisk/ivr_cache/*.wav
there. That cache is writable by the service user, so the copy must not be a
way to read root-only files: a planted symlink, FIFO or non-WAV file is skipped,
never copied as root. Runs the real script (python3 -I) on tmp dirs, so no
root and no real /usr/share are needed.
"""
import ast
import os
import stat
import subprocess
import sys
import wave
from pathlib import Path

from Orchestrator.asterisk import ivr_audio

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "Orchestrator" / "asterisk" / "configs" / "install_prompts.py"


def _wav(path: Path, frames: int = 4000):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(b"\x01\x00" * frames)


def _run(src, dest, *extra):
    return subprocess.run([sys.executable, "-I", str(SCRIPT), "--src", str(src), "--dest", str(dest), *extra],
                          capture_output=True, text=True, timeout=30)


def test_installs_valid_wavs_and_skips_anything_else(tmp_path):
    src, dest, outside = tmp_path / "cache", tmp_path / "sounds" / "ivr", tmp_path / "outside"
    src.mkdir()
    outside.mkdir()
    _wav(src / "greeting.wav")
    secret = outside / "secret.txt"
    secret.write_text("root-only-material\n" * 20)
    _wav(outside / "elsewhere.wav")
    os.symlink(secret, src / "leak.wav")                 # symlink to a non-WAV
    os.symlink(outside / "elsewhere.wav", src / "link.wav")  # symlink to a WAV
    (src / "notwav.wav").write_text("not a wav file at all, just text " * 10)
    (src / "tiny.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    _wav(src / "Bad Name.wav")
    os.mkfifo(src / "fifo.wav")

    result = _run(src, dest)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in dest.iterdir()) == ["greeting.wav"]
    installed = dest / "greeting.wav"
    assert installed.read_bytes() == (src / "greeting.wav").read_bytes()
    assert stat.S_IMODE(installed.stat().st_mode) == 0o644
    assert not installed.is_symlink()
    assert "root-only-material" not in result.stdout + result.stderr
    assert "1 installed" in result.stdout


def test_is_idempotent_and_refreshes_changed_prompts(tmp_path):
    src, dest = tmp_path / "cache", tmp_path / "dest"
    src.mkdir()
    _wav(src / "greeting.wav")
    assert _run(src, dest).returncode == 0
    before = (dest / "greeting.wav").stat().st_mtime_ns
    again = _run(src, dest)
    assert again.returncode == 0 and "0 installed" in again.stdout and "1 up to date" in again.stdout
    assert (dest / "greeting.wav").stat().st_mtime_ns == before
    _wav(src / "greeting.wav", frames=6000)
    assert "1 installed" in _run(src, dest).stdout
    assert (dest / "greeting.wav").read_bytes() == (src / "greeting.wav").read_bytes()


def test_missing_source_dir_fails_loudly(tmp_path):
    result = _run(tmp_path / "nope", tmp_path / "dest")
    assert result.returncode != 0


def _default(name):
    tree = ast.parse(SCRIPT.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
            return node.value
    raise AssertionError(f"{name} not defined in {SCRIPT}")


def test_defaults_match_the_runtime():
    dest = _default("DEFAULT_DEST")
    assert isinstance(dest, ast.Constant) and dest.value == ivr_audio.ASTERISK_SOUND_DIR
    result = subprocess.run([sys.executable, "-I", str(SCRIPT), "--print-defaults"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    src_line = [ln for ln in result.stdout.splitlines() if ln.startswith("src=")][0]
    assert Path(src_line[4:]).resolve() == Path(ivr_audio.LOCAL_CACHE_DIR).resolve()


def test_installers_run_it():
    """Scripts/install.sh (when Asterisk is installed) and a full
    configs/install.sh both install the prompts; an ARI rotation does not."""
    main = (REPO / "Scripts" / "install.sh").read_text()
    block = main.split("if [[ -x /usr/sbin/asterisk ]]; then", 1)[1].split("\nfi\n", 1)[0]
    assert "install_prompts.py" in block.split("else", 1)[0]
    configs = (REPO / "Orchestrator" / "asterisk" / "configs" / "install.sh").read_text()
    assert "install_prompts.py" in configs
