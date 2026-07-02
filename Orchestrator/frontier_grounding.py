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


# ── Coordinate adapter seam (M7: Gemini 0-999 + Anthropic/OpenAI absolute-px) ────────
class CoordinateAdapter:
    """Provider coordinate space → device pixel adapter (the seam).

    Two responsibilities, split by provider convention:

    * ``to_device_px`` — map a model-returned coordinate into REAL device pixels
      (Gemini: normalized 0-999; Anthropic/OpenAI: absolute pixels in the DOWNSCALED
      image the model was shown).
    * ``model_view_dims`` / ``prepare_screenshot`` — for the vision-first providers,
      DOWNSCALE the device screenshot to the model's max long-edge BEFORE the model
      sees it (M7 task 7.1a), and report the pixel dimensions the model reasons in.
      Gemini reasons in 0-999 regardless of image size, so its screenshot passes
      through untouched (the base defaults are the Gemini behaviour).

    Keeping every provider's convention behind this one class means the loop + the
    hybrid grounding never hardcode a coordinate space or a downscale rule.
    """

    #: Name of the provider whose coordinate convention this adapter speaks.
    provider: str = "base"

    def to_device_px(self, x, y, width: int, height: int) -> Tuple[int, int]:
        raise NotImplementedError

    def to_model_px(self, x, y, width: int, height: int) -> Tuple[int, int]:
        """Inverse of ``to_device_px`` into the MODEL-VIEW space: map a DEVICE-pixel point
        into the pixel space the model reasons in. Default (Gemini): identity — the model-view
        equals the device-pixel space for bounds purposes (Gemini reads element bounds in device
        px, coordinates in 0-999). The abs-px adapters override this to apply their downscale
        factor, so the a11y ``bounds`` the model reads are in the SAME space as the downscaled
        screenshot it sees (M7-I1)."""
        return (int(round(float(x))), int(round(float(y))))

    def model_view_dims(self, width: int, height: int) -> Tuple[int, int]:
        """The (w, h) in pixels the model reasons in for a device of (width, height).
        Default: the device dims unchanged (Gemini — no downscale)."""
        return (int(width), int(height))

    def prepare_screenshot(self, png_bytes: Optional[bytes],
                           width: int, height: int) -> Optional[bytes]:
        """Return the screenshot bytes to hand the model. Default: unchanged (Gemini)."""
        return png_bytes


class GeminiCoordinateAdapter(CoordinateAdapter):
    """Gemini normalized 0-999 → device pixels.

    ``real = int(coord / 999 * dimension)`` — identical to
    ``ADBCommands.denormalize_coords`` so the frontier path lands on the same pixel the
    legacy ADB path would. Coordinates are clamped to [0, 999] so a stray out-of-range
    value from the model can never index off-screen. ``model_view_dims`` /
    ``prepare_screenshot`` inherit the pass-through defaults — Gemini reasons in 0-999
    regardless of the screenshot resolution, so no downscale is applied.
    """

    provider = "gemini"
    COORD_MAX = 999

    def to_device_px(self, x, y, width: int, height: int) -> Tuple[int, int]:
        cx = min(max(int(round(float(x))), 0), self.COORD_MAX)
        cy = min(max(int(round(float(y))), 0), self.COORD_MAX)
        return (int(cx / self.COORD_MAX * width), int(cy / self.COORD_MAX * height))


