"""Binary per-model vector stores — replaces inline-JSON embeddings.

Layout under {base_dir}/{slug}/ (get_store() defaults base_dir to
config.EMBEDDINGS_STORES_DIR):
  vectors.f32 — raw little-endian float32, row-major N×dims
  ids.json    — ordered snap_id list; row i ↔ vector i
  meta.json   — {slug, dims, normalized: True, count, last_updated} (ISO-8601 UTC)

The active-model pointer lives at {base_dir}/active.json ({"active": slug}).

Schema v2 (chunk-for-scoring, audit A1–A3/A5) adds, per store:
  ordinals.json — parallel int list; row i's chunk ordinal within its snapshot
  meta.json     — additionally {schema: 2, rows, snapshots, generation};
                  `count` stays SNAPSHOT currency (status/UI binding contract)
ids.json rows stay BARE snap_ids, repeated once per chunk — snap_id is the one
currency of ids()/missing()/allowed_ids on every schema. A snapshot's chunks
form ONE contiguous group (ordinals 0..n-1) written in one lock hold; the
idempotency key stays snap_id and means "full group present" (whole incoming
group skipped). search/search_with_vectors collapse to unique snapshots during
the argsort descent: the first hit per snap_id IS its max-cosine best chunk.
Absent schema key ⇒ v1: those stores behave byte-identically to today, forever
(audit A6). Fresh stores default to v1 until the M6f cutover — schema=2 must be
requested explicitly at construction.

Invariants:
- Stored rows are L2-normalized at append time, so cosine similarity is a
  single mat-vec (scores = M @ q) — never a python loop over rows.
- ids.json and vectors.f32 are kept consistent by open()'s self-heal:
  whichever is longer is truncated to match the shorter (torn writes only
  ever lose the trailing rows, never corrupt earlier ones). v2 extends the
  heal to ordinals.json and drops a torn trailing PARTIAL group entirely
  (ordinal-contiguity walk-back), so a healed snapshot is fully absent →
  reported missing → cleanly re-embedded.
- One process owns the store (the orchestrator); a threading.Lock guards
  append/search state mutation across its worker threads.
"""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from Orchestrator import config
from Orchestrator.embeddings.registry import EMBEDDING_MODELS

VECTORS_FILE = "vectors.f32"
IDS_FILE = "ids.json"
ORDINALS_FILE = "ordinals.json"
META_FILE = "meta.json"
ACTIVE_FILE = "active.json"


