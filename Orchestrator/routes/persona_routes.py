# Orchestrator/routes/persona_routes.py — per-operator persona read/write/reset.
# Thin wrappers over the existing operator-preferences store (key "persona").
from pydantic import BaseModel
from Orchestrator.checkpoint import app   # same app object as tts_routes/admin_routes
from Orchestrator import state
from Orchestrator.behavioral_core import get_persona, DEFAULT_PERSONA_CHAT, PERSONA_PREF_KEY


class PersonaBody(BaseModel):
    persona: str = ""


@app.get("/operator/persona/{operator}")
def get_op_persona(operator: str):
    custom = state.get_operator_preference(operator, PERSONA_PREF_KEY, None)
    return {
        "operator": operator,
        "persona": get_persona(operator, "chat"),
        "is_custom": bool(custom and str(custom).strip()),
        "default": DEFAULT_PERSONA_CHAT,
    }


@app.put("/operator/persona/{operator}")
def put_op_persona(operator: str, body: PersonaBody):
    state.set_operator_preference(operator, PERSONA_PREF_KEY, body.persona)
    return {
        "status": "ok",
        "operator": operator,
        "persona": get_persona(operator, "chat"),
        "is_custom": bool(body.persona.strip()),
    }


@app.delete("/operator/persona/{operator}")
def delete_op_persona(operator: str):
    if operator in state.OPERATOR_PREFERENCES:
        state.OPERATOR_PREFERENCES[operator].pop(PERSONA_PREF_KEY, None)
        state.save_operator_preferences()
    return {
        "status": "ok",
        "operator": operator,
        "persona": DEFAULT_PERSONA_CHAT,
        "is_custom": False,
    }
