"""Flight Recorder oversight machinery (design 2026-07-23).

The Flight Recorder (FR) is the permanent, undeletable operator that oversees
the whole box: it reads every operator's chain (read-only — the widened gate
lives in config.reads_all_operators), collects live health signals, and mints
periodic cross-operator "flight reports" into ITS OWN chain.

This module owns: seeding (startup IS the migration), the persisted state file,
the watchtower signal collector, and the flight-report builder. HTTP surface is
in routes/oversight_routes.py; the cron direct-dispatch branch is in
scheduler/executor.py.
"""
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from Orchestrator.config import (
    CFG, FLIGHT_RECORDER_OPERATOR, VOL_PATH,
)

STATE_PATH = Path("Manifest/flight_recorder_state.json")

# Retired scheduled-job identity (kept for the retire migration only).
FR_REPORT_JOB_NAME = "Flight Report"

FR_REPORT_SOURCE_COUNT = CFG.getint("flight_recorder", "source_count", fallback=25)
FR_MIN_NEW_SNAPSHOTS = CFG.getint("flight_recorder", "min_new_snapshots", fallback=5)
FR_ESCALATE_CRITICAL = CFG.getboolean("flight_recorder", "escalate_critical", fallback=True)
# Natural trigger (Brandon 2026-07-23): a flight report mints after every N
# snapshots minted ACROSS ALL OPERATORS — mirrors the per-operator checkpoint
# rhythm, no scheduler involved. "It just naturally happens as users mint."
FR_REPORT_EVERY_N = CFG.getint("flight_recorder", "report_every_n_snapshots", fallback=25)

_state_lock = threading.Lock()
_state: Dict[str, Any] = {}


def _snap_key(snap_id: str) -> tuple:
    """Numeric sort key for SNAP-YYYYMMDD-NNNN ids — lexicographic string
    ordering breaks when the sequence crosses digit widths (999 → 1000);
    mirrors fossils' snapshot_sort_key (review 2026-07-23)."""
    try:
        parts = str(snap_id).split("-")
        return (int(parts[1]), int(parts[2]))
    except (IndexError, ValueError):
        return (0, 0)


# ── State file ──────────────────────────────────────────────────────────────

def load_flight_recorder_state() -> Dict[str, Any]:
    global _state
    with _state_lock:
        try:
            _state = json.loads(STATE_PATH.read_text())
        except Exception:
            _state = {}
        _state.setdefault("last_report_id", None)
        _state.setdefault("last_report_at", None)
        _state.setdefault("adopted_preexisting", False)
        _state.setdefault("report_job_id", None)
        _state.setdefault("mints_since_report", 0)
        _state.setdefault("synthesis_failures", 0)
        _state.setdefault("last_synthesis_error", None)
        return dict(_state)


def _save_state() -> None:
    with _state_lock:
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps(_state, indent=2))
        except Exception as e:
            print(f"[FLIGHT-RECORDER] state save failed: {e}")


def get_state_snapshot() -> Dict[str, Any]:
    with _state_lock:
        return dict(_state)


# ── Seeding (startup IS the migration) ──────────────────────────────────────

