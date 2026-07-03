#!/usr/bin/env python3
"""Measure a sensible semantic_threshold / junk_floor for an embedding store.

Default (no args): today's behavior, byte-identical — embeds a fixed query set
against the ACTIVE store and reports the top-k score distribution so an
operator can pick a floor that keeps strong matches well clear of the cut.

Store-override mode (M6f runbook step 2 / audit A8): point --store-dir at a
specific store — e.g. the schema-2 chunk candidate under
Manifest/embeddings/_build — and the report runs the same query set through
store.search (which on v2 collapses chunk hits to per-snapshot MAX-cosine
best chunks) and adds:
  * the relevance band as today (strong-query top-10 hits), PLUS
  * a NOISE band: deliberately off-topic queries' top-1/top-5 scores, PLUS
  * a floor-guidance line (worst-relevant minus margin vs the noise ceiling)
    feeding M9's per-model junk_floor values.

ALWAYS pass --schema 2 for the candidate (never autodetect). Read-only in
every mode: store.search only, and a --store-dir that has no existing store
is refused (get_store would otherwise CREATE one).

Usage:
    python scripts/calibrate_threshold.py
    python scripts/calibrate_threshold.py \
        --store-dir Manifest/embeddings/_build --schema 2
"""
import argparse, asyncio, json, statistics, sys
from pathlib import Path
sys.path.insert(0, ".")
from Orchestrator.embeddings import search as S
from Orchestrator.embeddings.store import get_active_slug, get_store

QUERIES = [
    "embeddings model switch reembed snapshot volume",
    "nav2 costmap inflation tuning robot",
    "voice agent streaming speech to text",
    "control phone on-device gemma delegate",
    "google workspace docs sheets integration",
    "memory leak restart oom recycle",
    "android audiorecord release race crash",
    "tts voice catalog openai gemini elevenlabs",
    "tailscale security perimeter operator auth",
    "checkpoint mint auto embedding searchable",
]

# Deliberately OFF-TOPIC for this corpus: their top-1 scores are what pure
# noise looks like under the store's scoring (chunk-max on v2), i.e. the
# ceiling a junk_floor must sit above.
NOISE_QUERIES = [
    "best chocolate cake frosting recipe",
    "premier league transfer rumors this week",
    "how to knit a sweater for beginners",
    "celebrity gossip red carpet dresses",
]

MARGIN = 0.05  # keep-clear margin below the worst relevant hit


def _embed_override(slug: str, texts: list) -> list:
    """Query vectors via the production provider layer for THIS slug (the
    override store may belong to a non-active model; the active-model
    generate_embedding_sync would embed garbage for it)."""
    from Orchestrator.embeddings.providers import get_provider
    return asyncio.run(get_provider(slug).embed(list(texts), "query"))


def _resolve_store(store_dir: str, slug: str, schema):
    """Open an EXISTING store only. Accepts the base dir containing <slug>/
    or the store dir itself; refuses to create (read-only contract)."""
    p = Path(store_dir).resolve()
    if (p / slug / "meta.json").is_file():
        base = p
    elif p.name == slug and (p / "meta.json").is_file():
        base = p.parent
    else:
        raise SystemExit(
            f"no existing store for slug {slug!r} under {p} "
            f"(need <dir>/{slug}/meta.json — refusing to create one)"
        )
    return get_store(slug, base_dir=base, schema=schema)


def _relevance_band(store, embed_one) -> tuple:
    """Per-query top-10 lines (today's exact format); -> (all_top, worst_top10)."""
    all_top = []
    worst_top10 = []
    for q in QUERIES:
        qv = embed_one(q)
        if not qv:
            print(f"  EMBED FAILED: {q!r}")
            continue
        hits = store.search(qv, 10, None)
        scores = [s for _, s in hits]
        all_top += scores
        if scores:
            worst_top10.append(min(scores))
        print(f"  {q[:40]:40} top1={scores[0]:.4f} top10={scores[-1]:.4f}")
    return all_top, worst_top10


