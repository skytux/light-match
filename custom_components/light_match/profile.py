"""Calibration profile math for Light Match.

A profile maps a *target look* (a colour or a white at a brightness, in the
calibrated, uniform space every twin shares) to the *source command* that
makes one particular lamp show that look.

The profile is a grid of cells: colour anchors x brightness levels and white
colour temperatures x brightness levels. A calibrated cell stores the source
command the user matched by eye; an uncalibrated cell falls back to a default
derived from the source's capabilities. Targets between cells are
interpolated: brightness in a perceptual (CIE L*) space, hue along the
colour circle between neighbouring anchors, colour temperature in mireds.

This module holds no Home Assistant state; it only uses the colour helpers
from ``homeassistant.util.color`` so it can be tested on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from homeassistant.util import color as color_util

from .const import (
    BEHAVIOUR_SMART,
    BRIGHTNESS_LEVELS,
    COLOR_ANCHORS,
    PROFILE_FORMAT,
    PROFILE_VERSION,
    WHITE_KELVINS,
)

CHANNEL_MODES: dict[str, int] = {"rgb": 3, "rgbw": 4, "rgbww": 5}
MODE_ATTR: dict[str, str | None] = {
    "rgb": "rgb_color",
    "rgbw": "rgbw_color",
    "rgbww": "rgbww_color",
    "hs": "hs_color",
    "color_temp": "color_temp_kelvin",
    "brightness": None,
}
KIND_COLOR = "color"
KIND_WHITE = "white"
DIM_KELVIN = 2700  # the single white row of a brightness-only lamp
_EPS = 1e-6


# ---------------------------------------------------------------------------
# Source capabilities
# ---------------------------------------------------------------------------


@dataclass
class SourceCaps:
    """What the source light can be told to do, and how it is commanded."""

    color_mode: str | None = None  # rgb, rgbw, rgbww, hs or None
    white_mode: str | None = None  # color_temp, a colour mode, brightness or None
    min_kelvin: int = 2000
    max_kelvin: int = 6500
    floor: int = 0  # lowest brightness (0-255) the lamp really dims to

    @classmethod
    def from_modes(
        cls,
        modes: list[str] | set[str] | None,
        min_kelvin: int | None = None,
        max_kelvin: int | None = None,
        floor: int = 0,
    ) -> SourceCaps:
        """Pick the command modes from a light's supported_color_modes."""
        modes = set(modes or [])
        color_mode: str | None = None
        for candidate in ("rgbw", "rgbww", "rgb"):
            if candidate in modes:
                color_mode = candidate
                break
        if color_mode is None and modes & {"hs", "xy"}:
            color_mode = "hs"
        if "color_temp" in modes:
            white_mode: str | None = "color_temp"
        elif color_mode is not None:
            white_mode = color_mode
        elif modes & {"brightness", "white"}:
            white_mode = "brightness"
        else:
            white_mode = None
        return cls(
            color_mode=color_mode,
            white_mode=white_mode,
            min_kelvin=int(min_kelvin or 2000),
            max_kelvin=int(max_kelvin or 6500),
            floor=max(0, min(255, int(floor or 0))),
        )

    @property
    def white_kelvins(self) -> list[int]:
        """The white rows of the grid for this lamp."""
        if self.white_mode is None:
            return []
        if self.white_mode == "brightness":
            return [DIM_KELVIN]
        return list(WHITE_KELVINS)


@dataclass(frozen=True)
class Target:
    """A look in the calibrated space. Brightness is a percentage."""

    kind: str
    brightness: float
    hue: float = 0.0
    sat: float = 100.0
    kelvin: float = DIM_KELVIN


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------


def cell_key(kind: str, anchor: str | int, level: float) -> str:
    """Build the key of a grid cell, e.g. ``color:red:5`` or ``white:2700:50``."""
    return f"{kind}:{anchor}:{level:g}"


