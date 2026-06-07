"""Tests for the ToolVault v2 validation core + CLI (Task 7.1).

Two layers:

* **Real tree** — ``validate_all()`` against the actual ``ToolVault/tools`` must
  report ``ok=True``, exactly 48 tools, no errors, and full embedding coverage
  (embeddings.json ships with the repo).
* **Hermetic** — point ``registry.TOOLS_DIR`` at a tmp dir with one invalid
  module and confirm ``ok=False`` and the folder appears in ``errors``.
"""

import json

import pytest

from Orchestrator.toolvault import registry, validate


EXPECTED_TOOL_COUNT = 48


# ---------------------------------------------------------------------------
# Real-tree validation (the shipping ToolVault/tools + embeddings.json)
# ---------------------------------------------------------------------------

def test_validate_all_real_tree_ok():
    """The shipping module tree validates clean with 48 tools, full coverage."""
    report = validate.validate_all()

    assert report["ok"] is True, f"unexpected errors: {report['errors']}"
    assert report["tool_count"] == EXPECTED_TOOL_COUNT
    assert report["errors"] == {}

    cov = report["embedding_coverage"]
    assert cov["total"] == EXPECTED_TOOL_COUNT
    assert cov["embedded"] == EXPECTED_TOOL_COUNT

    # schema_only is a sorted list of valid no-executor tools (the mcp-internal
    # ones). It must be a subset of all tools and contain no error folders.
    assert isinstance(report["schema_only"], list)
    assert report["schema_only"] == sorted(report["schema_only"])
    assert len(report["schema_only"]) <= EXPECTED_TOOL_COUNT


def test_cli_main_real_tree_exits_zero():
    """``validate.main()`` returns 0 on the clean real tree (CI gate green)."""
    assert validate.main() == 0


# ---------------------------------------------------------------------------
# Hermetic: a tmp tools dir with one invalid module → ok=False
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_tools(tmp_path, monkeypatch):
    """Point the registry at an empty tmp tools dir and reset its cache."""
    d = tmp_path / "tools"
    d.mkdir()
    monkeypatch.setattr(registry, "TOOLS_DIR", d)
    registry.invalidate_cache()
    yield d
    registry.invalidate_cache()


def _write_schema(tools_dir, name, schema):
    folder = tools_dir / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "schema.json").write_text(json.dumps(schema))
    return folder


def test_validate_all_invalid_module_not_ok(tmp_tools):
    """A schema whose name mismatches its folder → ok=False, folder in errors."""
    # Invalid: 'name' does not match the folder, and tier is bogus.
    _write_schema(
        tmp_tools,
        "broken_tool",
        {
            "name": "WRONG_NAME",
            "description": "x",
            "category": "communication",
            "groups": ["chat"],
            "tier": 99,
            "parameters": {"type": "object", "properties": {}},
        },
    )

    report = validate.validate_all()

    assert report["ok"] is False
    assert report["tool_count"] == 1
    assert "broken_tool" in report["errors"]
    assert report["errors"]["broken_tool"], "expected at least one error message"


def test_validate_all_bad_json_not_ok(tmp_tools):
    """A schema.json that isn't valid JSON is reported, never raised."""
    folder = tmp_tools / "bad_json"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "schema.json").write_text("{not valid json")

    report = validate.validate_all()

    assert report["ok"] is False
    assert "bad_json" in report["errors"]
    assert any("schema.json" in m for m in report["errors"]["bad_json"])


def test_validate_all_empty_dir_ok(tmp_tools):
    """An empty tools dir is vacuously ok with zero tools."""
    report = validate.validate_all()
    assert report["ok"] is True
    assert report["tool_count"] == 0
    assert report["errors"] == {}
    assert report["embedding_coverage"]["total"] == 0