def _atomic_write_json(path: Path, obj) -> None:
    """Write JSON durably: tmp file + fsync + os.replace (never a torn read)."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _ordinals_from_id_runs(ids: list) -> list:
    """Reconstruct the ordinals sidecar from consecutive id runs.

    Valid because groups are contiguous by construction and every run ramps
    0..m — a prefix-truncated ids list still reconstructs exactly (the cut
    only removes tail rows; the surviving partial run still starts at 0).
    """
    ordinals: list[int] = []
    i = 0
    while i < len(ids):
        j = i
        while j < len(ids) and ids[j] == ids[i]:
            ordinals.append(j - i)
            j += 1
        i = j
    return ordinals


class VectorStore:
    """Append-only float32 vector store for one embedding model.

    One live instance per (base_dir, slug) — always obtain stores via
    get_store(); constructing VectorStore directly is for tests only.
    """

    def __init__(self, slug: str, dims: int, base_dir, schema: "int | None" = None,
                 content_mode: str = "full"):
        if schema not in (None, 1, 2):
            raise ValueError(f"{slug}: unsupported store schema {schema!r}")
        if content_mode not in ("full", "body"):
            raise ValueError(f"{slug}: unsupported content_mode {content_mode!r}")
        self.slug = slug
        self.dims = dims
        self.dir = Path(base_dir) / slug
        self._row_bytes = 4 * dims  # float32
        self._ids: list[str] = []
        self._id_set: set[str] = set()
        # None = autodetect from meta.json (a fresh dir defaults to v1 until
        # the M6f cutover); 1/2 = require/create that schema. Materialized on
        # first append — open() stays side-effect free on empty dirs.
        self._requested_schema = schema
        self._schema = schema or 1
        # content_mode ("full" | "body", M14.3): are the stored chunk vectors
        # built from the WHOLE envelope-inclusive text ("full", today) or the
        # body-only content ("body")? Persisted in v2 meta and STORE-schema-
        # derived — mint, the windower, and migrate all read this one source of
        # truth so a re-embed's mixed corpus stays self-consistent. The disk
        # value wins on an existing store (like schema autodetect); a fresh
        # store keeps the constructor request. Absent meta field -> "full".
        self._requested_content_mode = content_mode
        self._content_mode = content_mode
        self._ordinals: list[int] = []  # v2 only: row i's chunk ordinal
        self._generation = 0            # v2 only: bumps on every mutation
        self._matrix = None  # lazily loaded; invalidated on append
        self._opened = False
        self._closed = False
        self._lock = threading.Lock()

    # ── paths ────────────────────────────────────────────────────────────────

    @property
    def vectors_path(self) -> Path:
        return self.dir / VECTORS_FILE

    @property
    def ids_path(self) -> Path:
        return self.dir / IDS_FILE

    @property
    def ordinals_path(self) -> Path:
        return self.dir / ORDINALS_FILE

    @property
    def meta_path(self) -> Path:
        return self.dir / META_FILE

    # ── open / self-heal ─────────────────────────────────────────────────────

    def open(self) -> "VectorStore":
        """Load ids + vector matrix; heal any ids↔rows mismatch to min(...).

        On a nonexistent dir this creates nothing — files appear on first
        append, so probing a store is side-effect free.
        """
        with self._lock:
            self._load_locked()
        return self

    def _load_locked(self) -> None:
        # Dims guard MUST precede self-heal: healing across a dims mismatch
        # reinterprets row boundaries (rows = bytes // wrong_row_bytes),
        # rewrites meta to the wrong dims, and silently corrupts searches.
        meta_obj = None
        if self.meta_path.exists():
            try:
                meta_obj = json.loads(self.meta_path.read_text(encoding="utf-8"))
                stored_dims = meta_obj["dims"]
            except (json.JSONDecodeError, KeyError, TypeError, OSError,
                    UnicodeDecodeError):
                meta_obj = None
                stored_dims = None  # unreadable meta: heal below rebuilds it
            if stored_dims is not None and stored_dims != self.dims:
                raise ValueError(
                    f"{self.slug}: store dims {stored_dims} != requested {self.dims}"
                )

        # Schema guard mirrors the dims guard: the on-disk schema wins, an
        # explicit conflicting request refuses (never reinterprets bytes).
        # Absent/unreadable meta: the constructor's request (default v1).
        if meta_obj is not None:
            disk_schema = meta_obj.get("schema", 1)
            if disk_schema not in (1, 2):
                raise ValueError(
                    f"{self.slug}: unsupported store schema {disk_schema!r}"
                )
            if (self._requested_schema is not None
                    and disk_schema != self._requested_schema):
                raise ValueError(
                    f"{self.slug}: store is schema {disk_schema}, "
                    f"requested {self._requested_schema}"
                )
            self._schema = disk_schema
            # content_mode: the on-disk value wins (mirror of the schema branch
            # above); absent field -> "full" so every pre-M14.3 store keeps
            # working byte-identically.
            self._content_mode = meta_obj.get("content_mode", "full")
        elif self._requested_schema is not None:
            self._schema = self._requested_schema
        elif self.ordinals_path.exists():
            # F1: the ordinals sidecar is v2-only. A metaless dir that has one
            # is a v2 store whose meta was lost (external damage; our own
            # writer materializes meta FIRST) — misreading it as v1 would
            # return duplicate chunk rows from search and the next append
            # would cement the downgrade.
            self._schema = 2
        else:
            self._schema = 1

        self._matrix = None
        self._ids = []
        if self.ids_path.exists():
            self._ids = json.loads(self.ids_path.read_text(encoding="utf-8"))

        rows = 0
        if self.vectors_path.exists():
            rows = self.vectors_path.stat().st_size // self._row_bytes

        if self._schema == 2:
            self._load_v2_locked(rows, meta_obj)
        else:
            target = min(len(self._ids), rows)
            if len(self._ids) != rows or (
                self.vectors_path.exists()
                and self.vectors_path.stat().st_size != target * self._row_bytes
            ):
                # Self-heal: truncate BOTH sides to the shorter (drops only
                # trailing rows, including any partial row from a torn write).
                print(
                    f"[VECSTORE] {self.slug}: healing ids={len(self._ids)} "
                    f"rows={rows} -> {target}"
                )
                if self.vectors_path.exists():
                    with open(self.vectors_path, "r+b") as f:
                        f.truncate(target * self._row_bytes)
                if len(self._ids) != target:
                    self._ids = self._ids[:target]
                    _atomic_write_json(self.ids_path, self._ids)
                self._write_meta_locked()

        self._id_set = set(self._ids)
        self._opened = True

    def _load_v2_locked(self, rows: int, meta_obj) -> None:
        """v2 load + 3-file self-heal with trailing-partial-group drop.

        Consistent length = min(rows, len(ids), len(ordinals)), then walk back
        to the last COMPLETE group boundary — a torn group heals to fully
        absent, so its snap_id is reported missing and re-embedded whole
        (audit A3: snap_id membership always means "full group present").
        """
        self._generation = 0
        if isinstance(meta_obj, dict):
            try:
                self._generation = int(meta_obj.get("generation", 0))
            except (TypeError, ValueError):
                self._generation = 0
        self._ordinals = []
        sidecar_lost = False
        if self.ordinals_path.exists():
            try:
                loaded = json.loads(
                    self.ordinals_path.read_text(encoding="utf-8")
                )
                if isinstance(loaded, list):
                    self._ordinals = loaded
                else:
                    sidecar_lost = True  # parseable but not a list — garbage
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                sidecar_lost = True
        else:
            sidecar_lost = rows > 0 or bool(self._ids)
        if sidecar_lost and self._ids:
            # Decision-4 improvement: the sidecar is fully derivable from ids
            # (groups are contiguous, each run ramps 0..m) — reconstruct
            # instead of wiping, so external-only damage costs no re-embed.
            # ONLY for a MISSING/unreadable sidecar: a VALID shorter list is
            # pre-crash truth (atomic write order) and must keep min()-healing
            # — reconstructing over it would resurrect legitimately-dropped
            # rows.
            print(
                f"[VECSTORE] {self.slug}: ordinals sidecar lost — "
                f"reconstructing from {len(self._ids)} id rows"
            )
            self._ordinals = _ordinals_from_id_runs(self._ids)

        n = min(rows, len(self._ids), len(self._ordinals))
        target = self._last_complete_group_boundary(n)
        if (
            sidecar_lost
            or len(self._ids) != target
            or len(self._ordinals) != target
            or (self.vectors_path.exists()
                and self.vectors_path.stat().st_size != target * self._row_bytes)
        ):
            print(
                f"[VECSTORE] {self.slug}: v2 healing ids={len(self._ids)} "
                f"ordinals={len(self._ordinals)} rows={rows} -> {target}"
            )
            if self.vectors_path.exists():
                with open(self.vectors_path, "r+b") as f:
                    f.truncate(target * self._row_bytes)
            self._ids = self._ids[:target]
            self._ordinals = self._ordinals[:target]
            _atomic_write_json(self.ids_path, self._ids)
            _atomic_write_json(self.ordinals_path, self._ordinals)
            self._generation += 1  # heal is a mutation (audit A5 / WI-5 seam)
            self._write_meta_locked()
        else:
            # F2: files consistent but meta may be STALE (crash between the
            # ordinals and meta writes) or absent. Downstream trusts meta —
            # the status payload and the future ANN cache key
            # (slug, schema, generation, rows) — so refresh counts and bump
            # generation like any other heal.
            actual_rows = len(self._ids)
            snapshots = len(set(self._ids))
            stale = (
                (meta_obj is None and actual_rows > 0)
                or (isinstance(meta_obj, dict) and (
                    meta_obj.get("schema") != 2
                    or meta_obj.get("rows") != actual_rows
                    or meta_obj.get("snapshots") != snapshots
                    or meta_obj.get("count") != snapshots
                ))
            )
            if stale:
                print(
                    f"[VECSTORE] {self.slug}: v2 meta stale/absent — "
                    f"refreshing (rows={actual_rows} snapshots={snapshots})"
                )
                self._generation += 1
                self._write_meta_locked()

    def _last_complete_group_boundary(self, n: int) -> int:
        """Largest t <= n where no torn trailing chunk group survives.

        The trailing group ending at t-1 is COMPLETE iff its ordinal run
        0..m is fully present (contiguous ramp, one snap_id) AND the next
        recorded entry — ordinals[t] in the PRE-heal sidecar, when one exists
        — starts a new group at ordinal 0. When t is the sidecar's end, the
        atomic ids/ordinals rewrite order guarantees the boundary UNLESS the
        pre-heal ids list itself continues the same snap_id past t (F3:
        externally-truncated ordinals ending mid-group must not claim
        completeness; a legitimate lagging-ordinals crash never trips this
        because dedupe forbids a new batch re-appending the trailing sid).
        A torn group drops WHOLE (t falls back to the group's first row);
        malformed runs (can't arise from our own write order) walk back
        defensively one row at a time.
        """
        ids, ordinals = self._ids, self._ordinals
        t = n
        while t > 0:
            o = ordinals[t - 1]
            start = t - 1 - o if isinstance(o, int) and o >= 0 else -1
            run_ok = start >= 0 and all(
                ordinals[start + j] == j and ids[start + j] == ids[t - 1]
                for j in range(o + 1)
            )
            if not run_ok:
                t -= 1
                continue
            if t == len(ordinals):
                if t >= len(ids) or ids[t] != ids[t - 1]:
                    return t
            elif ordinals[t] == 0:
                return t
            t = start  # run continues past t: torn group — drop it whole
        return 0

    def _ensure_open_locked(self) -> None:
        if not self._opened:
            self._load_locked()

    # ── write path ───────────────────────────────────────────────────────────

    def append(self, snap_id: str, vector) -> None:
        """L2-normalize and append one row; idempotent on snap_id.

        On a v2 store this is a legal 1-chunk group (ordinal [0])."""
        self.append_many([(snap_id, vector)])

    def append_group(self, snap_id: str, vectors) -> int:
        """Append one snapshot's chunk vectors as ONE atomic group (v2 only).

        Rows land contiguously with ordinals 0..n-1 under a single lock hold;
        idempotency is whole-group on snap_id (first group wins — a re-append
        writes nothing and returns 0). Returns rows written.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError(f"{self.slug}: store instance retired (re-embed activated)")
            self._ensure_open_locked()
            if self._schema != 2:
                # Fail loud: v1 first-wins dedupe would silently keep only the
                # first chunk and masquerade as a whole-snapshot vector.
                raise ValueError(
                    f"{self.slug}: append_group requires a schema-2 store"
                )
        return self.append_many([(snap_id, vec) for vec in vectors])

    def append_many(self, items: list[tuple[str, "np.ndarray | list"]]) -> int:
        """Append a batch of (snap_id, vector) rows; returns rows written.

        Validation is all-or-nothing: every item is checked (dims, finiteness)
        BEFORE any byte is written, so one bad item aborts the whole batch.
        Duplicate snap_ids (against the store or earlier in the batch — first
        wins) are skipped silently, matching append()'s idempotency. The whole
        batch costs ONE fsync set: one vectors.f32 append, one ids.json
        rewrite, one meta rewrite, one matrix invalidation.

        v2 stores treat consecutive same-snap_id runs as chunk GROUPS: each
        group lands whole (ordinals 0..n-1) or is skipped whole (duplicate
        snap_id — "already present" means the full group is). The entire
        batch, all groups, is written under ONE lock hold, so groups can never
        interleave with or span other batches.
        """
        prepared = []
        for snap_id, vector in items:
            vec = np.asarray(vector, dtype=np.float32)
            if vec.shape != (self.dims,):
                raise ValueError(
                    f"{self.slug}: vector has {len(vector)} dims, "
                    f"store expects {self.dims}"
                )
            # NaN/Inf would rank #1 in every search forever (NaN compares
            # poison argsort) — reject before normalize.
            if not np.isfinite(vec).all():
                raise ValueError(f"{snap_id}: non-finite vector")
            prepared.append((snap_id, vec))

        with self._lock:
            if self._closed:
                raise RuntimeError(f"{self.slug}: store instance retired (re-embed activated)")
            self._ensure_open_locked()
            if self._schema == 2:
                return self._append_groups_locked(prepared)
            new_ids: list[str] = []
            new_rows: list[bytes] = []
            seen: set[str] = set()
            for snap_id, vec in prepared:
                if snap_id in self._id_set or snap_id in seen:
                    continue  # idempotent: first write wins
                seen.add(snap_id)
                norm = float(np.linalg.norm(vec))
                if norm > 0:
                    vec = vec / norm
                new_ids.append(snap_id)
                new_rows.append(vec.astype("<f4").tobytes())
            if not new_ids:
                return 0

            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.vectors_path, "ab") as f:
                f.write(b"".join(new_rows))
                f.flush()
                os.fsync(f.fileno())
            # Vector rows are durable before ids.json names them — a crash
            # between the two leaves orphan rows that open() heals away.
            self._ids.extend(new_ids)
            self._id_set.update(new_ids)
            _atomic_write_json(self.ids_path, self._ids)
            self._write_meta_locked()
            self._matrix = None  # re-read lazily on next search
            return len(new_ids)

    def _append_groups_locked(self, prepared: list) -> int:
        """v2 write path: consecutive same-snap_id runs land as whole groups.

        A group whose snap_id already exists (in the store or earlier in the
        batch) is skipped WHOLE — snap_id membership always means "full group
        present" (post-heal invariant), which is what keeps transcode/migrate
        crash-rerun idempotency working unchanged.
        """
        new_ids: list[str] = []
        new_ordinals: list[int] = []
        new_rows: list[bytes] = []
        seen: set[str] = set()
        i = 0
        while i < len(prepared):
            snap_id = prepared[i][0]
            j = i
            while j < len(prepared) and prepared[j][0] == snap_id:
                j += 1
            if snap_id not in self._id_set and snap_id not in seen:
                seen.add(snap_id)
                for ordinal, (_, vec) in enumerate(prepared[i:j]):
                    norm = float(np.linalg.norm(vec))
                    if norm > 0:
                        vec = vec / norm
                    new_ids.append(snap_id)
                    new_ordinals.append(ordinal)
                    new_rows.append(vec.astype("<f4").tobytes())
            i = j
        if not new_ids:
            return 0

        self.dir.mkdir(parents=True, exist_ok=True)
        if not self.meta_path.exists():
            # F1 belt-and-braces: persist the schema identity BEFORE any data
            # lands, so no crash point during the first-ever append leaves a
            # metaless dir that autodetect could misread as v1.
            self._write_meta_locked()
        with open(self.vectors_path, "ab") as f:
            f.write(b"".join(new_rows))
            f.flush()
            os.fsync(f.fileno())
        # Write order (meta-on-materialize) → vectors → ids → ordinals → meta:
        # a crash at any point heals back to the last complete-group boundary
        # on the next open (ids/ordinals rewrites are individually atomic and
        # always end at a group boundary, so only a vectors↔sidecar length
        # mismatch can ever expose a partial group — which the heal drops
        # whole).
        self._ids.extend(new_ids)
        self._id_set.update(new_ids)
        self._ordinals.extend(new_ordinals)
        _atomic_write_json(self.ids_path, self._ids)
        _atomic_write_json(self.ordinals_path, self._ordinals)
        self._generation += 1
        self._write_meta_locked()
        self._matrix = None  # re-read lazily on next search
        return len(new_ids)

    def _write_meta_locked(self) -> None:
        meta = {
            "slug": self.slug,
            "dims": self.dims,
            "normalized": True,
            "count": len(self._ids),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        if self._schema == 2:
            # count stays SNAPSHOT currency on v2 — the status/UI binding
            # contract reads it (audit A11); rows is the new row count.
            snapshots = len(set(self._ids))
            meta["count"] = snapshots
            meta["schema"] = 2
            meta["rows"] = len(self._ids)
            meta["snapshots"] = snapshots
            meta["generation"] = self._generation
            # content_mode is a v2 concept (chunk vectors) — written on v2 only
            # so the v1 meta key set stays frozen (status/UI binding contract).
            meta["content_mode"] = self._content_mode
        _atomic_write_json(self.meta_path, meta)

    def close(self) -> None:
        """Retire this instance (re-embed activation): further appends RAISE so a
        mint that still holds this handle cannot write stale state into the dir
        that a same-slug promotion just replaced."""
        with self._lock:
            self._closed = True
            self._matrix = None

    # ── read path ────────────────────────────────────────────────────────────

    @property
    def count(self) -> int:
        """SNAPSHOT currency on every schema (v1 has one row per snapshot)."""
        with self._lock:
            self._ensure_open_locked()
            if self._schema == 2:
                return len(self._id_set)
            return len(self._ids)

    @property
    def rows(self) -> int:
        """Raw vector-row count (== count on v1; >= snapshots on v2)."""
        with self._lock:
            self._ensure_open_locked()
            return len(self._ids)

    @property
    def snapshots(self) -> int:
        """Distinct snap_ids present (full groups, post-heal)."""
        with self._lock:
            self._ensure_open_locked()
            return len(self._id_set)

    @property
    def schema(self) -> int:
        with self._lock:
            self._ensure_open_locked()
            return self._schema

    @property
    def content_mode(self) -> str:
        """"full" (chunk vectors from the whole envelope-inclusive text — the
        default and today's behavior) or "body" (chunk vectors from the
        body-only content, M14.3). v2-persisted; a v1 store is always "full"."""
        with self._lock:
            self._ensure_open_locked()
            return self._content_mode

    def ids(self) -> set:
        with self._lock:
            self._ensure_open_locked()
            return set(self._id_set)

    def missing(self, all_snap_ids) -> list:
        """snap_ids not yet in the store, preserving input order."""
        with self._lock:
            self._ensure_open_locked()
            present = self._id_set
            return [sid for sid in all_snap_ids if sid not in present]

    def _get_matrix_locked(self):
        if self._closed:
            return None            # retired instance: surface nothing, never stale-labeled rows
        if self._matrix is None and self.vectors_path.exists():
            try:
                self._matrix = np.fromfile(self.vectors_path, dtype="<f4").reshape(
                    -1, self.dims
                )
            except (OSError, ValueError) as e:
                print(f"[VECSTORE] {self.slug}: matrix re-read failed ({e}); returning empty")
                self._matrix = None
        return self._matrix

    def search(self, query_vec, k: int, allowed_ids=None) -> list:
        """Top-k cosine matches as [(snap_id, score), ...].

        Performance constraint: scoring is ONE numpy mat-vec over the whole
        matrix (rows are pre-normalized), never a python loop over rows.

        v2 stores collapse to UNIQUE snapshots during the descent (audit A1):
        the first row hit per snap_id is by argsort construction its
        max-cosine best chunk, and k counts distinct snapshots. v1 stores
        keep today's loop untouched (one row per id — collapse is a no-op).
        """
        with self._lock:
            self._ensure_open_locked()
            matrix = self._get_matrix_locked()
            ids = list(self._ids)
            schema = self._schema
        if matrix is None or matrix.shape[0] == 0:
            return []

        q = np.asarray(query_vec, dtype=np.float32)
        norm = float(np.linalg.norm(q))
        if norm > 0:
            q = q / norm
        scores = matrix @ q

        results = []
        if schema == 2:
            seen: set = set()
            for i in np.argsort(scores)[::-1]:
                sid = ids[i]
                if allowed_ids is not None and sid not in allowed_ids:
                    continue
                if sid in seen:
                    continue  # later chunk of an already-returned snapshot
                seen.add(sid)
                results.append((sid, float(scores[i])))
                if len(results) >= k:
                    break
            return results
        for i in np.argsort(scores)[::-1]:
            sid = ids[i]
            if allowed_ids is not None and sid not in allowed_ids:
                continue
            results.append((sid, float(scores[i])))
            if len(results) >= k:
                break
        return results

    def search_with_vectors(self, query_vec, k: int, allowed_ids=None,
                            with_ordinals: bool = False) -> list:
        """Top-k cosine matches WITH the matched row vector.

        Identical scoring to :meth:`search` (ONE numpy mat-vec over the
        pre-normalized matrix) but each result also carries the matched row,
        as [(snap_id, score, vector_np), ...]. The returned vectors are the
        L2-normalized stored rows (copied so callers can't mutate the matrix),
        so a downstream `vec @ other_vec` is a true cosine similarity — exactly
        what MMR diversity needs. Only the top-k rows are materialized; the
        full matrix is never copied.

        v2 stores collapse to unique snapshots exactly like :meth:`search`;
        the returned vector is the BEST chunk's row (first hit in the
        descent), so MMR diversifies on each snapshot's most-relevant chunk.

        with_ordinals (M8/WI-7a, OPT-IN — the default 3-tuple contract is
        frozen): when True, each tuple gains a 4th element `best_ordinal` —
        the winning row's chunk ordinal within its snapshot group on a v2
        store (0 = whole-doc/single-chunk = "no specific window"; >= 1 = a
        specific chunk won the collapse), or None on a v1 store (one whole-
        doc row per snapshot — there is no chunk identity to report). This
        is the best-chunk IDENTITY the matched-chunk delivery windowing
        needs; the vector alone cannot recover which span matched.
        """
        with self._lock:
            self._ensure_open_locked()
            matrix = self._get_matrix_locked()
            ids = list(self._ids)
            if matrix is None or matrix.shape[0] == 0:
                return []

            q = np.asarray(query_vec, dtype=np.float32)
            norm = float(np.linalg.norm(q))
            if norm > 0:
                q = q / norm
            scores = matrix @ q

            results = []
            if self._schema == 2:
                seen: set = set()
                for i in np.argsort(scores)[::-1]:
                    sid = ids[i]
                    if allowed_ids is not None and sid not in allowed_ids:
                        continue
                    if sid in seen:
                        continue  # later chunk of an already-returned snapshot
                    seen.add(sid)
                    row = (sid, float(scores[i]), matrix[i].copy())
                    if with_ordinals:
                        row = row + (self._ordinals[i],)
                    results.append(row)
                    if len(results) >= k:
                        break
                return results
            for i in np.argsort(scores)[::-1]:
                sid = ids[i]
                if allowed_ids is not None and sid not in allowed_ids:
                    continue
                # Copy the row so the caller can never mutate the live matrix.
                row = (sid, float(scores[i]), matrix[i].copy())
                if with_ordinals:
                    row = row + (None,)  # v1: whole-doc rows, no chunk identity
                results.append(row)
                if len(results) >= k:
                    break
            return results

    def max_cosine_for(self, query_vec, sids) -> dict:
        """Max cosine (over a snapshot's chunk rows) for each requested snap_id.

        One mat-vec over the whole pre-normalized matrix (same cost as
        :meth:`search`, NOT a per-id lookup loop) -> {sid: max_cosine} for the
        requested sids that exist in the store; absent sids are omitted. Used by
        the gated-keyword lever (retrieval.py) to gate keyword-only candidates on
        their TRUE semantic cosine — a candidate that only matched lexically
        (cosine below the model's floor) is dropped instead of RRF-injected.
        """
        want = set(sids)
        if not want:
            return {}
        with self._lock:
            self._ensure_open_locked()
            matrix = self._get_matrix_locked()
            ids = list(self._ids)
        if matrix is None or matrix.shape[0] == 0:
            return {}
        q = np.asarray(query_vec, dtype=np.float32)
        norm = float(np.linalg.norm(q))
        if norm > 0:
            q = q / norm
        scores = matrix @ q
        out: dict = {}
        for i, sid in enumerate(ids):
            if sid in want:
                s = float(scores[i])
                if s > out.get(sid, -2.0):
                    out[sid] = s
        return out


# ── module-level helpers ─────────────────────────────────────────────────────

_STORES: dict[tuple[str, str], VectorStore] = {}
_STORES_LOCK = threading.Lock()


def get_store(slug: str, dims: int = None, base_dir=None,
              schema: "int | None" = None, content_mode: str = "full") -> VectorStore:
    """Canonical-instance factory: ONE live VectorStore per (base_dir, slug).

    Two instances on the same directory would race each other's files, so all
    production code must come through here. dims defaults from
    EMBEDDING_MODELS[slug]; base_dir defaults to config.EMBEDDINGS_STORES_DIR.
    The key uses the realpath of base_dir so aliased paths share an instance.

    schema=None (default) autodetects from meta.json — fresh dirs stay v1
    until the M6f cutover. schema=2 creates/requires a chunk-group store (the
    6d rebuild path); an explicit request conflicting with an existing store
    (cached or on disk) refuses.
    """
    if dims is None:
        try:
            dims = EMBEDDING_MODELS[slug]["dims"]
        except KeyError:
            raise ValueError(
                f"unknown embedding model slug {slug!r}; "
                f"known: {sorted(EMBEDDING_MODELS)}"
            ) from None
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    key = (os.path.realpath(base), slug)
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            # open() before caching: a dims-mismatch refusal must not leave a
            # poisoned entry behind. content_mode is the FRESH-store request
            # (like schema): an existing store's on-disk value wins in open().
            store = VectorStore(
                slug, dims, base, schema=schema, content_mode=content_mode
            ).open()
            _STORES[key] = store
        elif schema is not None and store.schema != schema:
            raise ValueError(
                f"{slug}: live store is schema {store.schema}, "
                f"requested {schema}"
            )
        return store


def store_dir(slug: str, base_dir=None) -> Path:
    """Filesystem dir for a model's store: {base_dir or EMBEDDINGS_STORES_DIR}/{slug}."""
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    return base / slug


def evict_store(slug: str, base_dir=None) -> bool:
    """Drop the cached VectorStore for (base_dir, slug); True if one was cached.

    The re-embed activation seam renames a store dir out from under its cached
    instance; the next get_store must re-open the NEW dir, so the stale
    (realpath(base), slug) entry has to go. Same realpath keying as get_store.
    """
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    key = (os.path.realpath(base), slug)
    with _STORES_LOCK:
        return _STORES.pop(key, None) is not None


def store_exists(slug: str, base_dir=None) -> bool:
    """True when slug has ANY store artifact on disk under base_dir.

    The fresh-vs-existing probe for the migration target schema policy
    (post-gate default flip): a store only materializes files on first
    append, so an absent/empty dir means "fresh". meta.json alone is not
    enough — external damage can lose it while ids/vectors/ordinals survive
    (the F1 autodetect case), and a damaged-but-real store must never read
    as fresh: its schema is whatever open()'s autodetect recovers, never
    the fresh-store default.
    """
    d = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR) / slug
    return any(
        (d / name).exists()
        for name in (META_FILE, IDS_FILE, VECTORS_FILE, ORDINALS_FILE)
    )


def list_stores(base_dir) -> list:
    """[{slug, dims, count, schema, rows, last_updated}] from meta.json files;
    skips malformed dirs.

    `count` is SNAPSHOT currency on every schema (the status/UI binding
    contract, audit A11) — v2 metas keep count == snapshots while `rows` is
    the raw chunk-row count. v1 metas (no schema key) report schema 1 and
    rows == count (one row per snapshot).
    """
    base = Path(base_dir)
    if not base.is_dir():
        return []
    stores = []
    for child in sorted(base.iterdir()):
        meta_path = child / META_FILE
        if not child.is_dir() or not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            # A store's live dir is always named EXACTLY its slug; retained
            # rollbacks ({slug}.pre-rebuild.<ts>) and interrupted-swap staging
            # ({slug}.incoming) carry a copied meta.json but a different dir
            # name — they must never shadow the live store in status/list.
            # Safe for dotted slugs (e.g. qwen3-embedding-0.6b): compares the
            # whole dir name, not a prefix.
            if meta["slug"] != child.name:
                continue
            stores.append({
                "slug": meta["slug"],
                "dims": meta["dims"],
                "count": meta["count"],
                "schema": meta.get("schema", 1),
                "rows": meta.get("rows", meta["count"]),
                "last_updated": meta["last_updated"],
            })
        except (json.JSONDecodeError, KeyError, OSError, UnicodeDecodeError) as e:
            print(f"[VECSTORE] skipping malformed store dir {child}: {e}")
    return stores


def get_active_slug(base_dir=None) -> str:
    """Active model slug from {base_dir}/active.json, else the config default."""
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    try:
        return json.loads((base / ACTIVE_FILE).read_text(encoding="utf-8"))["active"]
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError, KeyError):
        return config.EMBEDDINGS_ACTIVE_DEFAULT


