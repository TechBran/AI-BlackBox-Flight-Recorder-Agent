#!/usr/bin/env python3
"""Check a box's ledger: every snapshot's byte range against the index, the
prefix-hash chain in Manifest/ledger_hashes.jsonl, and the archive copies the
manifest lists (Orchestrator/ledger_fsck.py; audit rank 77).

    python3 scripts/ledger_fsck.py                 # this checkout's box
    python3 scripts/ledger_fsck.py --root /path/to/box --json
    python3 scripts/ledger_fsck.py --verify-copies # also hash every Archive/ copy

Paths come from <root>/config.ini [paths] (volume, archive, manifest), and
the index is Manifest/snapshot_index.json beside the manifest; each can be
given instead. Read-only: nothing is written unless --record, which saves the
report as Manifest/ledger_fsck.json, the one /health shows. Safe on a running
box (the ledger only ever grows; the check reads the size it saw at start).
It loads neither the app nor its configuration module, so it runs with any
python3.

Exit status: 0 when the ledger checks out (warnings such as snapshots the
index does not list are reported, not failures), 1 when bytes were changed or
an index entry points at the wrong bytes, 2 when the files cannot be read.
"""
from __future__ import annotations

import argparse
import configparser
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from Orchestrator import ledger_fsck  # noqa: E402  (stdlib only)


def _paths(root: Path) -> dict:
    """The box's ledger paths from <root>/config.ini [paths], resolved
    against <root> (config.py resolves them against the service's working
    directory, which is the box root)."""
    cfg = configparser.ConfigParser()
    cfg.read(root / "config.ini", encoding="utf-8")
    paths = cfg["paths"] if cfg.has_section("paths") else {}

    def at(key: str, default: str) -> Path:
        p = Path(paths.get(key, default))
        return p if p.is_absolute() else root / p

    manifest = at("manifest", "./Manifest/manifest.json")
    return {
        "volume": at("volume", "./Volumes/SNAPSHOT_VOLUME.txt"),
        "archive": at("archive", "./Archive/"),
        "manifest": manifest,
        "index": manifest.parent / "snapshot_index.json",
        "hashes": manifest.parent / "ledger_hashes.jsonl",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=REPO, help="the box root (default: this checkout)")
    ap.add_argument("--volume", type=Path)
    ap.add_argument("--index", type=Path)
    ap.add_argument("--hashes", type=Path)
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--archive", type=Path)
    ap.add_argument("--verify-copies", action="store_true",
                    help="hash every archive copy too (reads each one whole)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--record", action="store_true",
                    help="save the report as Manifest/ledger_fsck.json for /health")
    args = ap.parse_args(argv)

    root = args.root.resolve()
    paths = _paths(root)
    for key in ("volume", "index", "hashes", "manifest", "archive"):
        if getattr(args, key) is not None:
            paths[key] = getattr(args, key)
    if not paths["volume"].exists():
        print(f"no ledger at {paths['volume']}", file=sys.stderr)
        return 2
    try:
        report = ledger_fsck.run(paths["volume"],
                                 paths["index"] if paths["index"].exists() else None,
                                 paths["hashes"], paths["manifest"], paths["archive"],
                                 verify_copies=args.verify_copies)
    except (OSError, ValueError) as e:
        print(f"cannot check the ledger: {e}", file=sys.stderr)
        return 2
    if args.record:
        ledger_fsck.write_report(paths["manifest"].parent / ledger_fsck.REPORT_NAME, report)
    print(json.dumps(report, indent=2) if args.json else ledger_fsck.format_text(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
