"""Executor for save_contact (migrated from blackbox_tools._execute_save_contact)."""
from Orchestrator.contacts import upsert_contact
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    """Save or update a contact."""
    name = params.get("name", "")
    notes = params.get("notes", "")
    tags = params.get("tags", [])

    if not name:
        return ToolResult(False, "Contact name is required")
    if not notes:
        return ToolResult(False, "Contact notes are required")
    if not tags:
        return ToolResult(False, "At least one tag is required")

    try:
        contact = upsert_contact(
            name=name,
            notes=notes,
            tags=tags,
            operator=ctx.operator,
            created_by=ctx.operator,
            phone=params.get("phone"),
            email=params.get("email"),
            relationship=params.get("relationship")
        )

        return ToolResult(
            success=True,
            result=f"Contact saved: {contact['name']}" + (f" ({contact.get('phone', '')})" if contact.get('phone') else ""),
            data={"contact": contact}
        )
    except Exception as e:
        return ToolResult(False, f"Save contact error: {str(e)}")
