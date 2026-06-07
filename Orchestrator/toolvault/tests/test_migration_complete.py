"""Tests for the ToolVault v2 module migration (Task 6.1).

These tests run against the REAL, populated ``ToolVault/tools`` tree generated
by ``scripts/toolvault_generate_modules.py`` from the canonical
``tool_registry.TOOL_DEFINITIONS``. Unlike the hermetic registry unit tests,
they intentionally assert on the on-disk product of the codegen step:

* at LEAST the 48 migrated module folders, each with a ``schema.json`` (all
  non-UGV tools, INCLUDING ``get_current_operator``) — more is fine, tools get
  added over time (see ToolVault/tools/ADDING_A_TOOL.md),
* zero ``ugv_*`` folders,
* every module validates clean against ``schema_spec``,
* the folder-name set equals the non-UGV tool-name set (relational — holds at
  any count since both derive from the modules post-cutover),
* ``registry.load_canonical()`` returns >= 48 with no load errors, and
* metadata spot checks (executor alias + ``get_current_operator`` placement).

NOTE: 48 is the one-time codegen BASELINE (a floor), not an exact count — the
v2 design makes adding tools the normal workflow. Per-tool regression (no
migrated tool silently lost or mutated) is guarded by test_registry_parity.py.
"""

import json

from Orchestrator.toolvault import registry, resolvers, schema_spec
from Orchestrator.tools.tool_registry import TOOL_DEFINITIONS

# The one-time codegen baseline. A FLOOR, not an exact count (tools get added).
MIGRATED_BASELINE = 48

NON_UGV_NAMES = {
    t["name"] for t in TOOL_DEFINITIONS if not t["name"].startswith("ugv_")
}


def _module_folders():
    return [p for p in registry.TOOLS_DIR.iterdir() if p.is_dir()]


def test_at_least_baseline_folders_each_with_schema():
    folders = _module_folders()
    assert len(folders) >= MIGRATED_BASELINE, (
        f"expected at least {MIGRATED_BASELINE} module folders, got {len(folders)}"
    )
    for folder in folders:
        assert (folder / "schema.json").exists(), (
            f"{folder.name} is missing schema.json"
        )


def test_no_ugv_folders():
    for folder in _module_folders():
        assert not folder.name.startswith("ugv_"), (
            f"unexpected ugv folder: {folder.name}"
        )


def test_every_module_validates_clean():
    for folder in _module_folders():
        data = json.loads((folder / "schema.json").read_text())
        errs = schema_spec.validate_module_dict(
            data, folder.name, known_sources=resolvers.KNOWN_SOURCES
        )
        assert errs == [], f"{folder.name} failed validation: {errs}"


def test_folder_set_equals_non_ugv_tool_names():
    folder_names = {p.name for p in _module_folders()}
    assert folder_names == NON_UGV_NAMES, (
        f"folder/name set mismatch; "
        f"missing={NON_UGV_NAMES - folder_names}, "
        f"extra={folder_names - NON_UGV_NAMES}"
    )


def test_registry_load_canonical_no_errors():
    registry.invalidate_cache()
    canonical = registry.load_canonical()
    assert len(canonical) >= MIGRATED_BASELINE, (
        f"load_canonical returned {len(canonical)}, expected >= {MIGRATED_BASELINE}"
    )
    assert registry.load_errors() == {}, (
        f"unexpected load errors: {registry.load_errors()}"
    )


def test_spot_check_search_snapshots_executor():
    registry.invalidate_cache()
    tool = registry.get_tool("search_snapshots")
    assert tool is not None, "search_snapshots not found in canonical"
    assert tool.get("executor") == "search_memory", (
        f"search_snapshots executor != 'search_memory': {tool.get('executor')!r}"
    )


def test_spot_check_get_current_operator_metadata():
    registry.invalidate_cache()
    tool = registry.get_tool("get_current_operator")
    assert tool is not None, "get_current_operator not found in canonical"
    assert tool.get("category") == "mcp_internal", (
        f"get_current_operator category != 'mcp_internal': "
        f"{tool.get('category')!r}"
    )
    assert tool.get("tier") == 3, (
        f"get_current_operator tier != 3: {tool.get('tier')!r}"
    )
