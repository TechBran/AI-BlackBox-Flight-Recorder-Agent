"""Full-disk encryption status of the box (spec §3.6, "Encryption"): does the
root filesystem, and the one holding data/wellness/, sit on dm-crypt / LUKS?

    {"root_encrypted": true | false | null, "method": "luks" | "dm-crypt" | null,
     "store_encrypted": true | false | null}

Read-only heuristics, stdlib only: no sudo, no lsblk, and it never raises.

  1. The filesystem's block device: os.stat(path).st_dev, or, for a
     filesystem with an anonymous device number (btrfs, overlay, zfs, nfs),
     the source of the longest /proc/self/mounts entry holding the path,
     when that is a /dev node.
  2. That device in sysfs (/sys/dev/block/<major>:<minor>), then what it
     sits on: a partition's parent and a mapping's slaves/, all the way
     down (LVM on LUKS, RAID on dm-crypt).
  3. A device-mapper target whose dm/uuid starts with "CRYPT-LUKS" is LUKS;
     any other "CRYPT-" is plain dm-crypt. A device found with no such layer
     under it is not encrypted (false).

Anything it cannot see or recognise (overlay, zfs, tmpfs, a network mount,
no sysfs entry) is null, never a guess. A self-encrypting drive is not
visible this way and reads false. The privacy policy points at this answer
(Settings, Health) instead of saying "ask whoever set up your box".
"""
from __future__ import annotations

import os
import re
from typing import Callable, Optional, Set, Tuple

PROC_MOUNTS = "/proc/self/mounts"
SYS_ROOT = "/sys"
MAX_DEPTH = 12

_ESCAPE = re.compile(r"\\([0-7]{3})")


def _unescape(field: str) -> str:
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def _existing(path: str, stat: Callable) -> Optional[str]:
    """The path itself or its nearest ancestor that exists (the store
    directory is created on the first enrollment)."""
    p = os.path.realpath(path)
    while True:
        try:
            stat(p)
            return p
        except OSError:
            if p == os.path.dirname(p):
                return None
            p = os.path.dirname(p)


def _mount_source(path: str, proc_mounts: str) -> Optional[str]:
    """The source of the mount holding `path` (the longest mount point; a
    later entry for the same point is the one on top)."""
    text = _read(proc_mounts)
    if not text:
        return None
    best, source = -1, None
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        src, point = _unescape(fields[0]), _unescape(fields[1])
        inside = path == point or path.startswith(point.rstrip("/") + "/")
        if inside and len(point) >= best:
            best, source = len(point), src
    return source


def _crypt_method(block_dir: str, seen: Set[str], depth: int) -> Optional[str]:
    if depth > MAX_DEPTH or block_dir in seen:
        return None
    seen.add(block_dir)
    uuid = _read(os.path.join(block_dir, "dm", "uuid")) or ""
    if uuid.startswith("CRYPT-"):
        return "luks" if uuid.startswith("CRYPT-LUKS") else "dm-crypt"
    under = []
    if os.path.exists(os.path.join(block_dir, "partition")):
        under.append(os.path.dirname(block_dir))
    slaves = os.path.join(block_dir, "slaves")
    try:
        under += [os.path.realpath(os.path.join(slaves, n)) for n in sorted(os.listdir(slaves))]
    except OSError:
        pass
    for d in under:
        method = _crypt_method(d, seen, depth + 1)
        if method:
            return method
    return None


def check(path: str, *, proc_mounts: str = PROC_MOUNTS, sys_root: str = SYS_ROOT,
          stat: Callable = os.stat) -> Tuple[Optional[bool], Optional[str]]:
    """(encrypted, method) of the filesystem holding `path`; (None, None)
    when it cannot tell."""
    try:
        target = _existing(path, stat)
        if target is None:
            return None, None
        dev = stat(target).st_dev
        if os.major(dev) == 0:
            source = _mount_source(target, proc_mounts)
            if not source or not source.startswith("/dev/"):
                return None, None
            dev = stat(source).st_rdev
            if not dev:
                return None, None
        block = os.path.join(sys_root, "dev", "block", f"{os.major(dev)}:{os.minor(dev)}")
        if not os.path.exists(block):
            return None, None
        method = _crypt_method(os.path.realpath(block), set(), 0)
        return method is not None, method
    except Exception:  # noqa: BLE001 - a status line must never break a status call
        return None, None


def status(store_dir: Optional[str] = None, *, proc_mounts: str = PROC_MOUNTS,
           sys_root: str = SYS_ROOT, stat: Callable = os.stat) -> dict:
    """The box's encryption status: the root filesystem, and the filesystem
    holding the health stores (the same one on a standard install)."""
    if store_dir is None:
        try:
            from Orchestrator.wellness import paths
            store_dir = str(paths.data_dir())
        except Exception:  # noqa: BLE001
            store_dir = None
    root, method = check("/", proc_mounts=proc_mounts, sys_root=sys_root, stat=stat)
    store = check(store_dir, proc_mounts=proc_mounts, sys_root=sys_root, stat=stat)[0] \
        if store_dir else None
    return {"root_encrypted": root, "method": method, "store_encrypted": store}
