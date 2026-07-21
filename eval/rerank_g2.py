#!/usr/bin/env python3
"""G2 gate: Qwen3-Reranker-8B @ Q8_0 GGUF validity + rank-order agreement (D13).

Broken reranker GGUFs (missing cls.output.weight) return degenerate ~1e-28
scores. This harness scores each golden query's passages through the SERVED
llama.cpp /v1/rerank (the same wire path Orchestrator/rerank.py:_score_localstack
uses — it reuses rerank._scatter_relevance_scores to parse), and gates on:

  * no degenerate scores (max |score| and the spread must clear a floor), AND
  * relevant passages rank above the hard-negatives on the served scores
    (min(relevant) > max(negative)), AND
  * (optional --hf-reference) per-query Spearman rank agreement between the
    served scores and a HuggingFace transformers reference >= --rank-agreement-min.

D13 (sequential retrieval) adds a SECOND measurement, separate from the validity
gate: --measure-swap times the per-search intra-group swap overhead (force the
embedder resident → time a rerank call that must evict the embedder and cold-load
the 8B reranker → subtract a warm rerank). This is the ~6–12s "swap cost on every
search" the design accepts; it is REPORTED (not pass/failed) so G5 can sign off.

The primary gate (degenerate + separation) needs only `requests`; --hf-reference
additionally needs torch + transformers (present on the GPU box's reranker venv).

Run (from the repo root, on the GPU box after serving the member):
    Orchestrator/venv/bin/python eval/rerank_g2.py
    Orchestrator/venv/bin/python eval/rerank_g2.py --base-url http://127.0.0.1:9098/v1
    Orchestrator/venv/bin/python eval/rerank_g2.py --measure-swap   # D13 per-search swap overhead
    Orchestrator/venv/bin/python eval/rerank_g2.py --hf-reference \
        --hf-model-dir ./llama.cpp/Qwen3-Reranker-8B

Writes eval/results/{date}-rerank-g2.{md,json} and exits non-zero on ANY failure
(a failed gate = STOP: do not let the wizard select the on-box reranker).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from Orchestrator import rerank  # noqa: E402

GOLDEN = REPO / "eval" / "rerank_golden.jsonl"
RESULTS_DIR = REPO / "eval" / "results"
SLUG = "qwen3-reranker-8b-local"

# A working reranker separates relevant from off-topic by far more than this; a
# broken GGUF collapses everything to ~1e-28. Both the magnitude and the spread
# must clear the floor.
_DEGENERATE_FLOOR = 1e-6


# ── pure logic (unit-tested in Orchestrator/tests/test_rerank_g2_harness.py) ──

def is_degenerate(scores: list[float]) -> bool:
    """True if the scores look like a broken conversion: empty, all near zero,
    or with no meaningful spread (the ~1e-28 signature)."""
    if not scores:
        return True
    mag = max(abs(s) for s in scores)
    spread = max(scores) - min(scores)
    return mag < _DEGENERATE_FLOOR or spread < _DEGENERATE_FLOOR


def separation_ok(scores: list[float], n_relevant: int) -> bool:
    """documents were built as relevant(n_relevant) + hard_negative(rest); a
    valid reranker scores every relevant passage above every negative. Not
    enough info to judge (no relevants, or all-relevant) is not a failure."""
    if n_relevant <= 0 or n_relevant >= len(scores):
        return True
    rel = scores[:n_relevant]
    neg = scores[n_relevant:]
    return min(rel) > max(neg)


def _rank(xs: list[float]) -> list[float]:
    """Average-rank of each element (ties share their mean rank)."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(a: list[float], b: list[float]) -> float:
    """Spearman rank correlation, pure Python (no scipy). nan on a length
    mismatch, <2 points, or a zero-variance (constant) series."""
    if len(a) != len(b) or len(a) < 2:
        return float("nan")
    ra, rb = _rank(a), _rank(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    va = sum((ra[i] - ma) ** 2 for i in range(n))
    vb = sum((rb[i] - mb) ** 2 for i in range(n))
    if va == 0 or vb == 0:
        return float("nan")
    return cov / (va ** 0.5 * vb ** 0.5)


# ── I/O (exercised as the G2 gate on the GPU box, not in CI) ──────────────────

def load_golden() -> list[dict]:
    rows = []
    for line in GOLDEN.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def score_via_endpoint(base_url: str, model_id: str, instruction: str,
                       query: str, documents: list[str],
                       timeout_s: float = 30.0) -> "list[float] | None":
    """POST the llama.cpp /v1/rerank shape and parse via the SAME scatter the
    production path uses (rerank._scatter_relevance_scores). None on any anomaly
    — including an unreachable/refused endpoint, so main()'s no-response FAIL
    report is written instead of a raw traceback (the exact case this gate
    exists to survive)."""
    try:
        resp = requests.post(
            base_url.rstrip("/") + "/rerank",
            json={"model": model_id, "query": instruction + query,
                  "documents": list(documents)},
            timeout=timeout_s,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return rerank._scatter_relevance_scores(resp.json(), len(documents))


def score_via_hf(model_dir: str, instruction: str, query: str,
                 documents: list[str]) -> list[float]:
    """HuggingFace transformers reference scores for Qwen3-Reranker (the yes/no
    logit recipe from the model card). Heavy (torch); imported lazily and only
    under --hf-reference. Returns P(yes) per document."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(model_dir, padding_side="left")
    model = AutoModelForCausalLM.from_pretrained(model_dir).eval()
    tok_yes = tok.convert_tokens_to_ids("yes")
    tok_no = tok.convert_tokens_to_ids("no")
    prefix = ("<|im_start|>system\nJudge whether the Document meets the "
              "requirements based on the Query and the Instruct provided. Note "
              'that the answer can only be "yes" or "no".<|im_end|>\n'
              "<|im_start|>user\n")
    suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    out: list[float] = []
    with torch.no_grad():
        for doc in documents:
            body = (f"<Instruct>: {instruction}\n<Query>: {query}\n"
                    f"<Document>: {doc}")
            enc = tok(prefix + body + suffix, return_tensors="pt")
            logits = model(**enc).logits[0, -1]
            yn = torch.softmax(
                torch.stack([logits[tok_no], logits[tok_yes]]), dim=0)
            out.append(float(yn[1]))
    return out


def measure_swap_overhead(base_url: str, model_id: str, instruction: str,
                          documents: list[str],
                          embed_model_id: str = "embed-qwen3-8b",
                          timeout_s: float = 120.0) -> dict:
    """D13 per-search swap overhead. The retrieval group is sequential
    (swap: true), so a search does embed → EVICT embedder → cold-load the 8B
    reranker → score. Force the embedder resident, then time the first rerank
    (which pays the intra-group swap) minus a warm rerank (reranker already
    loaded). REPORTED, not gated — this is the accepted ~6–12s/search cost that
    G5 signs off on."""
    import time
    # 1. make the embedder the resident retrieval member (evicts the reranker)
    try:
        requests.post(base_url.rstrip("/") + "/embeddings",
                      json={"model": embed_model_id, "input": "warm the embedder"},
                      timeout=timeout_s)
    except requests.RequestException:
        return {"error": "embed warm-up failed (is embed-qwen3-8b served?)"}
    body = {"model": model_id, "query": instruction + "swap timing probe",
            "documents": list(documents)}
    # 2. first rerank: pays evict-embedder + cold-load-8B-reranker
    try:
        t0 = time.perf_counter()
        cold = requests.post(base_url.rstrip("/") + "/rerank", json=body, timeout=timeout_s)
        cold_s = time.perf_counter() - t0
        # 3. second rerank: reranker already resident (steady-state scoring only)
        t0 = time.perf_counter()
        warm = requests.post(base_url.rstrip("/") + "/rerank", json=body, timeout=timeout_s)
        warm_s = time.perf_counter() - t0
    except requests.RequestException as exc:
        return {"error": f"rerank probe failed (endpoint unreachable: {exc})"}
    if cold.status_code != 200 or warm.status_code != 200:
        return {"error": f"rerank probe non-200 (cold {cold.status_code}, warm {warm.status_code})"}
    return {"cold_s": round(cold_s, 3), "warm_s": round(warm_s, 3),
            "swap_overhead_s": round(cold_s - warm_s, 3),
            "embed_model_id": embed_model_id}


def main(argv=None) -> int:
    import os
    os.chdir(REPO)  # results path + any config reads are repo-relative
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=None,
                    help="llama-swap /v1 front door (default: local_stack, else :9098/v1)")
    ap.add_argument("--hf-reference", action="store_true",
                    help="also compute the HuggingFace reference + Spearman agreement")
    ap.add_argument("--hf-model-dir", default=None,
                    help="path to the Qwen3-Reranker-8B HF checkpoint (for --hf-reference)")
    ap.add_argument("--measure-swap", action="store_true",
                    help="D13: also measure the per-search intra-group embed->rerank swap overhead (reported, not gated)")
    ap.add_argument("--rank-agreement-min", type=float, default=0.9,
                    help="min mean per-query Spearman vs HF reference (with --hf-reference)")
    ap.add_argument("--out-date", default=date.today().isoformat())
    args = ap.parse_args(argv)

    # Deliberately reads the CANONICAL registry entry (model_id + query_instruction)
    # rather than rerank.py's config-override resolution (the [rerank] query_instruction/
    # model fallbacks): this is a validity gate on the canonical member, not on a box's
    # locally-overridden live settings.
    entry = rerank.RERANK_MODELS[SLUG]
    instruction = entry["query_instruction"]
    model_id = entry["model_id"]
    base_url = args.base_url or rerank._localstack_base_url() or "http://127.0.0.1:9098/v1"

    rows = load_golden()
    per_query = []
    all_pass = True
    spearmans = []
    for row in rows:
        documents = list(row["relevant"]) + list(row["hard_negative"])
        n_rel = len(row["relevant"])
        served = score_via_endpoint(base_url, model_id, instruction,
                                    row["query"], documents)
        rec = {"id": row["id"], "n_relevant": n_rel,
               "served_scores": served}
        if served is None:
            rec["state"] = "no-response"
            all_pass = False
        else:
            degen = is_degenerate(served)
            sep = separation_ok(served, n_rel)
            rec["degenerate"] = degen
            rec["separation_ok"] = sep
            ok = (not degen) and sep
            if args.hf_reference:
                if not args.hf_model_dir:
                    print("--hf-reference requires --hf-model-dir", file=sys.stderr)
                    return 2
                ref = score_via_hf(args.hf_model_dir, instruction,
                                   row["query"], documents)
                rho = spearman(served, ref)
                rec["hf_scores"] = ref
                rec["spearman"] = rho
                spearmans.append(rho)
                ok = ok and (rho == rho and rho >= args.rank_agreement_min)  # rho==rho excludes nan
            rec["state"] = "pass" if ok else "fail"
            all_pass = all_pass and ok
        per_query.append(rec)

    mean_rho = (sum(spearmans) / len(spearmans)) if spearmans else None

    # D13: optional per-search intra-group swap-overhead measurement (reported,
    # NOT part of the pass/fail gate). Uses the first golden row's documents.
    swap = None
    if args.measure_swap and rows:
        first = rows[0]
        swap = measure_swap_overhead(
            base_url, model_id, instruction,
            list(first["relevant"]) + list(first["hard_negative"]))

    report = {
        "date": args.out_date, "slug": SLUG, "model_id": model_id,
        "base_url": base_url, "hf_reference": args.hf_reference,
        "rank_agreement_min": args.rank_agreement_min,
        "mean_spearman": mean_rho,
        "swap_overhead": swap,
        "pass": all_pass, "queries": per_query,
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / f"{args.out_date}-rerank-g2.json"
    json_path.write_text(json.dumps(report, indent=2))

    lines = [f"# G2 reranker validity — {args.out_date}", "",
             f"- model: `{model_id}` @ `{base_url}`",
             f"- HF reference: {args.hf_reference}"
             + (f" (mean Spearman {mean_rho:.3f}, min {args.rank_agreement_min})"
                if mean_rho is not None else ""),
             (f"- D13 per-search swap overhead: {swap['swap_overhead_s']}s "
              f"(cold {swap['cold_s']}s − warm {swap['warm_s']}s)"
              if swap and "swap_overhead_s" in swap
              else (f"- D13 swap overhead: {swap['error']}" if swap
                    else "- D13 swap overhead: not measured (pass --measure-swap)")),
             f"- **overall: {'PASS' if all_pass else 'FAIL'}**", "",
             "| query | state | degenerate | separation | spearman |",
             "|---|---|---|---|---|"]
    for r in per_query:
        lines.append(
            f"| {r['id']} | {r.get('state')} | {r.get('degenerate', '-')} "
            f"| {r.get('separation_ok', '-')} | "
            f"{r.get('spearman', '-') if 'spearman' in r else '-'} |")
    md_path = RESULTS_DIR / f"{args.out_date}-rerank-g2.md"
    md_path.write_text("\n".join(lines) + "\n")

    print(f"G2 {'PASS' if all_pass else 'FAIL'} — wrote {md_path} / {json_path}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
