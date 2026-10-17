# Orchestrator/tests/test_portal_app_preview_sandbox.py
"""#appPreviewFrame must not grant allow-same-origin (audit P07-html-css-13).

User apps are proxied under the Portal's own origin (/app-proxy/{port}/), so
`allow-scripts allow-same-origin` made the sandbox a no-op: LLM-written app
code ran with the Portal's DOM, localStorage and every Orchestrator endpoint,
and could even strip its own sandbox attribute. The CU viewer frame is
Orchestrator-owned and keeps allow-same-origin on purpose.
"""
import re
from pathlib import Path

import pytest

PORTAL = Path(__file__).resolve().parents[2] / "Portal"


def _frame_tag(html, frame_id):
    m = re.search(r'<iframe\b[^>]*\bid="%s"[^>]*>' % re.escape(frame_id), html)
    assert m, f"{frame_id} not found"
    return m.group(0)


def _sandbox_tokens(tag):
    m = re.search(r'\bsandbox="([^"]*)"', tag)
    assert m, f"no sandbox attribute on {tag}"
    return set(m.group(1).split())


@pytest.mark.parametrize("page", ["index.html", "index-modular.html"])
def test_app_preview_frame_has_opaque_origin(page):
    tokens = _sandbox_tokens(_frame_tag((PORTAL / page).read_text(encoding="utf-8"),
                                        "appPreviewFrame"))
    assert "allow-same-origin" not in tokens
    assert {"allow-scripts", "allow-forms"} <= tokens


def test_no_module_widens_the_preview_sandbox():
    """Nothing that touches the preview frame may re-grant allow-same-origin
    at runtime (the zellij terminal iframe needs it and never references it)."""
    for js in (PORTAL / "modules").glob("*.js"):
        text = js.read_text(encoding="utf-8")
        if "appPreviewFrame" in text:
            assert "allow-same-origin" not in text, js.name
