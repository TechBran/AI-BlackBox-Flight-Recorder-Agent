"""Pydantic v2 models for every body the phone (or a settings surface) sends.

Structure is strict and value checks are lenient, on purpose:

  * A malformed batch (unknown top-level key, wrong type, a string over its
    cap, more rows than a batch may carry) is a 422: the phone quarantines it.
  * One implausible VALUE (a heart rate of 400, a 30-hour "night") is never a
    422. ingest.py stores it FLAGGED (excluded from every rollup, counted in
    the ingest log) and a metric this build does not know is dropped and
    counted, so a newer app never wedges an older box.

Strings from the phone are stripped of control characters (the
Orchestrator/location.py approach) and capped: labels are truncated,
identifiers over their cap are refused (two long ids must never collapse into
one). The bbx.wellness.ingest/1 batch forbids unknown keys at every level
except the free-form `attrs` objects, whose known numeric keys are kept and
the rest dropped. The capability report and the enroll body ignore unknown
keys (they carry counts and flags, and a newer app may add fields); the
settings body forbids them, so a typo can never silently skip a narrowing.

The batch, exactly (times are Unix milliseconds, offsets seconds east of UTC):

    {"schema": "bbx.wellness.ingest/1", "batch_id": "<1-64 [A-Za-z0-9_.:-]>",
     "enrollment_id": "en_<8 hex>", "epoch": 2,
     "window": {"type": "changes|backfill|reconcile", "from": "<iso>", "to": "<iso>"},
     "origins": [{"key": "o1", "package": "com.sec.android.app.shealth", "label": null,
                  "device_type": "WATCH", "manufacturer": null, "model": null,
                  "recording_method": null}],
     "buckets": [{"metric": "heart_rate", "grain": "5min", "origin": "o1",
                  "replace_day": "2026-11-03", "offset_s": -18000,
                  "rows": [[t, avg, min, max, n], ...]},           # heart_rate
                 {"metric": "steps", "grain": "1h", "origin": "hc_merged",
                  "replace_day": "2026-11-03", "offset_s": -18000,
                  "rows": [[t, sum], ...]}],                       # steps, distance (m),
                                                                   # active/total_calories (kcal)
     "sessions": [{"kind": "sleep|exercise", "hc_id": "...", "origin": "o1",
                   "start": t, "end": t, "offset_s": -18000,
                   "stages": [[start, end, code], ...],            # sleep only
                   "exercise_type": 56,                            # exercise only (HC code or word)
                   "attrs": {"distance_m": 5012.0, "active_kcal": 410.0, "total_kcal": null,
                             "avg_hr": 148.0, "max_hr": 171.0, "steps": null},
                   "last_modified": t}],
     "readings": [{"metric": "resting_hr|hrv|spo2|respiratory_rate|blood_pressure",
                   "hc_id": "...", "origin": "o1", "t": t, "offset_s": -18000,
                   "v": 96.0, "v2": null,                          # blood_pressure: v/v2 = sys/dia
                   "attrs": {"measurement_location": 3, "body_position": 2,
                             "recording_method": 1},
                   "last_modified": t}],
     "deleted_ids": ["<HC id>", ...]}

`origin` is a key from `origins`, or "hc_merged" for an aggregate Health
Connect de-duplicated across sources. `offset_s` on a bucket group is the
zone offset of its replace_day; when absent the box uses the offset it last
saw for that day.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import (AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field,
                      StrictBool, field_validator, model_validator)

from Orchestrator.wellness import catalog

SCHEMA_INGEST = "bbx.wellness.ingest/1"

# Batch caps (a batch over any of them is malformed; the phone splits on 413
# and quarantines a 422).
MAX_ORIGINS = 64
MAX_BUCKET_GROUPS = 600
MAX_ROWS_PER_GROUP = 1500        # one local day of 1-minute buckets is 1,440
MAX_SESSIONS = 1000
MAX_STAGES = 600
MAX_READINGS = 20_000
MAX_DELETED = 20_000
MAX_ID = 128
MAX_LABEL = 80

MAX_OFFSET_S = 14 * 3600 + 3600   # UTC-12 .. UTC+14, with a margin
_TS_MIN_MS = 946_684_800_000        # 2000-01-01
_TS_MAX_MS = 4_102_444_800_000      # 2100-01-01

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁦-⁩﻿]")
_CTRL_KEEP_NL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏ -‮⁦-⁩﻿]")
_ID_RE = re.compile(r"[A-Za-z0-9_.:@/+=-]{1,%d}" % MAX_ID)
_BATCH_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_ENR_RE = re.compile(r"en_[0-9a-f]{8}")
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NONCE_RE = re.compile(r"[0-9a-f]{32}")


def clean_text(value: Any, cap: int, *, keep_newlines: bool = False) -> Optional[str]:
    """A bounded string with control and bidi characters removed (None stays None)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("must be a string")
    rx = _CTRL_KEEP_NL_RE if keep_newlines else _CTRL_RE
    text = rx.sub("", value.replace("\t", " ")).strip()
    return text[:cap]


