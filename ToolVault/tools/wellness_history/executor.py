"""Executor for wellness_history: the day-by-day rollup table for a date range,
at most 92 days per call, weekly rows when it must coarsen to fit (spec §10).

The operator is the session's (ctx.operator), never a parameter. The gate runs
per category first, and the answer is written to the operator's delivered log
for this turn (Orchestrator/wellness/access.run).
"""
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.wellness import access, api

TOOL = "wellness_history"


def execute(params: dict, ctx: ToolContext) -> ToolResult:
    start, end = params.get("start"), params.get("end")
    if not start or not end:
        return access.invalid(TOOL, "start and end are dates (YYYY-MM-DD), at most "
                                    f"{api.MAX_HISTORY_DAYS} days apart")
    wanted, unknown = access.requested(params.get("categories"))
    if unknown:
        return access.invalid(TOOL, access.CATEGORIES_HINT)

    def call(operator, categories, delivery, budget, now_ms):
        return api.history(operator, start, end, categories=categories, now_ms=now_ms,
                           delivery=delivery, budget_tokens=budget)

    return access.run(TOOL, ctx, wanted, call, explicit=wanted is not None)
