#!/usr/bin/env python3
"""location.py — per-turn location ride-along (M1).

Brandon's locked design: **location is metadata on a turn that was happening
anyway.** The phone attaches coordinates to the prompt it was already sending,
one line is appended to the user text, and that line reaches both the model and
the `conversation_log` item the mint renders — so the ledger records it for free
(`turns_threshold = 1`, one turn IS one snapshot).

NOT a tracking system: no polling, no background service, no location history
store, no location-minted snapshots. Nothing here writes anywhere.

**Absent location is a perfect no-op.** Every function is total — junk in
returns ``None``/``""``, never an exception — so a chat turn can never fail
because of a location payload.

This module is deliberately dependency-free and pure (stdlib `math`/`re` only)
so it is trivially testable and safe to import from any route.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Optional

__all__ = [
    "normalize",
    "format_location_line",
    "format_location_gauge",
    "append_location_to_text",
    "apply_location_to_messages",
]

# Untrusted-input caps. A place string arrives from a client we do not control;
# it lands in a prompt AND in the immutable ledger, so it is bounded and
# scrubbed before it is ever rendered.
_MAX_PART_LEN = 64      # per component (city / state)
_MAX_PLACE_LEN = 120    # composed place string

# Control characters (incl. newlines/tabs) + the bracket characters that
# delimit the rendered line. Stripped, never escaped — the line must stay one
# line and must not be able to fake a second field.
_STRIP_RE = re.compile(r"[\x00-\x1f\x7f\[\]]")

_LAT_KEYS = ("lat", "latitude")
_LON_KEYS = ("lon", "lng", "long", "longitude")
_CITY_KEYS = ("city", "locality", "town")
_STATE_KEYS = ("state", "region", "province", "admin_area", "adminArea")
_PLACE_KEYS = ("place", "place_name", "placeName", "label")


def _clean_str(value: Any, max_len: int = _MAX_PART_LEN) -> Optional[str]:
    """Coerce to a safe, bounded, single-line string. None for anything empty."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, str):
        # Numbers are acceptable place fragments in theory; anything else
        # (dict/list/object) is junk and is dropped rather than str()'d.
        if isinstance(value, (int, float)):
            value = str(value)
        else:
            return None
    cleaned = _STRIP_RE.sub("", value).strip()
    # Collapse internal whitespace runs so the ledger line stays tidy.
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return None
    return cleaned[:max_len].strip() or None


