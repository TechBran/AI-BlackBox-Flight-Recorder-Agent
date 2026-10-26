"""Gemini model discovery through google-genai (audit rank 71: X01-deps-python-07,
O01-core-runtime-04, O05-ledger-persistence-19, X05-architecture-10; W44).

Two catalogs read the live Gemini model list: the embeddings deprecation
watcher (watcher._gemini_catalog: is the active embedding model still listed,
is there a successor) and the chat model dropdown (admin_routes.
_fetch_google_models, GET /models/google). Both used the end-of-life
google.generativeai SDK's genai.list_models() and its supported_generation_
methods; they now page client.models.list() and filter on supported_actions
(the same field: the SDK maps supportedGenerationMethods onto it). NO NETWORK:
genai.Client is replaced on each module.
"""
import asyncio
import sys
from types import SimpleNamespace

import pytest
from google import genai

from Orchestrator.embeddings import watcher
from Orchestrator.embeddings.registry import EMBEDDING_MODELS

# What models.list() pages through on the Developer API: "models/"-prefixed
# names, and supported_actions possibly None.
CATALOG = [
    ("models/gemini-embedding-001", ["embedContent", "countTextTokens", "countTokens"]),
    ("models/gemini-2.5-flash", ["generateContent", "countTokens", "createCachedContent"]),
    ("models/text-embedding-004", ["embedContent"]),
    ("models/gemini-embedding-2", ["embedContent", "countTokens"]),
    ("models/gemini-2.5-pro", ["generateContent", "countTokens"]),
    ("models/aqa", ["generateAnswer"]),
    ("models/gemini-1.5-flash-aqa", ["generateContent"]),
    ("models/gemma-3-27b-it", ["generateContent", "countTokens"]),
    ("models/imagen-4.0-generate-001", ["predict"]),
    ("models/gemini-unlisted-actions", None),
]


