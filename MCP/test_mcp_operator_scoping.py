#!/usr/bin/env python3
"""Operator-scoping production-safety tests (the fresh-box / no-"Brandon" path).

Pure in-process unit checks (no subprocess, no backend) for the production fixes:
  (1) _validate_token_operators -- the startup roster guard:
        (1a) roster unavailable        -> keep all (fail OPEN)
        (1b) roster available, enforce  -> DROP the ghost ("Brandon" on an alice/bob box)
        (1c) enforce off                -> keep all (logged)
  (2) _load_token_map                  -> refuses "system"/blank span-all tokens
  (3) OAuth discovery                  -> 503 when BLACKBOX_MCP_PUBLIC_URL unset, 200 when set
  (4) deploy/mint_token._pick_operator -> validates the operator against the live roster

RUN:  cd MCP && BLACKBOX_ROOT=<repo-root> venv/bin/python test_mcp_operator_scoping.py
"""
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
os.environ.setdefault("BLACKBOX_ROOT", str(_REPO_ROOT))
# Import with the public URL UNSET so we can assert the OAuth discovery 503 path
# (the module reads the env at import time).
os.environ["BLACKBOX_MCP_PUBLIC_URL"] = ""
os.environ["BLACKBOX_MCP_ROSTER_ENFORCE"] = "1"
sys.path.insert(0, str(_HERE))

spec = importlib.util.spec_from_file_location("bbmcp", str(_HERE / "blackbox_mcp_server.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

fails = []


def ok(label):
    print(f"PASS {label}")


# --- (1) startup roster guard ---
TOKENS = {"tokA": "alice", "tokB": "bob", "tokGhost": "Brandon"}

mod._fetch_operators_sync = lambda: None  # roster unavailable
out = mod._validate_token_operators(dict(TOKENS))
ok("(1a) roster unavailable -> keep all (fail open)") if out == TOKENS else fails.append(f"(1a) {out}")

mod._fetch_operators_sync = lambda: ["alice", "bob"]
os.environ["BLACKBOX_MCP_ROSTER_ENFORCE"] = "1"
out = mod._validate_token_operators(dict(TOKENS))
ok("(1b) enforce ON -> ghost 'Brandon' dropped") if out == {"tokA": "alice", "tokB": "bob"} else fails.append(f"(1b) {out}")

os.environ["BLACKBOX_MCP_ROSTER_ENFORCE"] = "0"
out = mod._validate_token_operators(dict(TOKENS))
ok("(1c) enforce OFF -> keep all (logged)") if out == TOKENS else fails.append(f"(1c) {out}")
os.environ["BLACKBOX_MCP_ROSTER_ENFORCE"] = "1"

# --- (2) _load_token_map refuses span-all "system"/blank ---
os.environ["BLACKBOX_MCP_TOKENS"] = json.dumps(
    {"tokS": "system", "tokSys2": "System", "tokOk": "alice", "tokBlank": ""})
# Isolate the FILE source: the module constant was bound at import to the real
# store, so override the attribute (not the env) to a nonexistent path.
mod.BLACKBOX_MCP_TOKENS_FILE = str(_HERE / "__no_such_token_file__.json")
m = mod._load_token_map()
ok("(2) _load_token_map drops system/System/blank, keeps alice") if m == {"tokOk": "alice"} else fails.append(f"(2) {m}")
del os.environ["BLACKBOX_MCP_TOKENS"]

# --- (3) OAuth discovery fail-loud / configured ---
mod.BLACKBOX_MCP_PUBLIC_URL = ""
r1 = asyncio.run(mod._oauth_as_metadata_handler(None))
r2 = asyncio.run(mod._oauth_pr_metadata_handler(None))
if getattr(r1, "status_code", None) == 503 and getattr(r2, "status_code", None) == 503:
    ok("(3) OAuth discovery -> 503 when BLACKBOX_MCP_PUBLIC_URL unset")
else:
    fails.append(f"(3) as={getattr(r1,'status_code',None)} pr={getattr(r2,'status_code',None)}")

mod.BLACKBOX_MCP_PUBLIC_URL = "https://example.ts.net:8443"
mod.BLACKBOX_MCP_RESOURCE_URL = mod.BLACKBOX_MCP_PUBLIC_URL + mod.BLACKBOX_MCP_HTTP_PATH
r3 = asyncio.run(mod._oauth_as_metadata_handler(None))
ok("(3b) OAuth discovery -> 200 when URL set") if getattr(r3, "status_code", None) == 200 else fails.append(f"(3b) {getattr(r3,'status_code',None)}")

# --- (4) mint helper operator validation ---
spec2 = importlib.util.spec_from_file_location("bbmint", str(_HERE / "deploy" / "mint_token.py"))
mint = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(mint)

try:
    mint._pick_operator(["alice", "bob"], "alice", "Brandon", True)
    fails.append("(4a) non-roster operator did not exit")
except SystemExit:
    ok("(4a) mint rejects non-roster operator 'Brandon'")

ok("(4b) mint accepts roster operator 'bob'") if mint._pick_operator(["alice", "bob"], "alice", "bob", True) == "bob" else fails.append("(4b)")
ok("(4c) single operator auto-selected") if mint._pick_operator(["solo"], "solo", None, True) == "solo" else fails.append("(4c)")

try:
    mint._pick_operator([], "", None, True)
    fails.append("(4d) empty roster did not exit")
except SystemExit:
    ok("(4d) mint errors on empty roster")

# --- (5) N-1: the OAuth consent gate authenticates against the roster-VALIDATED
# map, so a ghost-operator token rejected at /mcp can't mint an OAuth token either.
# This asserts the exact composition the /authorize POST now uses.
mod._fetch_operators_sync = lambda: ["alice", "bob"]
os.environ["BLACKBOX_MCP_ROSTER_ENFORCE"] = "1"
validated = mod._validate_token_operators({"ghosttok": "Brandon", "goodtok": "alice"})
op_ghost, _ = mod._match_token("ghosttok", validated)
op_good, _ = mod._match_token("goodtok", validated)
if op_ghost is None and op_good == "alice":
    ok("(5) consent-gate validated map: ghost token refused, roster token accepted")
else:
    fails.append(f"(5) ghost={op_ghost} good={op_good}")

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    sys.exit(1)
print("ALL operator-scoping tests passed")
