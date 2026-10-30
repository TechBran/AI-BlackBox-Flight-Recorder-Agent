"""Executor for wellness_summary: every metric the person shares for one period,
with their usual range, coverage and the notable workouts (spec §4.2, §10).

The operator is the session's (ctx.operator), never a parameter. The gate runs
per category first, and the answer is written to the operator's delivered log
for this turn (Orchestrator/wellness/access.run). A plain def: it reads the
store in-process, on the ToolVault worker pool.
"""
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.wellness import access, api

TOOL = "wellness_summary"


def execute(params: dict, ctx: ToolContext) -> ToolResult:
    period = params.get("period")
    if not isinstance(period, str) or period not in api.PERIODS:
        return access.invalid(TOOL, "period is one of " + ", ".join(api.PERIODS))
    start, end = params.get("start"), params.get("end")
    wanted, unknown = access.requested(params.get("categories"))
    if unknown:
        return access.invalid(TOOL, access.CATEGORIES_HINT)

    def call(operator, categories, delivery, budget, now_ms):
        return api.summary(operator, period, categories, start=start, end=end, now_ms=now_ms,
                           delivery=delivery, budget_tokens=budget)

    return access.run(TOOL, ctx, wanted, call, explicit=wanted is not None)