def _relevance_summary(all_top: list, worst_top10: list) -> None:
    print(f"\nAll top-10 scores: min={min(all_top):.4f} "
          f"p10={statistics.quantiles(all_top, n=10)[0]:.4f} "
          f"median={statistics.median(all_top):.4f} max={max(all_top):.4f}")
    print(f"Worst per-query top-10 floor: min={min(worst_top10):.4f}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="semantic threshold / junk_floor calibration")
    ap.add_argument("--store-dir", default=None,
                    help="calibrate against this store instead of the live active one: "
                         "base dir containing <slug>/ (e.g. Manifest/embeddings/_build) "
                         "or the store dir itself; must already exist")
    ap.add_argument("--schema", type=int, choices=(1, 2), default=None,
                    help="require this store schema (ALWAYS pass 2 for the M6f "
                         "candidate; default autodetects from meta.json)")
    ap.add_argument("--slug", default=None,
                    help="model slug for the override store (default: active slug); "
                         "query embeds use THIS slug's provider")
    args = ap.parse_args(argv)
    override = any(v is not None for v in (args.store_dir, args.schema, args.slug))
    slug = args.slug or get_active_slug()

    if not override:
        # Legacy path — byte-identical to the pre-M6f script.
        store = S.get_active_store()
        print(f"active model: {slug}  store.count={store.count}")
        all_top, worst_top10 = _relevance_band(
            store, lambda q: S.generate_embedding_sync(q, purpose="query"))
        if all_top:
            _relevance_summary(all_top, worst_top10)
            print(f"SUGGESTED threshold = worst_top10_min - 0.05 = {min(worst_top10) - 0.05:.4f} "
                  f"(keep strong matches clear of the cut; never above p10)")
        return

    if args.store_dir:
        store = _resolve_store(args.store_dir, slug, args.schema)
    else:
        store = get_store(slug, schema=args.schema)
    meta = json.loads(store.meta_path.read_text(encoding="utf-8"))
    print(f"store: slug={slug}  schema={store.schema}  rows={store.rows}  "
          f"snapshots={store.snapshots}  generation={meta.get('generation', 0)}")

    texts = QUERIES + NOISE_QUERIES
    vec_by_query = dict(zip(texts, _embed_override(slug, texts)))

    print("relevance band (strong-query top-10 hits, per-snapshot chunk-max cosine on v2):")
    all_top, worst_top10 = _relevance_band(store, vec_by_query.get)

    print("\nnoise band (deliberately off-topic queries):")
    noise_top1 = []
    for q in NOISE_QUERIES:
        qv = vec_by_query.get(q)
        if not qv:
            print(f"  EMBED FAILED: {q!r}")
            continue
        hits = store.search(qv, 5, None)
        scores = [s for _, s in hits]
        if not scores:
            print(f"  {q[:40]:40} no hits")
            continue
        noise_top1.append(scores[0])
        print(f"  {q[:40]:40} top1={scores[0]:.4f} top5={scores[-1]:.4f}")

    if all_top:
        _relevance_summary(all_top, worst_top10)
    if noise_top1:
        print(f"Noise top-1 scores: max={max(noise_top1):.4f} "
              f"median={statistics.median(noise_top1):.4f} min={min(noise_top1):.4f}")
    if all_top and noise_top1:
        ceiling = max(noise_top1)
        worst_rel = min(worst_top10)
        suggested = worst_rel - MARGIN
        print(f"\nnoise ceiling (max off-topic top-1) = {ceiling:.4f}; "
              f"worst relevant top-10 hit = {worst_rel:.4f}; "
              f"band gap = {worst_rel - ceiling:+.4f}")
        line = (f"FLOOR GUIDANCE: junk_floor must sit ABOVE the noise ceiling "
                f"({ceiling:.4f}) and BELOW the worst relevant hit ({worst_rel:.4f}); "
                f"worst-relevant - {MARGIN} margin = {suggested:.4f}")
        if suggested <= ceiling:
            line += "  [WARNING: margin crosses the noise ceiling — bands too close, pick manually]"
        print(line)


if __name__ == "__main__":
    main()
