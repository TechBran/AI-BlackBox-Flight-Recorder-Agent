"""Check the ledger against its index, its prefix-hash records and its
archive copies (audit rank 77, O05-ledger-persistence-02/-03).

run() walks:
  * every index entry: its byte range lies inside the ledger, starts with its
    own START marker and ends with its own END marker, and does not overlap
    the entry before it;
  * the bytes between entries: blank separators, or else counted (gap
    bytes), with any whole snapshot found there reported as unindexed and any
    START with no END as torn (an interrupted append);
  * Manifest/ledger_hashes.jsonl: each record's sha256 must be the hash of
    the ledger up to its byte_end (one pass over the file hashes every
    prefix), the records must run in order, and each must start where the
    one before it ended (the chain);
  * the archive copies the manifest lists: the ledger must still begin with
    each one (the prefix hash at the copy's size is the copy's recorded
    sha256), and with verify_copies the copy files themselves are hashed.

Read-only: it never writes unless the caller records the report
(write_report). Stdlib only, no Orchestrator.config import, so
scripts/ledger_fsck.py runs it on a box's files without loading the app; the
maintenance pass runs it once a day and /health shows the last report.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

from Orchestrator import ledger

# The snapshot markers, as bytes, found the way rebuild_snapshot_index finds
# them: anywhere on a line (early snapshots put a zero-width space before an
# END, or a version between its id and its UTC, and some STARTs have no
# closing ===).
_DASH = rb"(?:\xe2\x80\x94|-)"
START_MARK = re.compile(
    rb"===[ \t]*START SNAPSHOT[ \t]*" + _DASH + rb"[ \t]*UTC[ \t]*[^\r\n]*?" + _DASH
    + rb"[ \t]*(?P<snap>SNAP-\d{8}-\d+)(?!\d)")
END_MARK = re.compile(
    rb"===[ \t]*END SNAPSHOT[ \t]*" + _DASH + rb"[ \t]*(?P<snap>SNAP-\d{8}-\d+)(?!\d)[^\r\n]*?===")
# Between index entries an END without its closing === still ends a snapshot
# (the first days of the ledger wrote them so; rebuild never indexed those).
END_MARK_LOOSE = re.compile(
    rb"===[ \t]*END SNAPSHOT[ \t]*" + _DASH + rb"[ \t]*(?P<snap>SNAP-\d{8}-\d+)(?!\d)")

# How far into a snapshot its START marker, and how far back from its end
# its END marker, may sit.
_EDGE = 4096
# Problems listed by name in a report (the counts cover them all).
_MAX_LISTED = 50
REPORT_NAME = "ledger_fsck.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_index(index: Union[None, str, Path, Dict[str, Any]]) -> Dict[str, Any]:
    if isinstance(index, dict):
        return index
    if index is None:
        return {}
    with open(index, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def read_hash_records(path: Union[None, str, Path]) -> Dict[str, Any]:
    """The records of Manifest/ledger_hashes.jsonl, in file order, and how
    many lines did not parse."""
    records, bad = [], 0
    if path is None or not os.path.exists(path):
        return {"records": records, "bad_lines": bad}
    with open(path, "rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
                int(rec["byte_end"]), int(rec["byte_start"]), str(rec["sha256"])
            except (ValueError, KeyError, TypeError):
                bad += 1
                continue
            records.append(rec)
    return {"records": records, "bad_lines": bad}


def _prefix_digests(view: "ledger.LedgerView", boundaries: Iterable[int]) -> Dict[int, str]:
    """sha256(ledger[:b]) for every boundary b inside the ledger, in one pass."""
    out: Dict[int, str] = {}
    h = hashlib.sha256()
    pos = 0
    for b in sorted({int(b) for b in boundaries if 0 <= int(b) <= view.size}):
        while pos < b:
            end = min(b, pos + ledger.CHUNK)
            h.update(view.slice(pos, end))
            pos = end
        out[b] = h.copy().hexdigest()
    return out


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(ledger.CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _note(problems: List[str], text: str) -> None:
    if len(problems) < _MAX_LISTED:
        problems.append(text)


def _check_entries(view, index, problems) -> Dict[str, Any]:
    counts = {"indexed": len(index), "verified": 0, "out_of_bounds": 0, "bad_start": 0,
              "bad_end": 0, "overlapping": 0, "unindexed": 0, "duplicate_ids": 0, "torn": 0,
              "gap_regions": 0, "gap_bytes": 0, "tail_torn": False}
    entries = []
    for sid, meta in index.items():
        try:
            entries.append((int(meta["byte_start"]), int(meta["byte_end"]), sid))
        except (KeyError, TypeError, ValueError):
            counts["out_of_bounds"] += 1
            _note(problems, f"{sid}: no byte range")
    entries.sort()

    covered = 0  # the end of the furthest entry so far
    gaps = []
    for start, end, sid in entries:
        if start < 0 or end <= start or end > view.size:
            counts["out_of_bounds"] += 1
            _note(problems, f"{sid}: bytes {start}-{end} are outside the {view.size}-byte ledger")
            continue
        if start < covered:
            counts["overlapping"] += 1
            _note(problems, f"{sid}: bytes {start}-{end} overlap the entry ending at {covered}")
            covered = max(covered, end)
            continue
        if start > covered:
            gaps.append((covered, start, False))
        covered = end
        head = view.read(start, min(end, start + _EDGE))
        m = START_MARK.search(head)
        if not m or head[:m.start()].strip() or m.group("snap").decode() != sid:
            counts["bad_start"] += 1
            found = m.group("snap").decode() if m else "no START marker"
            _note(problems, f"{sid}: byte {start} holds {found}")
            continue
        tail_lo = max(start, end - _EDGE)
        tail = view.read(tail_lo, end)
        last = None
        for last in END_MARK.finditer(tail):
            pass
        if last is None or tail[last.end():].strip() or last.group("snap").decode() != sid:
            counts["bad_end"] += 1
            found = last.group("snap").decode() if last else "no END marker"
            _note(problems, f"{sid}: the range ending at byte {end} ends with {found}")
            continue
        counts["verified"] += 1
    if view.size > covered:
        gaps.append((covered, view.size, True))

    for lo, hi, at_tail in gaps:
        gap = view.read(lo, hi)
        if not gap.strip():
            continue
        counts["gap_regions"] += 1
        counts["gap_bytes"] += len(gap)
        ends: Dict[bytes, List[int]] = {}
        for m in END_MARK_LOOSE.finditer(gap):
            ends.setdefault(m.group("snap"), []).append(m.start())
        for m in START_MARK.finditer(gap):
            sid = m.group("snap")
            if any(pos > m.end() for pos in ends.get(sid, ())):
                if sid.decode() in index:
                    # The same id minted twice: the index holds the other one.
                    counts["duplicate_ids"] += 1
                    _note(problems, f"{sid.decode()}: a second snapshot with this id at byte "
                                    f"{lo + m.start()} (the index holds the other)")
                else:
                    counts["unindexed"] += 1
                    _note(problems, f"{sid.decode()}: a whole snapshot at byte {lo + m.start()} "
                                    f"has no index entry")
            else:
                counts["torn"] += 1
                if at_tail:
                    counts["tail_torn"] = True
                _note(problems, f"{sid.decode()}: a START at byte {lo + m.start()} has no END "
                                f"(an interrupted append)")
    return counts


def _check_records(records, bad_lines, index, digests, size, problems) -> Dict[str, Any]:
    counts = {"records": len(records), "verified": 0, "mismatched": 0, "past_eof": 0,
              "unordered": 0, "breaks": 0, "not_indexed": 0, "bad_lines": bad_lines,
              "first_byte": None, "last_byte_end": None}
    prev_end = None
    for rec in records:
        start, end, sid = int(rec["byte_start"]), int(rec["byte_end"]), rec.get("snap_id", "?")
        if counts["first_byte"] is None:
            counts["first_byte"] = start
        if end > size:
            counts["past_eof"] += 1
            _note(problems, f"record {sid}: ends at byte {end}, past the {size}-byte ledger")
            continue
        if prev_end is not None and end <= prev_end:
            counts["unordered"] += 1
            _note(problems, f"record {sid}: ends at byte {end}, not after the record before it ({prev_end})")
        elif prev_end is not None and start != prev_end:
            counts["breaks"] += 1
            _note(problems, f"record {sid}: starts at byte {start}, the record before it ended at {prev_end}")
        prev_end = end if prev_end is None else max(prev_end, end)
        counts["last_byte_end"] = end
        if digests.get(end) == rec["sha256"]:
            counts["verified"] += 1
        else:
            counts["mismatched"] += 1
            _note(problems, f"record {sid}: the ledger's first {end} bytes no longer hash to "
                            f"{rec['sha256'][:16]}… (bytes before {end} were changed)")
        meta = index.get(sid)
        if not meta or int(meta.get("byte_end", -1)) != end:
            counts["not_indexed"] += 1
    return counts


def _archive_entries(manifest: Optional[Dict[str, Any]], archive_dir: Optional[Path]):
    listed, missing = [], 0
    names = set()
    for entry in (manifest or {}).get("archive") or []:
        if not isinstance(entry, dict):
            continue
        p = Path(entry.get("path", ""))
        if archive_dir is not None and not p.is_absolute() and not p.exists():
            p = archive_dir / p.name
        if not p.exists():
            missing += 1
            continue
        names.add(p.name)
        listed.append((p, entry.get("sha256", ""), p.stat().st_size))
    unlisted = []
    if archive_dir is not None and archive_dir.exists():
        for p in sorted(archive_dir.glob("Volume_*.txt")):
            if p.name not in names:
                unlisted.append((p, None, p.stat().st_size))
    return listed, unlisted, missing


def run(volume: Union[str, Path], index: Union[None, str, Path, Dict[str, Any]] = None,
        hashes: Union[None, str, Path] = None, manifest: Union[None, str, Path, Dict[str, Any]] = None,
        archive_dir: Union[None, str, Path] = None, verify_copies: bool = False,
        size: Union[None, int, Callable[[], int]] = None) -> Dict[str, Any]:
    """Check the ledger at `volume` against its index (a path or the loaded
    dict), prefix-hash records, manifest and archive directory. Returns the
    report: counts per check, "problems" (the first few by name), "ok"
    (nothing that means changed or misplaced bytes) and "warnings".

    `size` is a committed ledger size to check up to, or a function that
    measures one, called once everything pointing into the ledger is read
    (the app measures it under mint_lock, so a mint part way through its
    append is not reported as a torn tail); by default the file's size."""
    problems: List[str] = []
    # Everything that points into the ledger is read before the ledger is
    # looked at: a mint appends before it indexes, records or copies, so all
    # of it lies inside the view, and a mint while this runs is never
    # mistaken for a record or entry past the end.
    idx = _load_index(index)
    recs = read_hash_records(hashes)
    if isinstance(manifest, dict) or manifest is None:
        man = manifest
    else:
        try:
            with open(manifest, "r", encoding="utf-8") as f:
                man = json.load(f)
        except (OSError, ValueError):
            man = None
    arc_dir = Path(archive_dir) if archive_dir is not None else None
    listed, unlisted, missing = _archive_entries(man, arc_dir)
    if callable(size):
        size = size()
    view = ledger.LedgerReader(max_files=1).view(volume)
    if size is not None:
        view = view.prefix(size)

    boundaries = [int(r["byte_end"]) for r in recs["records"]]
    boundaries += [size for _p, _s, size in listed + unlisted]
    boundaries.append(view.size)
    digests = _prefix_digests(view, boundaries)

    snapshots = _check_entries(view, idx, problems)
    chain = _check_records(recs["records"], recs["bad_lines"], idx, digests, view.size, problems)

    archives = {"copies": len(listed) + len(unlisted), "listed": len(listed),
                "unlisted": len(unlisted), "missing": missing, "verified": 0,
                "not_prefix": 0, "copy_mismatched": 0, "copies_hashed": verify_copies}
    for path, sha, size in listed + unlisted:
        prefix = digests.get(size) if size <= view.size else None
        if sha:
            if prefix != sha:
                archives["not_prefix"] += 1
                _note(problems, f"{path.name}: the ledger no longer begins with this copy "
                                f"({size} bytes)")
                continue
        if verify_copies:
            got = _file_sha256(path)
            if got != (sha or prefix):
                archives["copy_mismatched"] += 1
                _note(problems, f"{path.name}: the copy's bytes do not hash to "
                                f"{'its recorded sha256' if sha else 'the ledger prefix'}")
                continue
        if sha or verify_copies:
            archives["verified"] += 1

    failures = (snapshots["out_of_bounds"] + snapshots["bad_start"] + snapshots["bad_end"]
                + snapshots["overlapping"] + chain["mismatched"] + chain["past_eof"]
                + chain["unordered"] + archives["not_prefix"] + archives["copy_mismatched"])
    warnings = (snapshots["unindexed"] + snapshots["duplicate_ids"] + snapshots["torn"] + chain["breaks"]
                + chain["bad_lines"] + archives["missing"])
    return {
        "utc": _now(),
        "ok": failures == 0,
        "failures": failures,
        "warnings": warnings,
        "ledger": {"path": str(volume), "bytes": view.size, "sha256": digests.get(view.size, "")},
        "snapshots": snapshots,
        "hash_chain": chain,
        "archives": archives,
        "problems": problems,
    }


