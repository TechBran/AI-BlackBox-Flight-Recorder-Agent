#!/usr/bin/env python3
"""Backfill / migrate snapshot embeddings — thin CLI over the migration job.

This script is the OPS FALLBACK, not the canonical path. The canonical way to
switch embedding models or backfill a store is the onboarding wizard /
`POST /embeddings/migrate` on the RUNNING orchestrator: the service runs the
exact same engine (Orchestrator/embeddings/migrate.py), survives restarts,
and — critically — performs the in-memory cutover (`search.swap_active`) in
the process that serves searches.

Use this script when the service is STOPPED (headless box, broken service,
pre-first-boot provisioning). All embed/diff/cutover logic lives in the
engine; this file only parses arguments, validates, and prints.

Cross-process caveats (why the API path is canonical):
  * The migration engine's one-job-at-a-time claim is an IN-PROCESS
    singleton. The only cross-process signal is migration_state.json, which
    this script checks: if it says "running" — possibly the live orchestrator
    mid-migration — the script refuses unless --force. Never --force past a
    genuinely running service-side job: two writers on the same store files
    race each other.
  * The cutover's in-memory half (search.swap_active) happens in THIS
    process. A live orchestrator holds its own active-store handle in module
    state and will NOT observe an active-slug flip done here until it is
    restarted. The script prints a restart reminder after every cutover.

Usage (from the project root):
    python3 Orchestrator/backfill_embeddings.py                  # fill active store
    python3 Orchestrator/backfill_embeddings.py --target <slug>  # migrate + cutover
    python3 Orchestrator/backfill_embeddings.py --rebuild <slug> # chunk-store build (NO cutover)
    python3 Orchestrator/backfill_embeddings.py --list           # ops view, no writes

Liveness guard (M6d audit finding): before any write mode the script probes
GET localhost:9091/embeddings/status. ANY response means the orchestrator is
RUNNING — a second writer in this process would race its appends on the same
store files and can destroy a store — so the script refuses unless --force
(which remains ONLY for a stopped service; see the cross-process caveats).

Exit codes: 0 done/nothing-to-do, 1 unexpected error, 2 unknown slug,
3 state file says a job is running (no --force), 4 job stalled/cancelled,
5 the orchestrator service is alive (no --force).
"""

import argparse
import asyncio
import json
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

# Invoked as `python3 Orchestrator/backfill_embeddings.py`, sys.path[0] is
# Orchestrator/ — put the project root first so the package imports resolve.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Load environment from .env (must happen before importing central config)
load_dotenv()

from Orchestrator import config
from Orchestrator.embeddings import migrate
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import get_active_slug, get_store

BANNER_WIDTH = 70

# Any HTTP response from here = the orchestrator owns the store files.
SERVICE_STATUS_URL = "http://localhost:9091/embeddings/status"
SERVICE_PROBE_TIMEOUT_S = 1.0


