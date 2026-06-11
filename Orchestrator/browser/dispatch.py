"""Resolve a CU model id to its backend driver using the same filter rules
as the /models/computer-use catalog."""
import re

from Orchestrator.config import CU_MODEL_FILTERS, CU_MODEL_DEFAULT


def resolve_backend(model: str) -> str:
    model = (model or CU_MODEL_DEFAULT).strip()
    for backend, pattern in CU_MODEL_FILTERS.items():
        if re.match(pattern, model):
            return backend
    return "anthropic"  # unknown claude-adjacent ids and "" fall through here
