"""Agent access to wellness data (spec §4.1-§4.3, §4.6, §10): what a turn is
offered, how the wellness_* tools run, and what the model is told.

Offering. The ToolVault feature FEATURES["wellness"] asks offered() for every
wellness entry of an injection, the toolvault meta-tool's search / read / list
ask the same thing, and so does BlackBoxToolExecutor before it runs a
wellness tool (the catch-all of every chat loop goes through it). All of them
reduce to offer(ctx): the ToolContext must name the operator of the turn its
entry point stamped (turn.py), from the same surface, and the provider whose
model will read the answer (the injection's provider, the chat loop's own:
tool_dispatch.LoopProfile.provider), which must be the stamped one; the gate
(gate.check_all) must then pass for at least one category. No ctx, no stamped
turn, no provider, a caller of /local/tools/execute (the on-device bridge,
the MCP gateway) or any mismatch is nothing at all: default-deny. The entry
points stamp the provider that will run, never the client's spelling of it
(routes/chat/stream.dispatched_provider).

Executing. run() re-runs the gate for each category the call needs (the
authoritative check: gate.check_all, which is gate.check for every category
with the switch, the enrollment and the destination read once, off the event
loop on the ToolVault pool), answers through api.* with a Delivery for the
current turn (so the delivered row carries its turn_key) and the token budget
of the destination class, and returns ToolResult(result=render(env), data=env).
Refusals, no data, not enough history and a paused store with nothing in it
come back ok:false with the user-facing fix.

Telling the model. instructions() is appended to the tool instructions: the
WELLNESS RULES and the operator's instinct prompt when a wellness tool is
offered, the one "not shared with this AI" line when only the provider stands
in the way, nothing otherwise.

The Signal frame. used_frame() builds the UI-only wellness_used data (ref,
categories, as_of, destination); a stream sends it per answering tool call
(signal_event), the non-stream worker puts the turn's newest one in
result_data with the turn's refs (used_fields), so both clients render the
same footer. Never a value, never a prompt, never the ledger.

Stamping. request_turn() / task_turn() build the WellnessTurn an entry point
stamps; origin_from_request() verifies an X-BlackBox-Origin header (the
enrolled phone's device key); issue_grant() / redeem_grant() carry a verified
/chat request to the worker that runs it, in memory only (a restart loses
them, which denies).

Nothing here logs a health value, an argument or a result.
"""
from __future__ import annotations

import dataclasses
import json
import re
import secrets
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from Orchestrator.wellness import catalog, gate, turn as turn_mod

CATEGORY = "wellness"                       # the ToolVault category of every wellness_* tool
# /local/tools/execute callers: the on-device bridge ("local") and the remote
# MCP gateway. Neither lists nor runs a wellness tool in v1 (spec §4.5).
DENIED_CALLERS = frozenset({"local", "mcp-gateway"})
# A tool that needs one of these categories (the rest need any one).
TOOL_NEEDS = {"wellness_sessions": ("sleep", "activity")}

# Request / task fields (see request_turn, task_turn).
GRANT_KEY = "_wellness_grant"               # set only by POST /chat for a verified request
DECLARED_KEY = "wellness_surface"           # an internal caller's own, narrowing-only, surface
CRON_JOB_KEY = "cron_job_id"
GRANT_TTL_S = 900

# The share of the destination's cap the API's JSON may use. The text the
# model reads is about half the JSON's size, except the history table (about
# the same size), which gets less, so the text fits the cap; compact JSON of
# the envelope is the fallback when it still does not.
RENDER_SHARE = {"wellness_history": 0.75}
CUSTOM_GUARD_SHARE = 0.5                    # of a custom server's window guard (spec §4.2)

PAUSED_FIX = ("Health collection is paused and nothing is stored for that period. Resume it "
              "in Settings, Health, on your phone.")
NO_DATA_FIX = "Nothing was recorded for that period."
NOT_ENOUGH_HISTORY_FIX = ("BlackBox has health data from {day} onward; ask about a later "
                          "period.")
ERROR_FIX = "The health store could not be read just now; try again in a moment."
NOT_SHARED = ("This operator's health data is not shared with this AI. If asked, say so; they "
              "can switch to {allowed} or change it in Settings, Health, on their phone.")
NOT_SHARED_NONE = ("This operator's health data is not shared with this AI. If asked, say so; "
                   "health answers are off until they allow an AI or add a local model in "
                   "Settings, Health, on their phone.")


def _now_ms() -> Optional[int]:
    """The clock the tools answer at (None = api's own; tests pin it)."""
    return None


# ------------------------------------------------------------------ offering

@dataclasses.dataclass(frozen=True)
class Offer:
    """What the current turn may read through the tools, or why nothing."""
    categories: Tuple[str, ...] = ()
    code: Optional[str] = None
    paused: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.categories)

    @property
    def fix(self) -> Optional[str]:
        return gate.FIXES.get(self.code) if self.code else None

    @property
    def not_shared(self) -> bool:
        """Enrolled, consented, on an allowed surface: only the provider is in the way."""
        return self.code == "provider_not_allowed"


def _same_provider(a, b) -> bool:
    from Orchestrator.wellness.destinations import normalize_provider
    return normalize_provider(a) == normalize_provider(b)