def parse_cell_key(key: str) -> tuple[str, str, float]:
    """Split a cell key into kind, anchor and level; raise ValueError if bad."""
    try:
        kind, anchor, level = key.split(":")
        level_f = float(level)
    except ValueError as err:
        raise ValueError(f"bad cell key {key!r}") from err
    if kind == KIND_COLOR and anchor in COLOR_ANCHORS or kind == KIND_WHITE and anchor.isdigit():
        pass
    else:
        raise ValueError(f"bad cell key {key!r}")
    if level_f not in BRIGHTNESS_LEVELS:
        raise ValueError(f"bad cell key {key!r}")
    return kind, anchor, level_f


def grid_cells(caps: SourceCaps) -> list[dict[str, Any]]:
    """All cells of the calibration grid for a lamp, in calibration order."""
    cells: list[dict[str, Any]] = []
    for kelvin in caps.white_kelvins:
        for level in BRIGHTNESS_LEVELS:
            cells.append(
                {
                    "key": cell_key(KIND_WHITE, kelvin, level),
                    "kind": KIND_WHITE,
                    "name": f"{kelvin} K",
                    "kelvin": kelvin,
                    "brightness": level,
                }
            )
    if caps.color_mode is not None:
        for name, hue in COLOR_ANCHORS.items():
            for level in BRIGHTNESS_LEVELS:
                cells.append(
                    {
                        "key": cell_key(KIND_COLOR, name, level),
                        "kind": KIND_COLOR,
                        "name": name,
                        "hue": hue,
                        "brightness": level,
                    }
                )
    return cells


def cell_target(key: str) -> Target:
    """The target look of a grid cell."""
    kind, anchor, level = parse_cell_key(key)
    if kind == KIND_COLOR:
        return Target(KIND_COLOR, level, hue=COLOR_ANCHORS[anchor], sat=100.0)
    return Target(KIND_WHITE, level, kelvin=float(anchor))


def nominal_command(target: Target) -> dict[str, Any]:
    """The plain Home Assistant command for a target, without any profile."""
    brightness = max(1, min(255, round(target.brightness * 255 / 100)))
    if target.kind == KIND_COLOR:
        return {
            "brightness": brightness,
            "hs_color": [round(target.hue, 2), round(target.sat, 2)],
        }
    return {"brightness": brightness, "color_temp_kelvin": round(target.kelvin)}


# ---------------------------------------------------------------------------
# Perceptual brightness
# ---------------------------------------------------------------------------


def lightness(y: float) -> float:
    """CIE L* (0-100) of a relative luminance y (0-1)."""
    y = max(0.0, y)
    if y > 216 / 24389:
        return 116 * y ** (1 / 3) - 16
    return y * 24389 / 27


def luminance(l_star: float) -> float:
    """Inverse of :func:`lightness`."""
    l_star = max(0.0, l_star)
    if l_star > 8:
        return ((l_star + 16) / 116) ** 3
    return l_star * 27 / 24389


def _mix_level(b0: float, b1: float, t: float) -> float:
    """Blend two brightness values (0-255) at t in perceptual space."""
    if t <= 0:
        return b0
    if t >= 1:
        return b1
    l_mix = lightness(b0 / 255) + (lightness(b1 / 255) - lightness(b0 / 255)) * t
    return luminance(l_mix) * 255


def _perceptual_t(x: float, x0: float, x1: float) -> float:
    """Where x lies between two brightness percentages, perceptually."""
    l0, l1 = lightness(x0 / 100), lightness(x1 / 100)
    if abs(l1 - l0) < _EPS:
        return 0.0
    return (lightness(x / 100) - l0) / (l1 - l0)


# ---------------------------------------------------------------------------
# Points: an interpolatable form of a source command
# ---------------------------------------------------------------------------


@dataclass
class Point:
    """A source command in a form that can be blended.

    ``level`` is the effective brightness (0-255): for channel modes it is
    brightness x strongest channel / 255, so weak channels at the floor count
    as dimmer than the floor. ``color`` is the normalised channel ratios
    (channel modes), (hue, sat) (hs), (mired,) (color_temp) or () (brightness).
    """

    mode: str
    level: float
    color: tuple[float, ...] = field(default_factory=tuple)


def _white_ratios(mode: str) -> tuple[float, ...]:
    if mode == "rgb":
        return (1.0, 1.0, 1.0)
    if mode == "rgbw":
        return (0.0, 0.0, 0.0, 1.0)
    return (0.0, 0.0, 0.0, 1.0, 1.0)