def ensure_flight_recorder() -> None:
    """Idempotent startup seeding: operator + report job + state.

    - Appends 'Flight Recorder' to [users] list if absent (in-place mutation
      pattern from admin_routes.add_operator — every importer sees it live).
    - NEVER touches USERS_DEFAULT, never reorders the list.
    - Exact-name collision → adopt (loud log + state flag); case/whitespace
      variants are left alone as unrelated operators, with a warning.
    """
    import os
    if os.getenv("BLACKBOX_SKIP_FR_SEED") == "1":
        # Test harnesses import the app (which fires startup) from the repo
        # root; this switch keeps them from writing the real config.ini/cron
        # DB. Never set in production units.
        return
    import Orchestrator.config as _cfg
    load_flight_recorder_state()
    try:
        if FLIGHT_RECORDER_OPERATOR in _cfg.USERS_LIST:
            if not _state.get("seeded_at"):
                # Present before we ever seeded → a pre-existing operator we adopt.
                print("[FLIGHT-RECORDER] adopted pre-existing operator "
                      f"'{FLIGHT_RECORDER_OPERATOR}' — its history is now the FR chain")
                with _state_lock:
                    _state["adopted_preexisting"] = True
        else:
            variants = [u for u in _cfg.USERS_LIST
                        if u.strip().lower() == FLIGHT_RECORDER_OPERATOR.lower()]
            if variants:
                print(f"[FLIGHT-RECORDER] near-collision: existing operator(s) "
                      f"{variants} resemble the reserved name; seeding the "
                      f"canonical '{FLIGHT_RECORDER_OPERATOR}' alongside them")
            new_list = _cfg.USERS_LIST + [FLIGHT_RECORDER_OPERATOR]
            config_path = Path("config.ini")
            if not CFG.has_section("users"):
                CFG.add_section("users")
            CFG.set("users", "list", ", ".join(new_list))
            with open(config_path, "w") as f:
                CFG.write(f)
            CFG.read(config_path)
            _cfg.USERS_LIST[:] = [u.strip() for u in
                                  CFG.get("users", "list", fallback="").split(",")
                                  if u.strip()]
            print(f"[FLIGHT-RECORDER] seeded operator '{FLIGHT_RECORDER_OPERATOR}' "
                  f"(operators now: {_cfg.USERS_LIST})")
        with _state_lock:
            _state.setdefault("seeded_at", datetime.now(timezone.utc).isoformat())
        _save_state()
    except Exception as e:
        # Seeding must never take the service down; next boot retries.
        print(f"[FLIGHT-RECORDER] seeding failed (will retry next boot): {e}")
    try:
        retire_flight_report_cron_job()
    except Exception as e:
        print(f"[FLIGHT-RECORDER] report-job retirement failed (non-fatal): {e}")


def retire_flight_report_cron_job() -> None:
    """MIGRATION (Brandon 2026-07-23): the scheduled report job is retired —
    reports now trigger NATURALLY from mint activity (on_snapshot_minted,
    every FR_REPORT_EVERY_N snapshots across ALL operators). Boxes that got
    the short-lived seeded job (dev + MS02) have it deleted here; idempotent
    no-op everywhere else."""
    with _state_lock:
        job_id = _state.get("report_job_id")
    try:
        from Orchestrator.scheduler import get_scheduler_manager
        manager = get_scheduler_manager()
        victims = [job_id] if job_id else []
        victims += [j["id"] for j in manager.list_jobs()
                    if j.get("operator") == FLIGHT_RECORDER_OPERATOR
                    and j.get("name") == FR_REPORT_JOB_NAME
                    and j["id"] not in victims]
        for vid in victims:
            try:
                if manager.delete_job(vid):
                    print(f"[FLIGHT-RECORDER] retired scheduled report job {vid} "
                          "(reports are mint-triggered now)")
            except Exception as e:
                print(f"[FLIGHT-RECORDER] could not retire job {vid}: {e}")
    finally:
        if job_id:
            with _state_lock:
                _state["report_job_id"] = None
            _save_state()


# ── Natural report trigger (Brandon 2026-07-23) ────────────────────────────

# Non-blocking in-flight guard: at most one auto report at a time.
_report_in_flight = threading.Lock()


