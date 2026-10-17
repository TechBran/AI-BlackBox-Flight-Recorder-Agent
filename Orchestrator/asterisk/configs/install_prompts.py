#!/usr/bin/env python3
"""install_prompts.py - install the IVR prompts where Asterisk plays them.

Run as root, outside blackbox.service:

    sudo python3 -I Orchestrator/asterisk/configs/install_prompts.py

blackbox.service runs with ProtectSystem=strict, so Asterisk's sound dir is
read-only to the service and to sudo inside it. The runtime can only cache
prompts in Orchestrator/asterisk/ivr_cache (tracked in git, plus anything it
generated). Scripts/install.sh and a full configs/install.sh run this to copy
that cache into the sound dir. No Asterisk reload is needed: playback reads
the files directly.

The cache is writable by the service user, so a copy made as root must not be
a way to read root-only files. Each source is opened with O_NOFOLLOW and
O_NONBLOCK, must be a regular file with a RIFF/WAVE header and a plain name,
and is written to a fresh root-owned 0644 file (temp file + rename). Anything
else is skipped. Stdlib only, so it runs under `python3 -I`.
"""
import argparse
import os
import re
import stat
import sys
import tempfile

# Must equal Orchestrator/asterisk/ivr_audio.py ASTERISK_SOUND_DIR (test-pinned).
DEFAULT_DEST = "/usr/share/asterisk/sounds/en/custom/ivr"
DEFAULT_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ivr_cache")

NAME_RE = re.compile(r"^[A-Za-z0-9_]+\.wav$")
MAX_BYTES = 10 * 1024 * 1024


def _read_wav(path):
    """Bytes of ``path`` if it is a plain, regular WAV file; else None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None  # symlink (ELOOP), vanished, unreadable
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not 100 < st.st_size <= MAX_BYTES:
            return None
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) <= 100 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    return data


def _same(path, data):
    try:
        with open(path, "rb") as f:
            return f.read() == data
    except OSError:
        return False


def _write(dest_dir, name, data):
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o644)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(dest_dir, name))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def install(src, dest):
    """Copy every valid prompt from ``src`` to ``dest``; returns (installed, current, skipped)."""
    names = sorted(os.listdir(src))
    os.makedirs(dest, mode=0o755, exist_ok=True)
    installed = current = 0
    skipped = []
    for name in names:
        if not name.endswith(".wav"):
            continue
        data = _read_wav(os.path.join(src, name)) if NAME_RE.match(name) else None
        if data is None:
            skipped.append(name)
            continue
        if _same(os.path.join(dest, name), data):
            current += 1
            continue
        _write(dest, name, data)
        installed += 1
    return installed, current, skipped


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install IVR prompts for Asterisk.")
    parser.add_argument("--src", default=DEFAULT_SRC, help="prompt cache (default: %(default)s)")
    parser.add_argument("--dest", default=os.environ.get("ASTERISK_IVR_SOUND_DIR") or DEFAULT_DEST,
                        help="Asterisk sound dir (default: %(default)s)")
    parser.add_argument("--print-defaults", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.print_defaults:
        print(f"src={DEFAULT_SRC}\ndest={DEFAULT_DEST}")
        return 0
    if not os.path.isdir(args.src):
        print(f"[install-prompts] ERROR: prompt cache not found: {args.src}", file=sys.stderr)
        return 1
    try:
        installed, current, skipped = install(args.src, args.dest)
    except OSError as e:
        print(f"[install-prompts] ERROR: cannot install into {args.dest}: {e}", file=sys.stderr)
        return 1
    print(f"[install-prompts] {args.dest}: {installed} installed, {current} up to date"
          + (f", {len(skipped)} skipped (not a plain WAV file): {', '.join(map(ascii, skipped))}"
             if skipped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