def _normalise(values: tuple[float, ...], mode: str) -> tuple[float, ...]:
    top = max(values) if values else 0.0
    if top <= 0:
        return _white_ratios(mode)
    return tuple(v / top for v in values)


def command_to_point(cmd: dict[str, Any], mode: str) -> Point:
    """Turn a source command into a point."""
    brightness = float(cmd.get("brightness", 255))
    if mode in CHANNEL_MODES:
        channels = tuple(float(c) for c in cmd[MODE_ATTR[mode]])
        top = max(channels)
        if top <= 0:
            return Point(mode, 0.0, _white_ratios(mode))
        return Point(mode, brightness * top / 255, tuple(c / top for c in channels))
    if mode == "hs":
        hue, sat = cmd["hs_color"]
        return Point(mode, brightness, (float(hue) % 360, float(sat)))
    if mode == "color_temp":
        return Point(mode, brightness, (1e6 / float(cmd["color_temp_kelvin"]),))
    return Point(mode, brightness, ())


def point_to_command(point: Point, floor: int = 0, subfloor: bool = True) -> dict[str, Any]:
    """Turn a point into a source command.

    For channel modes with ``subfloor``, a level below the lamp's brightness
    floor is reached by sending the floor brightness with the colour channels
    scaled down: the lamp then shows less light than its floor allows.
    """
    mode = point.mode
    level = max(0.0, min(255.0, point.level))
    if mode in CHANNEL_MODES:
        if subfloor and level < max(floor, 1):
            brightness = max(1, min(255, round(max(level, floor))))
            scale = min(1.0, level / brightness)
        else:
            brightness = max(1, min(255, round(level)))
            scale = 1.0
        channels = [max(0, min(255, round(r * 255 * scale))) for r in point.color]
        if max(channels) == 0:
            strongest = max(range(len(point.color)), key=lambda i: point.color[i])
            channels[strongest] = 1
        return {"brightness": brightness, MODE_ATTR[mode]: channels}
    brightness = max(1, min(255, round(level)))
    if mode == "hs":
        hue, sat = point.color
        return {
            "brightness": brightness,
            "hs_color": [round(hue % 360, 2), round(max(0.0, min(100.0, sat)), 2)],
        }
    if mode == "color_temp":
        return {"brightness": brightness, "color_temp_kelvin": round(1e6 / point.color[0])}
    return {"brightness": brightness}


def _hue_lerp(h0: float, h1: float, t: float) -> float:
    diff = ((h1 - h0 + 180) % 360) - 180
    return (h0 + diff * t) % 360


def mix_points(p0: Point, p1: Point, t: float) -> Point:
    """Blend two points of the same mode at t (0 = p0, 1 = p1)."""
    if t <= 0:
        return p0
    if t >= 1:
        return p1
    level = _mix_level(p0.level, p1.level, t)
    mode = p0.mode
    if mode in CHANNEL_MODES:
        color = _normalise(
            tuple(a + (b - a) * t for a, b in zip(p0.color, p1.color, strict=True)), mode
        )
    elif mode == "hs":
        color = (
            _hue_lerp(p0.color[0], p1.color[0], t),
            p0.color[1] + (p1.color[1] - p0.color[1]) * t,
        )
    elif mode == "color_temp":
        color = (p0.color[0] + (p1.color[0] - p0.color[0]) * t,)
    else:
        color = ()
    return Point(mode, level, color)


def _desaturate(point: Point, sat: float) -> Point:
    """Lower the saturation of a full-saturation colour point to sat (0-100)."""
    if sat >= 100:
        return point
    s = max(0.0, sat) / 100
    if point.mode in CHANNEL_MODES:
        white = _white_ratios(point.mode)
        color = _normalise(
            tuple(c * s + w * (1 - s) for c, w in zip(point.color, white, strict=True)),
            point.mode,
        )
        return Point(point.mode, point.level, color)
    if point.mode == "hs":
        return Point(point.mode, point.level, (point.color[0], point.color[1] * s))
    return point


