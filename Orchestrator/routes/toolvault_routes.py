#!/usr/bin/env python3
from __future__ import annotations

"""
toolvault_routes.py - ToolVault v2 operational endpoints (Task 7.2).

Three small admin endpoints for production hardening of the module system:

  * GET  /toolvault/health   — lightweight status (never fails on errors).
  * GET  /toolvault/validate — full validate_all() report (HTTP 200; the `ok`
                               flag tells the caller whether anything's wrong).
  * POST /toolvault/reload   — invalidate the registry cache + re-sync embeddings
                               so an edited module is picked up WITHOUT a restart.

Registered via the project's star-import pattern (``from Orchestrator.checkpoint
import app`` + ``@app.<verb>`` decorators), the same as admin_routes.py.
"""

import logging

from Orchestrator.checkpoint import app
from Orchestrator.toolvault import embeddings, registry
from Orchestrator.toolvault.validate import validate_all

logger = logging.getLogger("blackbox.toolvault")


@app.get("/toolvault/health")
def toolvault_health():
    """Lightweight ToolVault status. Reports problems but never fails on them.

    Returns tool counts, the schema-only set, any load/validation errors, and
    embedding coverage. A caller polling this for liveness gets a 200 even when
    individual modules are broken — use ``/toolvault/validate`` for the gate.
    """
    report = validate_all()
    return {
        "tool_count": report["tool_count"],
        "schema_only": report["schema_only"],
        "load_errors": report["errors"],
        "embedding_coverage": report["embedding_coverage"],
    }


@app.get("/toolvault/validate")
def toolvault_validate():
    """Full ``validate_all()`` report as HTTP 200.

    The body's ``ok`` flag (and ``errors`` map) is the signal — a failing
    validation is still returned with status 200 so the caller can inspect it.
    """
    return validate_all()


@app.post("/toolvault/reload")
def toolvault_reload():
    """Hot-reload: re-scan every module + re-sync embeddings (no restart).

    This is the "edit a module on disk → hit reload" path. It clears the
    registry's schema + executor caches, then syncs embeddings against the
    freshly-loaded canonical list (only changed descriptions re-embed).
    """
    registry.invalidate_cache()
    canonical = registry.load_canonical()
    store = embeddings.sync_embeddings(canonical)
    errors = registry.load_errors()
    logger.info(
        "[TOOLVAULT] reload: tools=%d embedded=%d errors=%d",
        len(canonical), len(store), len(errors),
    )
    return {
        "reloaded": True,
        "tool_count": len(canonical),
        "embedded": len(store),
        "errors": errors,
    }
