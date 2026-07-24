"""Guard: onboarding.css narrow-viewport grid-area rules stay SCOPED.

Regression (2026-07-23, Brandon's Fold): the @media (max-width: 720px) block
assigned `grid-area: input` to bare `.ob-provider-input`. That class is also
used by the custom-server wizard cards, whose fields live in plain
single-column grids with NO named areas — so at phone width every input in a
card collapsed into one implicit grid cell. Only the last input in DOM order
(the type=number context-tokens field) stayed visible/tappable, which
presented as "the Base URL field only accepts a few numbers" in the Android
in-app wizard WebView.

The rule set was written for .ob-provider-input-row (provider key cards:
input | reveal-eye | validate). This test asserts every grid-area assignment
inside ANY media query is scoped under .ob-provider-input-row, so a future
responsive pass cannot re-plant the same landmine.
"""

import re
from pathlib import Path

CSS_PATH = (
    Path(__file__).resolve().parents[2] / "Portal" / "onboarding" / "onboarding.css"
)


def _media_blocks(css: str):
    """Yield (header, body) for every top-level @media block."""
    for m in re.finditer(r"@media[^{]*\{", css):
        depth = 1
        i = m.end()
        while i < len(css) and depth:
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
            i += 1
        yield css[m.start(): m.end() - 1], css[m.end(): i - 1]


def _unscoped_grid_area_selectors(css: str):
    """Selectors inside @media blocks that set grid-area without the
    .ob-provider-input-row ancestor scope."""
    offenders = []
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    for _header, body in _media_blocks(css):
        for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", body):
            selector, decls = rule.group(1).strip(), rule.group(2)
            if "grid-area" not in decls:
                continue
            # Every comma-alternative of the selector must carry the scope.
            for alt in selector.split(","):
                alt = alt.strip()
                if alt and ".ob-provider-input-row" not in alt:
                    offenders.append(alt)
    return offenders


def test_media_query_grid_area_rules_are_scoped():
    css = CSS_PATH.read_text(encoding="utf-8")
    offenders = _unscoped_grid_area_selectors(css)
    assert not offenders, (
        "onboarding.css assigns grid-area to unscoped selectors inside a "
        f"media query: {offenders}. Scope them under .ob-provider-input-row "
        "— bare .ob-provider-input/.ob-validate-btn rules stack the "
        "custom-server card inputs into one grid cell at phone width "
        "(see module docstring)."
    )


def test_guard_catches_the_original_bug():
    """Self-check: the pre-fix CSS shape must be flagged."""
    bad = (
        "@media (max-width: 720px) {\n"
        "    .ob-provider-input { grid-area: input; }\n"
        "}\n"
    )
    assert _unscoped_grid_area_selectors(bad) == [".ob-provider-input"]
