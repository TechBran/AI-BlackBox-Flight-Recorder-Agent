"""Flight Recorder HTTP surface (design 2026-07-23 §7).

GET  /oversight/status        — seed health, state, live signal summary
POST /oversight/flight-report — manual report trigger (fire-and-forget thread)
"""
import threading

from Orchestrator.checkpoint import app
from Orchestrator import oversight


@app.get("/oversight/status")
def oversight_status():
    signals = oversight.collect_oversight_signals()
    state = oversight.get_state_snapshot()
    return {
        "operator": "Flight Recorder",
        "state": state,
        "signals": signals,
        "has_failures": oversight.has_failures(signals),
        "has_critical": oversight.has_critical_findings(signals),
    }


@app.post("/oversight/flight-report")
def oversight_flight_report():
    """Manual flight report. Mirrors create_checkpoint_manual's fire-and-forget
    thread — the mint can take minutes (LLM synthesis); the caller polls the
    FR chain / /oversight/status for the result."""
    t = threading.Thread(
        target=oversight.create_flight_report_async, kwargs={"manual": True},
        daemon=True, name="flight-report-manual")
    t.start()
    return {"status": "started",
            "message": "Flight report generation started; the report mints "
                       "into the Flight Recorder chain when synthesis completes."}