def _service_alive(url: str = SERVICE_STATUS_URL,
                   timeout: float = SERVICE_PROBE_TIMEOUT_S) -> bool:
    """True when the orchestrator answers on its port.

    ANY HTTP response counts — even an error status proves a live listener
    that will race this process on the store files. Only a connection-level
    failure (refused, timeout, DNS) means the service is down.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True   # a status-code reply is still a live service
    except Exception:  # noqa: BLE001 — refused/timeout/etc = not running
        return False


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill / migrate snapshot embeddings (ops fallback for a "
            "stopped service; the canonical path is the onboarding wizard / "
            "POST /embeddings/migrate on the running orchestrator)."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--target",
        metavar="SLUG",
        default=None,
        help="registry slug to migrate to (default: the active slug)",
    )
    mode.add_argument(
        "--rebuild",
        metavar="SLUG",
        default=None,
        help=(
            "build a schema-2 chunk store for SLUG under {stores}/_build "
            "(build-only: the active store/pointer is NOT touched; cutover "
            "is a separate explicit step — see the M6f runbook)"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "proceed even if migration_state.json says a job is running "
            "(ONLY safe when the orchestrator service is stopped)"
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print stores, active slug and per-store missing counts, then exit",
    )
    parser.add_argument(
        "--stores-dir",
        metavar="DIR",
        default=None,
        help="override the embeddings stores directory (default: from config)",
    )
    parser.add_argument(
        "--index",
        metavar="FILE",
        default=None,
        help="override the snapshot index path (default: from config)",
    )
    return parser.parse_args(argv)


def _load_index(index_path: Path) -> dict:
    """Snapshot index as a dict; empty when the file is missing/unreadable."""
    try:
        return json.loads(index_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"[BACKFILL] snapshot index not found at {index_path}")
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"[BACKFILL] could not read snapshot index {index_path}: {e}")
        return {}


def _read_state_file(stores_dir: Path) -> dict | None:
    """Persisted migration job dict, or None when absent/unreadable."""
    try:
        persisted = json.loads(
            (stores_dir / migrate.STATE_FILE).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return persisted if isinstance(persisted, dict) else None


def _print_banner(title: str) -> None:
    print("=" * BANNER_WIDTH)
    print(title)
    print("=" * BANNER_WIDTH)


def _list_stores(stores_dir: Path, index_path: Path) -> int:
    """The --list ops view: registry models x on-disk stores x missing counts."""
    index_ids = list(_load_index(index_path).keys())
    active = get_active_slug(base_dir=stores_dir)

    _print_banner("EMBEDDING STORES")
    print(f"  stores dir: {stores_dir}")
    print(f"  index:      {index_path} ({len(index_ids)} snapshots)")
    print(f"  active:     {active}")
    print()
    # count is SNAPSHOT currency on every schema (audit A11); rows is the raw
    # vector-row count (== count on v1, >= count on chunked v2 stores).
    print(f"  {'slug':<32} {'dims':>5} {'count':>7} {'missing':>8} "
          f"{'schema':>6} {'rows':>7}")
    for slug, spec in sorted(EMBEDDING_MODELS.items()):
        try:
            # Probing is side-effect free: VectorStore.open() creates nothing
            # on a nonexistent dir (files appear on first append).
            store = get_store(slug, base_dir=stores_dir)
            count = store.count
            missing = len(store.missing(index_ids))
            schema, rows = store.schema, store.rows
        except ValueError as e:  # dims mismatch vs registry — surface, keep listing
            print(f"{'*' if slug == active else ' '} {slug:<32} ERROR: {e}")
            continue
        mark = "*" if slug == active else " "
        note = "" if count else "  (no store yet)"
        print(f"{mark} {slug:<32} {spec['dims']:>5} {count:>7} {missing:>8} "
              f"{schema:>6} {rows:>7}{note}")

    persisted = _read_state_file(stores_dir)
    if persisted:
        print()
        print(
            f"  last job: state={persisted.get('state')} "
            f"target={persisted.get('target')} "
            f"done={persisted.get('done')}/{persisted.get('total')}"
        )
    return 0


def _install_sigint_cancel() -> None:
    """First Ctrl-C asks the engine to cancel cooperatively (finishes the
    current batch, persists a clean 'cancelled' state); a second Ctrl-C
    hard-interrupts, leaving the state file 'running' for a later resume."""
    seen = {"count": 0}

    def _handler(signum, frame):
        seen["count"] += 1
        if seen["count"] == 1 and migrate.request_cancel():
            print("\n[BACKFILL] cancel requested - finishing current batch "
                  "(Ctrl-C again to hard-stop)...")
            return
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handler)


def _join_toolvault_thread() -> None:
    """The engine's cutover hook re-embeds ToolVault descriptions on a daemon
    thread; in the orchestrator that outlives the request, but a CLI process
    exits immediately — wait for it so the re-embed isn't killed mid-write."""
    for t in threading.enumerate():
        if t.name == "toolvault-cutover-reembed":
            print("[BACKFILL] waiting for ToolVault re-embed to finish...")
            t.join(timeout=600)


