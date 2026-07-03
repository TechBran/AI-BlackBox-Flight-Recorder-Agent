"""Host hardware probe (WI-9, M10 task 10.1).

All command output is monkeypatched at the hardware._run seam — no real
nvidia-smi/lspci ever runs from tests. Fixture shapes are the box matrix from
the plan: this box (no GPU, AMD iGPU), an 8GB-VRAM box, a 16GB box, and the
nvidia-smi-missing-but-lspci-shows-NVIDIA rung.
"""
import pytest

from Orchestrator import hardware

# This box's real /proc/meminfo MemTotal (2026-07-03): 31915476 kB → 31167 MB.
MEMINFO_FIXTURE = (
    "MemTotal:       31915476 kB\n"
    "MemFree:         2707068 kB\n"
    "MemAvailable:   14688992 kB\n"
)

LSPCI_AMD_ONLY = (
    "07:00.0 VGA compatible controller: Advanced Micro Devices, Inc. "
    "[AMD/ATI] Raphael (rev c7)\n"
    "07:00.1 Audio device: Advanced Micro Devices, Inc. [AMD/ATI] Rembrandt\n"
)
LSPCI_NVIDIA = (
    "00:02.0 Host bridge: Intel Corporation Device 4660\n"
    "01:00.0 VGA compatible controller: NVIDIA Corporation GA104 "
    "[GeForce RTX 3070] (rev a1)\n"
)


@pytest.fixture(autouse=True)
def fresh_probe(tmp_path, monkeypatch):
    """Empty cache + hermetic meminfo for every test."""
    monkeypatch.setattr(hardware, "_cache", None)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(MEMINFO_FIXTURE, encoding="utf-8")
    monkeypatch.setattr(hardware, "MEMINFO_PATH", str(meminfo))
    return meminfo


def _fake_run(monkeypatch, outputs: dict, calls: list | None = None):
    """hardware._run stand-in: outputs maps cmd[0] → stdout str or None."""
    def run(cmd):
        if calls is not None:
            calls.append(cmd[0])
        return outputs.get(cmd[0])
    monkeypatch.setattr(hardware, "_run", run)


# ── the box matrix ───────────────────────────────────────────────────────────

def test_no_gpu_box_amd_igpu(monkeypatch):
    """This box's live shape: no nvidia-smi, lspci shows only the AMD iGPU."""
    _fake_run(monkeypatch, {"nvidia-smi": None, "lspci": LSPCI_AMD_ONLY})
    assert hardware.probe() == {
        "gpu": False, "gpu_name": None, "vram_mb": None,
        "ram_mb": 31167, "source": "none",
    }


def test_8gb_vram_box(monkeypatch):
    _fake_run(monkeypatch, {"nvidia-smi": "NVIDIA GeForce RTX 3070, 8192\n"})
    assert hardware.probe() == {
        "gpu": True, "gpu_name": "NVIDIA GeForce RTX 3070", "vram_mb": 8192,
        "ram_mb": 31167, "source": "nvidia-smi",
    }


def test_16gb_vram_box(monkeypatch):
    """The planned RTX 2000 Ada box (audit decision 1)."""
    _fake_run(
        monkeypatch, {"nvidia-smi": "NVIDIA RTX 2000 Ada Generation, 16380\n"}
    )
    result = hardware.probe()
    assert result["gpu"] is True
    assert result["gpu_name"] == "NVIDIA RTX 2000 Ada Generation"
    assert result["vram_mb"] == 16380
    assert result["source"] == "nvidia-smi"


def test_nvidia_smi_missing_but_lspci_shows_nvidia(monkeypatch):
    """Driver not installed yet: GPU presence without VRAM (vram_mb None —
    the preflight treats unverifiable VRAM as doesn't-fit → CPU)."""
    _fake_run(monkeypatch, {"nvidia-smi": None, "lspci": LSPCI_NVIDIA})
    assert hardware.probe() == {
        "gpu": True,
        "gpu_name": "NVIDIA Corporation GA104 [GeForce RTX 3070] (rev a1)",
        "vram_mb": None, "ram_mb": 31167, "source": "lspci",
    }


# ── fail-soft + degradation edges ────────────────────────────────────────────

def test_everything_fails_never_raises(monkeypatch, tmp_path):
    _fake_run(monkeypatch, {})  # every command → None
    monkeypatch.setattr(hardware, "MEMINFO_PATH", str(tmp_path / "absent"))
    assert hardware.probe() == {
        "gpu": False, "gpu_name": None, "vram_mb": None,
        "ram_mb": 0, "source": "none",
    }


def test_run_seam_is_failsoft_for_real():
    """The real _run (not the fake) returns None for a missing binary and a
    non-zero exit — the two live failure modes on this box."""
    assert hardware._run(["definitely-no-such-binary-xyz"]) is None
    assert hardware._run(["false"]) is None


def test_nvidia_smi_empty_output_falls_to_lspci(monkeypatch):
    """nvidia-smi present but reports no GPU → ladder continues to lspci."""
    _fake_run(monkeypatch, {"nvidia-smi": "\n", "lspci": LSPCI_AMD_ONLY})
    result = hardware.probe()
    assert result["gpu"] is False and result["source"] == "none"


def test_unparseable_vram_still_reports_presence(monkeypatch):
    _fake_run(monkeypatch, {"nvidia-smi": "NVIDIA Weird GPU, [N/A]\n"})
    result = hardware.probe()
    assert result["gpu"] is True
    assert result["vram_mb"] is None
    assert result["source"] == "nvidia-smi"


# ── cache ────────────────────────────────────────────────────────────────────

def test_probe_is_cached_within_ttl(monkeypatch):
    calls = []
    _fake_run(monkeypatch, {"nvidia-smi": "NVIDIA X, 8192\n"}, calls)
    first = hardware.probe()
    second = hardware.probe()
    assert first == second
    assert calls == ["nvidia-smi"]  # one command run, second call served hot


def test_ttl_zero_reprobes(monkeypatch):
    calls = []
    _fake_run(monkeypatch, {"nvidia-smi": "NVIDIA X, 8192\n"}, calls)
    hardware.probe(ttl_s=0)
    hardware.probe(ttl_s=0)
    assert calls.count("nvidia-smi") == 2


def test_probe_returns_copies(monkeypatch):
    _fake_run(monkeypatch, {"nvidia-smi": "NVIDIA X, 8192\n"})
    first = hardware.probe()
    first["gpu"] = "mutated"
    assert hardware.probe()["gpu"] is True  # cache unharmed
