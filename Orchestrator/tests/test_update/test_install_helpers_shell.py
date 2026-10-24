"""Runs the shell-level update-helper tests as part of the default gate.

The install.sh templating and the helper's root-resolution ladder are bash,
so their assertions live in scripts/tests/ alongside the existing
test_install_asterisk_block.sh. Nothing invoked those automatically, and an
un-run test is exactly how the stale-helper class rotted back in
(customer, 2026-07-27) — so `pytest Orchestrator/tests/` runs them too.

Every scripts/tests/test_*.sh is collected by name (audit
O21-python-tests-14: a hand-kept list had fallen to 6 of 11), the same set
scripts/tests/run_all.sh runs in the CI shell job. Each script sandboxes
itself into mktemp dirs and never touches the real /usr/local/sbin, /etc, or
apt.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SHELL_TESTS = REPO_ROOT / "scripts" / "tests"


SUITES = sorted(p.name for p in SHELL_TESTS.glob("test_*.sh"))


def test_shell_suites_are_found():
    assert len(SUITES) >= 11, SUITES


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize("script", SUITES)
def test_shell_suite(script):
    _run_shell_suite(script)


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("shellcheck") is None,
                    reason="bash or shellcheck not available")
def test_shellcheck_installers():
    _run_shell_suite("test_shellcheck_installers.sh")


def _run_shell_suite(script):
    path = SHELL_TESTS / script
    assert path.is_file(), f"missing shell test {path}"
    result = subprocess.run(["bash", str(path)], capture_output=True,
                            text=True, timeout=120, cwd=str(REPO_ROOT))
    if result.returncode == 0 and result.stdout.lstrip().startswith("SKIP:"):
        # A suite that cannot run here (no shellcheck, no systemd-analyze) says
        # so and exits 0; CI's ubuntu runners have both, so the gate runs there.
        pytest.skip(result.stdout.strip())
    assert result.returncode == 0, (
        f"{script} failed (rc={result.returncode}):\n"
        f"{result.stdout}\n{result.stderr}")
    # A suite that exits 0 before its checks prints neither banner.
    assert ("ALL TESTS PASSED" in result.stdout
            or f"PASS: {script}" in result.stdout), result.stdout
