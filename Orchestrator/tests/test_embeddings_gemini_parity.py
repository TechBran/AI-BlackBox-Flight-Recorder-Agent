"""Gemini embedding parity across the SDK port (audit rank 71: X01-deps-python-07,
O01-core-runtime-04, O05-ledger-persistence-19, X05-architecture-10).

Every Gemini vector store on a box was embedded through the legacy
google.generativeai SDK. golden/gemini_embed_parity.json holds that SDK's
vectors for five fixed sentences, every gemini registry entry, both purposes
(recorded once by scripts/record_gemini_embed_golden.py before the port). If
the provider's vectors drift from them, every stored Gemini vector is in a
different space from new query vectors and the store needs a re-embed, so the
live test below is the gate for shipping the port.

PARITY_MIN_COSINE: a faithful port lands at 1.0. The recorder measured the
service deterministic (the same request twice gave cosine 1.0 to float64
rounding), so the only room left is wire encoding (gRPC float32 against REST
JSON decimals, ~1e-7 relative) and the server running a batched request
through different kernels than a single one. What a port can get wrong lands
far below the floor: the wrong task type measures 0.89-0.95 on
gemini-embedding-001, two different sentences 0.53-0.70, and contents merged
into one request return one vector for N texts (the count guard). At the floor,
for unit vectors, no retrieval score can move by more than
sqrt(2 * (1 - PARITY_MIN_COSINE)) (0.014).

Measured on the google-genai port (dev box, 2026-10-25): cosine 1.0 on every
case below, and every component bit-identical to the legacy float32 vectors
once the REST floats are rounded to float32, on both models and both purposes,
batched, one text per call and across a batch split. The floor is the shipping
gate; the bit-identity is what says no store needs a re-embed.

The live tests are opt-in: BLACKBOX_TEST_LIVE=1 pytest -m live_service
Orchestrator/tests/test_embeddings_gemini_parity.py (cloud calls on the box's
own key; read-only, nothing is stored).
"""
import asyncio
import base64
import json
import math
import struct
from pathlib import Path

import pytest

from Orchestrator import config
from Orchestrator.embeddings import providers
from Orchestrator.embeddings.registry import EMBEDDING_MODELS

GOLDEN = Path(__file__).parent / "golden" / "gemini_embed_parity.json"
PARITY_MIN_COSINE = 0.9999
PURPOSES = ("document", "query")


def _golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _decode(b64: str) -> list[float]:
    raw = base64.b64decode(b64)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


GOLDEN_SLUGS = sorted(_golden()["vectors"])


# ── offline: the fixture is sound and still bound to the registry ────────────

def test_golden_fixture_is_bound_to_the_registry():
    """Each golden slug is a gemini registry entry with the model id and dims it
    was recorded with. The registry forbids repointing model_id under a slug; a
    repoint would also make these vectors compare against a different model."""
    golden = _golden()
    assert len(golden["sentences"]) == 5
    assert GOLDEN_SLUGS, "golden fixture holds no models"
    for slug in GOLDEN_SLUGS:
        recorded = golden["vectors"][slug]
        entry = EMBEDDING_MODELS.get(slug)
        assert entry is not None, f"{slug} left the registry"
        assert entry["provider"] == "gemini"
        assert (recorded["model_id"], recorded["dims"]) == (entry["model_id"], entry["dims"])
        for purpose in PURPOSES:
            vectors = [_decode(v) for v in recorded[purpose]]
            assert len(vectors) == len(golden["sentences"])
            for vec in vectors:
                assert len(vec) == entry["dims"]
                assert math.isclose(math.sqrt(sum(x * x for x in vec)), 1.0, abs_tol=1e-3)


def test_parity_floor_separates_what_a_port_can_get_wrong():
    """The floor must be able to fail: two different sentences, and (on a model
    that honours task types) the same sentence under the other task type, all
    sit below PARITY_MIN_COSINE in the golden itself."""
    golden = _golden()
    task_type_sensitive = []
    for slug in GOLDEN_SLUGS:
        docs = [_decode(v) for v in golden["vectors"][slug]["document"]]
        queries = [_decode(v) for v in golden["vectors"][slug]["query"]]
        for i in range(len(docs)):
            for j in range(i + 1, len(docs)):
                assert _cosine(docs[i], docs[j]) < PARITY_MIN_COSINE
        if all(_cosine(d, q) < PARITY_MIN_COSINE for d, q in zip(docs, queries)):
            task_type_sensitive.append(slug)
    assert task_type_sensitive, "no golden model separates document from query vectors"