class AbsolutePixelCoordinateAdapter(CoordinateAdapter):
    """Absolute-pixel adapter for the vision-first providers (Anthropic, OpenAI).

    The model is shown a screenshot DOWNSCALED so its long edge is ≤ ``max_long_edge``
    (never upscaled) and returns coordinates in THAT downscaled pixel space. One scale
    factor ``s = min(1, max_long_edge / max(w, h))`` drives both directions:

    * the screenshot is resized to ``(round(w·s), round(h·s))`` before the model sees it;
    * a returned coordinate ``(dx, dy)`` maps back to device px as ``(dx/s, dy/s)``,
      clamped on-screen.

    Round-trips are exact within ~1px (integer rounding). When the device already fits
    the cap (``s == 1``) the screenshot is passed through and coordinates are identity.
    """

    provider = "abs_px"

    def __init__(self, max_long_edge: int):
        self.max_long_edge = int(max_long_edge)

    def _scale(self, width: int, height: int) -> float:
        long_edge = max(int(width), int(height), 1)
        return min(1.0, self.max_long_edge / long_edge)

    def model_view_dims(self, width: int, height: int) -> Tuple[int, int]:
        s = self._scale(width, height)
        return (max(1, int(round(width * s))), max(1, int(round(height * s))))

    def to_device_px(self, x, y, width: int, height: int) -> Tuple[int, int]:
        s = self._scale(width, height) or 1.0
        dx = int(round(float(x) / s))
        dy = int(round(float(y) / s))
        return (min(max(dx, 0), int(width)), min(max(dy, 0), int(height)))

    def to_model_px(self, x, y, width: int, height: int) -> Tuple[int, int]:
        """Map a DEVICE-pixel point into the DOWNSCALED model-view space (the inverse of
        ``to_device_px``): ``(round(x·s), round(y·s))``. Used to render the a11y ``bounds`` the
        model reads in the SAME pixel space as the downscaled screenshot it is shown, so a model
        that clicks from a bound emits a coordinate that rescales back on-screen (M7-I1)."""
        s = self._scale(int(width), int(height))
        return (int(round(float(x) * s)), int(round(float(y) * s)))

    def prepare_screenshot(self, png_bytes: Optional[bytes],
                           width: int, height: int) -> Optional[bytes]:
        """Downscale ``png_bytes`` to ``model_view_dims`` (long-edge ≤ max). No-op when
        the image already fits the cap, or when PIL is unavailable / the bytes don't
        decode (returns them unchanged — never raises; correctness degrades to sending a
        larger image, which the API itself would downscale)."""
        if not png_bytes:
            return png_bytes
        if self._scale(width, height) >= 1.0:
            return png_bytes
        try:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
            tw, th = self.model_view_dims(width, height)
            img = img.resize((tw, th))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            return png_bytes


# Anthropic vision downscale caps (long edge, px): 1568 for the computer_20251124 tool on
# Claude 4.x; 2576 on the high-resolution vision models (Opus/Sonnet/Fable ≥ 4.7).
ANTHROPIC_MAX_LONG_EDGE = 1568
ANTHROPIC_HIRES_MAX_LONG_EDGE = 2576
# OpenAI's `computer` tool follows the pixel space of the screenshot we send; a ~1280
# long-edge (720p-class) balances coordinate accuracy against per-image token cost.
OPENAI_MAX_LONG_EDGE = 1280


def _anthropic_is_hires(model) -> bool:
    """Claude models with high-resolution vision (Opus/Sonnet/Fable/Mythos ≥ 4.7) accept up
    to 2576px on the long edge; older CU models (e.g. Opus 4.6) cap at 1568."""
    import re
    m = (model or "").strip().lower()
    match = re.match(r"claude-(?:opus|sonnet|fable|mythos)-(\d+)(?:-(\d+))?", m)
    if not match:
        return False
    major = int(match.group(1))
    minor = int(match.group(2)) if match.group(2) else 0
    return (major, minor) >= (4, 7)


class AnthropicCoordinateAdapter(AbsolutePixelCoordinateAdapter):
    """Anthropic absolute-px adapter. Long-edge cap is model-aware (1568, or 2576 for the
    high-resolution ≥4.7 models). The device screenshot is downscaled to that cap before
    Claude sees it, and Claude's returned pixel coordinate is rescaled back to device px."""

    provider = "anthropic"

    def __init__(self, model=None):
        super().__init__(ANTHROPIC_HIRES_MAX_LONG_EDGE if _anthropic_is_hires(model)
                         else ANTHROPIC_MAX_LONG_EDGE)
        self.model = model