def set_active_slug(slug: str, base_dir=None) -> None:
    """Atomically point active.json at a registry slug (cutover seam)."""
    if slug not in EMBEDDING_MODELS:
        raise ValueError(
            f"unknown embedding model slug {slug!r}; known: {sorted(EMBEDDING_MODELS)}"
        )
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    base.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(base / ACTIVE_FILE, {"active": slug})


# ── Per-box keep_alive override (local Ollama models) ────────────────────────
# How long Ollama keeps a model resident after the last embed. WARM pins it in
# RAM for instant embeds; COLD frees the RAM and reloads on demand (the first
# embed after idle pays the cold-load cost). The registry ships a conservative
# default per model (small boxes can't afford to pin a 6GB model); this is the
# per-box override the wizard toggle writes, stored as runtime state in Manifest
# (NOT config.ini, which code never rewrites). Only meaningful for ollama models.
KEEP_ALIVE_FILE = "keep_alive.json"
KEEP_ALIVE_WARM = "-1m"   # negative duration = stay resident indefinitely
KEEP_ALIVE_COLD = "5m"    # unload 5 minutes after the last embed


def get_keep_alive(slug: str, base_dir=None, fallback=None) -> "str | None":
    """Effective keep_alive for slug: per-box override, else the registry
    default, else `fallback` (for synthetic entries not in the registry)."""
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    try:
        overrides = json.loads((base / KEEP_ALIVE_FILE).read_text(encoding="utf-8"))
        if isinstance(overrides, dict) and slug in overrides:
            return overrides[slug]
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
        pass
    entry = EMBEDDING_MODELS.get(slug)
    if entry is not None:
        return entry.get("keep_alive")
    return fallback