def best_code(results: Dict[str, gate.GateResult]) -> str:
    """The refusal to report when no category passed: the first that is not
    merely "this category is off", else category_off."""
    codes = [r.code for r in results.values() if not r.ok and r.code]
    return next((c for c in codes if c != "category_off"), codes[0] if codes else "not_allowed")


def matched_turn(ctx) -> Tuple[Optional[turn_mod.WellnessTurn], Optional[str]]:
    """(the stamped turn ``ctx`` speaks for, None) or (None, the refusal code):
    the same operator, the same surface when ctx names one, and always the
    same provider (a ctx that cannot say who reads the answer gets nothing)."""
    if ctx is None or not getattr(ctx, "operator", None):
        return None, "not_allowed"
    if getattr(ctx, "caller", None) in DENIED_CALLERS:
        return None, "surface_not_allowed"
    t = turn_mod.current()
    if t is None or ctx.operator != t.operator:
        return None, "not_allowed"
    surface = getattr(ctx, "surface", None)
    if surface is not None and surface != t.surface:
        return None, "not_allowed"
    provider = getattr(ctx, "provider", None)
    if provider is None or not _same_provider(provider, t.provider):
        return None, "not_allowed"
    return t, None


def _offer_from(results: Dict[str, gate.GateResult]) -> Offer:
    allowed = tuple(c for c in catalog.CATEGORIES if results[c].ok)
    if allowed:
        return Offer(allowed, paused=any(results[c].paused for c in allowed))
    return Offer(code=best_code(results))


def offer(ctx=None, *, resolver=None) -> Offer:
    """The categories the turn behind ``ctx`` may read, through the full gate."""
    t, code = matched_turn(ctx)
    if t is None:
        return Offer(code=code)
    return _offer_from(gate.check_all(operator=ctx.operator, turn=t, resolver=resolver))


def offered(entry: dict, ctx, memo: Optional[dict] = None) -> bool:
    """FEATURES["wellness"]'s predicate (toolvault/availability.py): is this
    wellness tool there for the turn behind ``ctx``? ``memo`` holds one
    decision per filter call."""
    memo = memo if memo is not None else {}
    decision = memo.get("wellness_offer")
    if decision is None:
        decision = memo["wellness_offer"] = offer(ctx)
    if not decision.ok:
        return False
    needs = TOOL_NEEDS.get((entry or {}).get("name"))
    return not needs or any(c in decision.categories for c in needs)


def is_wellness_tool(name) -> bool:
    """A ToolVault tool of category wellness (by the registry, alias-aware)."""
    if not isinstance(name, str) or not name:
        return False
    try:
        from Orchestrator.toolvault import registry
        spec = registry.get_tool(registry.resolve_alias(name))
    except Exception:  # noqa: BLE001 - unknown means "not one we can vouch for"
        return name.startswith("wellness_")
    if spec is None:
        return name.startswith("wellness_")
    return spec.get("category") == CATEGORY


def refusal_for(tool: str, ctx) -> Optional[object]:
    """The ToolResult refusing ``tool`` for ``ctx`` when the turn is not
    offered it (the dispatcher's check, before the executor's own), else None."""
    decision = offer(ctx)
    if decision.ok and offered({"name": tool}, ctx, {"wellness_offer": decision}):
        return None
    code = decision.code or "category_off"
    return _result(False, error_envelope(tool, code, gate.FIXES.get(code)))


# ------------------------------------------------------------------ the prompt text

# The system prompt template's placeholders ({TOOL_INSTRUCTIONS}, {PERSONA}):
# the tool instructions fill the template BEFORE the persona does
# (tasks.build_core_system_prompt), so operator-written text in them (the
# instinct prompt, a model server's alias) must never spell one.
_PLACEHOLDER = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


def inert(text: str) -> str:
    """``text`` with any {PLACEHOLDER} token turned into a harmless [PLACEHOLDER]."""
    return _PLACEHOLDER.sub(r"[\1]", text or "")


def wellness_block(operator: str) -> str:
    """The WELLNESS RULES, then this operator's instinct prompt (spec §5, §10)."""
    try:
        from Orchestrator.wellness import enroll
        instinct = enroll.instinct_prompt(operator)
    except Exception:  # noqa: BLE001 - the rules never go out without an instinct text
        instinct = catalog.DEFAULT_INSTINCT_PROMPT
    return catalog.WELLNESS_RULES + "\n\n" + inert(instinct or catalog.DEFAULT_INSTINCT_PROMPT)


def allowed_labels(operator: str) -> List[str]:
    """The AIs this operator's health data may go to, by name: the cloud
    providers they ticked (Google only with the paid key) and the model
    servers on their network, each allowed for at least one consented
    category. The phone's own model is left out: it answers on the phone."""
    from Orchestrator.wellness import destinations, enroll
    rec = enroll.get_enrollment(operator) or {}
    consented = [c for c in catalog.CATEGORIES
                 if (rec.get("categories") or {}).get(c) in catalog.CONSENTED]
    out = []
    for row in destinations.rows():
        if row.get("class") == destinations.ON_DEVICE or not row.get("id"):
            continue
        if any(destinations.allowed(row["id"], c, rec)[0] for c in consented):
            label = row.get("label") or row["id"]
            if label not in out:
                out.append(label)
    return out


