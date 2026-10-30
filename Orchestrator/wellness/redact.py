"""Redact: tagged wellness turns are hidden from every ledger reader that may
not see them (spec §3.7, W25, W29; check A13).

A snapshot whose GAUGES carry a WELLNESS line is a tagged turn: perform_mint
renders the line for every turn a wellness result reached (mask.py masked its
numbers first). Only the header's GAUGES block counts: a line shaped like the
gauge anywhere in the session log (a quoted snapshot, this very format in a
code block) tags nothing. The numbers are gone, but the words stay (the user's own,
verbatim; the assistant's qualitative ones), and the ledger is read by others:
the Flight Recorder, `system`, other operators, cross-operator MCP and Claude
Code reads, and every provider that later receives retrieved, reranked,
embedded or compressed text. For those, every line of a tagged turn becomes

    [wellness conversation · not shared]

A reader may see a tagged turn only when

  * it is the snapshot's own operator, a live human, and
  * every category of the tag passes the gate for that reader (gate.evaluate:
    the kill switch, the enrollment, the category's consent, and the
    destination the text goes to: the turn's chat provider, or the reranker or
    embedder when that is who receives it), and
  * when the reader is a stamped turn, its surface may carry wellness data
    (not on the never-list; a credential, or household mode).

Default-deny, like the gate: a reader nobody stamped, an unknown provider, an
unparseable tag or owner all redact. A text without the tag is returned as the
SAME object, so an untagged snapshot is byte-identical through every reader.

The reader is ambient. for_reader() reads the turn stamped for this request
(turn.current(), set at the chat entry points) unless a caller named one with
reading_as() (the Flight Recorder, checkpoint compression, a tool called from
the MCP gateway). redact() is the explicit form for a caller that knows the
reader operator and the provider and nothing else (no surface proof): voice
sessions, the TTS replay, the MCP server's direct reads.

Every function that reads snapshot text out of the ledger calls one of these
(test_wellness_redact.py::test_every_ledger_reader_redacts enumerates them).

Module top level is stdlib only: the MCP server's lean venv imports it (its
direct reads pass no provider, so they always redact).
"""
from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import re
from typing import Iterator, List, Optional, Tuple

from Orchestrator.wellness import turn as turn_mod

PLACEHOLDER = "[wellness conversation · not shared]"
GAUGE_KEY = "WELLNESS"
GAUGE_SUFFIX = " · values not recorded"
# A reranker or embedder that runs on this box or its LAN (registry privacy
# "local"): a local destination, allowed like a local model server.
LOCAL_SERVICE = "box-local-service"
# Background summarizers (checkpoint compression, flight reports) read as this
# surface: on the never-list, so a tagged turn never reaches them.
BACKGROUND_SURFACE = "system"

_GAUGE_RE = re.compile(r"^WELLNESS: (?P<body>[^\n]+?) · values not recorded[ \t]*$", re.M)
_OPERATOR_RE = re.compile(r"^[ \t]*OPERATOR:[ \t]*(?P<op>\S[^\n]*?)[ \t]*$", re.M)
_LOG_HEAD = "Raw Session Log\n"
_GAUGES_HEAD = "GAUGES\n"
_ENTRY_RE = re.compile(r"^(- \[\d+\] )(\S+ )(?:location=[^\n]*? )?(operator=[^\n]*? (?:user|assistant):)")
_ENTRY_NO_RE = re.compile(r"^(- \[\d+\] )")
# The fixed trailer render_snapshot_body_v71 writes after the log.
_END_LINE = r"=== END SNAPSHOT — SNAP-\d{8}-\d+ — UTC \S+ ===\n?\Z"
_TRAILER_RE = re.compile(r"\nRelease Notes\n- Hybrid context window in effect; gauges record drift "
                         r"and token usage\.\n\n" + _END_LINE)
_END_RE = re.compile(r"\n" + _END_LINE)
_LABEL_JUNK = re.compile(r"[\x00-\x1f\x7f·]+")


@dataclasses.dataclass(frozen=True)
class Tag:
    categories: Tuple[str, ...]
    labels: Tuple[str, ...]
    refs: Tuple[str, ...]
    line: str


# ------------------------------------------------------------------ the tag

def _clean(text: str, limit: int = 80) -> str:
    return " ".join(_LABEL_JUNK.sub(" ", str(text)).split())[:limit]