def set_keep_alive(slug: str, warm: bool, base_dir=None) -> str:
    """Write the per-box keep_alive override for a LOCAL model; returns the
    value written. WARM → resident forever; cold → idle-unload."""
    entry = EMBEDDING_MODELS.get(slug)
    if entry is None:
        raise ValueError(f"unknown embedding model slug {slug!r}")
    if entry["provider"] != "ollama":
        raise ValueError(f"{slug!r} is not a local model; keep_alive is Ollama-only")
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    base.mkdir(parents=True, exist_ok=True)
    path = base / KEEP_ALIVE_FILE
    try:
        overrides = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(overrides, dict):
            overrides = {}
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
        overrides = {}
    value = KEEP_ALIVE_WARM if warm else KEEP_ALIVE_COLD
    overrides[slug] = value
    _atomic_write_json(path, overrides)
    return value


def is_warm(value) -> bool:
    """True when a keep_alive value means 'stay resident' (negative duration)."""
    return isinstance(value, str) and value.strip().startswith("-")


# ── Per-box device placement (local Ollama models, WI-9) ─────────────────────
# Which device a local model runs on: "cpu" pins it off the GPU (the provider
# sends options.num_gpu: 0 — zero layers offloaded), "gpu"/absent = auto
# (num_gpu omitted; Ollama offloads to the GPU when one exists). Same
# runtime-state pattern as keep_alive.json above: written by the wizard/Portal
# toggle via POST /embeddings/placement, read fresh per embed call so a toggle
# takes effect on the model's next load without a restart. Only meaningful for
# ollama models; recommendations come from Orchestrator/hardware.probe().
PLACEMENT_FILE = "placement.json"
PLACEMENTS = ("gpu", "cpu")