def _join(words: Sequence[str]) -> str:
    words = list(words)
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " or " + words[-1]


def not_shared_line(operator: str) -> str:
    try:
        allowed = allowed_labels(operator)
    except Exception:  # noqa: BLE001 - never more than the plain sentence
        allowed = []
    return inert(NOT_SHARED.format(allowed=_join(allowed))) if allowed else NOT_SHARED_NONE


def instructions(entries: Iterable[dict], ctx) -> str:
    """What the tool instructions gain for this turn (the injector appends it):
    the rules + instinct prompt when a wellness tool is among ``entries`` (the
    resolved schemas offered), the not-shared line when the operator is
    enrolled and only the provider is not allowed, else ""."""
    if ctx is None or not getattr(ctx, "operator", None):
        return ""
    if any((e or {}).get("category") == CATEGORY for e in entries or ()):
        return wellness_block(ctx.operator)
    try:
        decision = offer(ctx)
    except Exception:  # noqa: BLE001 - no line is the safe default
        return ""
    return not_shared_line(ctx.operator) if decision.not_shared else ""


def tool_context(provider: Optional[str] = None, *, origin_device_id: Optional[str] = None,
                 caller: Optional[str] = None, operator: Optional[str] = None):
    """The ToolContext an injection site passes: the stamped turn's operator
    and surface, this request's origin device, the provider the tools are
    built for. With nothing stamped it is the "system" context of old (no
    wellness tool, the full operator roster for the x-source enums)."""
    from Orchestrator.toolvault.context import ToolContext
    t = turn_mod.current()
    op = operator or (t.operator if t is not None else None)
    if not op:
        return ToolContext(origin_device_id=origin_device_id, caller=caller, provider=provider)
    return ToolContext(operator=op, origin_device_id=origin_device_id, caller=caller,
                       surface=t.surface if t is not None else None, provider=provider)


# ------------------------------------------------------------------ x-source resolvers

def metric_names(ctx) -> List[str]:
    """The metrics the turn may ask a series of (empty when nothing is offered)."""
    cats = offer(ctx).categories
    return [m for m, spec in catalog.METRICS.items()
            if spec.category in cats and m != "sleep_stages"]


def category_names(ctx) -> List[str]:
    return list(offer(ctx).categories)


# ------------------------------------------------------------------ stamping

async def origin_from_request(request) -> Optional[object]:
    """The verified enrollment (enroll.Signed) behind a request's
    X-BlackBox-Origin header, or None: no header, a bad or replayed proof, a
    pending replacement, or the kill switch off. Reads the exact body bytes
    the proof covers. Never logs."""
    from Orchestrator.wellness import origin_proof
    headers = getattr(request, "headers", None)
    header = headers.get(origin_proof.HEADER) if headers is not None else None
    if not header:
        return None
    try:
        from Orchestrator import config
        if not config.wellness_enabled():
            return None
        body = await request.body()
        return verify_origin(header, request.method, request.url.path, body)
    except Exception:  # noqa: BLE001 - an unreadable proof is no proof
        return None


def verify_origin(header, method: str, path: str, body: bytes) -> Optional[object]:
    from Orchestrator.wellness import enroll
    try:
        signed = enroll.verify_signed_request(header, method, path, body or b"")
    except enroll.EnrollError:
        return None
    return signed if signed.which == "active" else None


def _declared(value) -> Optional[str]:
    return value if isinstance(value, str) and value in turn_mod.DECLARABLE_SURFACES else None


# Where on the enrolled phone a CREDENTIALED turn says it comes from: the
# Portal inside the app signs through the app's bridge with the phone's own
# key, so its proof is the phone's; the label only tells the disclosure log
# ("Who saw my health data") which screen asked. It narrows a credentialed
# android turn and nothing else: without a valid proof it is ignored, and it
# never adds a credential.
CREDENTIALED_LABELS = frozenset({turn_mod.SURFACE_PORTAL_WEBVIEW})


def request_turn(*, operator: str, provider: Optional[str], model: Optional[str],
                 signed=None, origin_device_id: Optional[str] = None,
                 declared: Optional[str] = None, surface: Optional[str] = None,
                 cron_job_id: Optional[str] = None) -> turn_mod.WellnessTurn:
    """The WellnessTurn for one chat request, with a fresh turn key.

    The credential is the verified origin proof of THIS operator's enrolled
    phone and nothing else. A ``declared`` surface (cron, mcp, ...) can only
    narrow; "portal_webview" (CREDENTIALED_LABELS) relabels a credentialed
    phone turn and is ignored on any other. A Computer Use turn is a CU
    agent's (never). ``surface`` names the entry point when it is not a chat
    client (on_device)."""
    credential = signed is not None and getattr(signed, "operator", None) == operator
    surface_id = getattr(signed, "enrollment_id", None) if credential else None
    if provider == "computer-use":
        surface, credential, surface_id = "cu_agent", False, None
    elif _declared(declared):
        surface, credential, surface_id = _declared(declared), False, None
    elif surface is None:
        if credential:
            surface = declared if isinstance(declared, str) and declared in CREDENTIALED_LABELS \
                else turn_mod.SURFACE_ANDROID
        else:
            surface = turn_mod.SURFACE_ANDROID if origin_device_id else turn_mod.SURFACE_PORTAL
    return turn_mod.WellnessTurn(operator=operator, surface=surface, credential_ok=credential,
                                 surface_id=surface_id, provider=provider, model=model,
                                 turn_key=turn_mod.new_turn_key(),
                                 cron_job_id=str(cron_job_id) if cron_job_id else None)


