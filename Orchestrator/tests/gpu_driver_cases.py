"""The GPU driver-state decision table, shared by the two implementations of it.

Orchestrator/hardware.py (probe(): driver_ok / gpu_reason) and
installer/templates/blackbox-gpu-preflight.sh (the blackbox-models.service
ExecStartPre gate) must agree on every row: the Aug 7-11 2026 outage ran four
days because the card stayed on the PCI bus while the driver was dead, and
both the probe and the unit treated that as "fine". test_hardware.py and
test_gpu_preflight_script.py each run every case below.

A case gives what nvidia-smi does on the box and what lspci prints:
  nvidia-smi: OK_SMI (driver answers), ABSENT (binary not installed),
              EMPTY (exits 0, lists no GPU), TIMEOUT, or Fail(rc, message).
  lspci:      a listing, or ABSENT (pciutils not installed).
and the verdict both sides must reach:
  gpu / source / driver_ok   the probe() fields
  preflight_exit             0 = start the stack, 1 = refuse (driver dead)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fail:
    rc: int
    message: str


ABSENT = None
EMPTY = ""
TIMEOUT = "<timeout>"

# What the two commands print when the driver answers. The probe queries
# name + VRAM; the preflight lists GPUs with -L.
OK_SMI = "NVIDIA RTX 2000 Ada Generation, 16380\n"
OK_SMI_L = "GPU 0: NVIDIA RTX 2000 Ada Generation (UUID: GPU-00000000-0000-0000-0000-000000000000)\n"

# nvidia-smi's real message with the module missing (Secure Boot rejected it).
DRIVER_DEAD = Fail(9, "NVIDIA-SMI has failed because it couldn't communicate with "
                      "the NVIDIA driver. Make sure that the latest NVIDIA driver "
                      "is installed and running.")

LSPCI_NVIDIA = (
    "00:02.0 Host bridge: Intel Corporation Device 4660\n"
    "01:00.0 VGA compatible controller: NVIDIA Corporation AD107GL "
    "[RTX 2000 Ada Generation] (rev a1)\n"
)
LSPCI_AMD_ONLY = (
    "07:00.0 VGA compatible controller: Advanced Micro Devices, Inc. "
    "[AMD/ATI] Raphael (rev c7)\n"
)


@dataclass(frozen=True)
class Case:
    name: str
    smi: object
    lspci: object
    gpu: bool
    source: str
    driver_ok: "bool | None"
    preflight_exit: int


CASES = (
    # The healthy GPU box.
    Case("driver-ok", OK_SMI, LSPCI_NVIDIA, True, "nvidia-smi", True, 0),
    # The storm outage: card on the bus, kernel module rejected.
    Case("driver-dead", DRIVER_DEAD, LSPCI_NVIDIA, True, "lspci", False, 1),
    # A wedged driver hangs nvidia-smi instead of failing it.
    Case("driver-hung", TIMEOUT, LSPCI_NVIDIA, True, "lspci", False, 1),
    # nvidia-smi runs but sees no device: the card is not bound to the driver.
    Case("smi-lists-nothing", EMPTY, LSPCI_NVIDIA, True, "lspci", False, 1),
    # The card was never set up: no NVIDIA userspace at all. The installer
    # built the CPU stack for this box, so nothing is degraded by starting it.
    Case("utils-not-installed", ABSENT, LSPCI_NVIDIA, True, "lspci", None, 0),
    # CPU-only boxes, whatever is or isn't installed, always start.
    Case("cpu-box", ABSENT, LSPCI_AMD_ONLY, False, "none", None, 0),
    Case("stale-utils-no-card", DRIVER_DEAD, LSPCI_AMD_ONLY, False, "none", None, 0),
    Case("no-pciutils", ABSENT, ABSENT, False, "none", None, 0),
    # Without lspci the card cannot be seen, so a failing nvidia-smi is not
    # proof of a dead driver: both sides fail open (and say so).
    Case("no-pciutils-smi-fails", DRIVER_DEAD, ABSENT, False, "none", None, 0),
    Case("no-pciutils-smi-ok", OK_SMI, ABSENT, True, "nvidia-smi", True, 0),
)