def point_hs(point: Point) -> tuple[float, float]:
    """Approximate hue and saturation of a point (for inverse mapping)."""
    if point.mode in CHANNEL_MODES:
        rgb = _ratios_to_rgb(point.color, point.mode)
        return color_util.color_RGB_to_hs(*rgb)
    if point.mode == "hs":
        return point.color[0], point.color[1]
    if point.mode == "color_temp":
        rgb = color_util.color_temperature_to_rgb(1e6 / point.color[0])
        return color_util.color_RGB_to_hs(*rgb)
    return 0.0, 0.0


def _ratios_to_rgb(ratios: tuple[float, ...], mode: str) -> tuple[float, float, float]:
    ch = [r * 255 for r in ratios]
    if mode == "rgb":
        return ch[0], ch[1], ch[2]
    if mode == "rgbw":
        return color_util.color_rgbw_to_rgb(*(round(c) for c in ch))
    # rgbww: treat the two white channels as a neutral white
    white = (ch[3] + ch[4]) / 2
    return (
        min(255.0, ch[0] + white),
        min(255.0, ch[1] + white),
        min(255.0, ch[2] + white),
    )


def _rgb_to_channels(rgb: tuple[float, float, float], mode: str, caps: SourceCaps) -> tuple[float, ...]:
    r, g, b = (max(0, min(255, round(c))) for c in rgb)
    if mode == "rgb":
        return (r, g, b)
    if mode == "rgbw":
        return color_util.color_rgb_to_rgbw(r, g, b)
    return color_util.color_rgb_to_rgbww(r, g, b, caps.min_kelvin, caps.max_kelvin)


# ---------------------------------------------------------------------------
# Command validation and conversion
# ---------------------------------------------------------------------------


def validate_command(cmd: Any, mode: str) -> dict[str, Any]:
    """Check a source command for a mode; return a clean copy or raise ValueError."""
    if not isinstance(cmd, dict):
        raise ValueError("command must be an object")
    try:
        brightness = int(cmd["brightness"])
    except (KeyError, TypeError, ValueError) as err:
        raise ValueError("command needs a brightness 1-255") from err
    if not 1 <= brightness <= 255:
        raise ValueError("brightness must be 1-255")
    clean: dict[str, Any] = {"brightness": brightness}
    attr = MODE_ATTR.get(mode)
    if mode not in MODE_ATTR:
        raise ValueError(f"unknown mode {mode!r}")
    if attr is None:
        return clean
    if attr not in cmd:
        raise ValueError(f"command needs {attr}")
    value = cmd[attr]
    if mode in CHANNEL_MODES:
        if not isinstance(value, (list, tuple)) or len(value) != CHANNEL_MODES[mode]:
            raise ValueError(f"{attr} needs {CHANNEL_MODES[mode]} values")
        channels = [int(v) for v in value]
        if any(not 0 <= v <= 255 for v in channels) or max(channels) == 0:
            raise ValueError(f"{attr} values must be 0-255, not all 0")
        clean[attr] = channels
    elif mode == "hs":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError("hs_color needs [hue, saturation]")
        hue, sat = float(value[0]), float(value[1])
        if not 0 <= hue <= 360 or not 0 <= sat <= 100:
            raise ValueError("hue must be 0-360 and saturation 0-100")
        clean[attr] = [hue % 360, sat]
    else:
        kelvin = int(value)
        if not 1000 <= kelvin <= 20000:
            raise ValueError("color_temp_kelvin must be 1000-20000")
        clean[attr] = kelvin
    return clean


