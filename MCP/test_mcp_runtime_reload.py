#!/usr/bin/env python3
"""M7.3: runtime public-URL source + localhost /internal/reload (URL + token hot-reload).

RUN:  cd MCP && BLACKBOX_ROOT=<repo-root> venv/bin/python test_mcp_runtime_reload.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
os.environ.setdefault("BLACKBOX_ROOT", str(_REPO))
os.environ["BLACKBOX_MCP_PUBLIC_URL"] = ""
_tmp = Path(tempfile.mkdtemp(prefix="bbmcp_m73_"))
RUNTIME = _tmp / "mcp_runtime.json"
os.environ["BLACKBOX_MCP_RUNTIME_FILE"] = str(RUNTIME)
sys.path.insert(0, str(_HERE))

spec = importlib.util.spec_from_file_location("bbmcp", str(_HERE / "blackbox_mcp_server.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

fails = []


def ok(label):
    print("PASS", label)


# (1) precedence: ENV wins over the file
os.environ["BLACKBOX_MCP_PUBLIC_URL"] = "https://env.example:8443"
RUNTIME.write_text(json.dumps({"public_url": "https://file.example:8443"}))
ok("(1) ENV wins") if mod._load_runtime_public_url() == "https://env.example:8443" else fails.append("1")

# (2) file used when ENV blank; trailing slash stripped
os.environ["BLACKBOX_MCP_PUBLIC_URL"] = ""
RUNTIME.write_text(json.dumps({"public_url": "https://file.example:8443/"}))
ok("(2) runtime file used, slash stripped") if mod._load_runtime_public_url() == "https://file.example:8443" else fails.append("2")

# (3) "" when neither
RUNTIME.unlink()
ok("(3) empty when neither set") if mod._load_runtime_public_url() == "" else fails.append("3")

# (4) /internal/reload rebinds BOTH url globals together
RUNTIME.write_text(json.dumps({"public_url": "https://reloaded.example:8443"}))
loc = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
r = asyncio.run(mod._internal_reload_handler(loc))
body = json.loads(r.body)
cond4 = (mod.BLACKBOX_MCP_PUBLIC_URL == "https://reloaded.example:8443"
         and mod.BLACKBOX_MCP_RESOURCE_URL == "https://reloaded.example:8443" + mod.BLACKBOX_MCP_HTTP_PATH
         and body["public_url"] == "https://reloaded.example:8443")
ok("(4) reload rebinds public_url + resource_url together") if cond4 else fails.append(f"4 {mod.BLACKBOX_MCP_RESOURCE_URL}")


# (5) token hot-reload: middleware map mutated IN PLACE
class FakeMW:
    def __init__(self):
        self.token_map = {"oldtok": "Alice"}


mw = FakeMW()
_orig_map, _orig_val = mod._load_token_map, mod._validate_token_operators
mod._AUTH_MIDDLEWARE["instance"] = mw
mod._load_token_map = lambda: {"newtok": "Brandon"}
mod._validate_token_operators = lambda m: m
asyncio.run(mod._internal_reload_handler(loc))
mod._load_token_map, mod._validate_token_operators = _orig_map, _orig_val
ok("(5) token map hot-swapped in place") if mw.token_map == {"newtok": "Brandon"} else fails.append(f"5 {mw.token_map}")

# (6) non-localhost -> 403 (defense-in-depth)
remote = SimpleNamespace(client=SimpleNamespace(host="1.2.3.4"))
r = asyncio.run(mod._internal_reload_handler(remote))
ok("(6) non-localhost -> 403") if r.status_code == 403 else fails.append(f"6 {r.status_code}")

print()
if fails:
    print("FAILED", fails)
    sys.exit(1)
print("ALL M7.3 reload tests passed")
