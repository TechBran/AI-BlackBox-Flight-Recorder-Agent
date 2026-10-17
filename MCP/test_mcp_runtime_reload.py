#!/usr/bin/env python3
"""M7.3: runtime public-URL source + localhost /internal/reload (URL + token hot-reload).

Audit M3 U20 (O20-mcp-server-08): Tailscale Funnel delivers INTERNET requests to
127.0.0.1:9093, so the loopback check proves nothing on its own. The reload also
needs the per-boot secret in credentials/mcp_reload.secret (0600), which only
processes running as the service user can read (Orchestrator/utils/internal_secret.py).

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
SECRET_FILE = _tmp / "credentials" / "mcp_reload.secret"
os.environ["BLACKBOX_MCP_RELOAD_SECRET_FILE"] = str(SECRET_FILE)
sys.path.insert(0, str(_HERE))

spec = importlib.util.spec_from_file_location("bbmcp", str(_HERE / "blackbox_mcp_server.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

fails = []


def ok(label):
    print("PASS", label)


from starlette.datastructures import Headers  # noqa: E402
from Orchestrator.utils import internal_secret  # noqa: E402

SECRET = internal_secret.ensure_secret(SECRET_FILE)   # what the Orchestrator side does


def req(host="127.0.0.1", secret=SECRET):
    headers = {internal_secret.SECRET_HEADER: secret} if secret is not None else {}
    return SimpleNamespace(client=SimpleNamespace(host=host), headers=Headers(headers))


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
loc = req()
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

# (6) non-localhost -> 403 (defense-in-depth), even with the secret
remote = req(host="1.2.3.4")
r = asyncio.run(mod._internal_reload_handler(remote))
ok("(6) non-localhost -> 403") if r.status_code == 403 else fails.append(f"6 {r.status_code}")


# (7)-(10): a Funnel request arrives FROM 127.0.0.1 -- only the secret tells it apart.
# None of them may reload the token map.
class Tripwire:
    def __init__(self):
        self.token_map = {"keep": "Alice"}


tw = Tripwire()
mod._AUTH_MIDDLEWARE["instance"] = tw
mod._load_token_map = lambda: {"attacker": "Mallory"}
mod._validate_token_operators = lambda m: m
for label, request in (("(7) loopback, no secret", req(secret=None)),
                       ("(8) loopback, empty secret", req(secret="")),
                       ("(9) loopback, wrong secret", req(secret=SECRET + "x"))):
    r = asyncio.run(mod._internal_reload_handler(request))
    if r.status_code == 403 and tw.token_map == {"keep": "Alice"}:
        ok(label + " -> 403, nothing reloaded")
    else:
        fails.append(f"{label} {r.status_code} {tw.token_map}")

_real_boot = internal_secret._boot_id
internal_secret._boot_id = lambda: "a-later-boot"
r = asyncio.run(mod._internal_reload_handler(req()))
internal_secret._boot_id = _real_boot
if r.status_code == 403 and tw.token_map == {"keep": "Alice"}:
    ok("(10) last boot's secret -> 403")
else:
    fails.append(f"10 {r.status_code}")

r = asyncio.run(mod._internal_reload_handler(req()))
mod._load_token_map, mod._validate_token_operators = _orig_map, _orig_val
ok("(11) the right secret still reloads") if (r.status_code == 200 and tw.token_map == {"attacker": "Mallory"}) \
    else fails.append(f"11 {r.status_code} {tw.token_map}")

print()
if fails:
    print("FAILED", fails)
    sys.exit(1)
print("ALL M7.3 reload tests passed")