_GRANTS: Dict[str, Tuple[float, str, str]] = {}
_GRANTS_LOCK = threading.Lock()


def issue_grant(signed, operator: str, *, now: Optional[float] = None) -> Optional[str]:
    """A one-use token carrying a verified /chat request to its worker (in
    memory, GRANT_TTL_S). None when the proof is not this operator's."""
    if signed is None or getattr(signed, "operator", None) != operator:
        return None
    now = time.time() if now is None else now
    token = "wg_" + secrets.token_hex(16)
    with _GRANTS_LOCK:
        for old in [k for k, v in _GRANTS.items() if v[0] < now]:
            del _GRANTS[old]
        _GRANTS[token] = (now + GRANT_TTL_S, operator, signed.enrollment_id)
    return token


def redeem_grant(token, operator: str, *, now: Optional[float] = None) -> Optional[str]:
    """The enrollment id a grant was issued for, once; None for anything else."""
    if not isinstance(token, str) or not token:
        return None
    now = time.time() if now is None else now
    with _GRANTS_LOCK:
        grant = _GRANTS.pop(token, None)
    if grant is None or grant[0] < now or grant[1] != operator:
        return None
    return grant[2]


def task_turn(inp: dict, operator: str, provider: Optional[str],
              model: Optional[str]) -> turn_mod.WellnessTurn:
    """The WellnessTurn for a queued /chat turn (tasks.process_chat_task): an
    inbound SMS or a peer turn is "sms" (never), a cron or MCP turn is what it
    declares, a turn POST /chat verified is the enrolled phone's (the Portal
    inside the app's when its body says "portal_webview"), anything else is
    an uncredentialed Portal or app turn."""
    inp = inp if isinstance(inp, dict) else {}
    grant = inp.get(GRANT_KEY)
    if inp.get("sms_mode") or inp.get("sms_peer"):
        return request_turn(operator=operator, provider=provider, model=model, declared="sms")
    declared = _declared(inp.get(DECLARED_KEY))
    if declared:
        return request_turn(operator=operator, provider=provider, model=model, declared=declared,
                            cron_job_id=inp.get(CRON_JOB_KEY) if declared == "cron" else None)
    enrollment_id = redeem_grant(grant, operator)
    signed = _Granted(operator, enrollment_id) if enrollment_id else None
    return request_turn(operator=operator, provider=provider, model=model, signed=signed,
                        origin_device_id=inp.get("origin_device_id"),
                        declared=inp.get(DECLARED_KEY))


@dataclasses.dataclass(frozen=True)
class _Granted:
    operator: str
    enrollment_id: str


# ------------------------------------------------------------------ executing

def budget_tokens(t: Optional[turn_mod.WellnessTurn]) -> int:
    """The per-call payload cap for the turn's destination (api.CAP_TOKENS):
    cloud and local servers 8,000, on-device 1,500; a custom server's answer
    also stays inside half of its window guard (spec §4.2, §10)."""
    from Orchestrator.wellness import api, destinations
    provider = t.provider if t is not None else None
    model = t.model if t is not None else None
    cls = destinations.classify(provider, model) or destinations.CLOUD
    cap = api.CAP_TOKENS.get(cls, api.CAP_TOKENS["cloud"])
    p = destinations.normalize_provider(provider) or ""
    if p == "custom" or p.startswith("custom:"):
        try:
            from Orchestrator.onboarding import custom_servers
            srv, bare = custom_servers.resolve_model(model or "")
            if srv is not None:
                cap = min(cap, int(custom_servers.window_guard_tokens(srv, bare)
                                   * CUSTOM_GUARD_SHARE))
        except Exception:  # noqa: BLE001 - the class cap stands
            pass
    return cap


def error_envelope(tool: str, code: str, fix: Optional[str] = None, **extra) -> dict:
    env = {"ok": False, "tool": tool, "error": code, "fix": fix}
    env.update({k: v for k, v in extra.items() if v not in (None, [], {})})
    return env


def requested(categories) -> Tuple[Optional[List[str]], List[str]]:
    """(the categories a call asked for in catalog order, or None for all;
    the names it sent that are not categories). A model may send one name
    as a plain string; an empty list means all."""
    if categories is None or categories == "" or categories == []:
        return None, []
    if isinstance(categories, str):
        categories = [categories]
    if not isinstance(categories, (list, tuple)):
        return None, [str(categories)]
    names = [str(x).strip().lower() for x in categories]
    unknown = [n for n in names if n not in catalog.CATEGORIES]
    return [c for c in catalog.CATEGORIES if c in names], unknown


def invalid(tool: str, what: str) -> object:
    """An invalid-argument refusal (nothing was read)."""
    return _result(False, error_envelope(tool, "invalid_argument", what))


