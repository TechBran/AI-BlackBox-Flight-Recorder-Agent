#!/usr/bin/env python3
"""
internet_routes.py - Cellular Internet Failover API Endpoints

Passive monitoring + manual deep-reconnect. NM handles connection lifecycle.

  GET  /internet/status         — Full modem + signal + connection + routing status
  POST /internet/deep-reconnect — Cycle radio for fresh carrier session (manual only)
  POST /internet/speed-test     — Run speed test through wwan0 (spends cellular data)
  POST /internet/at-command     — Send AT command via mmcli (debug)
  GET  /internet/history        — Recent event log
"""

from pydantic import BaseModel
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from Orchestrator.checkpoint import app


class AtCommandRequest(BaseModel):
    command: str


def _get_manager():
    from Orchestrator.cellular.internet_manager import get_internet_manager
    mgr = get_internet_manager()
    if mgr is None:
        raise HTTPException(status_code=503, detail="Cellular internet manager not running")
    return mgr


@app.get("/internet/status")
async def internet_status():
    mgr = _get_manager()
    status = await mgr.get_full_status()
    return JSONResponse(content=status)


@app.post("/internet/deep-reconnect")
async def internet_deep_reconnect():
    """Cycle radio off/on for fresh carrier session (software unplug/replug)."""
    mgr = _get_manager()
    result = await mgr.deep_reconnect()
    code = 200 if result.get("ok") else 502
    return JSONResponse(content=result, status_code=code)


@app.post("/internet/speed-test")
async def internet_speed_test():
    """Pull ~10 MB down and push up over wwan0. POST, not GET: it spends
    metered cellular data, and a cross-site <img> must never start it."""
    mgr = _get_manager()
    result = await mgr.run_speed_test()
    return JSONResponse(content=result)


@app.post("/internet/at-command")
async def internet_at_command(body: AtCommandRequest):
    """Send AT command via mmcli --command."""
    mgr = _get_manager()
    if not body.command.strip().upper().startswith("AT"):
        raise HTTPException(status_code=400, detail="Command must start with AT")
    result = await mgr.send_at_command(body.command.strip())
    return JSONResponse(content=result)


@app.get("/internet/history")
async def internet_history(limit: int = 50):
    mgr = _get_manager()
    events = mgr.get_event_log(limit=min(limit, 100))
    return JSONResponse(content={"events": events})
