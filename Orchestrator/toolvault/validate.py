"""ToolVault v2 — validation core + CLI (Task 7.1).

A single ``validate_all()`` that sweeps every module folder under
``registry.TOOLS_DIR`` and reports, in one dict, exactly what's wrong (if
anything) and how complete the system is:

* **schema** — ``schema.json`` parses as JSON and passes
  :func:`schema_spec.validate_module_dict` (with the live
  :data:`resolvers.KNOWN_SOURCES`).
* **executor** — if ``<folder>/executor.py`` exists, ``registry.get_executor``
  must return a callable; otherwise the executor-load error from
  ``registry.load_errors()`` is surfaced. A *missing* ``executor.py`` is NOT an
  error — schema-only tools (the mcp-internal ones) are legitimate.
* **embedding coverage** — how many canonical tools have a cached vector in
  ``embeddings.json`` vs the total.

This reuses ``registry`` / ``schema_spec`` / ``resolvers`` / ``embeddings``
wholesale — it adds NO new validation logic, it only aggregates and reports.

CLI: ``python -m Orchestrator.toolvault.validate`` prints a human summary and
exits non-zero when ``not ok`` (a CI gate).
"""

import json
import sys

from . import availability, embeddings, registry, resolvers, schema_spec


def _x_availability_feature_errors(data) -> list:
    """Guard: an ``x-availability.feature``, when present, MUST be a known
    ``availability.FEATURES`` key.

    schema_spec validates the gate's shape (provider/requires_env) but is kept
    free of the FEATURES registry. This check lives here, next to that registry,
    because an unknown feature is not a structural error — it is a *runtime
    landmine*: ``availability.enabled_providers`` does ``FEATURES[feature]``, so a
    bogus feature raises KeyError inside ``filter_available`` and takes down the
    live injector AND makes the MCP ``list_tools`` return ZERO tools. Catching it
    at validate time (CI gate) is the guard against the next person tagging a tool
    with a feature the gate cannot serve.

    Returns a list of error strings (empty when clean). Never raises.
    """
    if not isinstance(data, dict):
        return []
    gate = data.get("x-availability")
    if not isinstance(gate, dict) or "feature" not in gate:
        return []
    feature = gate.get("feature")
    if feature in availability.FEATURES:
        return []
    return [
        f"x-availability.feature {feature!r} is not a known availability.FEATURES "
        f"key; known features: {sorted(availability.FEATURES)}"
    ]


def _cli_agent_mcp_group_errors(data) -> list:
    """Guard the D1 invariant for EVERY tool, not just the three known names: a
    tool categorized ``cli_agent`` must NEVER be in the ``mcp`` group.

    The ``mcp`` group feeds ``MCP/blackbox_mcp_server.py``, exposed over the
    Tailscale **Funnel** (public HTTPS ingress). A fully-open CLI-agent tool there
    is remote arbitrary command execution under the operator's shell. A test
    parametrized over today's tool names can't catch a 4th cli_agent tool added
    later with ``mcp`` in its groups — a category-keyed CI check can, and does.

    Returns a list of error strings (empty when clean). Never raises.
    """
    if not isinstance(data, dict) or data.get("category") != "cli_agent":
        return []
    groups = data.get("groups")
    if isinstance(groups, list) and "mcp" in groups:
        name = data.get("name", "<unknown>")
        return [
            f"cli_agent tool {name!r} must NOT be in the 'mcp' group: the mcp "
            f"group is served over the public Tailscale Funnel, which would make a "
            f"fully-open CLI agent remote arbitrary command execution (invariant D1)."
        ]
    return []


def _array_items_errors(data) -> list:
    """Guard: every ``"type": "array"`` node anywhere in ``parameters`` MUST
    carry an ``items`` schema (a dict).

    Google's Gemini Live BidiGenerateContent setup validator rejects the ENTIRE
    multi-tool setup with WS close 1007 (``...items: missing field``) when any
    declared array lacks ``items`` — one bad tool kills every voice session for
    every operator, silently (2026-07-11 root cause: update_sheet_values' 2D
    ``values`` param, dead since 2026-06-20). This check walks the whole
    parameters tree (properties, items, nested anything) so N-dimensional
    arrays are covered.

    Returns a list of error strings (empty when clean). Never raises.
    """
    if not isinstance(data, dict):
        return []
    params = data.get("parameters")
    if not isinstance(params, dict):
        return []
    errors: list = []

    def walk(node, path):
        if isinstance(node, dict):
            if node.get("type") == "array" and not isinstance(node.get("items"), dict):
                errors.append(
                    f"array at {path} lacks required 'items' schema — Gemini Live "
                    f"rejects the whole tool setup with WS close 1007"
                )
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(params, "parameters")
    return errors