def _coord(value: Any, limit: float) -> Optional[float]:
    """Coerce one coordinate to a finite float inside +/-limit, else None.

    `bool` is rejected explicitly (it is an `int` subclass — `True` would
    otherwise become 1.0). Numeric strings are accepted because a JSON client
    may quote them; ``"NaN"``/``"inf"``/``"north"`` all fail the finite check
    and are rejected.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
            num = float(value)
        elif isinstance(value, (int, float)):
            num = float(value)
        else:
            return None
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    if num < -limit or num > limit:
        return None
    return num


def _first(raw: Dict[str, Any], keys) -> Any:
    for k in keys:
        if k in raw:
            return raw[k]
    return None


def _fmt_coord(value: float) -> str:
    """Render a coordinate without trailing-zero noise (43.2557 -> '43.2557')."""
    text = ("%.6f" % value).rstrip("0").rstrip(".")
    return text if text not in ("", "-", "-0") else "0"


def normalize(raw: Any) -> Optional[Dict[str, Any]]:
    """Validate an UNTRUSTED client location payload.

    Returns ``{"lat": float, "lon": float, "place": str|None, "city": str|None,
    "state": str|None, "accuracy_m": float|None}`` or ``None`` when the payload
    is missing/invalid. Never raises.
    """
    try:
        if not isinstance(raw, dict):
            return None

        lat = _coord(_first(raw, _LAT_KEYS), 90.0)
        lon = _coord(_first(raw, _LON_KEYS), 180.0)
        if lat is None or lon is None:
            return None
        # "Null island" (0, 0) is a valid coordinate on paper — a spot in the Gulf
        # of Guinea — but in practice it is the universal signature of a BAD FIX:
        # a provider that returned zeroes, an uninitialised struct, a stubbed
        # client. The Android capture path already rejects it, and the two halves
        # disagreeing about the same rule is exactly how a future non-Android
        # client ends up stamping a bogus position into the immutable ledger.
        if lat == 0.0 and lon == 0.0:
            return None

        city = _clean_str(_first(raw, _CITY_KEYS))
        state = _clean_str(_first(raw, _STATE_KEYS))
        place = _clean_str(_first(raw, _PLACE_KEYS), _MAX_PLACE_LEN)
        if not place:
            parts = [p for p in (city, state) if p]
            place = ", ".join(parts) if parts else None
        if place:
            place = place[:_MAX_PLACE_LEN].strip() or None

        accuracy = _coord(raw.get("accuracy_m", raw.get("accuracy")), 1_000_000.0)
        if accuracy is not None and accuracy < 0:
            accuracy = None

        return {
            "lat": lat,
            "lon": lon,
            "place": place,
            "city": city,
            "state": state,
            "accuracy_m": accuracy,
        }
    except Exception:
        # Totality guarantee: a location payload can never fail a chat turn.
        return None


def format_location_gauge(loc: Optional[Dict[str, Any]], place: Optional[str] = None) -> str:
    """The bare value used by the snapshot ``LOCATION:`` gauge.

    ``"43.2557,-79.8711 · Hamilton, Ontario"``, degrading to coordinates alone
    without a place name. ``""`` for absent/invalid input.
    """
    try:
        if not isinstance(loc, dict):
            return ""
        lat = loc.get("lat")
        lon = loc.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return ""
        if isinstance(lat, bool) or isinstance(lon, bool):
            return ""
        if not math.isfinite(float(lat)) or not math.isfinite(float(lon)):
            return ""
        coords = f"{_fmt_coord(float(lat))},{_fmt_coord(float(lon))}"
        text = _clean_str(place, _MAX_PLACE_LEN) or _clean_str(loc.get("place"), _MAX_PLACE_LEN)
        return f"{coords} · {text}" if text else coords
    except Exception:
        return ""


def format_location_line(loc: Optional[Dict[str, Any]], place: Optional[str] = None) -> str:
    """The single line appended to the user prompt.

    ``[user location: 43.2557,-79.8711 · Hamilton, Ontario]``, degrading to
    coordinates alone when there is no place name. ``""`` for ``None`` — an
    absent location must be a perfect no-op.
    """
    gauge = format_location_gauge(loc, place)
    return f"[user location: {gauge}]" if gauge else ""


def append_location_to_text(text: Any, loc: Optional[Dict[str, Any]],
                            place: Optional[str] = None) -> Any:
    """Append the location line to one user-text string.

    Returns `text` UNCHANGED (same object) when there is no valid location or
    the line is already present — the no-op path must be byte-identical.
    """
    try:
        line = format_location_line(loc, place)
        if not line or not isinstance(text, str):
            return text
        if line in text:
            return text  # client already appended it; never double-stamp
        return f"{text}\n{line}" if text else line
    except Exception:
        return text


def apply_location_to_messages(messages: Any, loc: Optional[Dict[str, Any]],
                               place: Optional[str] = None) -> Any:
    """Append the location line to the LAST user message of a chat list.

    Returns the SAME list object untouched when there is nothing to add.
    Otherwise returns a new list in which only that one message is replaced by
    a copy (the caller's message dicts are never mutated in place). Handles both
    string content and OpenAI-style multi-part content.
    """
    try:
        line = format_location_line(loc, place)
        if not line or not isinstance(messages, list) or not messages:
            return messages

        idx = None
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if isinstance(m, dict) and m.get("role") == "user":
                idx = i
                break
        if idx is None:
            return messages

        msg = dict(messages[idx])
        content = msg.get("content", "")

        if isinstance(content, str):
            new_content: Any = append_location_to_text(content, loc, place)
            if new_content == content:
                return messages
            msg["content"] = new_content
        elif isinstance(content, list):
            parts = list(content)
            target = None
            for i in range(len(parts) - 1, -1, -1):
                p = parts[i]
                if isinstance(p, dict) and isinstance(p.get("text"), str) and \
                        p.get("type", "text") == "text":
                    target = i
                    break
            if target is None:
                parts.append({"type": "text", "text": line})
            else:
                part = dict(parts[target])
                new_text = append_location_to_text(part.get("text", ""), loc, place)
                if new_text == part.get("text", ""):
                    return messages
                part["text"] = new_text
                parts[target] = part
            msg["content"] = parts
        else:
            return messages

        out = list(messages)
        out[idx] = msg
        return out
    except Exception:
        return messages