# ── live: the provider of this tree reproduces the legacy vectors ────────────

def _live_provider(slug):
    if not config.GOOGLE_API_KEY:
        pytest.skip("GOOGLE_API_KEY is not set on this box")
    return providers.GeminiProvider(slug, EMBEDDING_MODELS[slug])  # fresh, uncached


def _assert_parity(slug, purpose, got, golden_vectors):
    cosines = [_cosine(g, w) for g, w in zip(got, golden_vectors)]
    print(f"[PARITY] {slug} {purpose}: cosine min {min(cosines):.12f} "
          f"max score shift {math.sqrt(max(0.0, 2 * (1 - min(cosines)))):.2e}")
    assert len(got) == len(golden_vectors)
    assert min(cosines) >= PARITY_MIN_COSINE, (
        f"{slug} {purpose} drifted from the legacy SDK's vectors: {cosines}")


@pytest.mark.live_service
@pytest.mark.parametrize("purpose", PURPOSES)
@pytest.mark.parametrize("slug", GOLDEN_SLUGS)
def test_live_batch_embed_matches_legacy_golden(slug, purpose):
    """All five sentences in one provider.embed call (the mint and migration
    shape: many chunks per call)."""
    golden = _golden()
    provider = _live_provider(slug)
    got = asyncio.run(provider.embed(golden["sentences"], purpose))
    _assert_parity(slug, purpose, got,
                   [_decode(v) for v in golden["vectors"][slug][purpose]])


@pytest.mark.live_service
@pytest.mark.parametrize("purpose", PURPOSES)
@pytest.mark.parametrize("slug", GOLDEN_SLUGS)
def test_live_single_embed_matches_legacy_golden(slug, purpose):
    """One sentence per provider.embed call (the query and probe shape)."""
    golden = _golden()
    provider = _live_provider(slug)
    got = [asyncio.run(provider.embed([s], purpose))[0] for s in golden["sentences"]]
    _assert_parity(slug, purpose, got,
                   [_decode(v) for v in golden["vectors"][slug][purpose]])


@pytest.mark.live_service
@pytest.mark.parametrize("slug", GOLDEN_SLUGS)
def test_live_split_batch_keeps_each_vector_on_its_text(slug):
    """More texts than one batchEmbedContents call takes (GEMINI_EMBED_BATCH_MAX):
    the golden sentences, placed on both sides of the split, come back at their
    own positions with their own vectors."""
    golden = _golden()
    limit = providers.GEMINI_EMBED_BATCH_MAX
    texts = [f"Filler line {i} of a long snapshot, about nothing in particular."
             for i in range(limit + 4)]
    positions = [0, limit - 2, limit - 1, limit, limit + 3]
    for pos, sentence in zip(positions, golden["sentences"]):
        texts[pos] = sentence
    provider = _live_provider(slug)
    got = asyncio.run(provider.embed(texts, "document"))
    assert len(got) == len(texts)
    _assert_parity(slug, "document (split batch)", [got[p] for p in positions],
                   [_decode(v) for v in golden["vectors"][slug]["document"]])


@pytest.mark.live_service
@pytest.mark.parametrize("slug", GOLDEN_SLUGS)
def test_live_full_batch_of_chunk_sized_texts_is_accepted(slug):
    """The heaviest call a mint or a rebuild makes: GEMINI_EMBED_BATCH_MAX texts
    in one request, chunk-sized (the chunker's 1024-token window on the chars/2
    floor) plus one whole-snapshot text at the clamp budget. The API takes it
    as one request."""
    from Orchestrator.embeddings.chunker import chunks_for_snapshot

    limit = providers.GEMINI_EMBED_BATCH_MAX
    line = ("Step {i}: the operator re-ran the migration for store {i} and the "
            "gap check reported {i} missing ids before the cutover.\n")
    body = "".join(line.format(i=i) for i in range(4000))
    chunks = chunks_for_snapshot(body, model_key=slug)
    assert len(chunks) >= limit, "the synthetic snapshot must fill a batch"
    texts = chunks[:limit]  # chunks[0] is the whole text, clamped by the provider
    provider = _live_provider(slug)
    got = asyncio.run(provider.embed(texts, "document"))
    assert len(got) == limit
    assert all(len(vec) == EMBEDDING_MODELS[slug]["dims"] for vec in got)
