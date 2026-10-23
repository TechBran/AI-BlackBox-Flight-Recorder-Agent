"""W09: provider billing failures classified into ONE error kind, credits_exhausted.

The 2026-10-09 audit found 20 Anthropic "credit balance is too low" 400s in
journald (Sep 2 - Oct 6). The provider's own text reached the task rows, but
nothing classified it: preflight only checked that a key existed, and no push
reached anyone. ``Orchestrator.provider_billing`` is the one classifier every
surface calls. These tests pin it against REAL error bodies:

  * Anthropic: the verbatim journald body (CU + chat task, Jul-Oct 2026), the
    documented 402 billing_error, the documented spend-cap 429 and the
    self-set spend-limit 400 (platform.claude.com/docs/en/api/errors and
    /api/rate-limits).
  * OpenAI: the verbatim journald realtime STT event (insufficient_quota,
    Jul 29 2026), the documented credit/spend codes (developers.openai.com
    error-codes guide), the long-standing billing_hard_limit_reached and
    billing_not_active codes, and the openai SDK's exception string.
  * Google Gemini: the 402 "prepayment credits are depleted" body
    (ai.google.dev/gemini-api/docs/billing + discuss.ai.google.dev reports),
    the 429 variant, BILLING_DISABLED, and the free-tier quota 429 that MUST
    NOT match (it says "check your plan and billing details" too).
  * xAI: the team-credits messages xAI users report (xAI documents no exact
    shape), matched by phrase.
  * The CLI agents' own renderings: Claude Code's "Credit balance is too low"
    and Codex's "Quota exceeded. Check your plan and billing details."

Plain-English phrases ("spending limit", "out of credits") count only on a
real provider refusal status, and an exception without a provider status is
never billing: a model's or an agent's own words must not read as a refusal.

Plus the record/notify side: one push per provider per hour (deduped, also
across a restart; a push that reached no device gives the hour back), a
successful call clears the flag preflight reads, a CLI agent's own login never
sets the API-key flag, and tests never touch the live state file.

Hermetic: no network; the push seam is a recorder and the state file is a tmp
path.
"""
import json

import pytest

from Orchestrator import provider_billing as pb


ANTHROPIC_CREDIT_400 = (
    '{"type":"error","error":{"type":"invalid_request_error","message":"Your credit '
    'balance is too low to access the Anthropic API. Please go to Plans & Billing to '
    'upgrade or purchase credits."},"request_id":"req_011CdMtESYTPKVGB7xBFWKw4"}'
)

OPENAI_REALTIME_QUOTA_EVENT = {
    "type": "error", "event_id": "event_E6potORfbqHyglrT9h37k",
    "error": {"type": "insufficient_quota", "code": "insufficient_quota",
              "message": "You exceeded your current quota, please check your plan and "
                         "billing details. For more information on this error, read the "
                         "docs: https://platform.openai.com/docs/guides/error-codes/api-errors.",
              "param": None, "event_id": None},
}

GEMINI_PREPAY_402 = json.dumps({"error": {
    "code": 402,
    "message": "Your prepayment credits are depleted. Please go to AI Studio at "
               "https://ai.studio/projects to manage your project and billing. Learn more "
               "at https://ai.google.dev/gemini-api/docs/billing#prepay.",
    "status": "RESOURCE_EXHAUSTED"}})

GEMINI_FREE_TIER_429 = json.dumps({"error": {
    "code": 429,
    "message": "You exceeded your current quota, please check your plan and billing "
               "details. For more information on this error, head to: "
               "https://ai.google.dev/gemini-api/docs/rate-limits.",
    "status": "RESOURCE_EXHAUSTED",
    "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaMetric": "generativelanguage.googleapis.com/"
                                        "generate_content_free_tier_requests",
                         "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"}]}})