def validate_all() -> dict:
    """Validate every module under ``registry.TOOLS_DIR`` and report.

    Returns a dict::

        {
            "ok": bool,                       # True iff errors == {}
            "tool_count": int,                # folders with a schema.json
            "errors": {folder: [msgs], ...},  # schema/JSON/executor failures
            "schema_only": [folder, ...],     # valid tools with no executor.py
            "embedding_coverage": {"embedded": X, "total": N},
        }

    Never raises — a bad module is reported, never thrown. ``tool_count`` counts
    every folder that has a ``schema.json`` (valid OR invalid); ``errors`` keys
    the ones that failed.
    """
    errors: dict = {}
    schema_only: list = []
    tool_count = 0
    canonical_names: set = set()

    tools_dir = registry.TOOLS_DIR
    folders = []
    if tools_dir.exists():
        folders = sorted(p for p in tools_dir.iterdir() if p.is_dir())

    for folder in folders:
        folder_name = folder.name
        schema_path = folder / "schema.json"
        if not schema_path.exists():
            # Not a tool module (no schema.json) — skip entirely.
            continue

        tool_count += 1
        folder_errors: list = []

        # --- schema.json: parse + validate ---------------------------------
        data = None
        try:
            data = json.loads(schema_path.read_text())
        except (OSError, ValueError) as e:  # ValueError covers JSONDecodeError
            folder_errors.append(f"failed to load schema.json: {e}")

        if data is not None:
            folder_errors.extend(
                schema_spec.validate_module_dict(
                    data, folder_name, known_sources=resolvers.KNOWN_SOURCES
                )
            )
            # An x-availability.feature must be a known FEATURES key (see the
            # helper's docstring: an unknown one is a live-injector/MCP landmine).
            folder_errors.extend(_x_availability_feature_errors(data))
            # A cli_agent-category tool must never be in the public `mcp` group.
            folder_errors.extend(_cli_agent_mcp_group_errors(data))
            # Every declared array MUST carry an `items` schema — Gemini Live's
            # setup validator rejects the whole tool payload otherwise (P1.2).
            folder_errors.extend(_array_items_errors(data))

        # Track the canonical name for embedding coverage (best effort —
        # even if other fields are invalid, a string name is what the store
        # keys on).
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            canonical_names.add(data["name"])

        # --- executor: required iff executor.py exists ---------------------
        exec_path = folder / "executor.py"
        if exec_path.exists():
            fn = registry.get_executor(folder_name)
            if not callable(fn):
                # get_executor records the load error in load_errors() — pull it.
                load_errs = registry.load_errors().get(folder_name)
                if load_errs:
                    folder_errors.extend(load_errs)
                else:
                    folder_errors.append(
                        "executor.py exists but get_executor returned no callable"
                    )
        else:
            # Schema-only tool — valid only if the schema itself is clean.
            if not folder_errors:
                schema_only.append(folder_name)

        if folder_errors:
            errors[folder_name] = folder_errors

    # --- embedding coverage: store vs canonical names ----------------------
    store = embeddings.load_embeddings_store()
    embedded = sum(
        1
        for name in canonical_names
        if isinstance(store.get(name), dict) and store[name].get("vector")
    )

    return {
        "ok": not errors,
        "tool_count": tool_count,
        "errors": errors,
        "schema_only": sorted(schema_only),
        "embedding_coverage": {"embedded": embedded, "total": len(canonical_names)},
    }


def _format_summary(report: dict) -> str:
    """Render a human-readable multi-line summary of a ``validate_all`` report."""
    lines = []
    ok = report["ok"]
    lines.append(f"ToolVault validation: {'OK' if ok else 'FAILED'}")
    lines.append(f"  tools:           {report['tool_count']}")
    lines.append(f"  schema-only:     {len(report['schema_only'])}")
    cov = report["embedding_coverage"]
    lines.append(
        f"  embeddings:      {cov['embedded']}/{cov['total']} embedded"
    )
    errors = report["errors"]
    if errors:
        lines.append(f"  errors:          {len(errors)} tool(s)")
        for folder in sorted(errors):
            for msg in errors[folder]:
                lines.append(f"    - {folder}: {msg}")
    else:
        lines.append("  errors:          none")
    return "\n".join(lines)


def main() -> int:
    """CLI entrypoint: print the summary, return 0 if ok else 1."""
    report = validate_all()
    print(_format_summary(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
