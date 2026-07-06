"""Source-text guard for the per-card RE-EMBED UI in the Memory step.

No JS test infra in Portal (see test_onboarding_steps_parity.py), so — same
house pattern as test_embeddings_step_reranker_selector.py — we parse
Portal/onboarding/steps/embeddings.js and assert the re-embed affordance is
wired to the shipped /embeddings/* contract:
  * every card shows the store's embedding STRATEGY (chunked / whole_document /
    none), read from status.models[].strategy
  * a "Re-embed all snapshots" control (id/class token `ob-emb-reembed`) posts
    to /embeddings/reembed {target}
  * startReembed mirrors startMigrate's launch/attach idiom: 200 → seed
    status.job then routeRender; 409 → refreshStatus + routeRender (attach, NOT
    an error)
  * CLOUD models gate the POST behind an inline cost confirm; LOCAL models don't
  * the progress panel reads the reembed job's kind/phase — an "activating…"
    sub-label + hidden Cancel gated on state==='running' && phase==='activating'

These literals MUST stay greppable in the source (no string-concatenated URLs /
class tokens) so this guard keeps working — same rule as the reranker test.
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
    """Body of a top-level `function <name>(...) { ... }` (brace-balanced)."""
    src = _src()
    m = re.search(rf"function {re.escape(name)}\s*\([^)]*\)\s*\{{", src)
    assert m, f"could not find function {name}(...) in embeddings.js"
    i = m.end() - 1  # at the opening brace
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i + 1:j]
    raise AssertionError(f"unbalanced braces scanning {name}()")


# ── Task 2.1 — per-card strategy line ────────────────────────────────

def test_card_renders_the_store_strategy():
    body = _fn("strategyHtml")
    assert "m.strategy" in body, "strategyHtml must read status.models[].strategy"
    assert '"chunked"' in body, "must handle the chunked (current) strategy"
    assert '"whole_document"' in body, "must handle the whole_document (older) strategy"
    # whole_document is the upgrade prompt; none is 'Not built yet'.
    assert "re-embed to upgrade" in body.lower()


def test_strategy_line_is_wired_into_the_card():
    # renderCard must actually render the strategy line (not just define it).
    assert "strategyHtml(m)" in _fn("renderCard")


# ── Task 2.2 — Re-embed button (active + non-active built cards) ──────

def test_reembed_button_present_and_labelled():
    body = _fn("reembedHtml")
    assert "ob-emb-reembed" in body, "the re-embed control needs the ob-emb-reembed token"
    assert "Re-embed all snapshots" in body, "button label"
    # Offered on the active card OR any card whose store exists.
    assert "store_exists" in body and "status.active" in body


def test_reembed_disabled_while_a_job_runs():
    body = _fn("reembedHtml")
    assert 'status.job && status.job.state === "running"' in body, \
        "the re-embed button must disable while an embed job is running"
    assert "disabled" in body


def test_reembed_wired_in_card_actions():
    body = _fn("wireCardActions")
    assert "ob-emb-reembed" in body and "startReembed(" in body, \
        "wireCardActions must bind .ob-emb-reembed → startReembed(slug)"


def test_card_actions_render_the_reembed_control():
    # Active card (was blurb-only) and non-active built cards get it.
    body = _fn("cardActionsHtml")
    assert "reembedHtml(m)" in body


# ── startReembed mirrors startMigrate (launch/attach idiom) ──────────

def test_startreembed_posts_to_the_reembed_endpoint():
    src = _src()
    assert '"/embeddings/reembed"' in src, "URL must be a greppable literal (no concatenation)"
    body = _fn("startReembed")
    assert '"/embeddings/reembed"' in body
    assert "target: slug" in body, "POST body carries {target: slug}"


def test_startreembed_seeds_job_on_200_then_routes():
    body = _fn("startReembed")
    # 200 path: seed status.job from the response body, then render via routeRender.
    assert "status.job = seeded" in body, "200 must seed status.job from the body"
    assert "routeRender()" in body


def test_startreembed_attaches_on_409_without_error():
    body = _fn("startReembed")
    m = re.search(r"if\s*\(\s*r\.status === 409\s*\)\s*\{(.*?)\}", body, re.DOTALL)
    assert m, "startReembed must handle the 409 (already-running) case"
    branch = m.group(1)
    assert "refreshStatus()" in branch and "routeRender()" in branch, \
        "409 must refreshStatus + routeRender to ATTACH to the running panel"
    assert "showHint" not in branch, "409 is an attach, NOT an error"


def test_startreembed_cloud_confirms_local_does_not():
    body = _fn("startReembed")
    # Cloud gate: privacy !== 'local' stages the inline confirm before posting.
    assert 'm.privacy !== "local"' in body
    assert "reembedConfirm" in body, "cloud POST is gated behind reembedConfirm"
    # Local: no confirm, but surface the advisory cpu_warning if present.
    assert "cpu_warning" in body


def test_stalled_retry_is_preconfirmed():
    """Important review fix: a stalled-retry re-triggers an ALREADY-confirmed
    re-embed, and the stalled panel has NO #ob-emb-grid to stage a fresh confirm
    into (renderGrid would no-op → a second click would POST without ever showing
    the cost). So the retry must bypass the cloud confirm gate (preConfirmed)."""
    sig = re.search(r"async function startReembed\s*\(([^)]*)\)", _src())
    assert sig and "preConfirmed" in sig.group(1), \
        "startReembed must accept a preConfirmed param"
    body = _fn("startReembed")
    assert "!preConfirmed" in body, "the cloud confirm gate must honour preConfirmed"
    stalled = _fn("renderJobStalled")
    assert re.search(
        r"startReembed\(\s*job\.target\s*,\s*retry\s*,\s*true\s*\)", stalled
    ), "stalled reembed retry must call startReembed(..., true) (pre-confirmed)"


def test_reembed_not_offered_on_not_ready_card():
    """Review fix: a not-ready store would just stall the POST — gate the button
    on m.ready (the active model stays re-embeddable through a transient dip)."""
    body = _fn("reembedHtml")
    assert "m.ready" in body, "reembedHtml must gate the button on readiness"


def test_running_panel_clears_staged_confirm():
    """Review fix: a staged cloud confirm must not survive an unrelated job and
    reappear on return to the picker — the running panel drops it."""
    assert "reembedConfirm = null" in _fn("renderJobPanel")


def test_cancel_copy_is_kind_aware():
    """Review fix: the post-cancel hint reads 'Re-embed cancelled …' for a
    re-embed, 'Migration cancelled …' for a model switch."""
    src = _src()
    assert "Re-embed cancelled — progress so far is kept." in src
    assert "Migration cancelled — progress so far is kept." in src


# ── Task 2.4 — progress-panel phase + cancel-hide + kind ─────────────

def test_job_panel_reads_reembed_kind():
    panel = _fn("renderJobPanel")
    assert 'job.kind === "reembed"' in panel, \
        "the running panel header must special-case a reembed job"


def test_update_panel_shows_activating_and_hides_cancel():
    body = _fn("updateJobPanel")
    # Gated on BOTH running state AND the activating phase (a done reembed also
    # carries phase info and must not resurrect Cancel here).
    assert 'job.state === "running"' in body
    assert 'job.phase === "activating"' in body
    assert "activating" in body  # the sub-label copy
    # Cancel is hidden while activating.
    assert "cancelBtn.hidden = true" in body


def test_done_copy_is_kind_aware():
    body = _fn("renderJobDone")
    assert 'job.kind === "reembed"' in body
    assert "re-embedded" in body
    assert "now live" in body and "ready to switch to" in body, \
        "completion copy differs for the active vs non-active model"
    # active-vs-not is decided by target === status.active.
    assert "job.target === status.active" in body
