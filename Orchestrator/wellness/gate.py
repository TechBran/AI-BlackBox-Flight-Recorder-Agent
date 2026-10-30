"""The gate: who gets wellness data on a turn (spec §4.1). Default-deny.

The six checks, in order; the first that fails is the answer:

  1. [wellness] enabled in config.ini, read fresh (the box-wide kill switch)  wellness_disabled
  2. the turn is stamped, its operator is a live human (not blank, system or
     the Flight Recorder) and equals the operator the tool runs for          not_allowed
  3. the operator's enrollment is active, paused, stale or needs_attention
     (the last three answer from stored data and say so)                     not_enrolled
  4. the category is consented (ask or proactive)                            category_off
  5. the surface is not on the never-list, and the turn holds a credential
     or the box is in household mode (cron, voice and on-device turns always
     need their own credential: turn.CREDENTIAL_ONLY_SURFACES)              surface_not_allowed
  6. the provider may receive the category (destinations.allowed)            provider_not_allowed

evaluate() is pure over its inputs (the matrix test drives it directly);
check() reads the switch, the roster and the enrollment fresh and calls it;
check_all() does the same for every category with one read of each.
Offering (Orchestrator/wellness/access.py) asks check_all(); every executor
call asks check(), and the executor's call is the authoritative one.
"""
from __future__ import annotations

import dataclasses
from typing import Callable, Dict, Iterable, Optional, Tuple

from Orchestrator.wellness import catalog, turn as turn_mod

GATE_STATES = frozenset({"active", "paused", "stale", "needs_attention"})

FIXES = {
    "wellness_disabled": "Health is turned off on this BlackBox.",
    "not_allowed": "Health data is available only to its own operator.",
    "not_enrolled": "Health isn't set up. Set it up in Settings, Health, on your phone.",
    "category_off": "Turn it on in Settings, Health, on your phone.",
    "surface_not_allowed": ("Ask from the BlackBox app on your phone, or approve this "
                            "browser from your phone."),
    "provider_not_allowed": ("This AI isn't allowed to see your health data. Switch to an "
                             "allowed AI, or change it in Settings, Health, on your phone."),
}
CODES = tuple(FIXES)


@dataclasses.dataclass(frozen=True)
class GateResult:
    ok: bool
    code: Optional[str] = None
    reason: Optional[str] = None
    paused: bool = False

    @property
    def fix(self) -> Optional[str]:
        return FIXES.get(self.code) if self.code else None

    def error(self) -> dict:
        """The tool envelope for a refusal."""
        return {"ok": False, "error": self.code, "fix": self.fix}


ALLOW = GateResult(True)


def _deny(code: str, reason: Optional[str] = None) -> GateResult:
    return GateResult(False, code, reason)


def live_humans(roster: Iterable[str], reserved: Iterable[str]) -> list:
    blocked = {r.lower() for r in reserved} | {"system", ""}
    return [op for op in roster if isinstance(op, str) and op.strip()
            and op.strip().lower() not in blocked]


def household(surfaces_mode: Optional[str], humans: list) -> bool:
    if surfaces_mode == "household":
        return True
    if surfaces_mode == "credentialed":
        return False
    return len(humans) == 1      # auto: one human on the box means everyone is them


def evaluate(*, enabled: bool, roster: Iterable[str], reserved: Iterable[str],
             operator: Optional[str], turn: Optional[turn_mod.WellnessTurn],
             enrollment: Optional[dict], category: str,
             destination_ok: Callable[[], Tuple[bool, Optional[str]]]) -> GateResult:
    if not enabled:
        return _deny("wellness_disabled")
    humans = live_humans(roster, reserved)
    if turn is None or not isinstance(turn, turn_mod.WellnessTurn):
        return _deny("not_allowed", "unstamped")
    if not operator or turn.operator != operator or operator not in humans:
        return _deny("not_allowed", "operator")
    state = (enrollment or {}).get("state")
    if state not in GATE_STATES:
        return _deny("not_enrolled", state or "none")
    if category not in catalog.CATEGORIES or \
            (enrollment.get("categories") or {}).get(category) not in catalog.CONSENTED:
        return _deny("category_off", category)
    if turn.surface in turn_mod.NEVER_SURFACES or turn.surface not in turn_mod.ALLOWED_SURFACES:
        return _deny("surface_not_allowed", turn.surface or "none")
    if not turn.credential_ok and (turn.surface in turn_mod.CREDENTIAL_ONLY_SURFACES or
                                   not household(enrollment.get("surfaces_mode"), humans)):
        return _deny("surface_not_allowed", "no_credential")
    ok, why = destination_ok()
    if not ok:
        return _deny("provider_not_allowed", why)
    return GateResult(True, paused=state == "paused")


def _roster():
    from Orchestrator import config
    return list(config.USERS_LIST), set(config.RESERVED_OPERATORS)


def check(category: str, *, operator: Optional[str] = None,
          turn: Optional[turn_mod.WellnessTurn] = None, resolver=None) -> GateResult:
    """The gate for `category` on the current turn, everything read fresh."""
    from Orchestrator import config
    from Orchestrator.wellness import destinations, enroll
    turn = turn if turn is not None else turn_mod.current()
    op = operator if operator is not None else (turn.operator if turn else None)
    roster, reserved = _roster()
    enrollment = enroll.get_enrollment(op) if op else None
    if enrollment is not None:
        enrollment = dict(enrollment, state=enroll.effective_state(enrollment))
    return evaluate(
        enabled=config.wellness_enabled(), roster=roster, reserved=reserved, operator=op,
        turn=turn, enrollment=enrollment, category=category,
        destination_ok=lambda: destinations.allowed(turn.provider, category, enrollment,
                                                    model=turn.model, resolver=resolver))


def check_all(*, operator: Optional[str] = None, turn: Optional[turn_mod.WellnessTurn] = None,
              resolver=None) -> Dict[str, GateResult]:
    """check() for every category, reading the switch, the roster, the
    enrollment and the destination once (offering asks this per injection)."""
    from Orchestrator import config
    from Orchestrator.wellness import destinations, enroll
    turn = turn if turn is not None else turn_mod.current()
    op = operator if operator is not None else (turn.operator if turn else None)
    roster, reserved = _roster()
    enrollment = enroll.get_enrollment(op) if op else None
    if enrollment is not None:
        enrollment = dict(enrollment, state=enroll.effective_state(enrollment))
    enabled = config.wellness_enabled()
    resolved: list = []

    def destination_ok(category: str):
        def ok():
            if not resolved:
                resolved.append(destinations.destination(turn.provider, turn.model, resolver))
            return destinations.allowed_to(resolved[0], category, enrollment)
        return ok

    return {c: evaluate(enabled=enabled, roster=roster, reserved=reserved, operator=op,
                        turn=turn, enrollment=enrollment, category=c,
                        destination_ok=destination_ok(c))
            for c in catalog.CATEGORIES}


def allowed_categories(*, operator: Optional[str] = None,
                       turn: Optional[turn_mod.WellnessTurn] = None, resolver=None) -> list:
    """The categories the current turn may read (each through the full gate)."""
    results = check_all(operator=operator, turn=turn, resolver=resolver)
    return [c for c in catalog.CATEGORIES if results[c].ok]
