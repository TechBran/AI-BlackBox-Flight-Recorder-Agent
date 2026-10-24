#!/usr/bin/env python3
"""Record the Gemini embedding golden vectors from the LEGACY SDK (audit rank 71).

One-shot recorder for Orchestrator/tests/golden/gemini_embed_parity.json, the
fixture test_embeddings_gemini_parity.py compares the google-genai port
against. It embeds GOLDEN_SENTENCES through the CURRENT GeminiProvider.embed
(google.generativeai over gRPC, the code every Gemini store on a box was
embedded with) for every gemini registry entry and both purposes, then embeds
them a second time to measure the server's own repeat noise.

It refuses to run once providers.py no longer embeds through
google.generativeai: a golden recorded by the new SDK would compare the port
against itself.

Usage (makes 4 cloud embed calls per gemini registry entry; needs the box's
GOOGLE_API_KEY in the environment or in a .env load_dotenv can find):
    python scripts/record_gemini_embed_golden.py
"""
import asyncio
import base64
import datetime
import json
import math
import os
import struct
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Pin the checkout like the test suite does: a .env load_dotenv() finds above a
# nested worktree may carry another tree's BLACKBOX_ROOT.
os.environ.setdefault("BLACKBOX_ROOT", str(ROOT))
os.chdir(ROOT)  # config.py reads ./config.ini
sys.path.insert(0, str(ROOT))

from Orchestrator import config  # noqa: E402
from Orchestrator.embeddings import providers  # noqa: E402
from Orchestrator.embeddings.registry import EMBEDDING_MODELS  # noqa: E402

OUT = ROOT / "Orchestrator" / "tests" / "golden" / "gemini_embed_parity.json"

GOLDEN_SENTENCES = [
    "Fixed the upload preview CSS bug in the Portal chat composer.",
    "How did we configure the cron scheduler to retry failed jobs?",
    "def embed(texts):\n    return [model.encode(t) for t in texts]",
    "La grabadora de vuelo guarda cada conversación en un libro mayor inmutable.",
    (
        "Snapshot 42: the operator asked for a 3-step plan. Step 1 moves the "
        "vector store to schema 2; step 2 re-embeds 1,600 snapshots at 32 "
        "chunks per call; step 3 flips active.json only after the gap check "
        "passes (0 missing ids, newest 20 present)."
    ),
]


def _encode(vec):
    return base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode("ascii")


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def main() -> int:
    if getattr(providers, "genai", None) is None or \
            providers.genai.__name__ != "google.generativeai":
        print("refusing: providers.py no longer embeds through google.generativeai")
        return 2
    if not config.GOOGLE_API_KEY:
        print("refusing: GOOGLE_API_KEY is not set")
        return 2

    slugs = sorted(s for s, e in EMBEDDING_MODELS.items() if e["provider"] == "gemini")
    vectors, repeat_min = {}, 1.0
    for slug in slugs:
        entry = EMBEDDING_MODELS[slug]
        provider = providers.GeminiProvider(slug, entry)  # fresh, uncached
        vectors[slug] = {"model_id": entry["model_id"], "dims": entry["dims"]}
        for purpose in ("document", "query"):
            first = asyncio.run(provider.embed(GOLDEN_SENTENCES, purpose))
            again = asyncio.run(provider.embed(GOLDEN_SENTENCES, purpose))
            cos = [_cosine(a, b) for a, b in zip(first, again)]
            repeat_min = min(repeat_min, *cos)
            print(f"{slug} {purpose}: repeat cosine min {min(cos):.10f}")
            vectors[slug][purpose] = [_encode(v) for v in first]

    golden = {
        "_meta": {
            "what": "Gemini embeddings of `sentences`, recorded through the legacy "
                    "google.generativeai GeminiProvider before the google-genai port "
                    "(audit rank 71). Historical: never re-record with the new SDK.",
            "recorded_utc": datetime.datetime.now(datetime.timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "recorder": "scripts/record_gemini_embed_golden.py",
            "sdk": f"google-generativeai {metadata.version('google-generativeai')} "
                   f"(google-ai-generativelanguage "
                   f"{metadata.version('google-ai-generativelanguage')}, gRPC)",
            "encoding": "base64 of little-endian float32 (the gRPC response is float32, "
                        "so this is lossless)",
            "task_types": {"document": "retrieval_document", "query": "retrieval_query"},
            "repeat_min_cosine": repeat_min,
        },
        "sentences": GOLDEN_SENTENCES,
        "vectors": vectors,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(golden, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
