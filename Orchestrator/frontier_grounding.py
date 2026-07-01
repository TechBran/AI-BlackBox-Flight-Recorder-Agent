"""Hybrid grounding + coordinate adapter for the frontier device-control loop (M2, decision #2).

The cloud frontier model reasons in a PROVIDER coordinate space (Gemini = normalized
0-999). The phone actuates either by SEMANTIC accessibility node (element_click /
element_set_text — stable, resolution-independent, the PREFERRED path) or by raw device
PIXEL coordinate (coordinate_tap / coordinate_swipe — the fallback). This module is the
seam between the two:

  * ``CoordinateAdapter`` / ``GeminiCoordinateAdapter`` — provider-space → device-pixel
    denormalization (mirrors the 0-999 math in ``Orchestrator/adb/commands.py``; the small
    seam M7 extends with Anthropic/OpenAI absolute-pixel adapters).
  * ``snap_to_element`` — the hybrid-grounding core: denormalize a model coordinate to
    device pixels, find the nearest actionable a11y node whose ``bounds`` contain/are
    closest to the point (preferring a node with a stable ``resource_id``), and emit an
    ``element_click`` / ``element_set_text`` action frame. Falls back to a
    ``coordinate_tap`` frame ONLY when no node matches AND the device advertises
    ``supportsCoordinateGesture`` (XR reports false → element-only, no coordinate fallback).

Everything here is PURE (no network, no model, no device) so it is fully unit-testable.
The action frames returned carry ONLY the ``action.json`` variant fields (``type`` +
payload); the loop stamps ``msg`` / ``task_id`` / ``operator`` before it hits the wire.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# Default screen size used ONLY as a last resort when neither a screenshot nor the
# a11y tree bounds reveal the device's real resolution. Matches ADBCommands' default.
DEFAULT_DEVICE_WH: Tuple[int, int] = (1080, 2400)


# ── Coordinate adapter seam (M7 extends with Anthropic/OpenAI absolute-px) ───────────
class CoordinateAdapter:
    """Provider coordinate space → device pixel adapter (the seam).

    M2 ships only :class:`GeminiCoordinateAdapter` (normalized 0-999). M7 adds Anthropic
    (absolute-px with ≤1568/2576 downscale+rescale) and OpenAI (absolute-px) subclasses,
    selected off the CU backend regex. Keeping the denormalization behind this one method
    means the loop + grounding never hardcode a provider's coordinate convention.
    """

    #: Name of the provider whose coordinate convention this adapter speaks.
    provider: str = "base"

    def to_device_px(self, x, y, width: int, height: int) -> Tuple[int, int]:
        raise NotImplementedError


class GeminiCoordinateAdapter(CoordinateAdapter):
    """Gemini normalized 0-999 → device pixels.

    ``real = int(coord / 999 * dimension)`` — identical to
    ``ADBCommands.denormalize_coords`` so the frontier path lands on the same pixel the
    legacy ADB path would. Coordinates are clamped to [0, 999] so a stray out-of-range
    value from the model can never index off-screen.
    """

    provider = "gemini"
    COORD_MAX = 999

    def to_device_px(self, x, y, width: int, height: int) -> Tuple[int, int]:
        cx = min(max(int(round(float(x))), 0), self.COORD_MAX)
        cy = min(max(int(round(float(y))), 0), self.COORD_MAX)
        return (int(cx / self.COORD_MAX * width), int(cy / self.COORD_MAX * height))


def get_coordinate_adapter(provider: str) -> CoordinateAdapter:
    """Factory: a coordinate adapter for ``provider``. M2 only knows Gemini; unknown
    providers fall back to the Gemini (0-999) adapter with a documented default so the
    loop never hard-fails on an unrecognized provider (M7 registers the rest)."""
    p = (provider or "").strip().lower()
    if p in ("gemini", "google", ""):
        return GeminiCoordinateAdapter()
    # M7: register anthropic / openai adapters here. Until then, default to Gemini's
    # normalized space rather than raising — the loop stays resilient.
    return GeminiCoordinateAdapter()


# ── Bounds parsing + device-dimension derivation ─────────────────────────────────────
def parse_bounds(bounds: Optional[str]) -> Optional[Tuple[int, int, int, int]]:
    """Parse a ``UiNode.bounds`` string 'left,top,right,bottom' → (l, t, r, b) ints.

    Returns ``None`` on any malformed / missing value (never raises) so callers degrade
    gracefully. Matches the ``ui_node.json`` bounds pattern (four signed integers).
    """
    if not bounds or not isinstance(bounds, str):
        return None
    parts = bounds.strip().split(",")
    if len(parts) != 4:
        return None
    try:
        l, t, r, b = (int(p) for p in parts)
    except (ValueError, TypeError):
        return None
    return (l, t, r, b)


def _png_dimensions(png_bytes: bytes) -> Optional[Tuple[int, int]]:
    """Read (width, height) from a PNG's IHDR without PIL. ``None`` if not a valid PNG."""
    if len(png_bytes) < 24:
        return None
    if png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if png_bytes[12:16] != b"IHDR":
        return None
    width = int.from_bytes(png_bytes[16:20], "big")
    height = int.from_bytes(png_bytes[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return (width, height)


def derive_device_dimensions(observation: Dict,
                             default: Tuple[int, int] = DEFAULT_DEVICE_WH) -> Tuple[int, int]:
    """Best-effort device screen (width, height) in pixels from an observation.

    The wire ``device_capability`` carries NO explicit resolution, so we derive it, in
    priority order:
      1. the screenshot PNG's real dimensions (exact, when a screenshot is present);
      2. the max right / max bottom across the a11y tree bounds (a full-width bar / root
         element spans the screen — a good approximation from actionable nodes alone);
      3. the ``default`` (1080×2400) as a last resort.
    Never raises.
    """
    shot = observation.get("screenshot")
    if shot:
        try:
            raw = base64.b64decode(shot, validate=False)
            dims = _png_dimensions(raw)
            if dims:
                return dims
        except (binascii.Error, ValueError, TypeError):
            pass

    max_r = max_b = 0
    for node in observation.get("ui_tree") or []:
        b = parse_bounds(node.get("bounds") if isinstance(node, dict) else None)
        if b:
            max_r = max(max_r, b[2])
            max_b = max(max_b, b[3])
    if max_r > 0 and max_b > 0:
        return (max_r, max_b)
    return default


# ── Node targeting ───────────────────────────────────────────────────────────────────
def _rect_area(b: Tuple[int, int, int, int]) -> int:
    return max(1, b[2] - b[0]) * max(1, b[3] - b[1])


def _rect_dist_sq(b: Tuple[int, int, int, int], px: int, py: int) -> int:
    """Squared distance from point (px,py) to rect b (0 if inside)."""
    dx = max(b[0] - px, 0, px - b[2])
    dy = max(b[1] - py, 0, py - b[3])
    return dx * dx + dy * dy


def find_target_node(px: int, py: int, tree: List[Dict],
                     prefer_editable: bool = False) -> Optional[Dict]:
    """The actionable a11y node a device-pixel point (px,py) should resolve to.

    Strategy (deterministic):
      1. Among nodes whose bounds CONTAIN the point, prefer the wanted kind (editable for a
         type, clickable for a tap); among those pick the SMALLEST (most specific), with a
         ``resource_id`` node winning ties.
      2. If no node contains the point, pick the NEAREST node (by squared distance to its
         rect), same wanted-kind + resource_id preference.
    Returns ``None`` only for an empty / all-malformed tree → the caller falls back to a
    raw coordinate (or reports no groundable target on a coordinate-less device).
    """
    candidates: List[Tuple[Dict, Tuple[int, int, int, int]]] = []
    for node in tree or []:
        if not isinstance(node, dict):
            continue
        b = parse_bounds(node.get("bounds"))
        if b:
            candidates.append((node, b))
    if not candidates:
        return None

    def wanted(n: Dict) -> bool:
        return bool(n.get("editable")) if prefer_editable else bool(n.get("clickable"))

    containing = [(n, b) for n, b in candidates if b[0] <= px <= b[2] and b[1] <= py <= b[3]]
    if containing:
        wanted_pool = [(n, b) for n, b in containing if wanted(n)]
        pool = wanted_pool or containing
        best = min(pool, key=lambda nb: (_rect_area(nb[1]), 0 if nb[0].get("resource_id") else 1))
        return best[0]

    # Nothing contains the point → nearest actionable node.
    wanted_pool = [(n, b) for n, b in candidates if wanted(n)]
    pool = wanted_pool or candidates
    best = min(pool, key=lambda nb: (_rect_dist_sq(nb[1], px, py),
                                     0 if nb[0].get("resource_id") else 1))
    return best[0]


def _element_ref(node: Dict) -> Dict:
    """Address a node by its STABLE resource_id when present, else its positional node_id."""
    rid = node.get("resource_id")
    if rid:
        return {"resource_id": rid}
    return {"node_id": int(node.get("node_id", 0))}


@dataclass
class GroundedAction:
    """A grounded action frame + how it was grounded (for logging/telemetry)."""
    frame: Optional[Dict]          # action.json variant fields, or None if ungroundable
    method: str                    # "element" | "coordinate" | "none"
    node_id: Optional[int] = None
    resource_id: Optional[str] = None


def snap_to_element(coord_0_999: Tuple[int, int],
                    observation_tree: List[Dict],
                    device_wh: Tuple[int, int],
                    *,
                    editable: bool = False,
                    text: Optional[str] = None,
                    supports_coordinate: bool = True,
                    adapter: Optional[CoordinateAdapter] = None) -> GroundedAction:
    """Hybrid-ground a provider coordinate to an action frame (decision #2, element-preferred).

    Denormalize ``coord_0_999`` → device pixels via ``adapter`` (default Gemini 0-999),
    find the nearest actionable node, and emit:
      * ``element_set_text`` (when ``editable``, addressing an editable node) — the caller
        supplies ``text``; on a password field the DEVICE discards it (credential handoff);
      * ``element_click`` (a tap) addressing a clickable node;
      * ``coordinate_tap`` at the denormalized pixel — the fallback used ONLY when no node
        is found AND ``supports_coordinate`` (a tap; there is no coordinate "type" on the
        wire, so an editable target with no node is ungroundable → ``frame=None``).

    Returns a :class:`GroundedAction`. ``frame=None`` means "nothing to actuate" (empty
    tree on a coordinate-less device, or a text target with no node) — the loop reports it
    back to the model and re-observes rather than crashing.
    """
    adapter = adapter or GeminiCoordinateAdapter()
    width, height = device_wh
    px, py = adapter.to_device_px(coord_0_999[0], coord_0_999[1], width, height)

    node = find_target_node(px, py, observation_tree, prefer_editable=editable)
    if node is not None:
        ref = _element_ref(node)
        if editable:
            frame = {"type": "element_set_text", **ref, "text": text or ""}
        else:
            frame = {"type": "element_click", **ref}
        return GroundedAction(frame=frame, method="element",
                              node_id=node.get("node_id"),
                              resource_id=node.get("resource_id") or None)

    # No a11y node matched (tree-blind). Fall back to a raw coordinate tap ONLY when the
    # device can actuate by coordinate (XR reports supportsCoordinateGesture=false → no
    # fallback: element-only). A text target has no coordinate equivalent on the wire.
    if not editable and supports_coordinate:
        return GroundedAction(frame={"type": "coordinate_tap", "x": px, "y": py},
                              method="coordinate")
    return GroundedAction(frame=None, method="none")


def snap_swipe_to_coordinate(start_0_999: Tuple[int, int],
                             end_0_999: Tuple[int, int],
                             device_wh: Tuple[int, int],
                             *,
                             duration_ms: Optional[int] = None,
                             supports_coordinate: bool = True,
                             adapter: Optional[CoordinateAdapter] = None) -> GroundedAction:
    """Ground a drag/long-press (two provider coords) to a ``coordinate_swipe`` frame.

    Swipes/drags/long-presses are inherently positional (no semantic-node equivalent), so
    they always denormalize to device pixels — gated on ``supports_coordinate`` (XR: no
    coordinate gesture → ``frame=None``, skipped/logged). A long-press is a zero-distance
    swipe with a long duration (matches ``input swipe x y x y 1000`` semantics).
    """
    if not supports_coordinate:
        return GroundedAction(frame=None, method="none")
    adapter = adapter or GeminiCoordinateAdapter()
    width, height = device_wh
    x1, y1 = adapter.to_device_px(start_0_999[0], start_0_999[1], width, height)
    x2, y2 = adapter.to_device_px(end_0_999[0], end_0_999[1], width, height)
    frame: Dict = {"type": "coordinate_swipe", "x": x1, "y": y1, "x2": x2, "y2": y2}
    if duration_ms:
        frame["duration_ms"] = int(duration_ms)
    return GroundedAction(frame=frame, method="coordinate")
