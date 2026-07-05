"""Source-text guard for the M10.1 Memory-step reranker SELECTOR.

No JS test infra in Portal (see test_onboarding_steps_parity.py), so — house
pattern — we parse Portal/onboarding/steps/embeddings.js and assert the
reranker line became a tier-driven selector over configured providers:
  * reads the additive per-model catalog (rr.model_catalog) + rr.tier
  * tier-gates options (only models whose tiers include this box's tier)
  * gates cloud/LLM selectability on the model's key_present
  * un-keyed cloud models deep-link back to the API-Keys step (no key entry here)
  * Vertex deep-links the SA-upload (optional_integrations step) as "Advanced"
  * selecting POSTs /rerank/select WITHOUT an api_key (key already in .env)
  * rerankStatus === null still HIDES the whole block
"""
import re
from pathlib import Path

EMB_JS = (
    Path(__file__).resolve().parents[2]
    / "Portal" / "onboarding" / "steps" / "embeddings.js"
)


def _src() -> str:
    return EMB_JS.read_text(encoding="utf-8")


def _rerank_fn() -> str:
    """The rerankLineHtml function body (the selector renderer)."""
    src = _src()
    m = re.search(r"function rerankLineHtml\s*\(\s*\)\s*\{(.*?)\n\}", src, re.DOTALL)
    assert m, "could not find function rerankLineHtml() {...} in embeddings.js"
    return m.group(1)


def test_null_status_hides_the_block():
    body = _rerank_fn()
    # The very first guard returns "" when there is no rerank status.
    assert re.search(r"if\s*\(\s*!rr\s*\)\s*return\s*\"\"", body), \
        "rerankLineHtml must return '' when rerankStatus is null (hide the block)"


def test_selector_reads_catalog_and_tier():
    body = _rerank_fn()
    assert "model_catalog" in body, "selector must read rr.model_catalog"
    assert "rr.tier" in body or ".tier" in body, "selector must read the tier"


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
        "an un-keyed cloud reranker must deep-link back to the API-Keys step"


def test_vertex_deeplinks_to_credentials_upload_step():
    body = _rerank_fn()
    assert "step=optional_integrations" in body, \
        "Vertex (Advanced) must deep-link the SA-upload (optional_integrations)"


def test_select_posts_without_api_key():
    """Selecting POSTs /rerank/select; the body carries provider/model/enabled
    but NEVER an api_key (the key is already in .env from the API-Keys step)."""
    src = _src()
    assert '"/rerank/select"' in src
    m = re.search(r"function onRerankSelect\s*\([^)]*\)\s*\{(.*?)\n\}", src, re.DOTALL)
    assert m, "expected an onRerankSelect(...) handler"
    handler = m.group(1)
    assert "/rerank/select" in handler
    assert "api_key" not in handler, \
        "the reranker select POST must NOT include api_key (key already in .env)"
    # POST shape: provider + model + enabled.
    assert "provider" in handler and "model" in handler and "enabled" in handler