def _run_rebuild_cli(target: str, stores_dir: Path, index_path: Path) -> int:
    """--rebuild mode: build a schema-2 chunk-store CANDIDATE, build-only.

    Runs migrate.run_rebuild(target) to completion — the candidate lands
    under {stores}/_build/{target}. NO cutover happens here (or anywhere on
    the rebuild path): activation is a separate explicit step (the M6f
    stop-service dir-swap runbook).
    """
    index = _load_index(index_path)
    spec = EMBEDDING_MODELS[target]

    _print_banner("CHUNK-STORE REBUILD (build-only)")
    print(f"  target:    {target} ({spec['provider']}, {spec['dims']} dims, schema 2)")
    print(f"  builds to: {stores_dir / migrate.BUILD_DIR_NAME / target}")
    print(f"  index:     {len(index)} snapshots ({index_path})")
    print()
    print("  NOTE: build-only - the active store and pointer are NOT touched.")
    print("  Cutover to the candidate is a separate explicit step (stop the")
    print("  service, dir-swap per the M6f runbook, restart).")
    print("=" * BANNER_WIDTH)

    _install_sigint_cancel()
    start = time.time()
    try:
        job = asyncio.run(migrate.run_rebuild(target))
    except KeyboardInterrupt:
        print()
        print("[BACKFILL] hard-interrupted; migration_state.json still says")
        print("  'running' (kind=rebuild). Progress lives in the build store -")
        print("  re-run --rebuild to resume, or start the orchestrator and let")
        print("  boot auto-resume finish it (build-only either way).")
        return 130
    elapsed = time.time() - start

    print()
    _print_banner(f"REBUILD {job['state'].upper()}")
    print(f"  state:     {job['state']}")
    print(f"  embedded:  {job['done']}/{job['total']} snapshots")
    print(f"  skipped:   {len(job['skipped'])}"
          + (f" {job['skipped']}" if 0 < len(job["skipped"]) <= 10 else ""))
    if job.get("rows") is not None:
        print(f"  candidate: {job.get('snapshots')} snapshots, {job.get('rows')} rows")
    if job.get("error"):
        print(f"  error:     {job['error']}")
    print(f"  elapsed:   {elapsed:.1f}s")

    if job["state"] != "done":
        print()
        print("[BACKFILL] rebuild did not complete - skipped ids stay missing in")
        print("  the build store; re-run --rebuild to retry them.")
        return 4

    print()
    print(f"[BACKFILL] rebuild complete: candidate ready at "
          f"{stores_dir / migrate.BUILD_DIR_NAME / target}.")
    print("  The active store was NOT changed; cutover is a separate explicit")
    print("  step (M6f runbook: stop service, dir-swap, restart, re-verify).")
    return 0