def get_placement(slug: str, base_dir=None) -> "str | None":
    """Persisted placement override for slug: "gpu" / "cpu", or None (= auto,
    Ollama decides). Unknown values in the file read as None — fail-open to
    today's auto behavior, never to a surprise CPU pin."""
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    try:
        overrides = json.loads((base / PLACEMENT_FILE).read_text(encoding="utf-8"))
        if isinstance(overrides, dict):
            value = overrides.get(slug)
            if value in PLACEMENTS:
                return value
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
        pass
    return None


def set_placement(slug: str, placement: "str | None", base_dir=None) -> "str | None":
    """Write (placement in PLACEMENTS) or clear (None = auto) the per-box
    placement for a LOCAL model; returns the value written."""
    entry = EMBEDDING_MODELS.get(slug)
    if entry is None:
        raise ValueError(f"unknown embedding model slug {slug!r}")
    if entry["provider"] != "ollama":
        raise ValueError(f"{slug!r} is not a local model; placement is Ollama-only")
    if placement is not None and placement not in PLACEMENTS:
        raise ValueError(
            f"placement must be one of {PLACEMENTS} or None (auto), got {placement!r}"
        )
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    base.mkdir(parents=True, exist_ok=True)
    path = base / PLACEMENT_FILE
    try:
        overrides = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(overrides, dict):
            overrides = {}
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
        overrides = {}
    if placement is None:
        overrides.pop(slug, None)
    else:
        overrides[slug] = placement
    _atomic_write_json(path, overrides)
    return placement


