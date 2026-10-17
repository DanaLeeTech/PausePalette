"""Offline colour naming and luminance.

Filament names are looked up with a nearest-colour table (no API). Luminance is
used by the planner's dark-to-light heuristic.
"""

from __future__ import annotations

import re

# A small table of filament-common colour names with representative sRGB values.
# Nearest-colour lookup means these do not have to be exact.
NAMED_COLOURS: list[tuple[str, str]] = [
    ("Black", "#000000"),
    ("Dark grey", "#404040"),
    ("Charcoal", "#333333"),
    ("Grey", "#808080"),
    ("Light grey", "#D3D3D3"),
    ("Silver", "#C0C0C0"),
    ("White", "#FFFFFF"),
    ("Ivory", "#FFFFF0"),
    ("Cream", "#FFFDD0"),
    ("Beige", "#F5F5DC"),
    ("Peach", "#FFDAB9"),
    ("Tan", "#D2B48C"),
    ("Brown", "#8B4513"),
    ("Dark brown", "#5C3317"),
    ("Chocolate", "#D2691E"),
    ("Gold", "#FFD700"),
    ("Yellow", "#FFFF00"),
    ("Neon yellow", "#DFFF00"),
    ("Lime", "#BFFF00"),
    ("Neon green", "#39FF14"),
    ("Green", "#008000"),
    ("Forest green", "#228B22"),
    ("Olive", "#808000"),
    ("Teal", "#008080"),
    ("Cyan", "#00FFFF"),
    ("Light blue", "#ADD8E6"),
    ("Sky blue", "#87CEEB"),
    ("Blue", "#0000FF"),
    ("Navy", "#000080"),
    ("Indigo", "#4B0082"),
    ("Purple", "#800080"),
    ("Violet", "#EE82EE"),
    ("Magenta", "#FF00FF"),
    ("Neon pink", "#FF6EC7"),
    ("Pink", "#FFC0CB"),
    ("Hot pink", "#FF69B4"),
    ("Red", "#FF0000"),
    ("Dark red", "#8B0000"),
    ("Crimson", "#DC143C"),
    ("Orange", "#FFA500"),
    ("Dark orange", "#FF8C00"),
    ("Coral", "#FF7F50"),
    ("Salmon", "#FA8072"),
    ("Maroon", "#800000"),
    ("Turquoise", "#40E0D0"),
]

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
_HEX_RE_SHORT = re.compile(r"^#?([0-9a-fA-F]{3})$")


def normalise_hex(value: str) -> str:
    """Return an upper-case ``#RRGGBB`` string, or raise ValueError."""
    v = (value or "").strip()
    m = _HEX_RE.match(v)
    if m:
        return "#" + m.group(1).upper()
    m = _HEX_RE_SHORT.match(v)
    if m:
        r, g, b = m.group(1)
        return ("#" + r + r + g + g + b + b).upper()
    raise ValueError(f"not a hex colour: {value!r}")


def rgb(value: str) -> tuple[int, int, int]:
    h = normalise_hex(value)[1:]
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _srgb_channel_to_linear(c: int) -> float:
    s = c / 255.0
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4


def luminance(value: str) -> float:
    """WCAG relative luminance, 0.0 (black) .. 1.0 (white)."""
    r, g, b = rgb(value)
    return (
        0.2126 * _srgb_channel_to_linear(r)
        + 0.7152 * _srgb_channel_to_linear(g)
        + 0.0722 * _srgb_channel_to_linear(b)
    )


def name_for_hex(value: str) -> str:
    """Nearest filament name for a hex colour (deterministic, offline)."""
    try:
        r, g, b = rgb(value)
    except ValueError:
        return (value or "unknown").strip()
    best_name = "Unknown"
    best_dist = None
    for name, hexval in NAMED_COLOURS:
        nr, ng, nb = rgb(hexval)
        # Weighted RGB distance approximating perceptual difference.
        d = 2 * (r - nr) ** 2 + 4 * (g - ng) ** 2 + 3 * (b - nb) ** 2
        if best_dist is None or d < best_dist:
            best_dist = d
            best_name = name
    return best_name
