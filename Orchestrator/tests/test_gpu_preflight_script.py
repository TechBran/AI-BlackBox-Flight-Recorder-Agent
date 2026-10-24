"""installer/templates/blackbox-gpu-preflight.sh — the blackbox-models.service
ExecStartPre gate (2026-08-11 plan Track B Tasks 11-12, audit W11).

Runs the real script under bash with a PATH that holds ONLY fake lspci /
nvidia-smi (or neither) plus the few coreutils it needs, so the host's own
nvidia-smi or lspci can never leak into a verdict. The decision table is the
one hardware.probe() is tested against (gpu_driver_cases.py): the unit and the
status payload must never disagree about whether the GPU driver is alive.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from Orchestrator.tests import gpu_driver_cases as gdc

SCRIPT = Path(__file__).resolve().parents[2] / "installer" / "templates" / "blackbox-gpu-preflight.sh"
BASH = shutil.which("bash")
# What the script itself calls, besides the two commands under test.
_TOOLS = ("grep", "head", "timeout", "sleep", "cat", "rm", "mkdir")


def _bin(tmp_path: Path, smi, lspci) -> Path:
    """A PATH dir with the coreutils the script uses and fakes for nvidia-smi
    and lspci as the case describes (ABSENT -> no file at all)."""
    d = tmp_path / "bin"
    d.mkdir()
    for tool in _TOOLS:
        (d / tool).symlink_to(shutil.which(tool))
    if lspci is not gdc.ABSENT:
        (d / "lspci").write_text(f"#!/bin/sh\ncat <<'EOF'\n{lspci}EOF\n")
    if smi is not gdc.ABSENT:
        if smi == gdc.TIMEOUT:
            body = f"exec {shutil.which('sleep')} 30\n"
        elif isinstance(smi, gdc.Fail):
            body = f"cat <<'EOF'\n{smi.message}\nEOF\nexit {smi.rc}\n"
        elif smi == gdc.EMPTY:
            body = "exit 0\n"
        else:
            # A driver that answers: -L lists the card, the query prints it.
            body = (f"case \"$1\" in -L) printf '%s' '{gdc.OK_SMI_L}' ;; "
                    f"*) printf '%s' '{smi}' ;; esac\n")
        (d / "nvidia-smi").write_text("#!/bin/sh\n" + body)
    for f in d.iterdir():
        if not f.is_symlink():
            f.chmod(0o755)
    return d


def _run(tmp_path, smi, lspci, *args, env=None):
    path = _bin(tmp_path, smi, lspci)
    e = {"PATH": str(path), "BLACKBOX_GPU_PREFLIGHT_TIMEOUT": "1",
         "BLACKBOX_GPU_PREFLIGHT_POLL": "0.1"}
    e.update(env or {})
    return subprocess.run([BASH, str(SCRIPT), *args], env=e, capture_output=True,
                          text=True, timeout=60)


@pytest.mark.parametrize("case", gdc.CASES, ids=lambda c: c.name)
def test_decision_table_matches_the_probe(tmp_path, case):
    r = _run(tmp_path, case.smi, case.lspci)
    assert r.returncode == case.preflight_exit, r.stdout + r.stderr


def test_dead_driver_failure_is_loud_and_actionable(tmp_path):
    r = _run(tmp_path, gdc.DRIVER_DEAD, gdc.LSPCI_NVIDIA)
    assert r.returncode == 1
    err = r.stderr
    assert "NVIDIA driver not responding" in err
    assert "AD107GL" in err                                  # which card
    assert "exited 9" in err and "couldn't communicate" in err  # what nvidia-smi said
    assert "mokutil --sb-state" in err and "dkms status" in err  # where to look
    assert "systemctl reset-failed blackbox-models.service" in err  # how to recover
    assert "BLACKBOX_GPU_PREFLIGHT=off" in err               # the deliberate opt-out


def test_hung_driver_is_bounded_by_the_call_timeout(tmp_path):
    r = _run(tmp_path, gdc.TIMEOUT, gdc.LSPCI_NVIDIA)
    assert r.returncode == 1
    assert "timed out after 1s" in r.stderr


def test_wait_rides_out_a_driver_that_loads_late(tmp_path):
    """The boot race: nvidia-smi fails until the module finishes loading."""
    path = _bin(tmp_path, gdc.ABSENT, gdc.LSPCI_NVIDIA)
    marker = tmp_path / "attempts"
    (path / "nvidia-smi").write_text(
        "#!/bin/sh\n"
        f"echo x >> '{marker}'\n"
        f"[ \"$(grep -c x '{marker}')\" -ge 3 ] || {{ echo 'not yet'; exit 9; }}\n"
        f"printf '%s' '{gdc.OK_SMI_L}'\n")
    (path / "nvidia-smi").chmod(0o755)
    r = subprocess.run([BASH, str(SCRIPT), "--wait", "10"],
                       env={"PATH": str(path), "BLACKBOX_GPU_PREFLIGHT_POLL": "0.1"},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "NVIDIA driver OK" in r.stdout
    assert marker.read_text().count("x") == 3


def test_without_wait_a_dead_driver_is_refused_on_the_first_try(tmp_path):
    path = _bin(tmp_path, gdc.ABSENT, gdc.LSPCI_NVIDIA)
    marker = tmp_path / "attempts"
    (path / "nvidia-smi").write_text(f"#!/bin/sh\necho x >> '{marker}'\nexit 9\n")
    (path / "nvidia-smi").chmod(0o755)
    r = subprocess.run([BASH, str(SCRIPT)], env={"PATH": str(path)},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert marker.read_text().count("x") == 1


def test_operator_opt_out_starts_a_gpu_box_cpu_only(tmp_path):
    r = _run(tmp_path, gdc.DRIVER_DEAD, gdc.LSPCI_NVIDIA,
             env={"BLACKBOX_GPU_PREFLIGHT": "off"})
    assert r.returncode == 0
    assert "BLACKBOX_GPU_PREFLIGHT=off" in r.stderr


@pytest.mark.parametrize("args", [("--wait", "soon"), ("--bogus",)])
def test_bad_arguments_exit_2(tmp_path, args):
    assert _run(tmp_path, gdc.OK_SMI, gdc.LSPCI_NVIDIA, *args).returncode == 2


def test_unit_template_runs_the_gate_and_rate_limits():
    """blackbox-models.service runs this gate before llama-swap and stops
    retrying a broken start instead of looping forever: RestartSec=15 is
    longer than systemd's default 10 s start-limit window, so before this the
    limiter could never trip (MS02 logged 3366+ restarts)."""
    unit = (SCRIPT.parent / "blackbox-models.service").read_text(encoding="utf-8")
    unit_sec, _, svc_sec = unit.partition("[Service]")
    assert "StartLimitIntervalSec=300" in unit_sec and "StartLimitBurst=3" in unit_sec
    assert ("ExecStartPre=/bin/bash BLACKBOX_ROOT_PLACEHOLDER/installer/templates/"
            "blackbox-gpu-preflight.sh --wait 60") in svc_sec
    vllm = (SCRIPT.parent / "vllm-reranker.service").read_text(encoding="utf-8")
    v_unit, _, v_svc = vllm.partition("[Service]")
    assert "StartLimitIntervalSec=300" in v_unit and "StartLimitBurst=3" in v_unit
    # GPU-only unit: no card, no start (hard gate, unlike the always-on stack).
    assert "ExecStartPre=/usr/bin/nvidia-smi -L" in v_svc


def test_script_is_executable_by_its_interpreter_line():
    assert SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\n")
    assert os.access(SCRIPT, os.R_OK)
