"""Source-text guard for the M11 Portal updates-manager reranker SELECTOR.

No JS test infra in Portal (see test_onboarding_steps_parity.py /
test_embeddings_step_reranker_selector.py), so — house pattern — we parse
Portal/modules/updates-manager.js and assert the reranker card mirrors the
M10.1 Memory-step selector's contract (surface 2/3 of the wizard-vs-portal
split; markup MIRRORED, not shared):
  * a NEW GET /rerank/status fetch (updates-manager had none before M11)
  * reads the additive per-model catalog (rr.model_catalog) + rr.tier
  * tier-gates options (only models whose tiers include this box's tier)
  * gates cloud/LLM selectability on the model's key_present
  * un-keyed cloud models deep-link to the onboarding API-Keys step
    (no key entry in the Portal — honest, non-broken deep-link)
  * Vertex deep-links the SA-upload (optional_integrations step) as "Advanced"
  * selecting POSTs /rerank/select WITHOUT an api_key (key already in .env)
  * a null/failed /rerank/status still HIDES the whole block
"""
import re
from pathlib import Path

UPDATES_JS = (
    Path(__file__).resolve().parents[2]
    / "Portal" / "modules" / "updates-manager.js"
)


def _src() -> str:
    return UPDATES_JS.read_text(encoding="utf-8")


def _rerank_fn() -> str:
    """The _rerankCardHtml function body (the selector renderer)."""
    src = _src()
    m = re.search(r"function _rerankCardHtml\s*\(\s*\)\s*\{(.*?)\n\}", src, re.DOTALL)
    assert m, "could not find function _rerankCardHtml() {...} in updates-manager.js"
    return m.group(1)


def test_fetches_rerank_status():
    # M11 adds a /rerank/status fetch that updates-manager did not have before.
    assert '"/rerank/status"' in _src(), \
        "updates-manager must fetch GET /rerank/status (M11)"


def test_null_status_hides_the_block():
    body = _rerank_fn()
    # The very first guard returns "" when there is no rerank status.
    assert re.search(r"if\s*\(\s*!rr\s*\)\s*return\s*\"\"", body), \
        "_rerankCardHtml must return '' when the rerank status is null (hide the block)"


def test_selector_reads_catalog_and_tier():
    body = _rerank_fn()
    assert "model_catalog" in body, "selector must read rr.model_catalog"
    assert ".tier" in body, "selector must read the tier"


def test_selector_tier_gates_options():
    body = _rerank_fn()
    # Options are filtered to models whose tiers include this box's tier.
    assert ".tiers" in body and "includes(" in body, \
        "selector must filter options by model.tiers.includes(tier)"


def test_selector_gates_on_key_present():
    body = _rerank_fn()
    assert "key_present" in body, "cloud/LLM options must gate on key_present"


def test_unkeyed_cloud_deeplinks_to_api_keys_step():
    body = _rerank_fn()
    assert "step=api_keys" in body, \
        "an un-keyed cloud reranker must deep-link to the onboarding API-Keys step"


def test_vertex_deeplinks_to_credentials_upload_step():
    body = _rerank_fn()
    assert "step=optional_integrations" in body, \
        "Vertex (Advanced) must deep-link the SA-upload (optional_integrations)"


def test_vertex_selectable_when_sa_present():
    """Brandon live-test 2026-07-05: Vertex becomes SELECTABLE once the SA is
    uploaded (key_present), not permanently deep-linked. Mirrors the wizard."""
    body = _rerank_fn()
    m = re.search(r'provider === "vertex"(.*?)provider === "voyage"', body, re.DOTALL)
    assert m, "could not isolate the vertex branch"
    assert "key_present" in m.group(1), \
        "vertex branch must gate on key_present (selectable when the SA is present)"


def test_active_row_gets_visual_highlight_class():
    """Brandon live-test 2026-07-05: the selected reranker row carries an
    is-active class the CSS highlights (green) — mirrors the wizard."""
    body = _rerank_fn()
    assert "is-active" in body, \
        "the active reranker row must get an is-active class for the selected visual"


def test_renders_tier_guidance():
    """Brandon 2026-07-05: the Portal card renders the tier-aware guidance
    (free-first, leads with the local reranker) — mirrors the wizard."""
    body = _rerank_fn()
    assert "tier_guidance" in body, \
        "Portal card must render rr.tier_guidance (the free-first pick guidance)"


def test_select_posts_without_api_key():
    """Selecting POSTs /rerank/select; the body carries provider/model/enabled
    but NEVER an api_key (the key is already in .env from the API-Keys step)."""
    src = _src()
    assert '"/rerank/select"' in src
    m = re.search(r"function _onRerankSelect\s*\([^)]*\)\s*\{(.*?)\n\}", src, re.DOTALL)
    assert m, "expected an _onRerankSelect(...) handler"
    handler = m.group(1)
    assert "/rerank/select" in handler
    assert "api_key" not in handler, \
        "the reranker select POST must NOT include api_key (key already in .env)"
    # POST shape: provider + model + enabled.
    assert "provider" in handler and "model" in handler and "enabled" in handler


def test_turn_off_control_disables_reranking():
    body = _rerank_fn()
    # A turn-off affordance sends enabled:false when reranking is currently on.
    assert 'data-enabled="false"' in body, \
        "the reranker card must offer a turn-off control (data-enabled=false)"
