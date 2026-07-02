"""Store schema v2 — chunk groups, ordinals sidecar, collapse-in-search (M6 6a).

Design fixed by docs/plans/2026-07-01-retrieval-upgrade-spec-audit.md A1–A3/A5:
- ids.json rows stay BARE snap_ids (repeated per chunk); chunk ordinals live in
  a parallel ordinals.json sidecar. Currency of ids()/missing()/allowed_ids is
  bare snap_id everywhere.
- v2 meta adds {schema: 2, rows, snapshots, generation}; `count` stays SNAPSHOT
  currency (status/UI binding contract). v1 meta never gains the new keys.
- Chunk groups are atomic: one append_many call, whole-group idempotency on
  snap_id, self-heal drops a torn trailing group entirely.
- search/search_with_vectors collapse to unique snapshots during the argsort
  descent (first hit per snap_id IS its max-cosine best chunk).

All tests are hermetic against tmp_path — never the live Manifest/ stores.
"""
import json
import threading

import numpy as np
import pytest

from Orchestrator.embeddings.store import VectorStore, get_store

DIMS = 4
ROW_BYTES = 4 * DIMS  # float32

SLUG = "unit-v2-model"


def _v2_store(tmp_path, slug=SLUG, dims=DIMS):
    return VectorStore(slug, dims, tmp_path, schema=2).open()


def _basis(i):
    vec = [0.0] * DIMS
    vec[i % DIMS] = 1.0
    return vec


def _read_json(tmp_path, slug, name):
    return json.loads((tmp_path / slug / name).read_text(encoding="utf-8"))


def _assert_group_runs_contiguous(ids, ordinals):
    """Every snap_id forms exactly ONE contiguous run with ordinals 0..n-1."""
    assert len(ids) == len(ordinals)
    seen = set()
    i = 0
    while i < len(ids):
        sid = ids[i]
        assert sid not in seen, f"group {sid} split into multiple runs"
        seen.add(sid)
        j = i
        while j < len(ids) and ids[j] == sid:
            assert ordinals[j] == j - i, f"{sid}: ordinal ramp broken at row {j}"
            j += 1
        i = j


GROUP_X = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]]


# ── meta / schema detection ──────────────────────────────────────────────────

def test_v2_meta_fields(tmp_path):
    store = _v2_store(tmp_path)
    store.append_group("SNAP-X", GROUP_X)
    store.append("SNAP-Y", [0.0, 0.0, 0.0, 1.0])

    meta = _read_json(tmp_path, SLUG, "meta.json")
    assert meta["schema"] == 2
    assert meta["rows"] == 4
    assert meta["snapshots"] == 2
    # Binding contract (audit A11): count stays SNAPSHOT currency on v2.
    assert meta["count"] == 2
    assert meta["generation"] >= 1
    assert meta["slug"] == SLUG
    assert meta["dims"] == DIMS
    assert meta["normalized"] is True
    assert meta["last_updated"]

    # autodetect: reopening WITHOUT a schema request reads v2 from meta
    reopened = VectorStore(SLUG, DIMS, tmp_path).open()
    assert reopened.schema == 2
    assert reopened.rows == 4
    assert reopened.snapshots == 2
    assert reopened.count == 2

    # v1 meta (absent schema key) reads as schema 1
    v1 = VectorStore("unit-v1-model", DIMS, tmp_path).open()
    v1.append("SNAP-Z", [1.0, 0.0, 0.0, 0.0])
    assert VectorStore("unit-v1-model", DIMS, tmp_path).open().schema == 1


