"""Keeps every updater test off this box's /etc/blackbox trust pins (audit
X04-security-clients-infra-11). The update-origin and release-signer pins are
read from an empty per-test directory owned by the test user (TRUSTED_UID is
the test user), so an unpinned box is the default and a test that wants pins
writes them there. test_update_sh.py points its copy of Scripts/update.sh at
the same directory."""
from __future__ import annotations

import os

import pytest

from Orchestrator.update import verify


@pytest.fixture(autouse=True)
def trust(tmp_path_factory, monkeypatch):
    d = tmp_path_factory.mktemp("etc-blackbox")
    d.chmod(0o755)
    monkeypatch.setattr(verify, "TRUST_DIR", str(d))
    monkeypatch.setattr(verify, "TRUSTED_UID", os.getuid())
    return d