def on_snapshot_minted(snap_id: str, snap_type: str, operator: str) -> None:
    """Called by fossils.update_snapshot_index after EVERY mint — the one
    choke point all mint paths share. Counts snapshots across ALL operators
    (checkpoints and app/tool mints included — 'all of the snapshots that go
    in the system'); after FR_REPORT_EVERY_N, fires a flight report on a
    background thread. The FR's own flight reports never count and never
    re-trigger (no recursion). Must NEVER raise into the minting caller."""
    try:
        import os
        if os.getenv("BLACKBOX_SKIP_FR_SEED") == "1":
            return   # test harnesses: no background reports
        if snap_type == "flight_report":
            return
        with _state_lock:
            _state["mints_since_report"] = _state.get("mints_since_report", 0) + 1
            due = _state["mints_since_report"] >= FR_REPORT_EVERY_N
        if not due:
            return
        if not _report_in_flight.acquire(blocking=False):
            return   # one already synthesizing; the counter keeps it due
        def _run():
            try:
                create_flight_report_async(manual=False)
            finally:
                _report_in_flight.release()
        # Thread, not inline: the caller usually holds mint_lock; the report
        # takes mint_lock itself for its own append and must not deadlock.
        threading.Thread(target=_run, daemon=True,
                         name="flight-report-auto").start()
        print(f"[FLIGHT-RECORDER] {FR_REPORT_EVERY_N} snapshots since last "
              f"report — auto flight report triggered by {snap_id}")
    except Exception as e:
        print(f"[FLIGHT-RECORDER] mint-trigger error (non-fatal): {e}")


# ── Watchtower signal collector (§2.2 — all read-only) ─────────────────────

def collect_oversight_signals() -> Dict[str, Any]:
    """Live health digest. Every probe is individually guarded — a broken
    signal source becomes a reported collection error, never an exception."""
    signals: Dict[str, Any] = {"collected_at": datetime.now(timezone.utc).isoformat(),
                               "errors": []}

    # Cron: divergence + recent failures per job
    try:
        from Orchestrator.scheduler import get_scheduler_manager
        manager = get_scheduler_manager()
        jobs = manager.list_jobs()
        diverged, failures = [], []
        for job in jobs:
            if job.get("status") == "active":
                try:
                    if manager.scheduler.get_job(job["id"]) is None:
                        diverged.append({"job_id": job["id"], "name": job.get("name")})
                except Exception:
                    diverged.append({"job_id": job["id"], "name": job.get("name")})
            try:
                # cron_job_history has NO 'status' column — outcome lives in
                # delivery_status: 'delivered'/'error'/'skipped' (review
                # 2026-07-23 caught the dead 'status' read).
                for run in manager.get_job_history(job["id"], limit=5):
                    if (run.get("delivery_status") or "").lower() == "error":
                        failures.append({"job_id": job["id"], "name": job.get("name"),
                                         "run_at": run.get("run_at"),
                                         "error": str(run.get("error") or "")[:300]})
            except Exception:
                pass
        signals["cron"] = {"job_count": len(jobs), "diverged": diverged,
                           "recent_failures": failures}
    except Exception as e:
        signals["errors"].append(f"cron: {e}")

    # Background tasks: failed + stuck ('processing' with stale updated_at).
    # Reads the same projection the task pill uses (models.task_db) — no
    # operator filter, so this sees every operator's tasks.
    try:
        from Orchestrator.models import task_db
        rows = task_db.get_task_list(None)
        now = time.time()
        failed = [t for t in rows if (t.get("status") or "").lower() == "failed"]
        stuck = []
        for t in rows:
            if (t.get("status") or "").lower() in ("processing", "pending"):
                upd = t.get("updated_at") or t.get("created_at")
                try:
                    ts = datetime.fromisoformat(str(upd).replace("Z", "+00:00")).timestamp()
                    if now - ts > 3600:   # 1h with no progress = stuck, any type
                        stuck.append(t)
                except Exception:
                    pass
        # NOTE: the pill projection carries no error columns — id/type/
        # operator identify the task; the FR digs deeper via /tasks when
        # asked (review 2026-07-23: don't render an always-empty error field).
        signals["tasks"] = {
            "failed": [{"id": t.get("task_id"), "type": t.get("task_type"),
                        "operator": t.get("operator")} for t in failed[:20]],
            "stuck": [{"id": t.get("task_id"), "type": t.get("task_type"),
                       "operator": t.get("operator"),
                       "updated_at": t.get("updated_at")} for t in stuck[:20]],
        }
    except Exception as e:
        signals["errors"].append(f"tasks: {e}")

    # TTS queue
    try:
        from Orchestrator import tts_queue
        signals["tts_queue"] = tts_queue.queue_status()
    except Exception as e:
        signals["errors"].append(f"tts_queue: {e}")

    # Ledger + embedding coverage (index vs volume, straight from the index)
    try:
        from Orchestrator.fossils import load_snapshot_index  # noqa: F811
        index = load_snapshot_index()
        vol_size = VOL_PATH.stat().st_size if VOL_PATH.exists() else 0
        max_byte_end = max((v.get("byte_end", 0) for v in index.values()), default=0)
        # Embedding coverage comes from the ACTIVE binary store (pluggable-
        # embeddings architecture: the index carries offsets/metadata, vectors
        # live in per-model stores). store.missing() preserves order.
        recent_ids = sorted(index.keys(), key=_snap_key)[-50:]
        try:
            from Orchestrator.embeddings.store import get_store, get_active_slug
            missing_embeddings = get_store(get_active_slug()).missing(recent_ids)
        except Exception as emb_e:
            missing_embeddings = []
            signals["errors"].append(f"embedding-coverage: {emb_e}")
        signals["ledger"] = {
            "snapshot_count": len(index),
            "volume_bytes": vol_size,
            "indexed_through": max_byte_end,
            "unindexed_tail_bytes": max(0, vol_size - max_byte_end),
            "recent_missing_embeddings": missing_embeddings,
        }
    except Exception as e:
        signals["errors"].append(f"ledger: {e}")

    return signals