def convert_command(cmd: dict[str, Any], src_mode: str, dst_mode: str, caps: SourceCaps) -> dict[str, Any] | None:
    """Convert a command between modes; None if it cannot be expressed."""
    if src_mode == dst_mode:
        return dict(cmd)
    point = command_to_point(cmd, src_mode)
    kelvin: float | None = None
    rgb: tuple[float, float, float] | None
    if src_mode in CHANNEL_MODES:
        rgb = _ratios_to_rgb(point.color, src_mode)
    elif src_mode == "hs":
        rgb = color_util.color_hs_to_RGB(*point.color)
    elif src_mode == "color_temp":
        kelvin = 1e6 / point.color[0]
        rgb = color_util.color_temperature_to_rgb(kelvin)
    else:
        rgb = None
    if dst_mode == "brightness":
        return point_to_command(Point("brightness", point.level), caps.floor)
    if dst_mode == "color_temp":
        if kelvin is None:
            return None
        k = max(caps.min_kelvin, min(caps.max_kelvin, kelvin))
        return point_to_command(Point("color_temp", point.level, (1e6 / k,)), caps.floor)
    if rgb is None:
        return None
    if dst_mode == "hs":
        hue, sat = color_util.color_RGB_to_hs(*rgb)
        return point_to_command(Point("hs", point.level, (hue, sat)), caps.floor)
    channels = _normalise(tuple(float(c) for c in _rgb_to_channels(rgb, dst_mode, caps)), dst_mode)
    return point_to_command(Point(dst_mode, point.level, channels), caps.floor)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class Profile:
    """A lamp's calibration: calibrated cells plus defaults for the rest."""

    def __init__(
        self,
        caps: SourceCaps,
        cells: dict[str, dict[str, Any]] | None = None,
        fallback: str = BEHAVIOUR_SMART,
        identity: bool = False,
    ) -> None:
        """Create a profile for a lamp with these capabilities."""
        self.caps = caps
        self.cells: dict[str, dict[str, Any]] = dict(cells or {})
        self.fallback = fallback
        self.identity = identity

    # -- cells ---------------------------------------------------------------

    def mode_for(self, kind: str) -> str | None:
        """The command mode used for colour or white cells."""
        return self.caps.color_mode if kind == KIND_COLOR else self.caps.white_mode

    def valid_keys(self) -> set[str]:
        """Keys of the cells this lamp's grid has."""
        return {cell["key"] for cell in grid_cells(self.caps)}

    def set_cell(self, key: str, cmd: dict[str, Any]) -> dict[str, Any]:
        """Store a calibrated cell; validates the key and the command."""
        if key not in self.valid_keys():
            raise ValueError(f"this lamp has no cell {key!r}")
        kind = parse_cell_key(key)[0]
        mode = self.mode_for(kind)
        assert mode is not None
        clean = validate_command(cmd, mode)
        self.cells[key] = clean
        return clean

    def reset_cell(self, key: str) -> bool:
        """Forget a calibrated cell; True if there was one."""
        return self.cells.pop(key, None) is not None

    def default_command(self, kind: str, anchor: float, level: float) -> dict[str, Any]:
        """The fallback command for a target colour hue / white kelvin at a level."""
        return point_to_command(
            self._default_point(kind, anchor, level), self.caps.floor, self._subfloor
        )

    def cell_command(self, key: str) -> dict[str, Any]:
        """The command of a cell: calibrated if stored, else the default."""
        if key in self.cells and not self.identity:
            return dict(self.cells[key])
        kind, anchor, level = parse_cell_key(key)
        value = COLOR_ANCHORS[anchor] if kind == KIND_COLOR else float(anchor)
        return self.default_command(kind, value, level)

    @property
    def _subfloor(self) -> bool:
        return self.fallback == BEHAVIOUR_SMART and not self.identity

    def _default_point(self, kind: str, anchor: float, level: float) -> Point:
        caps = self.caps
        mode = self.mode_for(kind)
        target_level = level * 255 / 100
        if mode is None:
            return Point("brightness", target_level)
        if kind == KIND_COLOR:
            if mode in CHANNEL_MODES:
                rgb = color_util.color_hs_to_RGB(anchor, 100.0)
                base = tuple(float(c) for c in rgb)
                color = base + (0.0,) * (CHANNEL_MODES[mode] - 3)
                return Point(mode, target_level, _normalise(color, mode))
            return Point("hs", target_level, (anchor, 100.0))
        # white
        if mode == "color_temp":
            kelvin = max(caps.min_kelvin, min(caps.max_kelvin, anchor))
            return Point(mode, target_level, (1e6 / kelvin,))
        if mode == "brightness":
            return Point(mode, target_level)
        rgb = color_util.color_temperature_to_rgb(anchor)
        if mode == "hs":
            return Point(mode, target_level, color_util.color_RGB_to_hs(*rgb))
        channels = _normalise(tuple(float(c) for c in _rgb_to_channels(rgb, mode, caps)), mode)
        return Point(mode, target_level, channels)

    def _cell_point(self, kind: str, anchor_name: str | int, anchor: float, level: float) -> Point:
        key = cell_key(kind, anchor_name, level)
        mode = self.mode_for(kind)
        if key in self.cells and not self.identity and mode is not None:
            return command_to_point(self.cells[key], mode)
        return self._default_point(kind, anchor, level)

    # -- forward -------------------------------------------------------------

    def _level_point(self, kind: str, anchor_name: str | int, anchor: float, brightness: float) -> Point:
        """Interpolate one anchor's cells along brightness."""
        levels = BRIGHTNESS_LEVELS
        if brightness <= levels[0]:
            point = self._cell_point(kind, anchor_name, anchor, levels[0])
            return Point(point.mode, point.level * max(0.0, brightness) / levels[0], point.color)
        if brightness >= levels[-1]:
            return self._cell_point(kind, anchor_name, anchor, levels[-1])
        for low, high in zip(levels, levels[1:], strict=False):
            if low <= brightness <= high:
                t = _perceptual_t(brightness, low, high)
                return mix_points(
                    self._cell_point(kind, anchor_name, anchor, low),
                    self._cell_point(kind, anchor_name, anchor, high),
                    t,
                )
        raise AssertionError("unreachable")

    def _color_neighbours(self, hue: float) -> tuple[str, float, str, float, float]:
        anchors = sorted(COLOR_ANCHORS.items(), key=lambda item: item[1])
        hue %= 360
        for (n0, h0), (n1, h1) in zip(anchors, anchors[1:] + anchors[:1], strict=True):
            span = (h1 - h0) % 360 or 360
            offset = (hue - h0) % 360
            if offset < span:
                return n0, h0, n1, h1, offset / span
        raise AssertionError("unreachable")

    def _white_neighbours(self, kelvin: float) -> tuple[int, int, float]:
        kelvins = self.caps.white_kelvins
        if len(kelvins) == 1 or kelvin <= kelvins[0]:
            return kelvins[0], kelvins[0], 0.0
        if kelvin >= kelvins[-1]:
            return kelvins[-1], kelvins[-1], 0.0
        for k0, k1 in zip(kelvins, kelvins[1:], strict=False):
            if k0 <= kelvin <= k1:
                m0, m1, m = 1e6 / k0, 1e6 / k1, 1e6 / kelvin
                return k0, k1, (m - m0) / (m1 - m0)
        raise AssertionError("unreachable")

    def forward_point(self, target: Target) -> Point:
        """The (unrounded) source point for a target."""
        kind = target.kind
        if kind == KIND_COLOR and self.caps.color_mode is None:
            kind = KIND_WHITE
        if self.caps.white_mode is None:
            return Point("brightness", target.brightness * 255 / 100)
        if kind == KIND_COLOR:
            n0, h0, n1, h1, t = self._color_neighbours(target.hue)
            point = mix_points(
                self._level_point(kind, n0, h0, target.brightness),
                self._level_point(kind, n1, h1, target.brightness),
                t,
            )
            return _desaturate(point, target.sat)
        kelvin = target.kelvin if target.kind == KIND_WHITE else DIM_KELVIN
        k0, k1, t = self._white_neighbours(kelvin)
        p0 = self._level_point(kind, k0, float(k0), target.brightness)
        if k0 == k1:
            return p0
        return mix_points(p0, self._level_point(kind, k1, float(k1), target.brightness), t)

    def _exact_key(self, target: Target) -> str | None:
        if target.brightness not in BRIGHTNESS_LEVELS:
            return None
        if target.kind == KIND_COLOR and target.sat >= 100:
            for name, hue in COLOR_ANCHORS.items():
                if abs(hue - target.hue % 360) < _EPS:
                    return cell_key(KIND_COLOR, name, target.brightness)
        if target.kind == KIND_WHITE and int(target.kelvin) in self.caps.white_kelvins:
            return cell_key(KIND_WHITE, int(target.kelvin), target.brightness)
        return None

    def forward(self, target: Target) -> dict[str, Any]:
        """The source command for a target look."""
        if self.caps.white_mode is None:
            return {}
        key = self._exact_key(target)
        if key is not None and key in self.cells and not self.identity:
            return dict(self.cells[key])
        return point_to_command(self.forward_point(target), self.caps.floor, self._subfloor)

    # -- inverse -------------------------------------------------------------

    def inverse(self, state: dict[str, Any]) -> Target | None:
        """Estimate the target look that a source state corresponds to."""
        brightness = state.get("brightness")
        if brightness is None or self.caps.white_mode is None:
            return None
        color_mode = state.get("color_mode")
        level = float(brightness)
        mode = self.caps.color_mode
        if mode in CHANNEL_MODES and state.get(MODE_ATTR[mode]) and color_mode == mode:
            channels = state[MODE_ATTR[mode]]
            top = max(channels) if channels else 255
            level = level * top / 255
        hs = state.get("hs_color")
        if color_mode == "color_temp" or (color_mode in ("brightness", "white", "onoff") or hs is None):
            kelvin = state.get("color_temp_kelvin")
            return self._inverse_white(level, float(kelvin) if kelvin else None)
        hue, sat = float(hs[0]), float(hs[1])
        if self.caps.white_mode != "color_temp" and sat < 20:
            white = self._inverse_white_by_hs(level, hue, sat)
            if white is not None:
                return white
        if self.caps.color_mode is None:
            return self._inverse_white(level, None)
        return self._inverse_color(level, hue, sat)

    def _solve_brightness(self, make: Any, level: float) -> float:
        """Find the target brightness whose forward level matches level."""
        if self.forward_point(make(100.0)).level <= level:
            return 100.0
        low, high = 0.0, 100.0
        for _ in range(30):
            mid = (low + high) / 2
            if self.forward_point(make(mid)).level < level:
                low = mid
            else:
                high = mid
        return round((low + high) / 2, 3)

    def _inverse_white(self, level: float, kelvin: float | None) -> Target:
        kelvins = self.caps.white_kelvins
        target_k = float(kelvins[0])
        if kelvin is not None and len(kelvins) > 1 and self.caps.white_mode == "color_temp":
            actual = 1e6 / kelvin
            # the small second term breaks ties where the lamp's range clamps
            target_k = _scan(
                lambda k: abs(self.forward_point(Target(KIND_WHITE, 50.0, kelvin=k)).color[0] - actual)
                + 1e-4 * abs(1e6 / k - actual),
                kelvins[0],
                kelvins[-1],
            )
        elif len(kelvins) == 1:
            target_k = float(kelvins[0])
        b = self._solve_brightness(lambda x: Target(KIND_WHITE, x, kelvin=target_k), level)
        return Target(KIND_WHITE, b, kelvin=round(target_k))

    def _inverse_white_by_hs(self, level: float, hue: float, sat: float) -> Target | None:
        kelvins = self.caps.white_kelvins
        if len(kelvins) < 2:
            return None

        def dist(k: float) -> float:
            fh, fs = point_hs(self.forward_point(Target(KIND_WHITE, 50.0, kelvin=k)))
            return _hs_distance(fh, fs, hue, sat)

        target_k = _scan(dist, kelvins[0], kelvins[-1])
        if dist(target_k) > 15:
            return None
        b = self._solve_brightness(lambda x: Target(KIND_WHITE, x, kelvin=target_k), level)
        return Target(KIND_WHITE, b, kelvin=round(target_k))

    def _inverse_color(self, level: float, hue: float, sat: float) -> Target:
        def dist(h: float) -> float:
            fh, _ = point_hs(self.forward_point(Target(KIND_COLOR, 50.0, hue=h)))
            return abs(((fh - hue + 180) % 360) - 180)

        target_h = _scan(dist, 0.0, 360.0, steps=180, circular=True) % 360
        _, full_sat = point_hs(self.forward_point(Target(KIND_COLOR, 50.0, hue=target_h)))
        target_s = 100.0 if full_sat <= 0 else max(0.0, min(100.0, sat / full_sat * 100))
        if target_s > 97:
            target_s = 100.0
        b = self._solve_brightness(
            lambda x: Target(KIND_COLOR, x, hue=target_h, sat=target_s), level
        )
        return Target(KIND_COLOR, b, hue=round(target_h, 2), sat=round(target_s, 2))

    # -- curves and serialisation ---------------------------------------------

    def apply_brightness_curve(self, curve: dict[float, float]) -> int:
        """Fill uncalibrated cells from a brightness curve (target % -> lamp %).

        This is the shape of a Lightener brightness map. Colours keep their
        default; only the brightness is remapped. Returns cells written.
        """
        points = sorted((float(k), float(v)) for k, v in curve.items())
        if not points:
            return 0
        if points[0][0] > 0:
            points.insert(0, (0.0, 0.0))
        if points[-1][0] < 100:
            points.append((100.0, 100.0))

        def remap(x: float) -> float:
            for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
                if x0 <= x <= x1:
                    return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
            return x

        written = 0
        for cell in grid_cells(self.caps):
            if cell["key"] in self.cells:
                continue
            anchor = cell["hue"] if cell["kind"] == KIND_COLOR else float(cell["kelvin"])
            mapped = max(0.1, min(100.0, remap(cell["brightness"])))
            self.cells[cell["key"]] = self.default_command(cell["kind"], anchor, mapped)
            written += 1
        return written

    def to_json(self, model: str | None = None) -> dict[str, Any]:
        """Export the profile so it can be shared."""
        data: dict[str, Any] = {
            "format": PROFILE_FORMAT,
            "version": PROFILE_VERSION,
            "modes": {"color": self.caps.color_mode, "white": self.caps.white_mode},
            "floor": self.caps.floor,
            "cells": {key: dict(cmd) for key, cmd in sorted(self.cells.items())},
        }
        if model:
            data["model"] = model
        return data

    def import_json(self, data: Any, replace: bool = True) -> tuple[int, int]:
        """Load cells from an exported profile, converting modes if needed.

        Returns (cells imported, cells skipped). Raises ValueError if the
        data is not a profile.
        """
        if not isinstance(data, dict) or data.get("format") != PROFILE_FORMAT:
            raise ValueError("not a Light Match profile")
        if int(data.get("version", 0)) > PROFILE_VERSION:
            raise ValueError("profile is from a newer version of Light Match")
        modes = data.get("modes") or {}
        cells = data.get("cells") or {}
        if not isinstance(cells, dict):
            raise ValueError("profile cells must be an object")
        valid = self.valid_keys()
        imported: dict[str, dict[str, Any]] = {}
        skipped = 0
        for key, cmd in cells.items():
            try:
                kind = parse_cell_key(key)[0]
            except ValueError:
                skipped += 1
                continue
            dst = self.mode_for(kind)
            src = modes.get(kind)
            if key not in valid or dst is None or src not in MODE_ATTR:
                skipped += 1
                continue
            try:
                converted = convert_command(validate_command(cmd, src), src, dst, self.caps)
                if converted is None:
                    raise ValueError("cannot convert")
                imported[key] = validate_command(converted, dst)
            except (ValueError, KeyError, TypeError):
                skipped += 1
        if replace:
            self.cells = imported
        else:
            self.cells.update(imported)
        return len(imported), skipped


def _hs_distance(h0: float, s0: float, h1: float, s1: float) -> float:
    """Distance between two hs colours on the colour disc (0-200)."""
    a0, a1 = math.radians(h0), math.radians(h1)
    return math.hypot(s0 * math.cos(a0) - s1 * math.cos(a1), s0 * math.sin(a0) - s1 * math.sin(a1))


def _scan(func: Any, low: float, high: float, steps: int = 60, circular: bool = False) -> float:
    """Minimise func over [low, high]: a coarse scan, then two refinements."""
    best_x, best = low, math.inf
    span = high - low
    count = steps if circular else steps + 1
    for i in range(count):
        x = low + span * i / steps
        value = func(x)
        if value < best:
            best_x, best = x, value
    step = span / steps
    for _ in range(2):
        centre = best_x
        for i in range(-10, 11):
            x = centre + step * i / 10
            if not circular:
                x = max(low, min(high, x))
            value = func(x)
            if value < best:
                best_x, best = x, value
        step /= 10
    return best_x