def summary(report: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A report without its problem list: what /health shows."""
    if not isinstance(report, dict):
        return None
    out = {k: v for k, v in report.items() if k != "problems"}
    out["problems_listed"] = len(report.get("problems") or [])
    return out


def write_report(path: Union[str, Path], report: Dict[str, Any]) -> None:
    from Orchestrator.utils import atomic_json  # stdlib-only module
    atomic_json.write_bytes(path, (json.dumps(report, indent=2) + "\n").encode("utf-8"), mode=None)


def read_report(path: Union[str, Path]) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def format_text(report: Dict[str, Any]) -> str:
    """The report as a few lines for a terminal."""
    s, c, a = report["snapshots"], report["hash_chain"], report["archives"]
    lines = [
        f"ledger   {report['ledger']['path']}: {report['ledger']['bytes']:,} bytes, "
        f"sha256 {report['ledger']['sha256'][:16]}",
        f"index    {s['indexed']} entries: {s['verified']} verified, {s['out_of_bounds']} out of "
        f"bounds, {s['bad_start']} bad start, {s['bad_end']} bad end, {s['overlapping']} overlapping",
        f"gaps     {s['gap_regions']} regions ({s['gap_bytes']:,} bytes): {s['unindexed']} unindexed "
        f"snapshot(s), {s['duplicate_ids']} duplicate id(s), {s['torn']} torn"
        f"{' (one at the tail)' if s['tail_torn'] else ''}",
        f"chain    {c['records']} records: {c['verified']} verified, {c['mismatched']} mismatched, "
        f"{c['breaks']} breaks, {c['unordered']} out of order, {c['past_eof']} past EOF, "
        f"{c['bad_lines']} unreadable lines",
        f"archive  {a['copies']} copies ({a['listed']} listed, {a['unlisted']} unlisted, "
        f"{a['missing']} listed but missing): {a['verified']} verified, {a['not_prefix']} not a "
        f"prefix of the ledger, {a['copy_mismatched']} corrupt"
        + ("" if a["copies_hashed"] else " (copy files not hashed; --verify-copies)"),
    ]
    for p in report.get("problems") or []:
        lines.append(f"  - {p}")
    lines.append("OK" if report["ok"] else f"FAILED: {report['failures']} problem(s)")
    return "\n".join(lines)
