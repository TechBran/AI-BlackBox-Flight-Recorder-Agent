#!/usr/bin/env python3
"""Stratified labeled query set for retrieval eval (WI-6 Phase A / audit A10).

Builds eval/labeled_set.jsonl: ~500 (query, gold_snap_id) pairs sampled from the
LIVE snapshot index, plus the human-verified holdout pairs from
Orchestrator/tests/test_retrieval_golden.py. The bench runner (eval/run_bench.py)
scores these through the FULL retrieve() pipeline; the numbers GATE the M6
chunk-store swap.

Protocol (ALL of audit A10 is encoded here, not in operator memory):
  * Load the snapshot index exactly the way production does
    (Orchestrator.fossils.load_snapshot_index); decode bodies by byte offset
    from the volume, READ-ONLY. Nothing here writes to the ledger or mints.
  * Sample ~500 snapshots STRATIFIED by decoded-char length band
    (<6k / 6-10k / >10k) with the >10k band OVERSAMPLED (target >=150 — it is
    ~9%% of the corpus but it is where 10k-truncation recall loss is proven),
    spread across age quartiles and operators proportionally within each band.
  * Per snapshot: ONE query generated from a RANDOM 1-2k-char span at a RANDOM
    offset (uniform over the body). span_start/span_end and position_third
    (head/middle/tail by span midpoint) are recorded — this is the leakage
    guard that makes a chunk-arm win attributable to tail recovery rather than
    query-gen bias.
  * Query generation reuses the digest_ab prompt pattern (QUERY_SYS /
    QUERY_USER_TMPL, claude-haiku-4-5, direct Anthropic SDK — NON-minting; we
    never touch /chat) including its "Do NOT copy distinctive phrases
    verbatim" instruction.
  * Deterministic: sampling uses seed 20260702; each snapshot's span uses a
    per-snapshot RNG seeded from (seed, snap_id) so spans don't shift when the
    corpus grows. Generated queries are CACHED in the output jsonl keyed by
    (gold_snap_id, span_start, span_end); a re-run only generates new rows.
  * Every 10th generated row is flagged "validate": true (hand-validation set).
  * Holdout: the human-verified SINGLE_EVENT_GOLD pairs are appended with
    source="holdout" and operator="system" (mirroring the golden test's call).
  * Cost guard: aborts if the projected LLM spend exceeds $10.
  * Secret scan: generated queries are scanned for key-like strings before
    being written; hits are DROPPED and reported.

Run (from the repo root):
    Orchestrator/venv/bin/python eval/build_labeled_set.py [--dry-run] [--n 500]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.chdir(REPO)  # Orchestrator.config reads config.ini relative to CWD
sys.path.insert(0, str(REPO))

from Orchestrator.config import VOL_PATH  # noqa: E402
from Orchestrator.fossils import load_snapshot_index  # noqa: E402
# Reuse the digest_ab prompt machinery (plan says import, don't fork).
from benchmarks.digest_ab.run_ab import (  # noqa: E402
    LLM_MODEL,
    QUERY_SYS,
    QUERY_USER_TMPL,
    _anthropic_client,
    _llm_text,
)
# The 3 human-verified golden pairs (query, gold snap_id) — single source of truth.
from Orchestrator.tests.test_retrieval_golden import SINGLE_EVENT_GOLD  # noqa: E402

OUT_PATH = REPO / "eval" / "labeled_set.jsonl"

SEED = 20260702
DEFAULT_N = 500
OVERSAMPLE_10K_TARGET = 150   # >=150 rows from the >10k band (audit A10)
MIN_BODY_CHARS = 300          # too short for a meaningful 1-2k span
SPAN_MIN, SPAN_MAX = 1000, 2000

# claude-haiku-4-5 pricing (skill-verified 2026-07-02): $1/MTok in, $5/MTok out.
HAIKU_IN_PER_MTOK = 1.0
HAIKU_OUT_PER_MTOK = 5.0
COST_ABORT_USD = 10.0

SECRET_PATTERNS = [
    re.compile(p) for p in (
        r"sk-[A-Za-z0-9_-]{16,}",
        r"AIza[0-9A-Za-z_-]{30,}",
        r"xox[baprs]-[A-Za-z0-9-]{10,}",
        r"ghp_[A-Za-z0-9]{20,}",
        r"github_pat_[A-Za-z0-9_]{20,}",
        r"AKIA[0-9A-Z]{16}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"whsec_[A-Za-z0-9]{16,}",
        r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.",  # JWT
    )
]


def band_of(n_chars: int) -> str:
    if n_chars < 6000:
        return "<6k"
    if n_chars <= 10000:
        return "6-10k"
    return ">10k"


def largest_remainder(total: int, weights: dict) -> dict:
    """Proportional integer allocation of `total` across `weights` (deterministic)."""
    wsum = sum(weights.values())
    if wsum == 0 or total <= 0:
        return {k: 0 for k in weights}
    raw = {k: total * w / wsum for k, w in weights.items()}
    base = {k: math.floor(v) for k, v in raw.items()}
    leftover = total - sum(base.values())
    order = sorted(weights, key=lambda k: (-(raw[k] - base[k]), str(k)))
    for k in order[:leftover]:
        base[k] += 1
    return base


def cap_and_redistribute(alloc: dict, capacity: dict) -> dict:
    """Cap each cell at its capacity; hand the freed count to cells with room."""
    alloc = dict(alloc)
    deficit = 0
    for k in sorted(alloc, key=str):
        if alloc[k] > capacity[k]:
            deficit += alloc[k] - capacity[k]
            alloc[k] = capacity[k]
    while deficit > 0:
        movable = [k for k in sorted(alloc, key=str) if alloc[k] < capacity[k]]
        if not movable:
            break  # band exhausted — final total may fall short; reported below
        for k in movable:
            if deficit == 0:
                break
            alloc[k] += 1
            deficit -= 1
    return alloc


def load_corpus():
    """(records, quartile_bounds): one record per index entry, bodies decoded once."""
    index = load_snapshot_index()
    if not index:
        raise SystemExit("snapshot index empty — run from the repo root on a live box")
    vol = VOL_PATH.read_bytes()  # READ-ONLY; never written
    vol_len = len(vol)

    # Age quartiles over the WHOLE corpus (ISO timestamps sort lexicographically).
    ts_sorted = sorted((m.get("timestamp") or "", sid) for sid, m in index.items())
    rank = {sid: i for i, (_ts, sid) in enumerate(ts_sorted)}
    n = len(ts_sorted)

    def quartile(sid: str) -> str:
        q = min(3, rank[sid] * 4 // n)
        return f"Q{q + 1}"  # Q1 = oldest 25% ... Q4 = newest

    records = {}
    for sid in sorted(index):
        m = index[sid]
        start, end = m.get("byte_start", 0), m.get("byte_end", 0)
        if not (0 <= start < end <= vol_len):
            continue
        body = vol[start:end].decode("utf-8", errors="replace")
        records[sid] = {
            "body": body,
            "chars": len(body),
            "band": band_of(len(body)),
            "operator": m.get("operator") or "",
            "age_quartile": quartile(sid),
        }
    return records


def stratified_sample(records: dict, n_target: int, rng: random.Random):
    eligible = {sid: r for sid, r in records.items() if r["chars"] >= MIN_BODY_CHARS}
    band_counts = Counter(r["band"] for r in eligible.values())

    # Band targets: >10k oversampled to >=OVERSAMPLE_10K_TARGET, remainder split
    # proportionally between the two smaller bands.
    t10 = min(OVERSAMPLE_10K_TARGET, band_counts[">10k"])
    rest = largest_remainder(
        n_target - t10, {b: band_counts[b] for b in ("<6k", "6-10k")}
    )
    band_targets = {">10k": t10, **rest}

    sampled = []
    for band in sorted(band_targets):
        members = defaultdict(list)  # (age_quartile, operator) -> [sid]
        for sid in sorted(eligible):
            r = eligible[sid]
            if r["band"] == band:
                members[(r["age_quartile"], r["operator"])].append(sid)
        capacity = {cell: len(v) for cell, v in members.items()}
        alloc = largest_remainder(band_targets[band], capacity)
        alloc = cap_and_redistribute(alloc, capacity)
        for cell in sorted(alloc, key=str):
            take = alloc[cell]
            if take > 0:
                sampled.extend(rng.sample(members[cell], take))
    return sorted(sampled), band_targets, band_counts


def make_span(sid: str, body: str):
    """Random 1-2k-char span at a uniform offset; per-snapshot deterministic RNG."""
    srng = random.Random(f"{SEED}:{sid}")
    span_len = srng.randint(SPAN_MIN, SPAN_MAX)
    if len(body) <= span_len:
        start, end = 0, len(body)
    else:
        start = srng.randint(0, len(body) - span_len)
        end = start + span_len
    mid_frac = ((start + end) / 2) / max(1, len(body))
    third = "head" if mid_frac < 1 / 3 else ("middle" if mid_frac < 2 / 3 else "tail")
    return start, end, third


def scan_secrets(text: str):
    return [p.pattern for p in SECRET_PATTERNS if p.search(text)]


def load_cache():
    """(gold_snap_id, span_start, span_end) -> query, from a previous run's output."""
    cache = {}
    if OUT_PATH.exists():
        for line in OUT_PATH.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("source") == "generated" and row.get("query"):
                cache[(row["gold_snap_id"], row["span_start"], row["span_end"])] = row["query"]
    return cache


