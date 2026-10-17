"""tests/supervisor: the UGV Beast Gemini Live supervisor (marker ugv, excluded
by default; run with `pytest -m ugv tests/supervisor`).

The package under test lives in the UGV sub-project
(docs/ugv-beast/setup/ugv_tools_api), whose own tests/supervisor suite is the
maintained one. This directory is the earlier copy, kept so its cases still run
against the package. Put the package on sys.path, and skip collecting the
directory where the package's numeric deps are not installed (a bare pytest
still imports every module it collects, even the deselected ones).
"""
import importlib.util
import sys
from pathlib import Path

_UGV_PACKAGE_ROOT = (Path(__file__).resolve().parents[2]
                     / "docs" / "ugv-beast" / "setup" / "ugv_tools_api")

if str(_UGV_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_UGV_PACKAGE_ROOT))


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ImportError:  # a dotted name whose parent package is missing
        return False


if not all(_installed(m) for m in ("numpy", "scipy", "google.genai")):
    collect_ignore_glob = ["test_*.py"]
