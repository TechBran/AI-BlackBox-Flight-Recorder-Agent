"""
End-to-end smoke test for the CLI Agent WebSocket bridge.

Run from project root:
    ./Orchestrator/venv/bin/python Orchestrator/cli_agent/smoke_test.py

What it checks:
  1. WebSocket connection to /cli-agent/ws/<session_id> succeeds.
  2. Initial session_info text frame is received with state="created".
  3. At least one binary frame of PTY output arrives within 10s
     (proves bridge plumbing works regardless of what `claude` does).
  4. Clean WebSocket disconnect.
  5. tmux session survives the disconnect (the persistence promise).
  6. Reconnect attaches to existing session, session_info state="attaching".
  7. `kill` control text frame terminates the tmux session.
  8. tmux session is gone after kill.

Exit 0 on success, non-zero on failure. Always cleans up via tmux kill-session
even on failure, so reruns are safe.
"""

import asyncio
import json
import subprocess
import sys
from urllib.parse import urlencode

import websockets

from Orchestrator.config import current_default, orchestrator_url


HOST = orchestrator_url(scheme="ws")


def tmux_has(name: str) -> bool:
    return subprocess.run(
        ["tmux", "has-session", "-t", name], capture_output=True
    ).returncode == 0


def tmux_kill(name: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


async def collect_bytes(ws, timeout: float) -> bytes:
    """Pull binary frames from the WebSocket up to `timeout` seconds total."""
    out = b""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            msg = await asyncio.wait_for(
                ws.recv(),
                timeout=max(0.1, deadline - asyncio.get_running_loop().time()),
            )
        except asyncio.TimeoutError:
            break
        if isinstance(msg, bytes):
            out += msg
        # Text frames during this collect are unexpected (already consumed
        # session_info), but tolerate them.
    return out


def build_url(operator: str, provider: str, app: str, session_id: str) -> str:
    qs = urlencode({
        "op": operator, "provider": provider, "app": app,
        "cols": 80, "rows": 24,
    })
    return f"{HOST}/cli-agent/ws/{session_id}?{qs}"


async def kill_via_rest(session_id: str) -> None:
    """Use the REST DELETE endpoint to kill the session through the route
    (which lives in blackbox.service's namespace and CAN see the session)."""
    import urllib.request
    req = urllib.request.Request(
        orchestrator_url(f"/cli-agent/sessions/{session_id}"),
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: ASYNC210
            r.read()
    except Exception:
        pass


async def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--operator", default=None,
                        help="operator to run as (default: the roster default)")
    parser.add_argument("--provider", default="claude",
                        help="claude | gemini | codex (default: claude)")
    parser.add_argument("--app", default="grocery-store",
                        help='App slug under Apps/, or "" for Apps root')
    args = parser.parse_args()

    operator = args.operator or current_default()
    provider = args.provider
    app = args.app
    slug = app if app else "_root"
    session_id = f"cli-agent-{operator}__{provider}__{slug}"

    failures = []
    print(f"[smoke] target session_id: {session_id}")
    print("[smoke] NOTE: blackbox.service runs in a private mount namespace,")
    print("[smoke]       so we verify persistence via WebSocket reconnect")
    print("[smoke]       (not external `tmux ls`, which is in a different namespace).")

    # Pre-clean: ask the route to kill any leftover from a prior run.
    await kill_via_rest(session_id)

    # ---- Phase A: First connect, expect state=created -----------------------
    url = build_url(operator, provider, app, session_id)
    print(f"[smoke] connecting (A): {url}")
    try:
        async with websockets.connect(url) as ws:
            first = await asyncio.wait_for(ws.recv(), timeout=5.0)
            try:
                info = json.loads(first)
            except Exception:
                failures.append(f"A: first frame not JSON: {first!r}")
                await kill_via_rest(session_id)
                return _report(failures)
            print(f"[smoke] A session_info: {info}")
            if info.get("type") != "session_info":
                failures.append(f"A: expected type=session_info, got {info!r}")
            if info.get("state") != "created":
                failures.append(f"A: expected state=created, got {info.get('state')!r}")

            data = await collect_bytes(ws, timeout=10.0)
            print(f"[smoke] A collected {len(data)} bytes of PTY output (preview: {data[:120]!r})")
            if not data:
                failures.append("A: no PTY bytes received within 10s — bridge plumbing broken")
    except Exception as e:
        failures.append(f"A: connection failed: {e!r}")
        await kill_via_rest(session_id)
        return _report(failures)

    # ---- Phase B: Reconnect → state=attaching proves persistence ------------
    await asyncio.sleep(0.5)
    print("[smoke] connecting (B, reconnect to verify persistence)")
    try:
        async with websockets.connect(url) as ws:
            first = await asyncio.wait_for(ws.recv(), timeout=5.0)
            info = json.loads(first)
            print(f"[smoke] B session_info: {info}")
            if info.get("state") == "attaching":
                print("[smoke] B: tmux session persisted across disconnect ✓")
            elif info.get("state") == "created":
                failures.append("B: state=created on reconnect — session did NOT persist across WebSocket disconnect (this is the persistence-broken case)")
            else:
                failures.append(f"B: unexpected state={info.get('state')!r}")
            # Drain replay to confirm scrollback is intact
            replay = await collect_bytes(ws, timeout=2.0)
            print(f"[smoke] B replayed {len(replay)} bytes (preview: {replay[:80]!r})")

            # ---- Phase C: kill via control frame --------------------------
            print("[smoke] C: sending kill control frame")
            await ws.send(json.dumps({"type": "kill"}))
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    if isinstance(msg, str):
                        continue
            except asyncio.TimeoutError:
                pass
            except websockets.ConnectionClosed:
                pass
    except Exception as e:
        failures.append(f"B/C: reconnect/kill failed: {e!r}")
        await kill_via_rest(session_id)
        return _report(failures)

    # ---- Phase D: Reconnect after kill → state=created proves cleanup -------
    await asyncio.sleep(1.0)
    print("[smoke] connecting (D, reconnect after kill to verify cleanup)")
    try:
        async with websockets.connect(url) as ws:
            first = await asyncio.wait_for(ws.recv(), timeout=5.0)
            info = json.loads(first)
            print(f"[smoke] D session_info: {info}")
            if info.get("state") == "created":
                print("[smoke] D: kill control frame removed session ✓")
            else:
                failures.append(f"D: expected state=created after kill, got {info.get('state')!r} — kill didn't clean up")
        # Final cleanup
        await kill_via_rest(session_id)
    except Exception as e:
        failures.append(f"D: post-kill reconnect failed: {e!r}")
        await kill_via_rest(session_id)

    return _report(failures)


def _report(failures: list[str]) -> int:
    if failures:
        print("\n[smoke] FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n[smoke] ALL CHECKS PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