CATEGORIES_HINT = "categories are " + ", ".join(catalog.CATEGORIES)


Call = Callable[[str, List[str], object, int, Optional[int]], dict]


def run(tool: str, ctx, wanted: Optional[Sequence[str]], call: Call, *,
        explicit: bool = False) -> object:
    """One wellness tool call: the gate for each wanted category (all when
    None), the api answer for the allowed ones, its rendering.

    ``call(operator, categories, delivery, budget_tokens, now_ms)`` returns the
    envelope. ``explicit``: the caller named the categories, so the ones
    refused for being off are reported too (otherwise only refusals a person
    can act on, a local-only category on a cloud AI, are)."""
    try:
        t, code = matched_turn(ctx)
        if t is None:
            return _result(False, error_envelope(tool, code, gate.FIXES.get(code)))
        # The gate once for every category (gate.check per category, with the
        # switch, the enrollment and the destination read once).
        every = gate.check_all(operator=ctx.operator, turn=t)
        decision = _offer_from(every)
        if not decision.ok:
            code = decision.code or "not_allowed"
            return _result(False, error_envelope(tool, code, gate.FIXES.get(code)))
        cats = list(catalog.CATEGORIES) if wanted is None else list(wanted)
        if not cats:
            return _result(False, error_envelope(tool, "category_off", gate.FIXES["category_off"]))
        results = {c: every[c] for c in cats}
        allowed = [c for c in cats if results[c].ok]
        if not allowed:
            code = best_code(results)
            return _result(False, error_envelope(tool, code, gate.FIXES.get(code)))
        from Orchestrator.wellness import api
        delivery = api.Delivery(t.turn_key, t.surface, t.provider, t.model)
        cap = budget_tokens(t)
        env = call(ctx.operator, allowed, delivery, int(cap * RENDER_SHARE.get(tool, 1.0)),
                   _now_ms())
        withheld = [{"category": c, "error": r.code, "fix": r.fix}
                    for c, r in results.items()
                    if not r.ok and (explicit or r.code != "category_off")]
        return finish(tool, env, allowed, withheld, cap, ctx)
    except Exception as e:  # noqa: BLE001 - a tool never raises, and never says why in values
        print(f"[WELLNESS] {tool} failed ({type(e).__name__})")
        return _result(False, error_envelope(tool, "wellness_error", ERROR_FIX))


def _earliest(operator: str) -> Optional[str]:
    try:
        from Orchestrator.wellness import enroll
        rec = enroll.get_enrollment(operator)
        return enroll.earliest_available(rec) if rec else None
    except Exception:  # noqa: BLE001
        return None


def _period_end(env: dict) -> Optional[str]:
    end = env.get("end") or (env.get("period") or {}).get("end")
    if not end and env.get("points"):
        end = env["points"][-1].get("t")
    return str(end)[:10] if end else None


def _empty(env: dict) -> bool:
    """Nothing recorded: the api's no_data flag, or a history table whose
    metric cells are all null (the wear columns say nothing on their own)."""
    if env.get("no_data"):
        return True
    if env.get("tool") == "wellness_history" and env.get("columns"):
        skip = {0} | {i for i, c in enumerate(env["columns"]) if c in ("night_worn", "day_wear_h")}
        return not any(v is not None for row in env.get("rows") or []
                       for i, v in enumerate(row) if i not in skip)
    return False


def finish(tool: str, env: dict, categories: Sequence[str], withheld: List[dict], cap: int,
           ctx=None) -> object:
    """The ToolResult for an api envelope: empty answers become the errors
    the spec names (paused / not_enough_history / no_data) with what is known
    about why; a whole answer is rendered for the model."""
    if not isinstance(env, dict):
        return _result(False, error_envelope(tool, "wellness_error", ERROR_FIX))
    if env.get("ok") and categories:
        env.setdefault("categories", [c for c in catalog.CATEGORIES if c in categories])
    if withheld:
        env["withheld"] = withheld
    if env.get("ok") and _empty(env):
        keep = {k: env.get(k) for k in ("ref", "as_of", "stale", "troubles", "gaps", "withheld",
                                        "period", "window", "start", "end")}
        earliest = _earliest(getattr(ctx, "operator", None)) if ctx is not None else None
        end = _period_end(env)
        if env.get("paused"):
            err = error_envelope(tool, "paused", PAUSED_FIX, **keep)
        elif earliest and end and end < earliest:
            err = error_envelope(tool, "not_enough_history",
                                 NOT_ENOUGH_HISTORY_FIX.format(day=earliest), **keep)
        else:
            err = error_envelope(tool, "no_data", NO_DATA_FIX, **keep)
        err["categories"] = env.get("categories")
        return _result(False, err)
    if not env.get("ok"):
        return _result(False, env)
    text = render(env)
    from Orchestrator.wellness import api
    if (len(text) + api.CHARS_PER_TOKEN - 1) // api.CHARS_PER_TOKEN > cap:
        text = json.dumps(env, separators=(",", ":"), ensure_ascii=False)
    return _result(True, env, text)


def _result(ok: bool, env: dict, text: Optional[str] = None):
    from Orchestrator.toolvault.context import ToolResult
    return ToolResult(success=ok, result=text if text is not None else render(env), data=env)


