# M6f — Chunk-Store Build, Gate & Cutover Runbook (gemini-embedding-2 → schema v2)

Executed step-by-step; every step records its evidence here or in `eval/results/`.
Design: plan M6f + audit A5/A6. Preconditions and gates are HARD — a failed gate stops the
sequence and the box keeps serving v1 (nothing before step 6 changes production behavior).

## Preconditions (all must hold before step 1)
- [ ] M6d hardening landed: `_run_engine` schema-aware fill (the A4 side door closed —
      plain migrate/watcher recovery onto a v2 store lands proper chunk groups), in-service
      `start_rebuild` trigger, CLI guard tests service-independent. Guarded suite 0-failed.
- [ ] Full M6 code batch pushed to origin (inert until a v2 store is active).
- [ ] `GET /embeddings/status`: health ok, active `gemini-embedding-2` schema 1, missing 0.
- [ ] Disk: ≥2× the current store size free (v2 ≈ 2.5× rows of 93MB ≈ 235MB + v1 retained).

## Step 1 — Build (in-service, single writer, mints continue)
`POST /embeddings/migrate {"target":"gemini-embedding-2","rebuild":true}`
- Candidate lands at `Manifest/embeddings/_build/gemini-embedding-2` (schema 2, invisible to
  status/list/watcher by construction).
- Monitor: `migration_state.json` (kind=rebuild, activate=false) + journalctl
  `[MIGRATE] rebuild gemini-embedding-2: {done}/{total} snapshots ({rows} rows)`.
- Expected: ~7.6k snapshots → ~19-23k rows, cloud cost ~$2–3, well under 1h.
- Interruption at any point is safe: boot-resume stays build-only; group appends are
  crash-atomic (partial groups heal to absent and re-diff).

## Step 2 — Calibration on the candidate (chunk-max score bands)
Update `scripts/calibrate_threshold.py`: accept `--store-dir/--schema` override; report BOTH
the strong-query relevance band AND a noise band (3+ deliberately off-topic queries) over
collapsed per-snapshot max cosines. Run against the candidate. Output feeds M9's per-model
`junk_floor` values — record here:
- relevance band (top-10 hits): __
- noise band (off-topic top-1s): __
- current junk_floor 0.40 verdict under chunk-max: __ (looser/tighter; M9 decides the change,
  flag-gated — nothing changes at cutover)

## Step 3 — Bench gate (the swap authorization)
`eval/run_bench.py` chunk arm: store = candidate (get_store(slug, base_dir=_build, schema=2)),
query vectors = gemini-embedding-2 (active model — same model, so retrieve()'s internal embed
is CORRECT for this arm; the query_vector seam is not needed), include_keyword both arms.
GATES vs the corrected w=0.005 baselines (eval/results/2026-07-02-recency-sweep.md):
- [ ] hybrid r@10 ≥ 0.4891 (no overall regression)
- [ ] semantic r@10 ≥ 0.4970
- [ ] **>10k-char band r@10 > 0.6333 (MUST IMPROVE — the truncation population is the point)**
- [ ] tail-third recall improves vs baseline (position-stratified table — the A10 leakage
      guard makes this attributable)
- [ ] 3 human holdout pairs still hit (no regression on human-verified golds)
- [ ] goldens + lean-profile via store-override fixtures: `test_retrieval_golden.py` +
      `test_local_lean_retrieval.py` pass pointed at the candidate (monkeypatch fixture run,
      documented command)
Record the full table in `eval/results/2026-07-0X-chunk-gate.md`. ANY gate fails → STOP,
investigate, no cutover; the build artifact keeps (re-diff refreshes it cheaply).

## Step 4 — Cutover (explicit, service stopped, ~2 min window)
```bash
sudo systemctl stop blackbox.service
cd "Manifest/embeddings"
mv gemini-embedding-2 gemini-embedding-2.pre-chunk        # v1 retained = rollback asset
mv _build/gemini-embedding-2 gemini-embedding-2           # candidate goes live
sudo systemctl start blackbox.service                      # ~90s warm-up
```
- active.json untouched (same slug — that's the point of the dir swap).
- Post-start: `GET /embeddings/status` → active gemini-embedding-2 **schema 2**, rows ≈
  2.5× count, missing == (snapshots minted during the stop window; watch them heal/catch-up).

## Step 5 — Post-cutover verification (all must pass)
- [ ] `test_retrieval_golden.py` live: 7/7
- [ ] `test_local_lean_retrieval.py` live: pass (phone profile)
- [ ] `/debug/context` on a >10k-char snapshot's tail topic: that snapshot surfaces
- [ ] Catch-up: `POST /embeddings/migrate {"target":"gemini-embedding-2"}` (plain diff-fill —
      SAFE now: engine is schema-aware, lands chunk groups) → missing → 0
- [ ] Organic mint check: the M6 milestone snapshot itself — journalctl must show the
      group-append line `[INDEX] Stored {n}-chunk group for SNAP-... ` and
      `[EMBEDDING] Successfully generated embedding ({dims} dimensions, {n} chunks)`
- [ ] MCP `search_snapshots` + one voice session context + one chat turn: sane results
- [ ] 24h watch item: journalctl `[VECSTORE]`/`[EMBEDDING]` errors; memory RSS (matrix
      ~235MB vs 93MB — leak-hunt track awareness)

## Rollback (staged, tested direction)
```bash
sudo systemctl stop blackbox.service
cd "Manifest/embeddings"
mv gemini-embedding-2 gemini-embedding-2.chunked-rollback
mv gemini-embedding-2.pre-chunk gemini-embedding-2
sudo systemctl start blackbox.service
# IMMEDIATELY diff-fill the mint gap (v1 target = single-vector fill, correct by schema):
curl -X POST localhost:9091/embeddings/migrate -d '{"target":"gemini-embedding-2"}'
```
Mint path is schema-derived (6c): after rollback, mints write single vectors again with
zero code/flag changes. Never delete either store dir.

## Non-goals at cutover (explicitly deferred)
- junk_floor change (M9, flag-gated, uses step-2 bands)
- delivery-cap removal (M7), phone windowing (M8)
- non-active stores stay v1 untouched (rollback assets / lazy rebuild on switch)