def _label(cap: int):
    return BeforeValidator(lambda v: clean_text(v, cap))


def _ident(value: Any) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f"must be 1-{MAX_ID} identifier characters")
    return value


def _origin_key(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 32:
        raise ValueError("an origin key is 1-32 identifier characters")
    return _ident(value)


def _no_bool(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("must be a number")
    return value


Label = Annotated[Optional[str], _label(MAX_LABEL)]
Ident = Annotated[str, AfterValidator(_ident)]
Num = Annotated[float, BeforeValidator(_no_bool), Field(allow_inf_nan=False)]
Int = Annotated[int, BeforeValidator(_no_bool), Field(strict=True)]
TsMs = Annotated[int, BeforeValidator(_no_bool), Field(strict=True)]
Offset = Annotated[int, BeforeValidator(_no_bool),
                   Field(strict=True, ge=-MAX_OFFSET_S, le=MAX_OFFSET_S)]
SmallCode = Annotated[Optional[Union[int, str]], BeforeValidator(
    lambda v: clean_text(v, 32) if isinstance(v, str) else _no_bool(v))]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------- ingest/1

class Window(_Strict):
    type: Annotated[str, _label(24)] = "changes"
    from_: Annotated[Optional[str], _label(40)] = Field(default=None, alias="from")
    to: Annotated[Optional[str], _label(40)] = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Origin(_Strict):
    key: Annotated[str, AfterValidator(_origin_key)]
    package: Annotated[str, _label(128)]
    label: Label = None
    device_type: Annotated[Optional[str], _label(24)] = None
    manufacturer: Label = None
    model: Label = None
    recording_method: SmallCode = None


class BucketGroup(_Strict):
    metric: Annotated[str, _label(40)]
    grain: Literal["1min", "5min", "1h"]
    origin: Annotated[str, _label(32)]
    replace_day: Optional[str] = None
    offset_s: Optional[Offset] = None
    rows: List[List[Optional[Num]]] = Field(max_length=MAX_ROWS_PER_GROUP)

    @field_validator("replace_day")
    @classmethod
    def _day(cls, v):
        if v is not None and not _DAY_RE.fullmatch(v):
            raise ValueError("replace_day must be YYYY-MM-DD")
        return v

    @field_validator("rows")
    @classmethod
    def _rows(cls, rows):
        for row in rows:
            if not 2 <= len(row) <= 5:
                raise ValueError("a bucket row is [t, value] or [t, avg, min, max, n]")
            t = row[0]
            if t is None or t != int(t) or not _TS_MIN_MS <= t <= _TS_MAX_MS:
                raise ValueError("a bucket row starts with its time in Unix milliseconds")
        return rows


class SessionAttrs(_Lenient):
    distance_m: Optional[Num] = None
    active_kcal: Optional[Num] = None
    total_kcal: Optional[Num] = None
    kcal: Optional[Num] = None
    avg_hr: Optional[Num] = None
    max_hr: Optional[Num] = None
    steps: Optional[Num] = None


class Session(_Strict):
    kind: Literal["sleep", "exercise"]
    hc_id: Ident
    origin: Annotated[str, _label(32)]
    start: TsMs
    end: TsMs
    offset_s: Optional[Offset] = None
    stages: Optional[List[List[Int]]] = Field(default=None, max_length=MAX_STAGES)
    exercise_type: SmallCode = None
    subtype: Optional[Int] = None
    attrs: Optional[SessionAttrs] = None
    last_modified: Optional[TsMs] = None

    @field_validator("start", "end", "last_modified")
    @classmethod
    def _ts(cls, v):
        if v is not None and not _TS_MIN_MS <= v <= _TS_MAX_MS:
            raise ValueError("times are Unix milliseconds")
        return v

    @field_validator("stages")
    @classmethod
    def _stages(cls, stages):
        for st in stages or ():
            if len(st) != 3:
                raise ValueError("a stage is [start, end, code]")
        return stages


class ReadingAttrs(_Lenient):
    measurement_location: SmallCode = None
    body_position: SmallCode = None
    recording_method: SmallCode = None


class Reading(_Strict):
    metric: Annotated[str, _label(40)]
    hc_id: Ident
    origin: Annotated[str, _label(32)]
    t: TsMs
    offset_s: Optional[Offset] = None
    v: Num
    v2: Optional[Num] = None
    attrs: Optional[ReadingAttrs] = None
    last_modified: Optional[TsMs] = None

    @field_validator("t", "last_modified")
    @classmethod
    def _ts(cls, v):
        if v is not None and not _TS_MIN_MS <= v <= _TS_MAX_MS:
            raise ValueError("times are Unix milliseconds")
        return v


class IngestBatch(_Strict):
    schema_: Literal["bbx.wellness.ingest/1"] = Field(alias="schema")
    batch_id: str
    enrollment_id: str
    epoch: Annotated[int, Field(strict=True, ge=1, le=100_000)]
    window: Window = Field(default_factory=Window)
    origins: List[Origin] = Field(default_factory=list, max_length=MAX_ORIGINS)
    buckets: List[BucketGroup] = Field(default_factory=list, max_length=MAX_BUCKET_GROUPS)
    sessions: List[Session] = Field(default_factory=list, max_length=MAX_SESSIONS)
    readings: List[Reading] = Field(default_factory=list, max_length=MAX_READINGS)
    deleted_ids: List[Ident] = Field(default_factory=list, max_length=MAX_DELETED)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("batch_id")
    @classmethod
    def _batch_id(cls, v):
        if not _BATCH_ID_RE.fullmatch(v):
            raise ValueError("batch_id is 1-64 of [A-Za-z0-9_.:-]")
        return v

    @field_validator("enrollment_id")
    @classmethod
    def _enr(cls, v):
        if not _ENR_RE.fullmatch(v):
            raise ValueError("enrollment_id is en_<8 hex>")
        return v

    @model_validator(mode="after")
    def _origin_keys(self):
        keys = [o.key for o in self.origins]
        if len(set(keys)) != len(keys):
            raise ValueError("origin keys must be unique")
        return self


# ---------------------------------------------------------------- capability report

class CapabilityPhone(_Lenient):
    manufacturer: Label = None
    model: Label = None
    device_name: Label = None
    sdk_int: Optional[Int] = None
    app_version: Annotated[Optional[str], _label(40)] = None


class CapabilityHC(_Lenient):
    sdk_status: Annotated[Optional[str], _label(40)] = None
    background_feature: Annotated[Optional[str], _label(40)] = None
    history_feature: Annotated[Optional[str], _label(40)] = None
    granted: List[Annotated[str, _label(120)]] = Field(default_factory=list, max_length=64)


class CapabilitySamsung(_Lenient):
    installed: Optional[StrictBool] = None
    version: Annotated[Optional[str], _label(40)] = None


class CapabilityOriginMetric(_Lenient):
    metric: Annotated[str, _label(40)]
    count_14d: Optional[Int] = None
    last_sample: Annotated[Optional[str], _label(40)] = None
    median_gap_s: Optional[Num] = None


class CapabilityOrigin(_Lenient):
    package: Annotated[str, _label(128)]
    label: Label = None
    device_type: Annotated[Optional[str], _label(24)] = None
    model: Label = None
    manufacturer: Label = None
    recording_method: SmallCode = None
    metrics: List[CapabilityOriginMetric] = Field(default_factory=list, max_length=64)


class CapabilityReport(_Lenient):
    phone: CapabilityPhone = Field(default_factory=CapabilityPhone)
    health_connect: CapabilityHC = Field(default_factory=CapabilityHC)
    samsung_health: CapabilitySamsung = Field(default_factory=CapabilitySamsung)
    origins: List[CapabilityOrigin] = Field(default_factory=list, max_length=64)
    reported_at: Annotated[Optional[str], _label(40)] = None

    def granted_short(self) -> set:
        """Granted Health Connect permissions without the android prefix."""
        out = set()
        for perm in self.health_connect.granted:
            out.add(perm[len(catalog.HC_PERMISSION_PREFIX):]
                    if perm.startswith(catalog.HC_PERMISSION_PREFIX) else perm)
        return out


# ---------------------------------------------------------------- route bodies

Consent = Literal["off", "ask", "proactive"]
Category = Literal["sleep", "heart", "activity", "blood_pressure"]


def _operator(v: Any) -> str:
    from Orchestrator.utils.operators import validate_operator
    return validate_operator(v)


OperatorName = Annotated[str, AfterValidator(_operator)]


class EnrollBody(_Lenient):
    operator: OperatorName
    install_id: Annotated[str, _label(128)]
    tailnet_ips: List[Annotated[str, _label(64)]] = Field(default_factory=list, max_length=8)
    nonce: str
    capability_report: Optional[CapabilityReport] = None
    categories: Optional[Dict[Category, Consent]] = None
    replace: StrictBool = False
    assign_to_me: StrictBool = False

    @field_validator("nonce")
    @classmethod
    def _nonce(cls, v):
        if not isinstance(v, str) or not _NONCE_RE.fullmatch(v):
            raise ValueError("nonce is 32 lowercase hex characters")
        return v


class OperatorBody(_Strict):
    operator: OperatorName


class SelectBody(_Strict):
    operator: OperatorName
    device_id: Annotated[str, _label(MAX_ID)]


class DisconnectBody(_Strict):
    operator: OperatorName
    keep_history: StrictBool      # no default: the person must choose (spec §3.6)


class ReplaceApproveBody(_Strict):
    operator: OperatorName
    approve: StrictBool


class SettingsBody(_Strict):
    operator: OperatorName
    categories: Optional[Dict[Category, Consent]] = None
    local_only: Optional[Dict[Category, StrictBool]] = None
    providers_allowed: Optional[List[Annotated[str, _label(80)]]] = Field(default=None,
                                                                          max_length=32)
    google_paid_key_confirmed: Optional[StrictBool] = None
    instinct_prompt: Optional[str] = None
    surfaces_mode: Optional[Literal["auto", "household", "credentialed"]] = None

    @field_validator("instinct_prompt")
    @classmethod
    def _prompt(cls, v):
        if v is None:
            return None
        text = clean_text(v, 10 * catalog.INSTINCT_PROMPT_MAX, keep_newlines=True)
        if len(text) > catalog.INSTINCT_PROMPT_MAX:
            raise ValueError(f"the instinct prompt is at most {catalog.INSTINCT_PROMPT_MAX} "
                             "characters")
        return text


class EnrollmentRecord(BaseModel):
    """One operator's entry in data/wellness/enrollments.json. Unknown keys
    survive a round trip (a newer build's field is never dropped by this one)."""

    model_config = ConfigDict(extra="allow")

    enrollment_id: Optional[str] = None
    operator: str
    op_key: str
    device_id: Optional[str] = None
    tailnet_ip: Optional[str] = None
    install_id: Optional[str] = None
    epoch: int = 0
    state: Literal["awaiting_phone", "active", "paused", "stale", "needs_attention",
                   "ownership_changed", "revoked", "replace_pending"] = "awaiting_phone"
    categories: Dict[str, str] = Field(default_factory=lambda: dict(catalog.DEFAULT_CONSENT))
    local_only: Dict[str, bool] = Field(default_factory=lambda: dict(catalog.DEFAULT_LOCAL_ONLY))
    providers_allowed: List[str] = Field(default_factory=list)
    google_paid_key_confirmed: bool = False
    surfaces_mode: Literal["auto", "household", "credentialed"] = "auto"
    instinct_prompt: Optional[str] = None
    briefing: Dict[str, Any] = Field(default_factory=lambda: {"enabled": False})
    source_label: Optional[str] = None
    pending: Optional[Dict[str, Any]] = None
    # {device_id, tailnet_ip, epoch, at} of the last bind (or approved
    # replacement): the address the history is bound to (enroll.source_ip).
    last_source: Optional[Dict[str, Any]] = None
    # Stores a change of phone left behind (never readable; erase removes them).
    detached_op_keys: List[str] = Field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_push_at: Optional[str] = None
    policy_version: str = catalog.POLICY_VERSION