# ------------------------------------------------------------------ the Signal frame

MAX_TURN_REFS = 8                           # refs a non-stream result names (used_fields)


def used_frame(env: dict, t: Optional[turn_mod.WellnessTurn]) -> dict:
    """The data of the UI-only wellness_used frame for one delivered answer
    `env` on turn `t`: {ref, categories, as_of, provider, provider_label}
    ("Health data used · sleep, heart · as of 07:10 · shared with
    Anthropic"). No value: a ref, category names, a time, a destination.
    The stream sends it as an SSE event (signal_event), the non-stream
    worker puts it in result_data (used_fields): one builder, so the two
    clients render the same footer."""
    provider = t.provider if t is not None else None
    label = None
    try:
        from Orchestrator.wellness import destinations
        dest = destinations.destination(provider, t.model if t is not None else None)
        provider, label = dest.id or provider, dest.label
    except Exception:  # noqa: BLE001 - the frame is decoration
        pass
    return {"ref": env.get("ref"),
            "categories": [c for c in env.get("categories") or [] if isinstance(c, str)],
            "as_of": env.get("as_of"), "provider": provider, "provider_label": label}


def signal_event(tool_result) -> Optional[dict]:
    """The UI-only {"type": "wellness_used"} event for a wellness tool that
    answered on a stream: ref, categories, as_of and who it was shared with.
    It never reaches a prompt or the ledger (the system_activity precedent)."""
    env = getattr(tool_result, "data", None)
    if not getattr(tool_result, "success", False) or not isinstance(env, dict) \
            or not env.get("ok") or not env.get("ref"):
        return None
    return {"type": "wellness_used", "data": used_frame(env, turn_mod.current())}


def _delivered_env(row: dict) -> dict:
    """A delivered row as the answer the tool returned: its stored payload
    (recorded before access.finish names the categories, unless the api did)
    with the categories the row recorded, in catalog order."""
    env = dict(row.get("payload") or {}) if isinstance(row.get("payload"), dict) else {}
    env["ref"] = row.get("ref")
    cats = env.get("categories")
    if not (isinstance(cats, list) and all(isinstance(c, str) for c in cats)):
        recorded = set(row.get("categories") or [])
        env["categories"] = [c for c in catalog.CATEGORIES if c in recorded]
    return env


def used_fields(rows: Sequence[dict], t: Optional[turn_mod.WellnessTurn]) -> dict:
    """What a non-stream turn's result_data gains when the turn delivered
    (`rows`: its delivered rows, oldest first, store.deliveries_for_turn):
    `wellness_used`, the frame of its newest delivery (used_frame, the same
    object the stream's wellness_used event carries), `wellness_refs`, every
    ref of the turn (oldest first, at most MAX_TURN_REFS) and `wellness_ref`,
    the first. {} for a turn nothing was delivered on, so its result_data is
    byte-identical to before. Never raises (the turn is already recorded)."""
    try:
        rows = [r for r in rows or () if isinstance(r, dict) and isinstance(r.get("ref"), str)
                and r["ref"]]
        if not rows:
            return {}
        refs = list(dict.fromkeys(r["ref"] for r in rows))[:MAX_TURN_REFS]
        return {"wellness_used": used_frame(_delivered_env(rows[-1]), t),
                "wellness_refs": refs, "wellness_ref": refs[0]}
    except Exception as e:  # noqa: BLE001 - the footer is decoration; the class only
        print(f"[WELLNESS] used fields failed ({type(e).__name__})")
        return {}


# ------------------------------------------------------------------ rendering

def _disp(metric: str, value) -> str:
    if value is None:
        return "not recorded"
    if metric in catalog.METRICS:
        return catalog.display(metric, value) or "not recorded"
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def _usual(metric: str, usual, reason=None) -> Optional[str]:
    if isinstance(usual, dict) and usual.get("low") is not None and usual.get("high") is not None:
        text = f"usual {_disp(metric, usual['low'])} to {_disp(metric, usual['high'])}"
        if usual.get("median") is not None:
            text += f" (median {_disp(metric, usual['median'])}"
            text += f", {usual['n']} days)" if usual.get("n") else ")"
        return text
    return f"usual range: {reason}" if reason else None


def _coverage(cov) -> Optional[str]:
    if isinstance(cov, dict):
        label, pct = cov.get("label"), cov.get("pct")
        if label and pct is not None:
            return f"coverage {label} ({pct}%)"
        return f"coverage {label}" if label else (f"coverage {pct}%" if pct is not None else None)
    return f"coverage {cov}" if cov else None


def _vs(vs) -> Optional[str]:
    if isinstance(vs, dict):
        label, delta = vs.get("label"), vs.get("delta_display")
        return f"{label}, {delta}" if label and delta else label
    return vs or None