def format_signals_digest(signals: Dict[str, Any]) -> str:
    """Human/LLM-readable digest of collect_oversight_signals output."""
    lines = ["SYSTEM HEALTH DIGEST", ""]
    cron = signals.get("cron") or {}
    if cron:
        lines.append(f"Cron: {cron.get('job_count', 0)} jobs; "
                     f"{len(cron.get('diverged', []))} diverged; "
                     f"{len(cron.get('recent_failures', []))} recent failed runs")
        for f in cron.get("recent_failures", [])[:10]:
            lines.append(f"  - FAILED {f['name']} ({f['job_id']}) at {f.get('run_at')}: {f.get('error')}")
        for d in cron.get("diverged", [])[:10]:
            lines.append(f"  - DIVERGED (no live trigger): {d['name']} ({d['job_id']})")
    tasks = signals.get("tasks") or {}
    if tasks:
        lines.append(f"Tasks: {len(tasks.get('failed', []))} failed, "
                     f"{len(tasks.get('stuck', []))} stuck")
        for t in tasks.get("failed", [])[:10]:
            lines.append(f"  - FAILED task {t['id']} ({t.get('type')}, {t.get('operator')})")
        for t in tasks.get("stuck", [])[:10]:
            lines.append(f"  - STUCK task {t['id']} ({t.get('type')}) — no progress since {t.get('updated_at')}")
    tq = signals.get("tts_queue") or {}
    if tq:
        lines.append(f"TTS queue: {json.dumps({k: v for k, v in tq.items() if not isinstance(v, (list, dict))})}")
    ledger = signals.get("ledger") or {}
    if ledger:
        lines.append(f"Ledger: {ledger.get('snapshot_count')} snapshots; "
                     f"volume {ledger.get('volume_bytes')}B indexed through {ledger.get('indexed_through')}B "
                     f"(tail {ledger.get('unindexed_tail_bytes')}B); "
                     f"{len(ledger.get('recent_missing_embeddings', []))} of last 50 missing embeddings")
        for sid in ledger.get("recent_missing_embeddings", [])[:10]:
            lines.append(f"  - MISSING EMBEDDING: {sid}")
    for err in signals.get("errors", []):
        lines.append(f"Collector error: {err}")
    return "\n".join(lines)


def has_critical_findings(signals: Dict[str, Any]) -> bool:
    ledger = signals.get("ledger") or {}
    return bool(ledger.get("recent_missing_embeddings")
                or (ledger.get("unindexed_tail_bytes", 0) or 0) > 100)


