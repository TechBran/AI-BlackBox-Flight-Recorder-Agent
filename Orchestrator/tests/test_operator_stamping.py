"""A model-supplied ``operator`` never overrides the session operator.

Audit 2026-10-09 O18-toolvault-modules-06 (rank 33): every Google Workspace and
Gmail executor prefers ``params['operator']`` over ``ctx.operator``, and the
in-box chat / realtime-voice loops pass the model's arguments verbatim, so a
session running as operator A could read, send or delete as operator B simply
by naming B. ``BlackBoxToolExecutor.execute`` is the single dispatcher every
in-box surface goes through, so it enforces the session operator there.

A call that names ANOTHER operator is refused, not silently re-pointed: running
"send this from B's account" as A would send from the wrong account and report
success. Blank, missing or same-operator arguments run as the session.

Hermetic: the registry is pointed at a tmp tools dir with a throwaway module
that echoes the operator it was handed (exactly as the real executors read it)
and logs every run, so a refused call provably never reached the executor.
"""

import asyncio
import json

import pytest

from Orchestrator.toolvault import registry
from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor


# Mirrors the real Google Workspace executors' resolution line
# (ToolVault/tools/create_doc/executor.py and siblings).
_ECHO_OPERATOR = (
    "from Orchestrator.toolvault.context import ToolResult\n"
    "CALLS = {calls!r}\n"
    "async def execute(params, ctx):\n"
    "    operator = params.get('operator') or ctx.operator or 'system'\n"
    "    with open(CALLS, 'a') as fh:\n"
    "        fh.write(operator + '\\n')\n"
    "    return ToolResult(True, f\"acting-as:{{operator}}\")\n"
)


@pytest.fixture
def echo_tool(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    folder = tools / "echo_operator"
    folder.mkdir(parents=True)
    calls = tmp_path / "calls.log"
    (folder / "schema.json").write_text(json.dumps({
        "name": "echo_operator",
        "description": "Echo the operator the executor acts as.",
        "category": "communication",
        "groups": ["chat"],
        "tier": 2,
        "parameters": {
            "type": "object",
            "properties": {"operator": {"type": "string", "description": "Operator"}},
            "required": [],
        },
    }))
    (folder / "executor.py").write_text(_ECHO_OPERATOR.format(calls=str(calls)))
    monkeypatch.setattr(registry, "TOOLS_DIR", tools)
    registry.invalidate_cache()
    yield "echo_operator", calls
    registry.invalidate_cache()


def _run(executor, tool, params):
    return asyncio.run(executor.execute(tool, params))


def _ran(calls):
    return calls.read_text().splitlines() if calls.exists() else []


def test_another_operator_is_refused_and_never_runs(echo_tool):
    tool, calls = echo_tool
    res = _run(BlackBoxToolExecutor(operator="alice"), tool, {"operator": "SomeoneElse"})
    assert not res.success
    assert "acting-as" not in res.result
    # The refusal tells the model what it CAN do, so it retries correctly.
    assert "'alice'" in res.result and "SomeoneElse" in res.result
    assert _ran(calls) == [], "a refused call must never reach the executor"


def test_blank_or_missing_operator_resolves_to_the_session(echo_tool):
    tool, calls = echo_tool
    ex = BlackBoxToolExecutor(operator="alice")
    assert _run(ex, tool, {}).result == "acting-as:alice"
    assert _run(ex, tool, {"operator": ""}).result == "acting-as:alice"
    assert _run(ex, tool, {"operator": "   "}).result == "acting-as:alice"
    assert _run(ex, tool, {"operator": None}).result == "acting-as:alice"
    assert _ran(calls) == ["alice"] * 4


def test_the_session_operator_in_any_case_runs_as_the_session(echo_tool):
    tool, _ = echo_tool
    ex = BlackBoxToolExecutor(operator="alice")
    assert _run(ex, tool, {"operator": "alice"}).result == "acting-as:alice"
    # Same operator, sloppy spelling: run as the canonical session operator.
    assert _run(ex, tool, {"operator": " Alice "}).result == "acting-as:alice"


def test_overseer_is_held_to_its_own_operator_too(echo_tool):
    """The Flight Recorder's read-all capability rides ctx.operator through
    config.reads_all_operators, never a model argument, so it gets no
    act-as-anyone escape hatch here."""
    tool, calls = echo_tool
    ex = BlackBoxToolExecutor(operator="Flight Recorder")
    assert not _run(ex, tool, {"operator": "alice"}).success
    assert _run(ex, tool, {}).result == "acting-as:Flight Recorder"
    assert _ran(calls) == ["Flight Recorder"]


def test_system_executor_keeps_honouring_an_explicit_operator(echo_tool):
    """'system' (and an unset operator) is the trusted internal scope that has
    no session operator to protect; behaviour there is unchanged."""
    tool, _ = echo_tool
    assert _run(BlackBoxToolExecutor(), tool, {"operator": "bob"}).result == "acting-as:bob"
    assert _run(BlackBoxToolExecutor(operator=""), tool, {"operator": "bob"}).result == "acting-as:bob"


def test_callers_arguments_are_not_mutated(echo_tool):
    tool, _ = echo_tool
    for args in ({"operator": "SomeoneElse"}, {"operator": " Alice "}, {}):
        before = dict(args)
        _run(BlackBoxToolExecutor(operator="alice"), tool, args)
        assert args == before


# ---------------------------------------------------------------------------
# edit_cron_job: its schema still offers "Reassign the job to a different
# operator". A session operator asking for that must get a refusal, never a
# "Cron job updated." that silently kept the old owner.
# ---------------------------------------------------------------------------
@pytest.fixture()
def temp_manager(tmp_path, monkeypatch):
    from Orchestrator.scheduler import manager as manager_mod
    from Orchestrator.scheduler.manager import CronJobManager
    monkeypatch.setattr(manager_mod, "DB_PATH", tmp_path / "cron_jobs_stamping.db")
    mgr = CronJobManager()
    monkeypatch.setattr(manager_mod, "_manager_instance", mgr, raising=False)
    return mgr


def test_edit_cron_job_reassign_by_a_session_operator_is_refused(temp_manager):
    job = temp_manager.create_job(name="nightly", prompt="hi", schedule="0 7 * * *", operator="alice")
    res = _run(BlackBoxToolExecutor(operator="alice"), "edit_cron_job",
               {"job_id": job["id"], "operator": "bob", "name": "renamed"})
    assert not res.success, res.result
    stored = temp_manager.get_job(job["id"])
    assert stored["operator"] == "alice" and stored["name"] == "nightly", stored


def test_edit_cron_job_by_its_owner_still_works(temp_manager):
    job = temp_manager.create_job(name="nightly", prompt="hi", schedule="0 7 * * *", operator="alice")
    res = _run(BlackBoxToolExecutor(operator="alice"), "edit_cron_job",
               {"job_id": job["id"], "name": "renamed"})
    assert res.success, res.result
    stored = temp_manager.get_job(job["id"])
    assert stored["operator"] == "alice" and stored["name"] == "renamed", stored


def test_edit_cron_job_reassign_by_system_still_works(temp_manager):
    job = temp_manager.create_job(name="nightly", prompt="hi", schedule="0 7 * * *", operator="alice")
    res = _run(BlackBoxToolExecutor(operator="system"), "edit_cron_job",
               {"job_id": job["id"], "operator": "bob"})
    assert res.success, res.result
    assert temp_manager.get_job(job["id"])["operator"] == "bob"
