"""Executor for wellness_sources: what feeds this person's health store and how
complete it is, with no health values (spec §4.2).

The operator is the session's (ctx.operator), never a parameter. It answers
when the turn may read at least one category (Orchestrator/wellness/access.run)
and is written to the operator's delivered log like every wellness answer.
"""
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.wellness import access, api

TOOL = "wellness_sources"


def execute(params: dict, ctx: ToolContext) -> ToolResult:
    def call(operator, categories, delivery, budget, now_ms):
        return api.sources(operator, now_ms=now_ms, delivery=delivery, budget_tokens=budget)

    return access.run(TOOL, ctx, None, call)