def has_failures(signals: Dict[str, Any]) -> bool:
    cron = signals.get("cron") or {}
    tasks = signals.get("tasks") or {}
    return bool(cron.get("recent_failures") or cron.get("diverged")
                or tasks.get("failed") or tasks.get("stuck")
                or has_critical_findings(signals))


# ── Flight reports (§7 — the cross-operator checkpoint) ────────────────────

_REPORT_PROMPT = """You are the Flight Recorder of this AI BlackBox — its permanent overseer. Below are the {n} most recent snapshots from ALL operators on this box, followed by a live system-health digest (cron runs, background tasks, embedding and ledger integrity).

Produce a FLIGHT REPORT with these sections:

1. ACTIVITY — per operator: what they worked on, key decisions, notable sessions. Cite snapshot IDs. Weight human sessions over machine-generated 'system' snapshots, but note significant automated activity.
2. COMPLETIONS — jobs, scheduled tasks, and long-running generations that finished successfully since the last report.
3. FAILURES & INCOMPLETIONS — every cron run failure, failed or stuck task, queue failure, and embedding/ledger problem in the digest. Be exhaustive here; an unreported failure is a Flight Recorder failure. Include job/task IDs.
4. ANOMALIES — anything unusual: operators gone silent, jobs drifting from schedule, index/volume divergence, repeated retries, suspicious gaps in the record.
5. LEDGER STATUS — one short paragraph: snapshot count, embedding coverage, index health, since-last-report deltas.

Be factual and specific. Distinguish what the record shows from what you infer. If a section is empty, state that explicitly ("No failures observed.").

{snapshots}

{digest}"""


def _synthesize(prompt: str) -> Optional[str]:
    """Provider-resolved synthesis — NOT hardwired to Gemini (the checkpoint
    hardwire is a known portability gap). Order: [flight_recorder] provider/
    model config, else first provider with a key: google → anthropic → openai
    → custom. Returns None on failure (recorded by caller, never swallowed)."""
    import Orchestrator.config as _cfg
    messages = [{"role": "user", "content": prompt}]
    cfg_provider = CFG.get("flight_recorder", "provider", fallback="").strip().lower()
    cfg_model = CFG.get("flight_recorder", "model", fallback="").strip()

    def attempt(provider: str, model: str) -> Optional[str]:
        from Orchestrator.routes import chat_routes as cr
        fn = {"google": cr.call_gemini, "gemini": cr.call_gemini,
              "anthropic": cr.call_anthropic, "openai": cr.call_openai,
              "custom": cr.call_custom}.get(provider)
        if not fn:
            return None
        out = fn(messages, model=model, operator=FLIGHT_RECORDER_OPERATOR)
        reply = out[0] if isinstance(out, tuple) else out
        return reply or None

    candidates: List[tuple] = []
    if cfg_provider:
        candidates.append((cfg_provider, cfg_model or ""))
    if getattr(_cfg, "GOOGLE_API_KEY", ""):
        candidates.append(("google", _cfg.GEMINI_MODEL_DEFAULT))
    if getattr(_cfg, "ANTHROPIC_API_KEY", ""):
        candidates.append(("anthropic", _cfg.ANTHROPIC_MODEL_DEFAULT))
    if getattr(_cfg, "OPENAI_API_KEY", ""):
        candidates.append(("openai", _cfg.OPENAI_MODEL_DEFAULT))
    candidates.append(("custom", ""))   # LAN servers need no key

    last_err: Optional[str] = None
    for provider, model in candidates:
        try:
            reply = attempt(provider, model)
            if reply:
                return reply
        except Exception as e:
            last_err = f"{provider}: {e}"
    if last_err:
        with _state_lock:
            _state["last_synthesis_error"] = last_err
    return None