def test_v1_store_reads_unchanged(tmp_path):
    """A store dir without schema/ordinals behaves exactly as today."""
    store = VectorStore("unit-v1-model", DIMS, tmp_path).open()
    assert store.schema == 1
    store.append("snap-a", [1.0, 0.0, 0.0, 0.0])
    store.append_many([("snap-b", [0.0, 1.0, 0.0, 0.0])])

    store_dir = tmp_path / "unit-v1-model"
    # v1 meta never gains the new keys — exact key set unchanged on write
    meta = _read_json(tmp_path, "unit-v1-model", "meta.json")
    assert set(meta) == {"slug", "dims", "normalized", "count", "last_updated"}
    assert meta["count"] == 2
    # no ordinals sidecar appears on a v1 store
    assert not (store_dir / "ordinals.json").exists()

    assert store.count == 2
    assert store.rows == 2
    assert store.snapshots == 2
    results = store.search([1.0, 0.1, 0.0, 0.0], k=2)
    assert [sid for sid, _ in results] == ["snap-a", "snap-b"]

    # reopen: no heal, files untouched
    before = {
        p.name: p.read_bytes()
        for p in store_dir.iterdir() if p.name != "meta.json"
    }
    reopened = VectorStore("unit-v1-model", DIMS, tmp_path).open()
    assert reopened.schema == 1
    after = {
        p.name: p.read_bytes()
        for p in store_dir.iterdir() if p.name != "meta.json"
    }
    assert after == before


def test_fresh_store_defaults_v1_schema2_only_when_requested(tmp_path):
    """Conservative default (M6f flips it): fresh stores stay v1 unless
    schema=2 is requested explicitly — including through get_store."""
    default = VectorStore("fresh-default", DIMS, tmp_path).open()
    default.append("snap-a", [1.0, 0.0, 0.0, 0.0])
    assert default.schema == 1
    assert "schema" not in _read_json(tmp_path, "fresh-default", "meta.json")

    via_factory = get_store("fresh-v2", dims=DIMS, base_dir=tmp_path, schema=2)
    via_factory.append("snap-a", [1.0, 0.0, 0.0, 0.0])
    assert via_factory.schema == 2
    assert _read_json(tmp_path, "fresh-v2", "meta.json")["schema"] == 2
    # factory returns the cached instance on a matching re-request
    assert get_store("fresh-v2", dims=DIMS, base_dir=tmp_path, schema=2) is via_factory


def test_schema_mismatch_raises(tmp_path):
    _v2_store(tmp_path).append_group("SNAP-X", GROUP_X)
    with pytest.raises(ValueError):
        VectorStore(SLUG, DIMS, tmp_path, schema=1).open()

    v1 = VectorStore("unit-v1-model", DIMS, tmp_path).open()
    v1.append("snap-a", [1.0, 0.0, 0.0, 0.0])
    with pytest.raises(ValueError):
        VectorStore("unit-v1-model", DIMS, tmp_path, schema=2).open()
    with pytest.raises(ValueError):
        VectorStore("anything", DIMS, tmp_path, schema=3)


# ── group append ─────────────────────────────────────────────────────────────

def test_group_append_atomic(tmp_path):
    store = _v2_store(tmp_path)
    written = store.append_group("SNAP-X", GROUP_X)
    assert written == 3
    assert _read_json(tmp_path, SLUG, "ids.json") == ["SNAP-X"] * 3
    assert _read_json(tmp_path, SLUG, "ordinals.json") == [0, 1, 2]
    assert (tmp_path / SLUG / "vectors.f32").stat().st_size == 3 * ROW_BYTES
    assert store.rows == 3
    assert store.snapshots == 1
    assert store.count == 1  # snapshot currency


def test_group_append_idempotent(tmp_path):
    store = _v2_store(tmp_path)
    assert store.append_group("SNAP-X", GROUP_X) == 3
    # whole-group skip, first wins — even with different vectors/chunk counts
    assert store.append_group("SNAP-X", [[0.0, 0.0, 0.0, 1.0]]) == 0
    assert store.append_many([("SNAP-X", [0.0, 0.0, 0.0, 1.0])]) == 0
    assert store.rows == 3
    assert _read_json(tmp_path, SLUG, "ordinals.json") == [0, 1, 2]
    assert (tmp_path / SLUG / "vectors.f32").stat().st_size == 3 * ROW_BYTES