class FakeClient:
    """Stand-in for google.genai.Client: records its kwargs, whether it was
    closed, and on which thread models.list() was called."""

    instances: list = []

    def __init__(self, catalog=CATALOG, error=None, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.listed_on_loop = None
        self._catalog = catalog
        self._error = error
        self.models = SimpleNamespace(list=self._list)
        FakeClient.instances.append(self)

    def _list(self, **kwargs):
        try:
            asyncio.get_running_loop()
            self.listed_on_loop = True
        except RuntimeError:
            self.listed_on_loop = False
        if self._error is not None:
            raise self._error
        return iter([SimpleNamespace(name=n, supported_actions=a) for n, a in self._catalog])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


@pytest.fixture
def fake_client(monkeypatch):
    """google.genai.Client, which both catalogs build their client from."""
    FakeClient.instances = []
    monkeypatch.setattr(genai, "Client", FakeClient)
    return FakeClient


# ── watcher: the embedding catalog ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_watcher_catalog_lists_embed_capable_models_by_registry_id(fake_client, monkeypatch):
    monkeypatch.setattr(watcher.config, "GOOGLE_API_KEY", "test-key-not-real")
    ids = await watcher._gemini_catalog()
    assert ids == ["models/gemini-embedding-001", "models/text-embedding-004",
                   "models/gemini-embedding-2"]
    # the registry's model_id format, so _catalog_check's `in` test matches it
    for slug, entry in EMBEDDING_MODELS.items():
        if entry["provider"] == "gemini":
            assert entry["model_id"] in ids, slug
    (client,) = fake_client.instances
    assert client.kwargs["api_key"] == "test-key-not-real"
    assert client.kwargs["http_options"].timeout == int(watcher.CATALOG_TIMEOUT_S * 1000)
    assert client.closed
    assert client.listed_on_loop is False  # the sync pager runs off the event loop


@pytest.mark.asyncio
async def test_watcher_catalog_without_a_key_leaves_the_sdk_its_env_lookup(fake_client, monkeypatch):
    monkeypatch.setattr(watcher.config, "GOOGLE_API_KEY", "")
    await watcher._gemini_catalog()
    assert fake_client.instances[0].kwargs["api_key"] is None


@pytest.mark.asyncio
async def test_catalog_check_reads_listing_and_successor_from_the_new_catalog(fake_client):
    entry = EMBEDDING_MODELS["gemini-embedding-001"]
    listed, successor, note = await watcher._catalog_check(entry)
    # gemini-embedding-2 is the same-skeleton, higher-version GA id; the
    # deprecated text-embedding-004 is a different skeleton and never counts.
    assert (listed, successor, note) == (True, "models/gemini-embedding-2", None)
    listed, successor, note = await watcher._catalog_check(EMBEDDING_MODELS["gemini-embedding-2"])
    assert (listed, successor, note) == (True, None, None)


@pytest.mark.asyncio
async def test_catalog_check_skips_when_the_listing_fails(fake_client, monkeypatch):
    """A dead listing API (or a client that cannot be built) is a skip with a
    note, never a model failure."""
    monkeypatch.setattr(genai, "Client",
                        lambda **kw: FakeClient(error=RuntimeError("503 UNAVAILABLE"), **kw))
    listed, successor, note = await watcher._catalog_check(EMBEDDING_MODELS["gemini-embedding-2"])
    assert listed is True and successor is None
    assert note == "catalog check skipped (RuntimeError: 503 UNAVAILABLE)"


@pytest.mark.asyncio
async def test_catalog_check_flags_a_delisted_model(fake_client, monkeypatch):
    without_2 = [(n, a) for n, a in CATALOG if n != "models/gemini-embedding-2"]
    monkeypatch.setattr(genai, "Client", lambda **kw: FakeClient(catalog=without_2, **kw))
    listed, _, note = await watcher._catalog_check(EMBEDDING_MODELS["gemini-embedding-2"])
    assert listed is False and note is None


# ── admin_routes: the chat model dropdown ───────────────────────────────────

def test_google_chat_catalog_keeps_generate_capable_gemini_models(fake_client, monkeypatch):
    from Orchestrator.routes import admin_routes

    monkeypatch.setattr(admin_routes, "GOOGLE_API_KEY", "test-key-not-real")
    out = admin_routes._fetch_google_models()
    assert out["provider"] == "google"
    assert out["source"] == "live"
    assert out["models"] == [
        {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
        {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
    ]
    (client,) = fake_client.instances
    assert client.kwargs["api_key"] == "test-key-not-real"
    assert client.kwargs["http_options"].timeout == 5000  # ms, like the sibling fetchers' 5 s
    assert client.closed


def test_google_chat_catalog_without_chat_models_falls_back(fake_client, monkeypatch):
    from Orchestrator.routes import admin_routes

    embed_only = [(n, a) for n, a in CATALOG if a and "generateContent" not in a]
    monkeypatch.setattr(genai, "Client", lambda **kw: FakeClient(catalog=embed_only, **kw))
    assert admin_routes._fetch_google_models() is None


def test_google_chat_catalog_without_a_key_leaves_the_sdk_its_env_lookup(fake_client, monkeypatch):
    from Orchestrator.routes import admin_routes

    monkeypatch.setattr(admin_routes, "GOOGLE_API_KEY", "")
    admin_routes._fetch_google_models()
    assert fake_client.instances[0].kwargs["api_key"] is None


# ── live: the real catalog has the shape both filters rely on ────────────────
# Opt-in (a free model listing on the box's own key):
#   BLACKBOX_TEST_LIVE=1 pytest -m live_service Orchestrator/tests/test_gemini_model_discovery.py

def _live_key():
    from Orchestrator import config

    if not config.GOOGLE_API_KEY:
        pytest.skip("GOOGLE_API_KEY is not set on this box")


@pytest.mark.live_service
def test_live_catalog_lists_every_gemini_registry_model_as_embed_capable():
    _live_key()
    ids = asyncio.run(watcher._gemini_catalog())
    for slug, entry in EMBEDDING_MODELS.items():
        if entry["provider"] == "gemini":
            assert entry["model_id"] in ids, f"{slug}: {entry['model_id']} not listed"


@pytest.mark.live_service
def test_live_chat_catalog_returns_gemini_chat_models():
    _live_key()
    from Orchestrator.routes import admin_routes

    out = admin_routes._fetch_google_models()
    assert out and out["source"] == "live" and out["models"]
    assert all(m["id"].startswith("gemini") and "embedding" not in m["id"]
               for m in out["models"])


# ── the legacy SDK is gone from the runtime ─────────────────────────────────

def test_no_runtime_module_loads_the_legacy_sdk():
    """config.py imported google.generativeai and ran genai.configure at import,
    so every service start printed its end-of-support FutureWarning. Nothing the
    Orchestrator imports may load it now (the dependency-manifest test keeps it
    out of requirements.in, so a new import of it fails that gate too)."""
    from Orchestrator import config  # noqa: F401
    from Orchestrator.embeddings import providers, search  # noqa: F401
    from Orchestrator.routes import admin_routes, embeddings_routes  # noqa: F401

    assert "google.generativeai" not in sys.modules
    assert not hasattr(config, "genai")