def _metric_line(m: dict) -> str:
    metric = m.get("metric") or "?"
    head = f"- {metric}"
    if m.get("label"):
        head += f" ({m['label']})"
    parts = [f"{head}: {m.get('display') or 'not recorded'}"]
    if m.get("spoken") and m["spoken"] != "not recorded":
        parts.append(f'spoken "{m["spoken"]}"')
    if m.get("total_display"):
        parts.append(f"total {m['total_display']}")
    if m.get("range_display"):
        parts.append(f"range {m['range_display']}")
    for key in ("lowest", "highest"):
        if isinstance(m.get(key), dict):
            parts.append(f"{key} {m[key].get('display')} on {m[key].get('day')}")
    if m.get("bedtime") or m.get("wake_time"):
        parts.append(f"night of {m.get('night_of')}: asleep {m.get('bedtime') or '?'}, "
                     f"woke {m.get('wake_time') or '?'}")
    if m.get("readings"):
        parts.append(f"{m['readings']} reading{'s' if m['readings'] != 1 else ''}")
    for text in (_usual(metric, m.get("usual"), m.get("usual_reason")), _vs(m.get("vs_usual")),
                 _coverage(m.get("coverage"))):
        if text:
            parts.append(text)
    if m.get("method"):
        parts.append(f"method: {m['method']}")
    if m.get("flags"):
        parts.append("flags: " + ", ".join(str(f) for f in m["flags"]))
    return " | ".join(parts)


def _session_line(s: dict) -> str:
    kind, kind_type = s.get("kind"), s.get("type")
    parts = [f"- {s.get('session_ref')}",
             f"{kind} {kind_type}" if kind_type and kind_type != kind else str(kind),
             f"{s.get('start')} to {s.get('end')}", s.get("duration") or ""]
    if s.get("distance"):
        parts.append(s["distance"])
    if s.get("avg_hr_display") or s.get("avg_hr") is not None:
        hr = s.get("avg_hr_display") or _disp("heart_rate", s.get("avg_hr"))
        if s.get("max_hr") is not None:
            hr += f", max {_disp('heart_rate', s['max_hr'])}"
        parts.append(f"avg HR {hr}")
    if s.get("hr_coverage_pct") is not None:
        parts.append(f"HR coverage {s['hr_coverage_pct']}%")
    if s.get("calories"):
        parts.append(s["calories"])
    if isinstance(s.get("stages_min"), dict):
        parts.append("stages " + ", ".join(f"{k} {catalog.duration_display(v)}"
                                           for k, v in s["stages_min"].items() if v is not None))
    if s.get("source"):
        parts.append(f"source {s['source']}")
    return " | ".join(p for p in parts if p)


def _summary_body(env: dict) -> List[str]:
    lines = [_metric_line(m) for m in env.get("metrics") or []]
    if env.get("sessions"):
        lines.append("Notable workouts:")
        lines += [_session_line(s) for s in env["sessions"]]
    return lines or ["(no metrics)"]


def _series_body(env: dict) -> List[str]:
    metric = env.get("metric") or "?"
    gran = env.get("granularity_used")
    per = "points merged to fit" if gran == "coarsened" else f"one point per {gran}"
    lines = [f"{metric} ({env.get('unit')}), {env.get('window')}, {per} (stat {env.get('stat')})"]
    usual = _usual(metric, env.get("baseline"))
    if usual:
        lines.append(usual)
    lines.append("time | value | coverage")
    for p in env.get("points") or []:
        lines.append(f"{p.get('t')} | {p.get('display') or 'not recorded'} | "
                     f"{p.get('coverage_pct')}%")
    return lines


def _sessions_body(env: dict) -> List[str]:
    shown = len(env.get("sessions") or [])
    lines = [f"{env.get('kind')} sessions, {env.get('window')}: {env.get('total_in_window')} "
             f"found, newest first, {shown} shown"]
    lines += [_session_line(s) for s in env.get("sessions") or []]
    return lines


# A history cell is the display string without its unit when the unit never
# changes (it is named once in the column header); durations and distances
# keep their own ("7 h 05 min", "4.2 km") so nothing needs converting.
_CELL_SUFFIX = {"bpm": " bpm", "ms": " ms", "percent": " %", "breaths": " breaths/min",
                "steps": " steps", "kcal": " kcal", "mmhg": " mmHg"}


def _cell_suffix(column: str) -> Optional[str]:
    spec = catalog.METRICS.get(column)
    return _CELL_SUFFIX.get(spec.fmt) if spec else None


def _cell(column: str, value) -> str:
    if value is None:
        return "-"
    if column in ("sleep_onset", "sleep_wake", "night_worn"):
        return str(value)
    if column == "day_wear_h":
        return f"{value:g}" if isinstance(value, (int, float)) else str(value)
    text = _disp(column, value)
    suffix = _cell_suffix(column)
    return text[:-len(suffix)] if suffix and text.endswith(suffix) else text


def _column_head(column: str) -> str:
    suffix = _cell_suffix(column)
    if suffix:
        return f"{column} ({suffix.strip()})"
    return f"{column} (h)" if column == "day_wear_h" else column


def _history_body(env: dict) -> List[str]:
    cols = env.get("columns") or []
    lines = [f"one row per {env.get('resolution')}, {env.get('start')} to {env.get('end')}; "
             f"{env.get('legend')}"]
    usual = []
    for metric, base in (env.get("baselines") or {}).items():
        text = _usual(metric, base)
        if text:
            usual.append(f"{metric} {text[len('usual '):]}")
    if usual:
        lines.append("usual ranges: " + "; ".join(usual))
    lines.append(" | ".join([cols[0]] + [_column_head(c) for c in cols[1:]]) if cols else "")
    for row in env.get("rows") or []:
        lines.append(" | ".join([str(row[0])] + [_cell(c, v) for c, v in zip(cols[1:], row[1:])]))
    return lines