def test_v2_single_vector_append_is_group_of_one(tmp_path):
    store = _v2_store(tmp_path)
    store.append("SNAP-S", [1.0, 0.0, 0.0, 0.0])
    assert _read_json(tmp_path, SLUG, "ids.json") == ["SNAP-S"]
    assert _read_json(tmp_path, SLUG, "ordinals.json") == [0]
    meta = _read_json(tmp_path, SLUG, "meta.json")
    assert meta["rows"] == 1 and meta["snapshots"] == 1 and meta["count"] == 1


def test_append_group_on_v1_store_raises(tmp_path):
    """Fail loud: a silent first-wins single row would masquerade as a group."""
    store = VectorStore("unit-v1-model", DIMS, tmp_path).open()
    with pytest.raises(ValueError):
        store.append_group("SNAP-X", GROUP_X)
    assert store.rows == 0


def test_groups_never_span_batches(tmp_path):
    store = _v2_store(tmp_path)
    # one append_many with mixed groups: contiguous runs, per-group ordinals
    wrote = store.append_many([
        ("SNAP-A", [1.0, 0.0, 0.0, 0.0]),
        ("SNAP-A", [0.0, 1.0, 0.0, 0.0]),
        ("SNAP-B", [0.0, 0.0, 1.0, 0.0]),
        ("SNAP-B", [0.0, 0.0, 0.0, 1.0]),
        ("SNAP-B", [1.0, 1.0, 0.0, 0.0]),
    ])
    assert wrote == 5
    assert _read_json(tmp_path, SLUG, "ids.json") == ["SNAP-A"] * 2 + ["SNAP-B"] * 3
    assert _read_json(tmp_path, SLUG, "ordinals.json") == [0, 1, 0, 1, 2]

    # concurrent batches: each batch's groups land under ONE lock hold, so a
    # group can never interleave with another batch's rows
    n_threads, n_ops = 4, 10
    errors = []

    def worker(t):
        try:
            for i in range(n_ops):
                store.append_many(
                    [(f"SNAP-{t}-{i}", _basis(j)) for j in range(3)]
                    + [(f"SNAP-{t}-{i}-solo", _basis(t))]
                )
        except Exception as e:  # surfaced via assert below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []

    ids = _read_json(tmp_path, SLUG, "ids.json")
    ordinals = _read_json(tmp_path, SLUG, "ordinals.json")
    _assert_group_runs_contiguous(ids, ordinals)
    total_rows = 5 + n_threads * n_ops * 4
    assert store.rows == total_rows
    assert (tmp_path / SLUG / "vectors.f32").stat().st_size == total_rows * ROW_BYTES
    # a fresh open sees consistent files (no heal needed)
    reopened = VectorStore(SLUG, DIMS, tmp_path).open()
    assert reopened.schema == 2
    assert reopened.rows == total_rows


# ── search collapse (audit A1: collapse lives IN the store) ──────────────────

QUERY = [1.0, 0.0, 0.0, 0.0]

