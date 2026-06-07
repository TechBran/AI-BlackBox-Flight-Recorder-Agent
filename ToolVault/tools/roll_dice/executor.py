"""Executor for roll_dice — reference example tool (no external dependencies).

Demonstrates the ToolVault v2 executor contract:
  async def execute(params: dict, ctx: ToolContext) -> ToolResult
- read inputs from `params` (with defaults matching schema.json),
- validate and fail gracefully with ToolResult(success=False, ...),
- return structured data via the `data` field for downstream consumers.
`ctx` carries the operator + base_url (unused here, but available).
"""

import random

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    sides = int(params.get("sides", 6))
    count = int(params.get("count", 1))

    if sides < 2 or sides > 1000:
        return ToolResult(success=False, result="sides must be between 2 and 1000")
    if count < 1 or count > 100:
        return ToolResult(success=False, result="count must be between 1 and 100")

    rolls = [random.randint(1, sides) for _ in range(count)]
    total = sum(rolls)
    return ToolResult(
        success=True,
        result=f"Rolled {count}d{sides}: {rolls} (total {total})",
        data={"rolls": rolls, "total": total, "sides": sides, "count": count},
    )
