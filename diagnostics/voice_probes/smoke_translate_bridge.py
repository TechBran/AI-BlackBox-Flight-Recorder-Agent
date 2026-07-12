#!/usr/bin/env python3
"""P6a acceptance smoke — drive OUR backend bridges in translate mode.

Connects to ws://localhost:9091/ws/{realtime,gemini-live}/<id>
?mode=translate&target_language=es, sends the JSON connect message, and
asserts the bridge reaches "connected" without an error event. Exercises the
REAL upstream providers (keys must be configured — this is the dev box).

Usage:
    Orchestrator/venv/bin/python diagnostics/voice_probes/smoke_translate_bridge.py [realtime|gemini-live|all]
Exit 0 = all requested bridges passed.
"""
import asyncio
import json
import sys
import uuid

import websockets

BASE = "ws://localhost:9091"
TIMEOUT_S = 30


async def smoke(path: str) -> bool:
    sid = uuid.uuid4().hex[:12]
    url = f"{BASE}/ws/{path}/{sid}?mode=translate&target_language=es"
    print(f"--- {path}: {url}")
    try:
        async with websockets.connect(url, open_timeout=10) as ws:
            # JSON connect carries the same fields — exercises the
            # JSON-wins-over-URL precedence path too.
            await ws.send(json.dumps({
                "type": "connect",
                "operator": "system",
                "mode": "translate",
                "target_language": "es",
            }))
            loop = asyncio.get_event_loop()
            deadline = loop.time() + TIMEOUT_S
            while loop.time() < deadline:
                raw = await asyncio.wait_for(ws.recv(), timeout=TIMEOUT_S)
                msg = json.loads(raw)
                print(f"    <- {msg.get('type')}: {str(msg.get('data'))[:120]}")
                if msg.get("type") == "connected":
                    print(f"    PASS {path}")
                    return True
                if msg.get("type") == "error":
                    print(f"    FAIL {path}: {msg.get('data')}")
                    return False
    except Exception as e:
        print(f"    FAIL {path}: {type(e).__name__}: {e}")
        return False
    print(f"    FAIL {path}: no connected/error within {TIMEOUT_S}s")
    return False


async def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    paths = ["realtime", "gemini-live"] if target == "all" else [target]
    results = [await smoke(p) for p in paths]
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