# --------------------------------------------------------------------------- #
# Classifier                                                                   #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("provider,status,body,reason", [
    # Anthropic, verbatim journald (CU driver and the chat task row).
    ("anthropic", 400, ANTHROPIC_CREDIT_400, "no_credits"),
    ("anthropic", None, "400: " + ANTHROPIC_CREDIT_400, "no_credits"),
    ("anthropic", None, "API error 400: " + ANTHROPIC_CREDIT_400, "no_credits"),
    ("anthropic", 400, ANTHROPIC_CREDIT_400.encode(), "no_credits"),
    # Anthropic documented shapes.
    ("anthropic", 402, '{"type":"error","error":{"type":"billing_error","message":'
                       '"There is an issue with your billing or payment information."}}',
     "billing_inactive"),
    ("anthropic", 429, json.dumps({"type": "error", "error": {
        "type": "rate_limit_error",
        "message": "You have reached your API usage limits: your organization has crossed "
                   "its monthly API usage threshold, set based on your organization's API "
                   "tier. You will regain access on 2026-09-01 at 00:00 UTC.",
        "details": {"error_code": "enforced_spend_limit_reached"}}}), "spend_limit"),
    ("anthropic", 400, json.dumps({"type": "error", "error": {
        "type": "invalid_request_error",
        "message": "You have reached your specified workspace API usage limits. You will "
                   "regain access on 2026-11-01 at 00:00 UTC."}}), "spend_limit"),
    # OpenAI: verbatim journald realtime event, the chat 429, documented codes.
    ("openai", None, OPENAI_REALTIME_QUOTA_EVENT, "no_credits"),
    ("openai", 429, json.dumps({"error": OPENAI_REALTIME_QUOTA_EVENT["error"]}), "no_credits"),
    ("openai", 429, json.dumps({"error": {
        "message": "Your organization has no prepaid credits remaining.",
        "type": "insufficient_quota", "param": None, "code": "credit_balance_exhausted"}}),
     "no_credits"),
    ("openai", 429, json.dumps({"error": {
        "message": "Your organization reached its enforced spend limit.",
        "type": "insufficient_quota", "param": None,
        "code": "organization_spend_limit_exceeded"}}), "spend_limit"),
    ("openai", 429, json.dumps({"error": {
        "message": "Your project reached its enforced spend limit.",
        "type": "insufficient_quota", "param": None,
        "code": "project_spend_limit_exceeded"}}), "spend_limit"),
    ("openai", 400, json.dumps({"error": {
        "code": "billing_hard_limit_reached", "message": "Billing hard limit has been reached",
        "param": None, "type": "invalid_request_error"}}), "spend_limit"),
    ("openai", 429, json.dumps({"error": {
        "message": "Your account is not active, please check your billing details on our "
                   "website.", "type": "billing_not_active", "param": None,
        "code": "billing_not_active"}}), "billing_inactive"),
    # Google Gemini.
    ("google", 402, GEMINI_PREPAY_402, "no_credits"),
    ("gemini", 429, GEMINI_PREPAY_402.replace("402", "429"), "no_credits"),
    ("google", 403, json.dumps({"error": {
        "code": 403,
        "message": "This API method requires billing to be enabled. Please enable billing "
                   "on project #000000000000 by visiting https://console.developers.google"
                   ".com/billing/enable?project=000000000000 then retry.",
        "status": "PERMISSION_DENIED",
        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                     "reason": "BILLING_DISABLED", "domain": "googleapis.com"}]}}),
     "billing_inactive"),
    # Gemini's documented 400 FAILED_PRECONDITION (ai.google.dev/gemini-api/docs/
    # troubleshooting): no free tier in the caller's region, billing not enabled.
    ("google", 400, json.dumps({"error": {
        "code": 400,
        "message": "Gemini API free tier is not available in your country. Please enable "
                   "billing on your project in Google AI Studio.",
        "status": "FAILED_PRECONDITION"}}), "billing_inactive"),
    # The Codex CLI's own rendering of insufficient_quota (its binary's QuotaExceeded
    # text): the code itself never reaches the CLI's output.
    ("codex", None, "Quota exceeded. Check your plan and billing details.", "no_credits"),
    # xAI (phrases xAI users report; there is no documented body).
    ("xai", 403, json.dumps({
        "code": "The caller does not have permission to execute the specified operation",
        "error": "Your newly created team doesn't have any credits yet. You can purchase "
                 "credits on https://console.x.ai/team/00000000-0000-0000-0000-000000000000."}),
     "no_credits"),
    ("xai", 429, json.dumps({
        "code": "Some resource has been exhausted",
        "error": "Your team 00000000-0000-0000-0000-000000000000 has either used all "
                 "available credits or reached its monthly spending limit. To continue "
                 "making API requests, please purchase more credits or raise your "
                 "spending limit."}), "spend_limit"),
])
def test_billing_bodies_classify_as_credits_exhausted(provider, status, body, reason):
    err = pb.classify(provider, status, body)
    assert err is not None, body
    assert err.kind == pb.CREDITS_EXHAUSTED == "credits_exhausted"
    assert err.reason == reason
    assert err.provider in ("anthropic", "openai", "google", "xai")


