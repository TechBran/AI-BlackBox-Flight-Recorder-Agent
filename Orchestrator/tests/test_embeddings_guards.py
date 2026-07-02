"""Pluggable embeddings — guard ratchets (Task 16).

CU-pass-style literal ratchets (test_cu_catalog.py idiom): the registry in
Orchestrator/embeddings/registry.py is the SINGLE source of truth for
embedding-model data, so no embedding-model literal may appear as a working
string constant anywhere else in the scanned production modules. Everything
derives from EMBEDDING_MODELS / LEGACY_INLINE_SLUG / config.

Scope decisions (deliberate):
- Comments and docstrings are NOT ratcheted — explanatory prose may name
  models (e.g. watcher.py's vendor-catalog rationale); only functional string
  literals (assignments, dict values, f-string parts, call args) are scanned.
  The AST walk below skips docstring nodes and never sees comments.
- Test files and fixtures are excluded BY DESIGN: tests pin golden/historical
  behaviors of SPECIFIC models (3072-dim transcode parity vectors, the
  exact-slug dims table, vendor-catalog regression lists). Those are
  assertions about history and binding contracts, not live configuration —
  routing them through the registry would hollow out their ratchet value.
"""
import ast
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator import config, fossils
from Orchestrator.embeddings import ollama_io
from Orchestrator.embeddings.registry import EMBEDDING_MODELS, LEGACY_INLINE_SLUG
from Orchestrator.embeddings.store import set_active_slug
from Orchestrator.routes.embeddings_routes import router

ORCH = Path(__file__).resolve().parents[1]  # Orchestrator/

# Matches every model family the registry knows (slugs AND vendor model ids):
# gemini-embedding-001 / models/gemini-embedding-001, text-embedding-3-large,
# qwen3-embedding[:-]*, and the bare legacy "embedding-001" tail.
MODEL_LITERAL_RX = re.compile(
    r"gemini-embedding|text-embedding|qwen3-embedding|embedding-001"
)

_EXPLICIT_FILES = [
    ORCH / "monitoring.py",
    ORCH / "checkpoint.py",
    ORCH / "fossils.py",
    ORCH / "startup.py",
    ORCH / "backfill_embeddings.py",
    ORCH / "tokenization.py",  # WI-11: backends key off registry tokenizer specs, never slugs
    ORCH / "routes" / "embeddings_routes.py",
]
_EMBEDDINGS_FILES = sorted(
    p for p in (ORCH / "embeddings").glob("*.py") if p.name != "registry.py"
)
_TOOLVAULT_FILES = sorted(
    p for p in (ORCH / "toolvault").rglob("*.py") if "tests" not in p.parts
)
SCANNED_FILES = _EXPLICIT_FILES + _EMBEDDINGS_FILES + _TOOLVAULT_FILES


def _code_string_literals(path: Path) -> list[tuple[int, str]]:
    """(lineno, value) for every string constant that is NOT a docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstring_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_nodes.add(id(body[0].value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_nodes
    ]


# ── literal ratchets ─────────────────────────────────────────────────────────

def test_scan_list_is_real():
    """A renamed/moved module must not silently shrink the ratchet."""
    for path in _EXPLICIT_FILES:
        assert path.is_file(), f"scanned file vanished: {path}"
    assert _EMBEDDINGS_FILES, "embeddings package scan found no modules"
    assert _TOOLVAULT_FILES, "toolvault scan found no modules"
    # registry.py itself stays out of the scan — it's the one allowed home
    assert all(p.name != "registry.py" for p in _EMBEDDINGS_FILES)


@pytest.mark.parametrize(
    "path", SCANNED_FILES, ids=lambda p: str(p.relative_to(ORCH))
)
def test_no_embedding_model_literals_outside_registry(path):
    offenders = [
        (lineno, value)
        for lineno, value in _code_string_literals(path)
        if MODEL_LITERAL_RX.search(value)
    ]
    assert not offenders, (
        f"{path.relative_to(ORCH)} embeds embedding-model literals "
        f"{offenders}; derive from Orchestrator.embeddings.registry instead"
    )


# ── registry-membership invariants ───────────────────────────────────────────

def test_active_default_and_legacy_inline_slug_are_registry_members():
    """The active.json default and the transcode target must both resolve in
    the registry — a typo'd or dropped entry would orphan a persistent store."""
    assert config.EMBEDDINGS_ACTIVE_DEFAULT in EMBEDDING_MODELS
    assert LEGACY_INLINE_SLUG in EMBEDDING_MODELS


# ── status route: every offered slug comes from the registry ─────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    """Hermetic /embeddings router app (test_embeddings_routes.py recipe):
    tmp_path index + stores, ollama seams mocked — zero network, zero
    real Manifest/ access."""
    index_path = tmp_path / "snapshot_index.json"
    stores_dir = tmp_path / "embeddings"
    monkeypatch.setattr(fossils, "SNAPSHOT_INDEX", index_path)
    monkeypatch.setattr(fossils, "_index_cache", None)
    monkeypatch.setattr(fossils, "_index_cache_mtime", 0.0)
    monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(stores_dir))
    set_active_slug(LEGACY_INLINE_SLUG, base_dir=stores_dir)
    monkeypatch.setattr(ollama_io, "binary_installed", lambda: False)
    monkeypatch.setattr(ollama_io, "daemon_version", lambda: None)
    monkeypatch.setattr(ollama_io, "local_models", lambda: [])
    monkeypatch.setattr(ollama_io, "ram_preflight", lambda ram_gb: None)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_status_models_are_exactly_the_registry(client):
    """models[] (the only list the wizard/cards can offer) is built FROM the
    registry — every slug it serves must be a registry member, with none
    missing and none invented."""
    body = client.get("/embeddings/status").json()
    assert {m["slug"] for m in body["models"]} == set(EMBEDDING_MODELS)
    assert body["active"] in EMBEDDING_MODELS
