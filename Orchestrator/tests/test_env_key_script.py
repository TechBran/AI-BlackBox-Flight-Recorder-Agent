"""Scripts/env_key.py: install-time secrets are generated into .env, never committed.

Audit W05: the ARI password was a literal in two tracked shell scripts and
TELEPHONY_SECRET_KEY was left empty, so gateway credentials were encrypted
with a constant key published in this repo. The installers now generate both
into .env with this helper. It must:

  * write a strong random value into an empty or missing key, in place
    (owner and 0600 mode kept: the installers run it as root);
  * never overwrite a value that is already set unless --force;
  * collapse duplicate assignments (the last one would otherwise win);
  * never print the secret it generates (stdout reaches install logs).
"""
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

SCRIPT = Path(__file__).resolve().parents[2] / "Scripts" / "env_key.py"


def run(*args, check=True):
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *args],
                          capture_output=True, text=True, check=check)


@pytest.fixture
def env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# comment\nOPENAI_API_KEY=keep-me\nTELEPHONY_SECRET_KEY=\nASTERISK_ARI_USER=blackbox\n")
    os.chmod(p, 0o600)
    return p


def _value(path, key):
    return run("get", str(path), key).stdout.strip()


def test_generate_fernet_fills_empty_key_in_place(env_file):
    inode = env_file.stat().st_ino
    res = run("generate", "--kind", "fernet", str(env_file), "TELEPHONY_SECRET_KEY")
    key = _value(env_file, "TELEPHONY_SECRET_KEY")
    Fernet(key.encode())  # a valid Fernet key
    assert key not in res.stdout and key not in res.stderr
    text = env_file.read_text()
    assert "OPENAI_API_KEY=keep-me\n" in text and text.startswith("# comment\n")
    assert text.count("TELEPHONY_SECRET_KEY=") == 1
    assert env_file.stat().st_ino == inode
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_generate_token_appends_missing_key(env_file):
    run("generate", str(env_file), "ASTERISK_ARI_PASSWORD")
    value = _value(env_file, "ASTERISK_ARI_PASSWORD")
    assert len(value) >= 32
    assert env_file.read_text().endswith(f"ASTERISK_ARI_PASSWORD={value}\n")


def test_existing_value_is_kept_unless_forced(env_file):
    run("generate", str(env_file), "ASTERISK_ARI_PASSWORD")
    first = _value(env_file, "ASTERISK_ARI_PASSWORD")
    res = run("generate", str(env_file), "ASTERISK_ARI_PASSWORD")
    assert _value(env_file, "ASTERISK_ARI_PASSWORD") == first
    assert "already set" in res.stdout
    run("generate", "--force", str(env_file), "ASTERISK_ARI_PASSWORD")
    assert _value(env_file, "ASTERISK_ARI_PASSWORD") != first


def test_duplicate_assignments_collapse(tmp_path):
    p = tmp_path / ".env"
    p.write_text("TELEPHONY_SECRET_KEY=\nX=1\nTELEPHONY_SECRET_KEY=\n")
    run("generate", "--kind", "fernet", str(p), "TELEPHONY_SECRET_KEY")
    assert p.read_text().count("TELEPHONY_SECRET_KEY=") == 1


def test_get_strips_quotes_and_missing_is_empty(tmp_path):
    p = tmp_path / ".env"
    p.write_text('A="quoted value"\nB=\'single\'\n')
    assert _value(p, "A") == "quoted value"
    assert _value(p, "B") == "single"
    assert _value(p, "MISSING") == ""


def test_missing_env_file_fails_loudly(tmp_path):
    res = run("generate", str(tmp_path / "nope.env"), "K", check=False)
    assert res.returncode != 0


def test_rejects_bad_key_names(env_file):
    res = run("generate", str(env_file), "bad-name", check=False)
    assert res.returncode != 0