@pytest.mark.parametrize("provider,status,body", [
    # Gemini's free-tier per-minute quota says "check your plan and billing
    # details" too: it is a RATE limit and must not be called out of credits.
    ("google", 429, GEMINI_FREE_TIER_429),
    ("google", 429, '{"error":{"code":429,"message":"Resource has been exhausted (e.g. '
                    'check quota).","status":"RESOURCE_EXHAUSTED"}}'),
    ("anthropic", 429, '{"type":"error","error":{"type":"rate_limit_error","message":'
                       '"Number of request tokens has exceeded your per-minute rate limit"}}'),
    ("anthropic", 529, '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'),
    ("anthropic", 400, '{"type":"error","error":{"type":"invalid_request_error","message":'
                       '"messages.1.content.0.thinking.signature: Field required"}}'),
    ("anthropic", 401, '{"type":"error","error":{"type":"authentication_error","message":'
                       '"invalid x-api-key"}}'),
    ("openai", 429, json.dumps({"error": {
        "message": "Rate limit reached for gpt-5.1 in organization org-x on tokens per min.",
        "type": "tokens", "param": None, "code": "rate_limit_exceeded"}})),
    ("openai", 404, json.dumps({"error": {"message": "The model does not exist",
                                          "type": "invalid_request_error",
                                          "code": "model_not_found"}})),
    ("xai", 400, '{"code":"Client specified an invalid argument","error":"Incorrect API key"}'),
    # A LAN server has no billing account, whatever its proxy says.
    ("custom", 402, ANTHROPIC_CREDIT_400),
    ("unknown-provider", 400, ANTHROPIC_CREDIT_400),
    ("anthropic", 400, ""),
    ("anthropic", None, None),
])
def test_other_errors_are_not_billing(provider, status, body):
    assert pb.classify(provider, status, body) is None


@pytest.mark.parametrize("provider,status,body", [
    # Ordinary English anyone (a model, an agent, a file) may write. With no
    # provider refusal status these are never a billing refusal.
    ("anthropic", None, "Next I will open Settings > Cards and lower the spending limit"),
    ("anthropic", None, "# block purchases over the monthly spending limit"),
    ("codex", None, "Updated README: the game shows 'Out of credits' when the player "
                    "loses all coins."),
    ("claude", None, "I've added the `spend_limit` column and the spend cap validation."),
    ("xai", None, "Your team has reached its monthly spending limit."),
    ("openai", None, "insufficient credits for this purchase"),
    # A success or a server error is not a refusal either.
    ("xai", 200, "Your team has reached its monthly spending limit."),
    ("openai", 500, "upstream said: out of credits"),
])
def test_plain_english_phrases_need_a_provider_refusal_status(provider, status, body):
    assert pb.classify(provider, status, body) is None


def test_the_same_phrases_on_a_provider_refusal_still_classify():
    body = json.dumps({"code": "Some resource has been exhausted",
                       "error": "Your team has reached its monthly spending limit."})
    for status in (400, 402, 403, 429):
        err = pb.classify("xai", status, body)
        assert err is not None and err.reason == "spend_limit", status