def format_gauge(categories, labels=(), refs=()) -> str:
    """The WELLNESS gauge value (render_snapshot_body_v71 prefixes "WELLNESS: "):
    "sleep,heart · night of 2026-10-24 · ref w-20261025-7f3a · values not recorded".
    No values, ever: categories, period labels and refs only."""
    cats = ",".join(sorted({_clean(c, 24) for c in categories if c})) or "wellness"
    shown = [x for x in dict.fromkeys(_clean(lb, 60) for lb in labels) if x]
    label = "; ".join(shown[:3]) + ("; …" if len(shown) > 3 else "") or "health data"
    ref_list = [x for x in dict.fromkeys(_clean(r, 32) for r in refs) if x]
    ref_text = ", ".join(ref_list[:6]) + (", …" if len(ref_list) > 6 else "") or "none"
    return f"{cats} · {label} · ref {ref_text}{GAUGE_SUFFIX}"


def _gauges_block(text: str) -> str:
    """The header's GAUGES block (render_snapshot_body_v71: "GAUGES" up to the
    blank line, always before the session log), or "" when there is none.
    The header is the box's own text; the log is anyone's."""
    log = text.find("\n" + _LOG_HEAD)
    head = text if log < 0 else text[:log + 1]
    if head.startswith(_GAUGES_HEAD):
        start = len(_GAUGES_HEAD)
    else:
        i = head.find("\n" + _GAUGES_HEAD)
        if i < 0:
            return ""
        start = i + 1 + len(_GAUGES_HEAD)
    end = head.find("\n\n", start)
    return head[start:] if end < 0 else head[start:end + 1]


def parse_tag(text: Optional[str]) -> Optional[Tag]:
    """The WELLNESS tag of a snapshot text, or None (an untagged text)."""
    if not text or "WELLNESS: " not in text:
        return None
    m = _GAUGE_RE.search(_gauges_block(text))
    if not m:
        return None
    parts = m.group("body").split(" · ")
    cats = tuple(c.strip() for c in parts[0].split(",") if c.strip())
    refs: Tuple[str, ...] = ()
    labels: List[str] = []
    for part in parts[1:]:
        if part.startswith("ref "):
            refs = tuple(r.strip() for r in part[4:].split(",") if r.strip())
        else:
            labels.append(part.strip())
    return Tag(cats, tuple(labels), refs, m.group(0))


def is_tagged(text: Optional[str]) -> bool:
    return parse_tag(text) is not None


def pointer_line(text: str) -> str:
    """What a tagged snapshot is embedded as when the embedder may not read it."""
    tag = parse_tag(text)
    return tag.line.strip() if tag else ""


def snapshot_owner(text: str) -> Optional[str]:
    """The OPERATOR of the header's GAUGES block (None without one)."""
    m = _OPERATOR_RE.search(_gauges_block(text or ""))
    return m.group("op").strip() if m else None


# ------------------------------------------------------------------ hiding

def hide(text: str) -> str:
    """Every line of a tagged turn as the placeholder. The envelope (START,
    GAUGES, OPERATOR, provenance ids, END) stays, so ids, attribution and
    parsing still work; the WELLNESS gauge itself (categories, period) goes."""
    out = _GAUGE_RE.sub(f"{GAUGE_KEY}: {PLACEHOLDER}", text)
    i = out.find(_LOG_HEAD)
    if i < 0:
        # No session log to keep the shape of: nothing after the tag survives.
        j = out.find(f"{GAUGE_KEY}: {PLACEHOLDER}")
        return out[:j] + f"{GAUGE_KEY}: {PLACEHOLDER}\n" if j >= 0 else PLACEHOLDER
    start = i + len(_LOG_HEAD)
    trailer = _TRAILER_RE.search(out) or _END_RE.search(out)
    end = trailer.start() if trailer and trailer.start() >= start else len(out)
    entries = []
    for line in out[start:end].split("\n"):
        em = _ENTRY_RE.match(line)
        if em:
            entries.append(f"{em.group(1)}{em.group(2)}{em.group(3)} {PLACEHOLDER}")
        else:
            nm = _ENTRY_NO_RE.match(line)
            if nm:
                entries.append(f"{nm.group(1)}{PLACEHOLDER}")
    if not entries:
        entries.append(f"- [1] {PLACEHOLDER}")
    tail = out[trailer.start():] if trailer and trailer.start() >= start else "\n"
    return out[:start] + "\n".join(entries) + tail


# ------------------------------------------------------------------ the reader

