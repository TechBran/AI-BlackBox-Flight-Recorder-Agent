"""Source-text guard for the Memory-step reranker CARD (two-card redesign).

No JS test infra in Portal (see test_onboarding_steps_parity.py), so — house
pattern — we parse Portal/onboarding/steps/embeddings.js and assert the reranker
card preserves every behavior Brandon live-tested on 2026-07-05, now that the
2026-07-22 two-card redesign lifted the reranker into its OWN top-level card and
split the old monolithic rerankLineHtml() into rerankCardHtml() + per-row option
helpers (rerankCloudOptionHtml / rerankOnboxOptionHtml / rerankRowHtml):
  * reads the additive per-model catalog (rr.model_catalog) + rr.tier
  * tier-gates options (only models whose tiers include this box's tier)
  * gates cloud/LLM selectability on the model's key_present
  * un-keyed cloud models deep-link back to the API-Keys step (no key entry here)
  * Vertex deep-links the SA-upload (optional_integrations step) as "Advanced"
  * the active row carries an is-active class the CSS highlights (green)
  * renders rr.tier_guidance (the free-first pick guidance)
  * selecting POSTs /rerank/select WITHOUT an api_key (key already in .env)
  * rerankStatus === null still HIDES the whole card
  * NEW (2026-07-22): the on-box reranker keys on the backend `built` flag —
    the self-converted GGUF physically on disk — NOT the old
    gpu-and-service_reachable proxy, and a not-built box shows an honest note
    (never a faked build button).
"""
import re
from pathlib import Path

EMB_JS = (
    Path(__file__).resolve().parents[2]
    / "Portal" / "onboarding" / "steps" / "embeddings.js"
)


def _src() -> str:
    return EMB_JS.read_text(encoding="utf-8")


def _fn(name: str) -> str:
    """Body of a top-level function `[export ][async ]function NAME(...) {...}`.

    Relies on the house style that every such function's closing brace sits in
    column 0 (a `\\n}` boundary), which the non-greedy match stops at.
    """
    m = re.search(
        r"function " + re.escape(name) + r"\s*\([^)]*\)\s*\{(.*?)\n\}",
        _src(),
        re.DOTALL,
    )
    assert m, f"could not find function {name}(...) {{...}} in embeddings.js"
    return m.group(1)


def _card_fn() -> str:
    return _fn("rerankCardHtml")


def _cloud_fn() -> str:
    return _fn("rerankCloudOptionHtml")


def _onbox_fn() -> str:
    return _fn("rerankOnboxOptionHtml")


def _row_fn() -> str:
    return _fn("rerankRowHtml")


def test_null_status_hides_the_card():
    body = _card_fn()
    # The very first guard returns "" when there is no rerank status.
    assert re.search(r"if\s*\(\s*!rr\s*\)\s*return\s*\"\"", body), \
        "rerankCardHtml must return '' when rerankStatus is null (hide the card)"


def test_card_reads_catalog_and_tier():
    body = _card_fn()
    assert "model_catalog" in body, "card must read rr.model_catalog"
    assert "rr.tier" in body or ".tier" in body, "card must read the tier"


def test_card_tier_gates_options():
    body = _card_fn()
    # Options are filtered to models whose tiers include this box's tier.
    assert ".tiers" in body and "includes(" in body, \
        "card must filter options by model.tiers.includes(tier)"


def test_cloud_option_gates_on_key_present():
    body = _cloud_fn()
    assert "key_present" in body, "cloud/LLM options must gate on key_present"


def test_unkeyed_cloud_deeplinks_to_api_keys_step():
    body = _cloud_fn()
    assert "step=api_keys" in body, \
        "an un-keyed cloud reranker must deep-link back to the API-Keys step"


def test_vertex_deeplinks_to_credentials_upload_step():
    body = _cloud_fn()
    assert "step=optional_integrations" in body, \
        "Vertex (Advanced) must deep-link the SA-upload (optional_integrations)"


def test_vertex_selectable_when_sa_present():
    """Brandon live-test 2026-07-05: Vertex must become SELECTABLE once the SA is
    uploaded (key_present), not be permanently deep-linked. The vertex branch
    gates on key_present (select button when present, SA-upload link when not)."""
    body = _cloud_fn()
    m = re.search(r'provider === "vertex"(.*?)\}\s*else\s*\{', body, re.DOTALL)
    assert m, "could not isolate the vertex branch"
    assert "key_present" in m.group(1), \
        "vertex branch must gate on key_present (selectable when the SA is present)"


def test_active_row_gets_visual_highlight_class():
    """Brandon live-test 2026-07-05: the selected reranker must be visually clear
    — the active row carries an is-active class the CSS highlights (green)."""
    body = _row_fn()
    assert "is-active" in body, \
        "the active reranker row must get an is-active class for the selected visual"


def test_renders_tier_guidance():
    """Brandon 2026-07-05: the card renders the backend's tier-aware
    'which should I pick?' guidance (free-first, leads with the local reranker)."""
    body = _card_fn()
    assert "tier_guidance" in body, \
        "card must render rr.tier_guidance (the free-first pick guidance)"


def test_onbox_option_keys_on_built_flag_not_service_proxy():
    """2026-07-22 solidify: the on-box reranker's readiness is the backend `built`
    flag (the self-converted GGUF on disk), NOT the old gpu/service_reachable
    proxy — a working reranker must never read as un-set-up, and vice versa."""
    body = _onbox_fn()
    assert "m.built" in body or ".built" in body, \
        "on-box reranker option must key on the backend `built` flag"
    assert "service_reachable" not in body, \
        "on-box option must NOT resurrect the old gpu/service_reachable proxy"


def test_onbox_not_built_shows_honest_note_never_a_fake_build_button():
    """A not-built on-box reranker shows an honest 'provisioned by setup' note —
    we NEVER render a build button the wizard can't yet honor (no faked action)."""
    body = _onbox_fn()
    # The not-built branch renders a muted note, not a select/build button.
    assert "ob-emb-rerank-muted" in body, \
        "not-built on-box reranker must render the honest muted note"


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