def test_message_follows_the_house_template_and_keeps_the_provider_text():
    err = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)
    assert err.message == ("Anthropic account is out of credits - add credits or turn on "
                           "auto-reload in the Anthropic console")
    assert err.detail.startswith("Your credit balance is too low")
    assert "req_011" not in err.detail and "{" not in err.detail  # message only, never the raw body
    assert err.text.startswith(err.message)
    assert "Your credit balance is too low" in err.text
    assert err.tagged().startswith("credits_exhausted: Anthropic account is out of credits")
    g = pb.classify("google", 402, GEMINI_PREPAY_402)
    assert g.message.startswith("Google Gemini account is out of credits - add credits or turn "
                                "on auto-reload in the Google AI Studio")


def test_detail_is_redacted_and_bounded():
    fake_key = "sk-" + "a" * 32
    body = json.dumps({"error": {"message": f"credit balance is too low for key {fake_key} "
                                            + "x" * 2000, "type": "insufficient_quota"}})
    err = pb.classify("openai", 429, body)
    assert fake_key not in err.detail and fake_key not in err.text
    assert len(err.detail) <= 300


class _OpenAIStatusError(Exception):
    """Shape of openai.APIStatusError: str() is 'Error code: N - <python repr>'."""

    def __init__(self, status_code, body):
        super().__init__(f"Error code: {status_code} - {body!r}")
        self.status_code = status_code
        self.body = body


class _GenaiClientError(Exception):
    """Shape of google.genai.errors.ClientError: int .code, str '<code> <STATUS>. {...}'."""

    def __init__(self, code, response_json):
        super().__init__(f"{code} {response_json['error']['status']}. {response_json!r}")
        self.code = code
        self.status = response_json["error"]["status"]
        self.details = response_json


def test_sdk_exceptions_classify_with_their_status_and_message():
    oe = _OpenAIStatusError(429, {"error": OPENAI_REALTIME_QUOTA_EVENT["error"]})
    err = pb.classify_exception("openai", oe)
    assert err is not None and err.reason == "no_credits" and err.status == 429
    assert err.detail.startswith("You exceeded your current quota")

    ge = _GenaiClientError(402, json.loads(GEMINI_PREPAY_402))
    err = pb.classify_exception("google", ge)
    assert err is not None and err.status == 402
    assert err.detail.startswith("Your prepayment credits are depleted")

    free = _GenaiClientError(429, json.loads(GEMINI_FREE_TIER_429))
    assert pb.classify_exception("google", free) is None
    assert pb.classify_exception("anthropic", RuntimeError("connection reset")) is None


def test_an_exception_without_a_provider_status_is_never_billing():
    # A driver's own RuntimeError carries the MODEL's text (frontier_agent_loop
    # raises one when Claude stops for max_tokens/refusal): whatever it says, it
    # is not a provider refusal.
    for text in ("Claude stopped without an action (stop_reason=refusal): I can't "
                 "change the spending limit on your card",
                 "Claude stopped without an action (stop_reason=max_tokens): the "
                 "banner says 'Your credit balance is too low' so I will top up",
                 "400: " + ANTHROPIC_CREDIT_400):
        assert pb.classify_exception("anthropic", RuntimeError(text)) is None

    class _HTTPStatusError(Exception):   # httpx.HTTPStatusError: status on .response
        def __init__(self):
            super().__init__("Client error '400 Bad Request' for url 'https://api'")
            self.response = type("R", (), {"status_code": 400})()

    exc = _HTTPStatusError()
    exc.body = json.loads(ANTHROPIC_CREDIT_400)
    err = pb.classify_exception("anthropic", exc)
    assert err is not None and err.status == 400 and err.reason == "no_credits"