def _sources_body(env: dict) -> List[str]:
    phone = env.get("phone") or {}
    lines = [f"phone {phone.get('label') or 'unknown'}, last push {phone.get('last_push') or 'never'}"
             f", sync {env.get('sync_mode')}"]
    hc = (env.get("platforms") or {}).get("health_connect") or {}
    lines.append(f"Health Connect: {hc.get('status') or 'status unknown'}, background access "
                 f"{'yes' if hc.get('background_access') else 'no'}, past data access "
                 f"{'yes' if hc.get('past_data_access') else 'no'}, earliest available "
                 f"{hc.get('earliest_available') or 'unknown'}")
    sh = (env.get("platforms") or {}).get("samsung_health")
    if sh:
        lines.append(f"Samsung Health: {json.dumps(sh, separators=(',', ':'))}")
    for c in env.get("categories") or []:
        if isinstance(c, dict):
            lines.append(f"- {c.get('category')}: {c.get('consent')}"
                         f"{', local only' if c.get('local_only') else ''}"
                         + (f", {c['days_with_data_28']} of the last 28 days with data"
                            if c.get("days_with_data_28") is not None else ""))
    for o in env.get("origins") or []:
        lines.append(f"- {o.get('metric')} from {o.get('app')} ({o.get('device_type')}"
                     f"{', ' + o['model'] if o.get('model') else ''}), latest {o.get('latest')}, "
                     f"{o.get('count_14d')} in 14 days")
    wear = env.get("wear") or {}
    lines.append(f"wear: heart-rate mode {wear.get('hr_mode')}, worn {wear.get('nights_worn_7')} "
                 f"of the last 7 nights")
    lines.append("allowed AIs: " + (", ".join(env.get("providers_allowed") or []) or
                                    "local models only"))
    if env.get("samsung_only"):
        lines.append(env["samsung_only"])
    return lines


_BODIES = {"wellness_summary": _summary_body, "wellness_series": _series_body,
           "wellness_sessions": _sessions_body, "wellness_history": _history_body,
           "wellness_sources": _sources_body}


def _header(env: dict) -> str:
    parts = [f"{env.get('tool')} (ref {env.get('ref')})"]
    period = env.get("period")
    if isinstance(period, dict):
        parts.append(f"{period.get('label')}: {period.get('start')} to {period.get('end')} "
                     f"({period.get('tz')})")
    parts.append(f"as of {env.get('as_of') or 'never synced'}")
    if env.get("stale"):
        parts.append("STALE: say \"as of\" when you use it")
    if env.get("paused"):
        parts.append("collection paused: stored numbers only")
    if env.get("coarsened"):
        parts.append("shown coarser to fit")
    return " | ".join(parts)


def _tail(env: dict) -> List[str]:
    lines = []
    gaps = env.get("gaps") or []
    if gaps:
        lines.append("Not recorded: " + "; ".join(
            f"{g.get('metric')}{' on ' + g['day'] if g.get('day') else ''}: {g.get('reason')}"
            for g in gaps))
    elif env.get("gaps_count"):
        lines.append(f"Not recorded: {env['gaps_count']} metric(s) (details left out to fit)")
    if env.get("metrics_omitted"):
        lines.append("Left out to fit (ask about one of them): " +
                     ", ".join(env["metrics_omitted"]))
    for w in env.get("withheld") or []:
        lines.append(f"Not available here: {w.get('category')} ({w.get('error')}): {w.get('fix')}")
    for t in env.get("troubles") or []:
        lines.append(f"Trouble: {t.get('title')}. {t.get('fix')}")
    srcs = [s for s in env.get("sources") or [] if isinstance(s, dict)]
    if srcs:
        lines.append("Sources: " + "; ".join(
            f"{s.get('label')}" + (f" ({int(round(s['share'] * 100))}% of heart-rate data)"
                                   if s.get("share") is not None else "") for s in srcs))
    if env.get("notes"):
        lines.append("Notes: " + " ".join(env["notes"]))
    if env.get("over_budget"):
        lines.append("Still large after coarsening: ask a narrower question for detail.")
    return lines


def render(env: dict) -> str:
    """The text the model reads for an envelope: every number through its
    display (or spoken) string, the usual range, coverage, gaps with their
    reason, troubles, notes, as of. An error is its code and fix."""
    if not isinstance(env, dict):
        return "wellness: no answer"
    if not env.get("ok"):
        lines = [f"{env.get('tool')}: {env.get('error')}. {env.get('fix') or ''}".rstrip()]
        if env.get("as_of"):
            lines.append(f"as of {env['as_of']}{' (stale)' if env.get('stale') else ''}")
        lines += _tail({k: env.get(k) for k in ("gaps", "withheld", "troubles")})
        return "\n".join(lines)
    body = _BODIES.get(env.get("tool"))
    lines = [_header(env)]
    lines += body(env) if body else [json.dumps(env, separators=(",", ":"), ensure_ascii=False)]
    lines += _tail(env)
    return "\n".join(line for line in lines if line)