# ── Reranker selection sidecar (tiered reranker, M4) ─────────────────────────
# Which reranker the box uses: {enabled, provider, model}. Same runtime-state
# pattern as placement.json / keep_alive.json above (beside the stores under
# EMBEDDINGS_STORES_DIR, atomic write, fail-open read) — written by the wizard /
# Portal / POST /rerank/select, read fresh by rerank.get_settings() so a
# selection (and a live-pasted key mirrored into os.environ) takes effect on the
# next retrieve() without a restart or a config.ini edit. UNLIKE placement (a
# per-slug map), the WHOLE file IS the single selection object — not keyed by
# slug. rerank.get_settings() layers this ABOVE config.ini [rerank] and the code
# fallback (sidecar > config > default). Absent/corrupt/wrong-shape → None =
# fall back to config (audit A13 fresh-box rule: no sidecar = inert null).
RERANK_FILE = "rerank.json"


def get_rerank_selection(base_dir=None) -> "dict | None":
    """Persisted reranker selection {enabled, provider, model}, or None when the
    sidecar is absent/corrupt/not an object — fail-open exactly like
    get_placement (FileNotFoundError/NotADirectoryError/JSONDecodeError → None),
    so a missing or hand-mangled file resolves to the config/default path,
    never an exception."""
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    try:
        selection = json.loads((base / RERANK_FILE).read_text(encoding="utf-8"))
        if isinstance(selection, dict):
            return selection
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError):
        pass
    return None


def set_rerank_selection(selection: dict, base_dir=None) -> dict:
    """Write the reranker selection atomically (tmp + os.replace, mkdir parents);
    returns the dict written. The whole file IS the selection object (a single
    write REPLACES it wholesale — not a per-slug merge like set_placement)."""
    base = Path(base_dir if base_dir is not None else config.EMBEDDINGS_STORES_DIR)
    base.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(base / RERANK_FILE, selection)
    return selection