def create_flight_report_async(manual: bool = False) -> Optional[str]:
    """Build + mint one flight report. Returns the snap_id, or None when
    skipped (insufficient activity) or failed (failure recorded in state).

    Mirrors checkpoint.create_checkpoint_async's mint mechanics; gathers
    across ALL operators via the widened read gate; stamps TYPE: flight_report
    so the per-operator checkpoint pin is untouched (additive invariant #3).
    """
    from Orchestrator.fossils import (
        get_recent_fossils_for_operator, load_snapshot_index,
        update_snapshot_index,
    )
    from Orchestrator.volume import (
        append_snapshot_text, next_snap_id_from_tail, now_utc_iso,
        parse_tail, read_text_safe,
    )
    from Orchestrator.monitoring import render_snapshot_body_v71
    from Orchestrator.state import mint_lock
    from Orchestrator.checkpoint import _embed_for_index
    try:
        vol_txt = read_text_safe(VOL_PATH)
        # Widened gate: as the FR, this returns the most recent snapshots
        # across EVERY operator.
        recent = get_recent_fossils_for_operator(
            vol_txt, FLIGHT_RECORDER_OPERATOR,
            count=FR_REPORT_SOURCE_COUNT, cap_chars_each=10000)

        # Min-activity skip: how many snapshots are NEW since the last report?
        index = load_snapshot_index()
        all_ids = sorted(index.keys(), key=_snap_key)
        last_id = get_state_snapshot().get("last_report_id")
        new_since = [s for s in all_ids
                     if not last_id or _snap_key(s) > _snap_key(last_id)]
        if len(new_since) < FR_MIN_NEW_SNAPSHOTS and not manual:
            print(f"[FLIGHT-RECORDER] skipped report — only {len(new_since)} new "
                  f"snapshots since {last_id} (< {FR_MIN_NEW_SNAPSHOTS})")
            return None
        if not recent:
            print("[FLIGHT-RECORDER] skipped report — empty ledger")
            return None

        signals = collect_oversight_signals()
        digest = format_signals_digest(signals)
        covered_ids = all_ids[-len(recent):] if all_ids else []
        operators_covered = sorted({index[s].get("operator", "?")
                                    for s in covered_ids if s in index})

        prompt = _REPORT_PROMPT.format(
            n=len(recent),
            snapshots="\n\n" + ("=" * 80 + "\n\n").join(recent),
            digest=digest)

        summary = _synthesize(prompt)
        if not summary:
            with _state_lock:
                _state["synthesis_failures"] = _state.get("synthesis_failures", 0) + 1
                # Back off a full cycle rather than re-firing on every mint.
                _state["mints_since_report"] = 0
            _save_state()
            print("[FLIGHT-RECORDER] synthesis failed — recorded, will report next cycle")
            return None

        with mint_lock:
            utc = now_utc_iso()
            vol1 = read_text_safe(VOL_PATH)
            info1 = parse_tail(vol1)
            snap_id = next_snap_id_from_tail(info1["tail_id"])
            source_range = (f"{covered_ids[0]}..{covered_ids[-1]}"
                            if covered_ids else "none")
            reason = "MANUAL_FLIGHT_REPORT" if manual else "SCHEDULED_FLIGHT_REPORT"
            log_lines = [
                f"[FLIGHT-REPORT] Cross-operator synthesis of {len(recent)} snapshots",
                f"[FLIGHT-REPORT] Source range: {source_range}",
                f"[FLIGHT-REPORT] Operators covered: {', '.join(operators_covered)}",
                "",
                summary,
                "",
                "-" * 40,
                digest,
            ]
            gauges = {"drift": "green", "p": 0, "c": len(summary),
                      "t": len(summary), "model": "flight-recorder",
                      "operator": FLIGHT_RECORDER_OPERATOR}
            body = render_snapshot_body_v71(
                info=info1, snap_id=snap_id, utc=utc, reason=reason,
                log_lines=log_lines, gauges=gauges,
                provenance={"gm": True, "recent": [], "relevant": []})
            body = body.replace("MODE: Normal", "MODE: Flight Report")
            body = body.replace(
                f"OPERATOR: {FLIGHT_RECORDER_OPERATOR}",
                f"OPERATOR: {FLIGHT_RECORDER_OPERATOR}\nTYPE: flight_report"
                f"\nSOURCE_RANGE: {source_range}"
                f"\nOPERATORS_COVERED: {', '.join(operators_covered)}")

            byte_start = VOL_PATH.stat().st_size if VOL_PATH.exists() else 0
            append_snapshot_text(body)
            byte_end = VOL_PATH.stat().st_size
            embed_payload = _embed_for_index(snap_id, body)
            update_snapshot_index(
                snap_id=snap_id, byte_start=byte_start, byte_end=byte_end,
                operator=FLIGHT_RECORDER_OPERATOR, timestamp=utc,
                snap_type="flight_report", **embed_payload)

        with _state_lock:
            _state["last_report_id"] = snap_id
            _state["last_report_at"] = utc
            _state["mints_since_report"] = 0   # natural-trigger cycle restarts
        _save_state()
        print(f"[FLIGHT-RECORDER] minted {snap_id} ({len(summary)} chars, "
              f"operators: {', '.join(operators_covered)})")

        _maybe_notify(signals, snap_id)
        return snap_id
    except Exception as e:
        import traceback
        print(f"[FLIGHT-RECORDER] report failed: {e}")
        traceback.print_exc()
        with _state_lock:
            _state["synthesis_failures"] = _state.get("synthesis_failures", 0) + 1
            _state["last_synthesis_error"] = str(e)
            _state["mints_since_report"] = 0   # back off a full cycle
        _save_state()
        return None


