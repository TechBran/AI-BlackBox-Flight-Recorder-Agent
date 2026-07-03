import os
from pathlib import Path

import pytest

from Orchestrator.routes.cli_agent_routes import _resolve_provider_bin, provider_bin


def test_resolves_binary_in_default_path(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_bin = bin_dir / "fakeprov"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _resolve_provider_bin("fakeprov") == str(fake_bin)


def test_falls_back_to_bare_name_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _resolve_provider_bin("definitely-not-installed-xyz") == "definitely-not-installed-xyz"


def test_resolves_binary_in_nvm_node_versions(tmp_path, monkeypatch):
    nvm_root = tmp_path / ".nvm" / "versions" / "node"
    nvm_bin = nvm_root / "v20.19.6" / "bin"
    nvm_bin.mkdir(parents=True)
    fake_gemini = nvm_bin / "gemini"
    fake_gemini.write_text("#!/bin/sh\nexit 0\n")
    fake_gemini.chmod(0o755)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _resolve_provider_bin("gemini") == str(fake_gemini)


def test_prefers_newest_nvm_node_version_when_multiple_present(tmp_path, monkeypatch):
    base = tmp_path / ".nvm" / "versions" / "node"
    for ver in ("v18.20.0", "v20.19.6", "v22.1.0"):
        bin_dir = base / ver / "bin"
        bin_dir.mkdir(parents=True)
        target = bin_dir / "gemini"
        target.write_text("#!/bin/sh\nexit 0\n")
        target.chmod(0o755)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    resolved = _resolve_provider_bin("gemini")
    assert resolved.endswith("/v22.1.0/bin/gemini"), resolved


def test_prefers_newest_nvm_when_single_and_double_digit_majors_coexist(tmp_path, monkeypatch):
    base = tmp_path / ".nvm" / "versions" / "node"
    for ver in ("v9.0.0", "v10.0.0", "v22.1.0"):
        bin_dir = base / ver / "bin"
        bin_dir.mkdir(parents=True)
        target = bin_dir / "gemini"
        target.write_text("#!/bin/sh\nexit 0\n")
        target.chmod(0o755)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    resolved = _resolve_provider_bin("gemini")
    assert resolved.endswith("/v22.1.0/bin/gemini"), resolved


def test_skips_non_semver_nvm_dirs(tmp_path, monkeypatch):
    base = tmp_path / ".nvm" / "versions" / "node"
    for name in ("system", "iojs-v3.3.1", "v20.19.6"):
        bin_dir = base / name / "bin"
        bin_dir.mkdir(parents=True)
        target = bin_dir / "gemini"
        target.write_text("#!/bin/sh\nexit 0\n")
        target.chmod(0o755)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    resolved = _resolve_provider_bin("gemini")
    assert resolved.endswith("/v20.19.6/bin/gemini"), resolved


def test_path_wins_over_nvm_when_both_have_binary(tmp_path, monkeypatch):
    path_bin = tmp_path / "path_bin"
    path_bin.mkdir()
    path_target = path_bin / "gemini"
    path_target.write_text("#!/bin/sh\necho path\n")
    path_target.chmod(0o755)

    nvm_bin = tmp_path / ".nvm" / "versions" / "node" / "v22.1.0" / "bin"
    nvm_bin.mkdir(parents=True)
    nvm_target = nvm_bin / "gemini"
    nvm_target.write_text("#!/bin/sh\necho nvm\n")
    nvm_target.chmod(0o755)

    monkeypatch.setenv("PATH", str(path_bin))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _resolve_provider_bin("gemini") == str(path_target)


def test_empty_nvm_versions_dir_falls_back_to_bare_name(tmp_path, monkeypatch):
    (tmp_path / ".nvm" / "versions" / "node").mkdir(parents=True)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _resolve_provider_bin("gemini") == "gemini"


def test_grok_is_supported_and_resolves_from_local_bin(tmp_path, monkeypatch):
    """grok (xAI CLI) is a supported provider; its binary installs to
    ~/.local/bin/grok, which extended_path_dirs() covers — provider_bin
    must resolve it to the absolute path (not None / not the bare name)."""
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    fake_grok = local_bin / "grok"
    fake_grok.write_text("#!/bin/sh\nexit 0\n")
    fake_grok.chmod(0o755)
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("CLI_AGENT_GROK_BIN", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert provider_bin("grok") == str(fake_grok)