class OpenAICoordinateAdapter(AbsolutePixelCoordinateAdapter):
    """OpenAI absolute-px adapter. The `computer` tool reasons in the pixel space of the
    screenshot we send, so we downscale to a ~1280 long edge and rescale coordinates back."""

    provider = "openai"

    def __init__(self, model=None):
        super().__init__(OPENAI_MAX_LONG_EDGE)
        self.model = model


def _provider_from_model(model) -> Optional[str]:
    """Map a model id → CU backend name (anthropic / google / openai) via the config
    ``CU_MODEL_FILTERS`` regex — the SAME data that gates the CU model catalog, so the
    adapter factory and the backend gate never drift. Returns ``None`` on no match."""
    import re
    try:
        from Orchestrator.config import CU_MODEL_FILTERS
        filters = CU_MODEL_FILTERS
    except Exception:
        filters = {
            "anthropic": r"claude-(opus|sonnet|fable|mythos)-([4-9]|\d{2,})",
            "google": r"gemini-.*computer-use",
            "openai": r"(computer-use-preview|gpt-5\.5($|-\d))",
        }
    m = (model or "").strip()
    for backend, pattern in filters.items():
        try:
            if re.match(pattern, m):
                return backend
        except re.error:
            continue
    return None


def get_coordinate_adapter(provider: str, model: Optional[str] = None) -> CoordinateAdapter:
    """Factory: a coordinate adapter for ``provider`` (a provider NAME, or a MODEL id whose
    backend is inferred from ``config.CU_MODEL_FILTERS``). Gemini → 0-999; Anthropic /
    OpenAI → absolute-px (downscale+rescale). ``gemma`` runs on-device (grounding unused) →
    the normalized adapter. An unrecognised value defaults to Gemini so the loop never
    hard-fails on an unexpected provider/model."""
    key = (provider or "").strip().lower()
    if key in ("gemini", "google", "gemma", ""):
        return GeminiCoordinateAdapter()
    if key in ("anthropic", "claude"):
        return AnthropicCoordinateAdapter(model or provider)
    if key in ("openai", "gpt"):
        return OpenAICoordinateAdapter(model or provider)
    # Not a known provider name → treat it as a model id and infer the backend.
    backend = _provider_from_model(provider)
    if backend == "anthropic":
        return AnthropicCoordinateAdapter(provider)
    if backend == "openai":
        return OpenAICoordinateAdapter(provider)
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


def scale_bounds_to_model_view(bounds, adapter: Optional[CoordinateAdapter],
                               device_wh: Tuple[int, int]):
    """Render an a11y ``bounds`` string in the adapter's MODEL-VIEW pixel space (M7-I1).

    For a downscale (abs-px) adapter the model is shown a screenshot downscaled by a factor ``s``
    and emits coordinates in THAT space; the element bounds it reads must be in the same space, or
    a model clicking from a bound emits a full-res coordinate that rescales (÷s) off-screen and
    snaps to the wrong node. Scales each corner via ``adapter.to_model_px``. Identity for the
    Gemini adapter (model-view == device px) and a no-op for ``adapter is None`` or a malformed
    bounds string. PURE — never mutates the node, never raises. Callers use the returned string
    ONLY in the model-facing tree text; the internal grounding keeps snapping against the
    untouched full-res observation bounds.
    """
    if adapter is None:
        return bounds
    parsed = parse_bounds(bounds)
    if parsed is None:
        return bounds
    w, h = device_wh
    l, t, r, b = parsed
    ml, mt = adapter.to_model_px(l, t, w, h)
    mr, mb = adapter.to_model_px(r, b, w, h)
    return f"{ml},{mt},{mr},{mb}"


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
        # M7-M2: a type (``editable``) whose resolved node is NOT an editable field must not
        # element_set_text the wrong element. This happens when the model types with no prior
        # click: the abs-px `type` action carries no coordinate, so it reuses a (0,0) last-click
        # that snaps to the root container. Return ungroundable so the loop feeds it back and the
        # model clicks a field first — safer than dumping text into the root (no accidental type).
        if editable and not node.get("editable"):
            return GroundedAction(frame=None, method="none")
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