def _maybe_notify(signals: Dict[str, Any], snap_id: str) -> None:
    """Failures → notify FR subscribers; ledger-integrity criticals may
    escalate to 'all' (config [flight_recorder] escalate_critical, default on).
    FR is deliberately NOT in any notification-suppression set."""
    if not has_failures(signals):
        return
    try:
        import asyncio
        from Orchestrator.notifications.bus import notify
        critical = has_critical_findings(signals)
        title = "Flight Report: failures detected" if not critical else \
                "Flight Report: LEDGER INTEGRITY issue"
        body = f"{snap_id}: see the latest flight report for details."
        async def _send():
            await notify(FLIGHT_RECORDER_OPERATOR, title, body,
                         category="oversight", dedup_key=f"fr-{snap_id}")
            if critical and FR_ESCALATE_CRITICAL:
                # bus.notify has no broadcast: "all" only reaches devices
                # SUBSCRIBED with the all-sentinel (review 2026-07-23). A
                # ledger-integrity critical goes to every operator's own
                # subscribers explicitly.
                import Orchestrator.config as _cfg
                for op in list(_cfg.USERS_LIST):
                    if op != FLIGHT_RECORDER_OPERATOR:
                        await notify(op, title, body, category="oversight",
                                     dedup_key=f"fr-crit-{snap_id}-{op}")
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_send())
        except RuntimeError:
            asyncio.run(_send())
    except Exception as e:
        print(f"[FLIGHT-RECORDER] notify failed (non-fatal): {e}")


# ── Flight-report pin (sibling of the checkpoint pin — NOT widened) ────────

def get_recent_flight_reports(count: int = 1) -> List[str]:
    """The FR's own context pin: N most recent TYPE: flight_report snapshots
    from the FR chain. Other operators' checkpoint pins are untouched."""
    if count <= 0:
        return []
    from Orchestrator.fossils import load_snapshot_index
    from Orchestrator.volume import read_volume_bytes
    index = load_snapshot_index()
    matching = [(sid, meta) for sid, meta in index.items()
                if meta.get("operator") == FLIGHT_RECORDER_OPERATOR
                and meta.get("type") == "flight_report"]
    if not matching:
        return []
    matching.sort(key=lambda x: _snap_key(x[0]))
    vol_bytes = read_volume_bytes(VOL_PATH)
    out = []
    for sid, meta in matching[-count:]:
        try:
            out.append(vol_bytes[meta["byte_start"]:meta["byte_end"]].decode(
                "utf-8", errors="replace"))
        except Exception:
            continue
    return out