def test_events_carry_the_kind_beside_a_plain_string_message():
    err = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)
    ev = err.event()
    assert ev["type"] == "error" and ev["data"] == err.text
    assert ev["error_detail"] == {"kind": "credits_exhausted", "provider": "anthropic",
                                  "reason": "no_credits", "message": err.message}
    # The Gemini/OpenAI CU drivers wrap error data as {"message": ...}.
    assert err.event(wrap=True)["data"] == {"message": err.text}


def test_credits_exhausted_error_is_an_http_exception_keeping_the_status():
    from fastapi import HTTPException

    err = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)
    exc = pb.CreditsExhaustedError(err)
    assert isinstance(exc, HTTPException)
    assert exc.status_code == 400 and exc.detail == err.text
    assert pb.billing_of(exc) is err
    assert pb.billing_of(RuntimeError("x")) is None


# --------------------------------------------------------------------------- #
# Record + one push per provider per hour                                      #
# --------------------------------------------------------------------------- #

@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A tmp state file, a controllable clock and a recording push seam."""
    clock = {"t": 1_800_000_000.0}
    pushes = []

    def fake_push(title, body, dedup_key, on_result=None):
        pushes.append({"title": title, "body": body, "dedup_key": dedup_key,
                       "on_result": on_result})
        return True

    monkeypatch.setattr(pb, "STATE_PATH", tmp_path / "provider_billing_state.json")
    monkeypatch.setattr(pb, "_now", lambda: clock["t"])
    monkeypatch.setattr(pb, "_push", fake_push)
    pb._reset_cache()
    yield clock, pushes, tmp_path / "provider_billing_state.json"
    pb._reset_cache()


def test_one_push_per_provider_per_hour(ledger):
    clock, pushes, _ = ledger
    err = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)

    assert pb.report(err, surface="chat") is True
    clock["t"] += 60
    assert pb.report(err, surface="computer use") is False   # same hour: deduped
    clock["t"] += 3000
    assert pb.report(err, surface="cron") is False
    assert len(pushes) == 1
    assert pushes[0]["title"] == "Anthropic is out of credits"
    assert "Anthropic account is out of credits" in pushes[0]["body"]
    assert "chat" in pushes[0]["body"]

    # Another provider has its own hour.
    o = pb.classify("openai", None, OPENAI_REALTIME_QUOTA_EVENT)
    assert pb.report(o, surface="speech-to-text") is True
    assert len(pushes) == 2

    clock["t"] += 600   # now > 1 h after the first Anthropic push
    assert pb.report(err, surface="chat") is True
    assert len(pushes) == 3


def test_the_hourly_dedupe_survives_a_restart(ledger):
    clock, pushes, path = ledger
    err = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)
    pb.report(err, surface="chat")
    assert path.exists()

    pb._reset_cache()            # a new process reads the stamp back from disk
    clock["t"] += 120
    assert pb.report(err, surface="chat") is False
    assert len(pushes) == 1


def test_a_dropped_push_is_not_counted_as_sent(ledger, monkeypatch):
    clock, pushes, _ = ledger
    monkeypatch.setattr(pb, "_push", lambda *a, **k: False)   # no main loop yet
    err = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)
    assert pb.report(err, surface="chat") is False

    monkeypatch.setattr(pb, "_push", lambda t, b, k, on_result=None: pushes.append(t) or True)
    clock["t"] += 5
    assert pb.report(err, surface="chat") is True
    assert pushes == ["Anthropic is out of credits"]


def test_a_push_that_reached_no_device_gives_the_hour_back(ledger):
    # The audit's evidence was literally NOTIFY delivered=0/0: a push no device
    # received must not use up the hour, or a phone that comes back online
    # minutes later never hears about it.
    from Orchestrator.notifications.bus import NotifyResult

    clock, pushes, _ = ledger
    err = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)
    assert pb.report(err, surface="chat") is True
    pushes[-1]["on_result"](NotifyResult(notif_id="n1", delivered=[], unreachable=["d1"]))

    clock["t"] += 120
    assert pb.report(err, surface="chat") is True          # tried again
    pushes[-1]["on_result"](NotifyResult(notif_id="n2", delivered=["d1"]))

    clock["t"] += 120
    assert pb.report(err, surface="chat") is False         # this one landed: deduped
    assert len(pushes) == 2

    # A push whose coroutine failed (no result) is not counted as sent either.
    o = pb.classify("openai", None, OPENAI_REALTIME_QUOTA_EVENT)
    assert pb.report(o, surface="speech-to-text") is True
    pushes[-1]["on_result"](None)
    clock["t"] += 60
    assert pb.report(o, surface="speech-to-text") is True
    assert len(pushes) == 4


def test_a_late_give_back_never_undoes_a_newer_claim(ledger):
    from Orchestrator.notifications.bus import NotifyResult

    clock, pushes, _ = ledger
    err = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)
    pb.report(err, surface="chat")
    first = pushes[-1]["on_result"]
    clock["t"] += 4000                                      # next hour: a new claim
    assert pb.report(err, surface="chat") is True
    first(NotifyResult(notif_id="old", delivered=[]))       # the old push's result arrives late
    clock["t"] += 60
    assert pb.report(err, surface="chat") is False          # the new hour still stands


def test_tests_never_touch_the_live_state_file():
    # The root conftest points STATE_PATH at a temp file for every test: success
    # paths call note_ok on every response, and from the main checkout (the live
    # serving tree) an unpatched run would erase a real recorded refusal.
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    path = pb._state_path().resolve()
    assert repo / "Manifest" not in path.parents, path


def test_last_failure_is_what_preflight_reads_and_a_success_clears_it(ledger):
    clock, _, _ = ledger
    assert pb.last_failure("anthropic") is None
    err = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)
    pb.report(err, surface="computer use")

    rec = pb.last_failure("anthropic")
    assert rec["kind"] == "credits_exhausted" and rec["reason"] == "no_credits"
    assert rec["surface"] == "computer use"
    assert rec["message"] == err.message
    assert pb.last_failure("openai") is None

    pb._reset_cache()
    assert pb.last_failure("anthropic") is not None   # persisted

    pb.note_ok("anthropic")
    assert pb.last_failure("anthropic") is None
    pb._reset_cache()
    assert pb.last_failure("anthropic") is None       # the clear persisted too


def test_a_cli_login_failure_never_sets_the_api_key_flag(ledger):
    _, pushes, _ = ledger
    err = pb.classify("claude", None, "Credit balance is too low")
    assert err is not None and err.provider == "anthropic"
    assert pb.report(err, surface="CLI agent (claude)", scope="cli") is True
    assert pb.last_failure("anthropic") is None
    # The CLI's login and the box key are different accounts: separate hours.
    api = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)
    assert pb.report(api, surface="chat") is True
    assert len(pushes) == 2


def test_check_classifies_and_reports_in_one_call(ledger):
    _, pushes, _ = ledger
    assert pb.check("anthropic", 529, '{"type":"error","error":{"type":"overloaded_error"}}',
                    surface="chat") is None
    assert pushes == []
    err = pb.check("anthropic", 400, ANTHROPIC_CREDIT_400, surface="chat")
    assert err is not None and len(pushes) == 1


def test_raise_if_exhausted(ledger):
    pb.raise_if_exhausted("anthropic", 500, "upstream boom", surface="chat")  # no raise
    with pytest.raises(pb.CreditsExhaustedError) as info:
        pb.raise_if_exhausted("anthropic", 400, ANTHROPIC_CREDIT_400, surface="chat")
    assert info.value.billing.reason == "no_credits"


def test_a_corrupt_state_file_is_treated_as_empty(ledger):
    _, pushes, path = ledger
    path.write_text("{not json")
    pb._reset_cache()
    assert pb.last_failure("anthropic") is None
    err = pb.classify("anthropic", 400, ANTHROPIC_CREDIT_400)
    assert pb.report(err, surface="chat") is True
    assert json.loads(path.read_text())["exhausted"]["anthropic"]["reason"] == "no_credits"