def main(argv=None) -> int:
    args = _parse_args(argv)

    stores_dir = Path(args.stores_dir or config.EMBEDDINGS_STORES_DIR)
    index_path = Path(args.index or config.SNAPSHOT_INDEX)

    # The engine resolves paths through module state at call time; point it at
    # the overrides (separate process — this module state is ours alone).
    config.EMBEDDINGS_STORES_DIR = str(stores_dir)
    if args.index:
        from Orchestrator import fossils  # heavy import; only when overriding
        fossils.SNAPSHOT_INDEX = index_path
        fossils._index_cache = None

    if args.list:
        return _list_stores(stores_dir, index_path)

    # ── validate target against the registry ─────────────────────────────────
    target = args.rebuild or args.target or get_active_slug(base_dir=stores_dir)
    if target not in EMBEDDING_MODELS:
        print(f"[ERROR] unknown embedding model slug {target!r}")
        print(f"        valid slugs: {', '.join(sorted(EMBEDDING_MODELS))}")
        return 2

    # ── liveness guard (write modes only; --list above stays usable) ─────────
    # A RUNNING orchestrator owns the store files: its mint/heal appends and a
    # second writer in this process race each other and can destroy a store
    # (the engine's one-job claim is in-process only). ANY response on the
    # service port = refuse; --force remains for a stopped-service edge only.
    if not args.force and _service_alive():
        print("=" * BANNER_WIDTH)
        print("[REFUSED] the orchestrator service is RUNNING on localhost:9091")
        print()
        print("  Writing store files from this script while the service is up")
        print("  races its own appends (mints, gap-heal, migrations) and can")
        print("  corrupt a store. Use the canonical path instead:")
        print("      POST /embeddings/migrate   (wizard / API on the service)")
        print("  or stop the service first:")
        print("      sudo systemctl stop blackbox.service")
        print("  Re-run with --force ONLY if you are certain the service is")
        print("  not actually running (e.g. another process owns the port).")
        print("=" * BANNER_WIDTH)
        return 5

    # ── cross-process guard: migration_state.json is the only shared signal ──
    persisted = _read_state_file(stores_dir)
    if persisted and persisted.get("state") == "running" and not args.force:
        print("=" * BANNER_WIDTH)
        print("[WARNING] migration_state.json says a migration is RUNNING")
        print(f"          target={persisted.get('target')} "
              f"done={persisted.get('done')}/{persisted.get('total')} "
              f"started_at={persisted.get('started_at')}")
        print()
        print("  This may be the LIVE orchestrator mid-migration - running this")
        print("  script now would race it on the same store files. The canonical")
        print("  path is the wizard / POST /embeddings/migrate on the running")
        print("  service. If the service is STOPPED (job interrupted by a crash/")
        print("  shutdown), re-run with --force to resume; progress is preserved")
        print("  in the store itself.")
        print("=" * BANNER_WIDTH)
        return 3

    if args.rebuild:
        return _run_rebuild_cli(target, stores_dir, index_path)

    # ── banner: what this run is about to do ─────────────────────────────────
    index = _load_index(index_path)
    spec = EMBEDDING_MODELS[target]
    # MUST be the engine's own target-open helper, not a plain get_store
    # probe: get_store caches one instance per (base_dir, slug), and the
    # post-gate default creates FRESH targets as schema 2 — an autodetect
    # probe here would cache a v1 instance the engine then refuses.
    # (config.EMBEDDINGS_STORES_DIR was pointed at stores_dir above.)
    store = migrate.open_migration_target(target)
    missing = len(store.missing(list(index.keys())))
    active = get_active_slug(base_dir=stores_dir)

    _print_banner("EMBEDDING BACKFILL / MIGRATION")
    print(f"  target:    {target} ({spec['provider']}, {spec['dims']} dims, "
          f"schema {store.schema})")
    print(f"  active:    {active}")
    print(f"  index:     {len(index)} snapshots ({index_path})")
    print(f"  store:     {store.count} embedded, {missing} missing")
    print("=" * BANNER_WIDTH)

    if missing == 0 and target == active:
        print("Nothing to do - the active store already covers every snapshot.")
        return 0

    # ── run the engine to completion (claim + diff-and-fill + cutover) ───────
    _install_sigint_cancel()
    start = time.time()
    try:
        job = asyncio.run(migrate.run_migration(target))
    except KeyboardInterrupt:
        print()
        print("[BACKFILL] hard-interrupted; migration_state.json still says")
        print("  'running'. Progress lives in the store - resume with --force,")
        print("  or start the orchestrator and let boot auto-resume finish it.")
        return 130
    elapsed = time.time() - start

    # ── summary (engine printed [MIGRATE] lines as it went) ──────────────────
    print()
    _print_banner(f"MIGRATION {job['state'].upper()}")
    print(f"  state:     {job['state']}")
    print(f"  embedded:  {job['done']}/{job['total']}")
    print(f"  skipped:   {len(job['skipped'])}"
          + (f" {job['skipped']}" if 0 < len(job["skipped"]) <= 10 else ""))
    print(f"  raced:     {len(job['raced'])}")
    if job.get("error"):
        print(f"  error:     {job['error']}")
    print(f"  elapsed:   {elapsed:.1f}s")

    if job["state"] != "done":
        print()
        print("[BACKFILL] job did not complete - skipped ids stay missing in the")
        print("  store; re-run (or POST /embeddings/migrate) to retry them.")
        return 4

    _join_toolvault_thread()
    print()
    print(f"[BACKFILL] cutover complete: active model is now {target}.")
    print("  REMINDER: a running orchestrator will NOT see this switch - the")
    print("  in-memory store swap only happened in this CLI process. If the")
    print("  service is up, restart it (sudo systemctl restart blackbox.service)")
    print("  so live searches pick up the new active model.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
