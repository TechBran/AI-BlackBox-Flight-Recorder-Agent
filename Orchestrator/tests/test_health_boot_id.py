"""/health reports a boot_id that changes with every process (audit rank 43,
A13-ui-ops-01).

The Portal and Android waited for "the first /health 200" after Install,
Rollback or Restart Service. The restart fires 2 s after the update's
complete event, so that 200 came from the OLD process. They now read boot_id
before the action and wait for a different one.
"""
import subprocess
import sys
from pathlib import Path


def test_health_reports_a_boot_id_that_is_stable_within_a_process(app_client, isolated_stores):
    # A fresh box: an empty manifest and no gold master yet. /health must still
    # answer (status "GM FAIL"), or a restart poll could never see the box up.
    (isolated_stores.root / "Manifest" / "manifest.json").write_text("{}")
    a = app_client.get("/health")
    assert a.status_code == 200, a.text
    b = app_client.get("/health").json()
    assert isinstance(a.json().get("boot_id"), str) and a.json()["boot_id"]
    assert a.json()["boot_id"] == b["boot_id"]


def test_boot_id_changes_with_the_process():
    from Orchestrator.utils.boot_id import BOOT_ID
    other = subprocess.run(
        [sys.executable, "-c", "from Orchestrator.utils.boot_id import BOOT_ID; print(BOOT_ID)"],
        cwd=str(Path(__file__).resolve().parents[2]), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert other and other != BOOT_ID


def test_boot_id_without_proc_is_still_unique_per_process(monkeypatch):
    from Orchestrator.utils import boot_id
    monkeypatch.setattr(boot_id, "_KERNEL_BOOT_ID_FILE", "/nonexistent/boot_id")
    first, second = boot_id._compute(), boot_id._compute()
    assert first and second and first != second