_UNSET = object()
_READER: contextvars.ContextVar = contextvars.ContextVar("_WELLNESS_LEDGER_READER", default=_UNSET)
_KEEP = object()


@contextlib.contextmanager
def reading_as(reader: Optional[turn_mod.WellnessTurn]) -> Iterator[None]:
    """Name the ledger reader for the block (None: nobody may see a tagged
    turn). Overrides the stamped turn."""
    token = _READER.set(reader)
    try:
        yield
    finally:
        _READER.reset(token)


def current_reader() -> Optional[turn_mod.WellnessTurn]:
    r = _READER.get()
    if r is not _UNSET:
        return r
    return turn_mod.current()


def background_reader(operator: str) -> turn_mod.WellnessTurn:
    """A background summarizer working on `operator`'s ledger."""
    return turn_mod.WellnessTurn(operator=operator or "", surface=BACKGROUND_SURFACE)


@contextlib.contextmanager
def reading_for_tool(ctx) -> Iterator[None]:
    """A ToolVault snapshot executor's reader: the stamped turn when it is this
    tool call's operator, else nobody (the MCP gateway, any mismatch)."""
    caller = getattr(ctx, "caller", None)
    operator = getattr(ctx, "operator", None)
    reader = current_reader()
    if caller == "mcp-gateway" or reader is None or reader.operator != operator:
        reader = None
    with reading_as(reader):
        yield


# ------------------------------------------------------------------ the decision

def _destination_ok(provider, model, category, enrollment):
    if provider == LOCAL_SERVICE:
        return True, None
    from Orchestrator.wellness import destinations
    return destinations.allowed(provider, category, enrollment, model=model)


def _visible(tag: Tag, owner: Optional[str], reader: Optional[turn_mod.WellnessTurn],
             check_surface: bool) -> bool:
    if reader is None or not owner or reader.operator != owner:
        return False
    try:
        from Orchestrator import config
        from Orchestrator.wellness import enroll, gate
        rec = enroll.get_enrollment(owner)
        if rec is not None:
            rec = dict(rec, state=enroll.effective_state(rec))
        roster, reserved = gate._roster()
        enabled = config.wellness_enabled()
        seen = reader if check_surface else dataclasses.replace(
            reader, surface=turn_mod.SURFACE_ANDROID, credential_ok=True)
        for cat in tag.categories or ("unknown",):
            res = gate.evaluate(
                enabled=enabled, roster=roster, reserved=reserved, operator=owner, turn=seen,
                enrollment=rec, category=cat,
                destination_ok=lambda c=cat: _destination_ok(reader.provider, reader.model, c, rec))
            if not res.ok:
                return False
        return True
    except Exception:  # noqa: BLE001 - any doubt hides the turn
        return False


def for_reader(text: Optional[str], *, snapshot_operator: Optional[str] = None,
               provider=_KEEP, model: Optional[str] = None) -> Optional[str]:
    """`text` as the current reader may see it. `provider` (with `model`)
    names the destination when it is not the reader's own chat provider: the
    reranker that scores the passage."""
    tag = parse_tag(text)
    if tag is None:
        return text
    reader = current_reader()
    if reader is not None and provider is not _KEEP:
        reader = reader.with_route(provider, model)
    owner = snapshot_operator or snapshot_owner(text)
    return text if _visible(tag, owner, reader, check_surface=True) else hide(text)


def may_read(text: Optional[str], *, snapshot_operator: Optional[str] = None,
             provider=_KEEP, model: Optional[str] = None) -> bool:
    """False for a tagged turn the current reader may not see (for_reader
    would hide it). A ranked reader (keyword, semantic) leaves such a turn
    out of the candidates altogether, so which ids come back for a query
    says nothing about what a hidden turn holds."""
    tag = parse_tag(text)
    if tag is None:
        return True
    return for_reader(text, snapshot_operator=snapshot_operator, provider=provider,
                      model=model) is text


def redact(text: Optional[str], snapshot_operator: Optional[str], reader_operator: Optional[str],
           provider: Optional[str], *, model: Optional[str] = None) -> Optional[str]:
    """`text` as `reader_operator` may see it when it goes to `provider`
    (None: no model, or one nobody can vouch for, which never may)."""
    tag = parse_tag(text)
    if tag is None:
        return text
    reader = (turn_mod.WellnessTurn(operator=reader_operator, surface=turn_mod.SURFACE_ANDROID,
                                    credential_ok=True, provider=provider, model=model)
              if reader_operator else None)
    owner = snapshot_operator or snapshot_owner(text)
    return text if _visible(tag, owner, reader, check_surface=False) else hide(text)


