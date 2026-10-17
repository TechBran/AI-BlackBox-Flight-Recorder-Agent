#!/usr/bin/env python3
"""env_key.py - read or generate one secret in a .env file, for the installers.

Install-time secrets (TELEPHONY_SECRET_KEY, ASTERISK_ARI_PASSWORD) are
generated straight into .env instead of living in tracked files (audit W05).

    env_key.py get <env-file> <KEY>
        Print KEY's value ("" when missing). Matching quotes are stripped.
    env_key.py generate [--kind token|fernet] [--force] <env-file> <KEY>
        Give KEY a fresh random value when it is empty or missing (or always,
        with --force). token = 32 url-safe random bytes; fernet = a Fernet key.
        Prints only what it did, never the value.

The file is rewritten in place (open r+, truncate), so it keeps its owner and
mode even when an installer runs this as root. Duplicate assignments of KEY
collapse into one. Stdlib only: runs before any venv exists.
"""
import argparse
import base64
import os
import re
import secrets
import sys

_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _key_of(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return ""
    return stripped.split("=", 1)[0].strip()


def get_value(path: str, key: str) -> str:
    value = ""
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if _key_of(line) == key:
                value = line.split("=", 1)[1].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                break
    return value


def _new_value(kind: str) -> str:
    if kind == "fernet":
        return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    return secrets.token_urlsafe(32)


def generate(path: str, key: str, kind: str, force: bool) -> str:
    """Returns "generated" or "kept"."""
    if not force and get_value(path, key):
        return "kept"
    value = _new_value(kind)
    with open(path, "r+", encoding="utf-8") as f:
        lines = f.read().splitlines(keepends=True)
        out, placed = [], False
        for line in lines:
            if _key_of(line) == key:
                if not placed:
                    out.append(f"{key}={value}\n")
                    placed = True
                continue  # drop duplicate assignments
            out.append(line)
        if not placed:
            if out and not out[-1].endswith("\n"):
                out[-1] += "\n"
            out.append(f"{key}={value}\n")
        f.seek(0)
        f.write("".join(out))
        f.truncate()
    return "generated"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_get = sub.add_parser("get")
    p_gen = sub.add_parser("generate")
    p_gen.add_argument("--kind", choices=("token", "fernet"), default="token")
    p_gen.add_argument("--force", action="store_true")
    for p in (p_get, p_gen):
        p.add_argument("env_file")
        p.add_argument("key")
    args = parser.parse_args(argv)

    if not _KEY_RE.match(args.key):
        print(f"invalid env-var name: {args.key!r}", file=sys.stderr)
        return 2
    if not os.path.isfile(args.env_file):
        print(f"{args.env_file} not found", file=sys.stderr)
        return 1
    if args.cmd == "get":
        print(get_value(args.env_file, args.key))
        return 0
    result = generate(args.env_file, args.key, args.kind, args.force)
    if result == "kept":
        print(f"{args.key} already set in {args.env_file} (kept)")
    else:
        print(f"{args.key} generated in {args.env_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