# SNAP-A's first three chunks occupy raw ranks 1-3 against QUERY
# (cos 1.0, ~0.9995, ~0.9939); SNAP-B ~0.7071; SNAP-C 0.0.
CHUNKS_A = [
    [1.0, 0.0, 0.0, 0.0],   # best chunk
    [0.97, 0.03, 0.0, 0.0],
    [0.9, 0.1, 0.0, 0.0],
    [0.1, 0.9, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
]


def _chunked_corpus(tmp_path):
    store = _v2_store(tmp_path)
    store.append_group("SNAP-A", CHUNKS_A)
    store.append_group("SNAP-B", [[0.5, 0.5, 0.0, 0.0]])
    store.append("SNAP-C", [0.0, 1.0, 0.0, 0.0])
    return store


def test_search_collapses_to_unique_snapshots(tmp_path):
    store = _chunked_corpus(tmp_path)
    results = store.search_with_vectors(QUERY, k=3)
    # k = 3 UNIQUE snapshots, even though SNAP-A's chunks hold raw ranks 1-3
    assert [sid for sid, _, _ in results] == ["SNAP-A", "SNAP-B", "SNAP-C"]
    a_sid, a_score, a_vec = results[0]
    # best (max-cosine) chunk's score AND vector, not an arbitrary sibling's
    assert a_score == pytest.approx(1.0, abs=1e-5)
    assert np.allclose(a_vec, [1.0, 0.0, 0.0, 0.0], atol=1e-5)
    assert results[1][1] == pytest.approx(0.7071, abs=1e-3)
    # k smaller than the raw chunk span still yields k distinct snapshots
    two = store.search_with_vectors(QUERY, k=2)
    assert [sid for sid, _, _ in two] == ["SNAP-A", "SNAP-B"]


def test_collapse_covers_plain_search(tmp_path):
    store = _chunked_corpus(tmp_path)
    results = store.search(QUERY, k=3)
    assert [sid for sid, _ in results] == ["SNAP-A", "SNAP-B", "SNAP-C"]
    assert results[0][1] == pytest.approx(1.0, abs=1e-5)
    assert store.search(QUERY, k=2) == [
        ("SNAP-A", pytest.approx(1.0, abs=1e-5)),
        ("SNAP-B", pytest.approx(0.7071, abs=1e-3)),
    ]


def test_allowed_ids_scoping_on_chunked_store(tmp_path):
    store = _chunked_corpus(tmp_path)
    # bare-snap_id allowed_ids filters correctly; a scoped op gets results
    only_b = store.search(QUERY, k=5, allowed_ids={"SNAP-B"})
    assert [sid for sid, _ in only_b] == ["SNAP-B"]
    # scoping TO the chunked snapshot returns it once, best chunk's score
    only_a = store.search(QUERY, k=5, allowed_ids={"SNAP-A"})
    assert only_a == [("SNAP-A", pytest.approx(1.0, abs=1e-5))]
    with_vecs = store.search_with_vectors(QUERY, k=5, allowed_ids={"SNAP-A"})
    assert len(with_vecs) == 1
    assert np.allclose(with_vecs[0][2], [1.0, 0.0, 0.0, 0.0], atol=1e-5)
    assert store.search(QUERY, k=5, allowed_ids=set()) == []


def test_missing_is_snapshot_currency(tmp_path):
    store = _chunked_corpus(tmp_path)
    # ids() returns DISTINCT snap_ids; missing() diffs in snapshot currency
    assert store.ids() == {"SNAP-A", "SNAP-B", "SNAP-C"}
    assert store.missing(["SNAP-D", "SNAP-A", "SNAP-B", "SNAP-C", "SNAP-E"]) == [
        "SNAP-D", "SNAP-E",
    ]


# ── self-heal: 3 files + trailing-partial-group drop ─────────────────────────

GROUP_A2 = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
GROUP_B3 = [[0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0], [1.0, 1.0, 0.0, 0.0]]


def _torn_store(tmp_path, keep_rows_bytes):
    """A(2 chunks)+B(3 chunks), then vectors.f32 torn to keep_rows_bytes."""
    store = _v2_store(tmp_path)
    store.append_group("SNAP-A", GROUP_A2)
    store.append_group("SNAP-B", GROUP_B3)
    with open(tmp_path / SLUG / "vectors.f32", "r+b") as f:
        f.truncate(keep_rows_bytes)


@pytest.mark.parametrize("torn_bytes", [
    3 * ROW_BYTES + ROW_BYTES // 2,  # torn mid-row inside group B
    4 * ROW_BYTES,                   # clean row boundary but mid-group B
    2 * ROW_BYTES + 1,               # 1 byte into B's first row
])
def test_self_heal_truncates_three_files_and_drops_partial_group(tmp_path, torn_bytes):
    _torn_store(tmp_path, torn_bytes)

    healed = VectorStore(SLUG, DIMS, tmp_path).open()
    assert healed.schema == 2
    # the trailing PARTIAL group is dropped ENTIRELY (ordinal contiguity):
    # SNAP-B is fully absent, never a half-group
    assert healed.rows == 2
    assert healed.snapshots == 1
    assert healed.ids() == {"SNAP-A"}
    assert healed.missing(["SNAP-A", "SNAP-B"]) == ["SNAP-B"]
    # all three files healed consistently on disk
    assert (tmp_path / SLUG / "vectors.f32").stat().st_size == 2 * ROW_BYTES
    assert _read_json(tmp_path, SLUG, "ids.json") == ["SNAP-A"] * 2
    assert _read_json(tmp_path, SLUG, "ordinals.json") == [0, 1]
    meta = _read_json(tmp_path, SLUG, "meta.json")
    assert meta["rows"] == 2 and meta["snapshots"] == 1 and meta["count"] == 1
    # still searchable, and the healed snapshot re-appends cleanly
    assert [sid for sid, _ in healed.search(QUERY, k=5)] == ["SNAP-A"]
    assert healed.append_group("SNAP-B", GROUP_B3) == 3
    assert healed.rows == 5
    assert _read_json(tmp_path, SLUG, "ordinals.json") == [0, 1, 0, 1, 2]


def test_self_heal_ordinals_lagging_ids(tmp_path):
    """Crash between the ids and ordinals rewrites: vectors+ids carry the new
    group, ordinals.json is still the previous atomic state — the new group
    heals away whole (min-length is a group boundary by atomic-write order)."""
    store = _v2_store(tmp_path)
    store.append_group("SNAP-A", GROUP_A2)
    store_dir = tmp_path / SLUG
    # hand-craft the crash state: B's rows in vectors + ids, ordinals stale
    with open(store_dir / "vectors.f32", "ab") as f:
        for vec in GROUP_B3:
            f.write(np.asarray(vec, dtype="<f4").tobytes())
    (store_dir / "ids.json").write_text(json.dumps(["SNAP-A"] * 2 + ["SNAP-B"] * 3))

    healed = VectorStore(SLUG, DIMS, tmp_path).open()
    assert healed.rows == 2
    assert healed.ids() == {"SNAP-A"}
    assert (store_dir / "vectors.f32").stat().st_size == 2 * ROW_BYTES
    assert _read_json(tmp_path, SLUG, "ids.json") == ["SNAP-A"] * 2
    assert _read_json(tmp_path, SLUG, "ordinals.json") == [0, 1]
    assert healed.missing(["SNAP-A", "SNAP-B"]) == ["SNAP-B"]


def test_generation_bumps_on_append_and_heal(tmp_path):
    store = _v2_store(tmp_path)
    store.append("SNAP-1", [1.0, 0.0, 0.0, 0.0])
    gen1 = _read_json(tmp_path, SLUG, "meta.json")["generation"]
    assert gen1 >= 1
    store.append_group("SNAP-2", GROUP_A2)
    gen2 = _read_json(tmp_path, SLUG, "meta.json")["generation"]
    assert gen2 > gen1
    # a no-op append (duplicate) is not a mutation — no bump
    assert store.append("SNAP-1", [0.0, 1.0, 0.0, 0.0]) is None
    assert _read_json(tmp_path, SLUG, "meta.json")["generation"] == gen2

    # heal is a mutation: torn write -> reopen bumps generation
    with open(tmp_path / SLUG / "vectors.f32", "r+b") as f:
        f.truncate(2 * ROW_BYTES)  # mid-group SNAP-2
    VectorStore(SLUG, DIMS, tmp_path).open()
    gen3 = _read_json(tmp_path, SLUG, "meta.json")["generation"]
    assert gen3 > gen2
    # a clean reopen mutates nothing
    VectorStore(SLUG, DIMS, tmp_path).open()
    assert _read_json(tmp_path, SLUG, "meta.json")["generation"] == gen3