def write_rows(rows: list):
    tmp = OUT_PATH.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, OUT_PATH)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=DEFAULT_N)
    ap.add_argument("--dry-run", action="store_true",
                    help="build the sample + strata report, no LLM calls, no file write")
    args = ap.parse_args()

    records = load_corpus()
    rng = random.Random(SEED)
    sampled, band_targets, band_counts = stratified_sample(records, args.n, rng)

    # Assemble generated rows (query filled from cache or LLM below).
    rows = []
    for i, sid in enumerate(sampled):
        r = records[sid]
        start, end, third = make_span(sid, r["body"])
        rows.append({
            "query": None,
            "gold_snap_id": sid,
            "span_start": start,
            "span_end": end,
            "position_third": third,
            "length_band": r["band"],
            "operator": r["operator"],
            "age_quartile": r["age_quartile"],
            "validate": (i % 10 == 0),
            "source": "generated",
        })

    # Strata report.
    print(f"[sample] {len(rows)} snapshots (target {args.n}); corpus bands {dict(band_counts)}")
    print(f"[sample] band targets: {band_targets}")
    for key in ("length_band", "position_third", "age_quartile"):
        print(f"[sample] by {key}: {dict(sorted(Counter(x[key] for x in rows).items()))}")
    ops = Counter(x["operator"] for x in rows)
    print(f"[sample] top operators: {ops.most_common(8)}")
    print(f"[sample] validate-flagged: {sum(x['validate'] for x in rows)}")

    cache = load_cache()
    missing = [x for x in rows
               if (x["gold_snap_id"], x["span_start"], x["span_end"]) not in cache]
    print(f"[cache] {len(rows) - len(missing)} cached queries reused; {len(missing)} to generate")

    # Cost guard (chars/3.5 ~ conservative token estimate for prose+markup).
    est_in = sum(len(QUERY_SYS) + len(QUERY_USER_TMPL) + (x["span_end"] - x["span_start"])
                 for x in missing) / 3.5
    est_out = len(missing) * 60
    projected = est_in / 1e6 * HAIKU_IN_PER_MTOK + est_out / 1e6 * HAIKU_OUT_PER_MTOK
    print(f"[cost] projected {LLM_MODEL} spend: ~${projected:.2f} "
          f"({len(missing)} calls, ~{int(est_in)} in / {est_out} out tokens)")
    if projected > COST_ABORT_USD:
        raise SystemExit(f"projected cost ${projected:.2f} > ${COST_ABORT_USD} guard — aborting")

    if args.dry_run:
        print("[dry-run] no LLM calls, no file written")
        return

    vol_bodies = records  # alias for clarity
    client = _anthropic_client() if missing else None
    dropped = []
    generated = 0
    for x in rows:
        key = (x["gold_snap_id"], x["span_start"], x["span_end"])
        if key in cache:
            x["query"] = cache[key]
            continue
        span = vol_bodies[x["gold_snap_id"]]["body"][x["span_start"]:x["span_end"]]
        q = None
        for attempt in range(3):
            try:
                q = _llm_text(client, QUERY_SYS, QUERY_USER_TMPL.format(answer=span), 120)
                break
            except Exception as e:  # noqa: BLE001 - retry with backoff
                print(f"  [warn] {x['gold_snap_id']}: attempt {attempt + 1} failed: {e}")
                time.sleep(2.0 * (attempt + 1))
        if not q:
            dropped.append((x["gold_snap_id"], "llm_failed"))
            continue
        hits = scan_secrets(q)
        if hits:
            dropped.append((x["gold_snap_id"], f"secret_pattern:{hits}"))
            continue
        x["query"] = q
        cache[key] = q
        generated += 1
        if generated % 25 == 0:
            done = [r for r in rows if r["query"]]
            write_rows(done)  # checkpoint — a crash loses at most 25 generations
            print(f"  [{generated}/{len(missing)}] checkpointed {len(done)} rows")
        time.sleep(0.05)

    final_rows = [x for x in rows if x["query"]]

    # Holdout: human-verified pairs, retrieved as the golden test retrieves them
    # (operator="system"); span fields are null — no span protocol applies.
    for query, gold_id in SINGLE_EVENT_GOLD:
        r = records.get(gold_id)
        final_rows.append({
            "query": query,
            "gold_snap_id": gold_id,
            "span_start": None,
            "span_end": None,
            "position_third": None,
            "length_band": r["band"] if r else None,
            "operator": "system",
            "age_quartile": r["age_quartile"] if r else None,
            "validate": False,
            "source": "holdout",
        })

    write_rows(final_rows)
    print(f"[done] wrote {OUT_PATH} — {len(final_rows)} rows "
          f"({len(final_rows) - len(SINGLE_EVENT_GOLD)} generated + "
          f"{len(SINGLE_EVENT_GOLD)} holdout); {generated} newly generated")
    if dropped:
        print(f"[dropped] {len(dropped)} rows: {dropped}")


if __name__ == "__main__":
    main()
