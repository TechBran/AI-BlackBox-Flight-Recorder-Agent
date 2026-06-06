#!/usr/bin/env python3
"""ToolVault v2 — generate per-tool ``schema.json`` modules (Task 6.1).

Faithful 1:1 migration of the canonical ``tool_registry.TOOL_DEFINITIONS`` into
per-tool module folders under ``ToolVault/tools/<name>/schema.json``.

For each non-UGV tool we build a schema dict with:

    name        — tool name (== folder name)
    description — verbatim from the canonical def
    category    — migrate.CATEGORY_MAP.get(name, "uncategorized")
    groups      — verbatim from the canonical def
    tier        — migrate.get_tier(name)
    parameters  — deep copy, VERBATIM (no x-source; a later task adds those)
    executor    — only when _EXECUTOR_NAMES[name] != name
    returns/example/notes — enrichment from the OLD vault via read_tool(name),
                            mapped from its uppercase RETURNS/EXAMPLE/NOTES;
                            "" when the tool isn't in the old vault.

Every dict is validated via ``schema_spec.validate_module_dict`` (expecting ZERO
errors) before it is written. Output is deterministic and idempotent.

Run:
    Orchestrator/venv/bin/python scripts/toolvault_generate_modules.py
"""

import copy
import json
import sys
from pathlib import Path

# Make ``Orchestrator`` importable regardless of CWD.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Orchestrator.tools.tool_registry import TOOL_DEFINITIONS, _EXECUTOR_NAMES
from Orchestrator.toolvault import read_tool, resolvers, schema_spec
from Orchestrator.toolvault import migrate

TOOLS_DIR = PROJECT_ROOT / "ToolVault" / "tools"


def _enrichment(name: str) -> dict:
    """Return {returns, example, notes} from the OLD vault, else all ""."""
    old = read_tool(name)
    if not old:
        return {"returns": "", "example": "", "notes": ""}
    return {
        "returns": old.get("RETURNS") or "",
        "example": old.get("EXAMPLE") or "",
        "notes": old.get("NOTES") or "",
    }


def build_schema(tool_def: dict) -> dict:
    """Build the schema.json dict for one canonical tool def."""
    name = tool_def["name"]
    schema = {
        "name": name,
        "description": tool_def["description"],
        "category": migrate.CATEGORY_MAP.get(name, "uncategorized"),
        "groups": copy.deepcopy(tool_def.get("groups", [])),
        "tier": migrate.get_tier(name),
        "parameters": copy.deepcopy(
            tool_def.get(
                "parameters",
                {"type": "object", "properties": {}, "required": []},
            )
        ),
    }

    # executor: only when it actually differs from the tool name.
    executor = _EXECUTOR_NAMES.get(name)
    if executor and executor != name:
        schema["executor"] = executor

    # Enrichment from the old vault (optional keys; only emit when non-empty).
    enrich = _enrichment(name)
    for key in ("returns", "example", "notes"):
        if enrich[key]:
            schema[key] = enrich[key]

    return schema


def main() -> int:
    generated = 0
    skipped_ugv = 0
    errors = []

    for tool_def in TOOL_DEFINITIONS:
        name = tool_def["name"]
        if name.startswith("ugv_"):
            skipped_ugv += 1
            continue

        schema = build_schema(tool_def)

        errs = schema_spec.validate_module_dict(
            schema, name, known_sources=resolvers.KNOWN_SOURCES
        )
        if errs:
            errors.append((name, errs))
            print(f"  VALIDATION ERROR {name}: {errs}")
            continue

        out_dir = TOOLS_DIR / name
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "schema.json"
        out_path.write_text(
            json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
        )
        generated += 1

    print(f"\n{'='*60}")
    print(f"  ToolVault module codegen")
    print(f"  Generated:        {generated}")
    print(f"  Skipped (ugv_*):  {skipped_ugv}")
    print(f"  Validation errors: {len(errors)}")
    print(f"  Output: {TOOLS_DIR}")
    print(f"{'='*60}")

    if errors:
        print("\nFAILED — validation errors above; nothing further written for them.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
