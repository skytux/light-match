"""The shared reference space ``lm-ref-1``.

Profiles in the community database are calibrated against one common target,
so a profile made in one house works in another. ``lm-ref-1`` defines the
target of every grid cell absolutely:

* colour cells: the fully saturated sRGB colour of the anchor hue (D65 white
  point), as CIE 1931 xy, pulled into Philips Hue gamut C where it lies just
  outside it (only blue);
* white cells: the Planckian locus at the cell's colour temperature, as xy;
* brightness: the cell's percentage is perceived lightness, so the relative
  light output is ``Y / Y_max = luminance(L* = percentage)`` of the lamp's own
  maximum. No absolute lumens: a brighter lamp is not dimmed to match a
  weaker one.

Every target lies inside Hue gamut C, so a Hue colour lamp with gamut C
(White and Color Ambiance, model ids in the ``signify/hue-gamut-c`` profile)
sent ``xy_color`` and its HA brightness shows ``lm-ref-1`` closely enough to
be the by-eye stand-in. Profiles calibrated against any other lamp use the
target ``house``: they work in that house but are not portable.
"""

from __future__ import annotations

from typing import Any

from homeassistant.util import color as color_util

from .const import COLOR_ANCHORS
from .database import NAMED_GAMUTS, TARGET_HOUSE, TARGET_REFERENCE
from .profile import KIND_COLOR, Target, cell_key, cell_target, luminance

REFERENCE_ID = TARGET_REFERENCE
REFERENCE_GAMUT = color_util.GamutType(
    *(color_util.XYPoint(*p) for p in NAMED_GAMUTS["C"])
)
__all__ = ["REFERENCE_ID", "TARGET_HOUSE", "planck_xy", "reference_command", "target_of"]


def planck_xy(kelvin: float) -> tuple[float, float]:
    """xy of the Planckian locus (Kim et al. cubic approximation, 1667-25000 K)."""
    t = min(max(float(kelvin), 1667.0), 25000.0)
    if t <= 4000:
        x = -0.2661239e9 / t**3 - 0.2343589e6 / t**2 + 0.8776956e3 / t + 0.179910
    else:
        x = -3.0258469e9 / t**3 + 2.1070379e6 / t**2 + 0.2226347e3 / t + 0.240390
    if t <= 2222:
        y = -1.1063814 * x**3 - 1.34811020 * x**2 + 2.18555832 * x - 0.20219683
    elif t <= 4000:
        y = -0.9549476 * x**3 - 1.37418593 * x**2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x**3 - 5.87338670 * x**2 + 3.75112997 * x - 0.37001483
    return round(x, 4), round(y, 4)


def _srgb_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def srgb_xy(r: float, g: float, b: float) -> tuple[float, float]:
    """CIE 1931 xy of an sRGB colour (0-255 channels, IEC 61966-2-1, D65)."""
    lr, lg, lb = (_srgb_linear(c / 255) for c in (r, g, b))
    x = 0.4124 * lr + 0.3576 * lg + 0.1805 * lb
    y = 0.2126 * lr + 0.7152 * lg + 0.0722 * lb
    z = 0.0193 * lr + 0.1192 * lg + 0.9505 * lb
    total = x + y + z
    return (x / total, y / total) if total else (0.3127, 0.3290)


def hue_xy(hue: float, sat: float = 100.0) -> tuple[float, float]:
    """xy of an sRGB colour of full value, inside the reference gamut."""
    x, y = srgb_xy(*color_util.color_hs_to_RGB(hue, sat))
    if not color_util.check_point_in_lamps_reach((x, y), REFERENCE_GAMUT):
        x, y = color_util.get_closest_point_to_point((x, y), REFERENCE_GAMUT)
        # a hair towards white so the rounded point stays inside the gamut
        x, y = x + (0.3127 - x) * 0.002, y + (0.3290 - y) * 0.002
    return round(x, 4), round(y, 4)


def target_xy(target: Target) -> tuple[float, float]:
    """The chromaticity of a target look."""
    if target.kind == KIND_COLOR:
        return hue_xy(target.hue or 0.0, target.sat if target.sat is not None else 100.0)
    return planck_xy(target.kelvin or 2700)


def relative_output(percentage: float) -> float:
    """Relative light output (0-1) of a brightness percentage."""
    return round(luminance(max(0.0, min(100.0, percentage))), 6)


def target_of(key: str) -> dict[str, Any]:
    """The absolute target of a grid cell."""
    target = cell_target(key)
    x, y = target_xy(target)
    return {"key": key, "xy": [x, y], "relative_output": relative_output(target.brightness)}


def reference_command(target: Target) -> dict[str, Any]:
    """The command that makes a gamut C Hue lamp show a target."""
    brightness = max(1, min(255, round(target.brightness * 255 / 100)))
    if target.kind == KIND_COLOR:
        return {"brightness": brightness, "xy_color": list(target_xy(target))}
    return {"brightness": brightness, "color_temp_kelvin": round(target.kelvin or 2700)}


def reference_table() -> list[dict[str, Any]]:
    """Every cell of the full grid with its target (for the documentation)."""
    from .const import BRIGHTNESS_LEVELS, WHITE_KELVINS  # noqa: PLC0415

    keys = [cell_key(KIND_COLOR, name, lvl) for name in COLOR_ANCHORS for lvl in BRIGHTNESS_LEVELS]
    keys += [cell_key("white", k, lvl) for k in WHITE_KELVINS for lvl in BRIGHTNESS_LEVELS]
    return [target_of(k) for k in keys]