_START_RE = re.compile(r"^=== START SNAPSHOT — UTC (?P<utc>\S+) — (?P<snap>SNAP-\d{8}-\d+)\b[^\n]*$",
                       re.M)
# How far past its START a snapshot's END is looked for (the index rebuild's
# bound: fossils.rebuild_snapshot_index).
_MAX_SNAPSHOT = 200000


def redact_volume(text: Optional[str]) -> Optional[str]:
    """A run of whole snapshots (the ledger, or an excerpt of it), each
    through for_reader().

    A snapshot runs from its START header to its own END line, the way the
    index rebuild reads the ledger; a START line inside a snapshot's text (a
    quoted fossil) starts nothing. A tagged snapshot runs to the LAST copy of
    its own END line (id and time) within a snapshot's reach, so no line its
    text forges can end it early: at worst a later turn that quotes the END
    line is hidden with it. Text outside any snapshot passes through."""
    if not text or "WELLNESS: " not in text:
        return text
    pieces, pos = [], 0
    for m in _START_RE.finditer(text):
        if m.start() < pos:
            continue                      # inside the snapshot just handed out
        end_line = f"=== END SNAPSHOT — {m.group('snap')} — UTC {m.group('utc')} ==="
        first = text.find(end_line, m.end())
        if first < 0:
            continue                      # no END: not a whole snapshot
        head = text[m.start():first]
        last = max(first, text.rfind(end_line, m.end(), m.start() + _MAX_SNAPSHOT)) \
            if parse_tag(head) else first
        end = last + len(end_line)
        if text.startswith("\n", end):
            end += 1
        pieces.append(text[pos:m.start()])
        pieces.append(for_reader(text[m.start():end]))
        pos = end
    if not pieces:
        return for_reader(text)
    pieces.append(text[pos:])
    return "".join(pieces)


# ------------------------------------------------------------------ services

_LLM_KEY_PROVIDERS = {"GOOGLE_API_KEY": "google", "OPENAI_API_KEY": "openai",
                      "ANTHROPIC_API_KEY": "anthropic", "XAI_API_KEY": "xai"}
_EMBED_PROVIDERS = {"gemini": "google", "google": "google", "openai": "openai"}
_ON_BOX_RERANKERS = frozenset({"cpu", "vllm", "localstack"})


def rerank_destination() -> Tuple[Optional[str], Optional[str]]:
    """(provider id, model) of the configured reranker, as a destination."""
    try:
        from Orchestrator import rerank
        s = rerank.get_settings()
        entry = rerank.RERANK_MODELS.get(s.get("model")) or {}
    except Exception:  # noqa: BLE001
        return None, None
    p = (s.get("provider") or "").lower()
    privacy = entry.get("privacy")
    if privacy == "local" or (not entry and p in _ON_BOX_RERANKERS):
        return LOCAL_SERVICE, None
    if p == "vertex":
        return "google", None
    if p == "llm":
        return _LLM_KEY_PROVIDERS.get(entry.get("key_env") or s.get("key_env")), s.get("model_id")
    return f"rerank:{p}" if p else None, None


def embed_destination(slug: Optional[str] = None) -> Optional[str]:
    """The provider id the embedding model `slug` (default: the active one)
    sends text to."""
    try:
        from Orchestrator.embeddings.registry import EMBEDDING_MODELS
        if slug is None:
            from Orchestrator.embeddings.store import get_active_slug
            slug = get_active_slug()
        entry = EMBEDDING_MODELS.get(slug) or {}
    except Exception:  # noqa: BLE001
        return None
    if entry.get("privacy") == "local":
        return LOCAL_SERVICE
    p = (entry.get("provider") or "").lower()
    return _EMBED_PROVIDERS.get(p, f"embed:{p}" if p else None)


def embed_text(body: Optional[str], slug: Optional[str] = None) -> Optional[str]:
    """The text an embedding call may send for snapshot `body`: the body, or,
    for a tagged turn whose owner did not allow the embedder, the pointer line
    only (semantic search still finds "when did my sleep slip?")."""
    tag = parse_tag(body)
    if tag is None:
        return body
    owner = snapshot_owner(body)
    if owner and redact(body, owner, owner, embed_destination(slug)) is body:
        return body
    return tag.line.strip()
