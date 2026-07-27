"""Runs the shell-level update-helper tests as part of the default gate.

The install.sh templating and the helper's root-resolution ladder are bash,
so their assertions live in scripts/tests/ alongside the existing
test_install_asterisk_block.sh. Nothing invoked those automatically, and an
un-run test is exactly how the stale-helper class rotted back in
(customer, 2026-07-27) — so `pytest Orchestrator/tests/` runs them too.

Both scripts sandbox themselves into mktemp dirs and never touch the real
/usr/local/sbin, /etc, or apt.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SHELL_TESTS = REPO_ROOT / "scripts" / "tests"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize("script", [
    "test_install_update_helpers.sh",         # install.sh templates the real root
    "test_apt_helper_root_resolution.sh",     # env -> pointer -> templated default
    "test_install_perimeter_invariants.sh",   # /etc/blackbox, restart guard, ordering
])
def test_shell_suite(script):
    path = SHELL_TESTS / script
    assert path.is_file(), f"missing shell test {path}"
    result = subprocess.run(["bash", str(path)], capture_output=True,
                            text=True, timeout=120, cwd=str(REPO_ROOT))
    assert result.returncode == 0, (
        f"{script} failed (rc={result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}")
    assert "ALL TESTS PASSED" in result.stdout
