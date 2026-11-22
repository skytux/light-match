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
    WHITE_COLOR_ALWAYS,
    WHITE_COLOR_AUTO,
    WHITE_KELVINS,
)
from .formats import APP_VERSION, migrate, written_format

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
KELVIN_MARGIN = 100  # a white this far outside the lamp's ct range uses its colour LEDs
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
    gamut: tuple[tuple[float, float], ...] | None = None  # xy corners red, green, blue
    subfloor: bool = True  # the lamp dims its channels at the floor brightness
    channel_gain: tuple[float, ...] | None = None  # relative output per channel
    # Estimated relative output by brightness, ((brightness, output), ...)
    output_curve: tuple[tuple[float, float], ...] | None = None

    def curve_level(self, level: float) -> float | None:
        """The brightness (0-255) that gives the lm-ref-1 output of a level (%), from the curve.

        The target output is luminance(L* = level). The curve's lowest
        reading is treated as light-independent draw (a power reading has
        driver overhead) and subtracted. Where the curve is flat or too low to
        trust (below its first rising point after the flat start), the
        brightness is scaled linearly down to 0, joined continuously; the
        floor logic then handles anything below the lamp's floor. None when
        there is no usable curve.
        """
        curve = self.output_curve
        if not curve or len(curve) < 2:
            return None
        base = curve[0][1]
        span = curve[-1][1] - base
        if span <= _EPS:
            return None
        points = [(b, (y - base) / span) for b, y in curve]
        rising = [i for i in range(1, len(points)) if points[i][1] > _EPS]
        if not rising:
            return None
        # the first trustworthy point: the second one after the flat start, if there is one
        first = rising[min(1, len(rising) - 1)]
        b_rel, y_rel = points[first]
        level = max(0.0, min(100.0, level))
        y = luminance(level)
        if y <= y_rel:
            l_rel = lightness(y_rel)
            return b_rel * level / l_rel if l_rel > _EPS else b_rel
        for (b0, y0), (b1, y1) in zip(points[first - 1 :], points[first:], strict=False):
            if y0 <= y <= y1:
                return b0 + (b1 - b0) * ((y - y0) / (y1 - y0) if y1 - y0 > _EPS else 1.0)
        return 255.0

    def white_mode_at(self, kelvin: float) -> str | None:
        """The command mode for a white: its colour LEDs when outside a narrow ct range."""
        if (
            self.white_mode == "color_temp"
            and self.color_mode is not None
            and (kelvin < self.min_kelvin - KELVIN_MARGIN or kelvin > self.max_kelvin + KELVIN_MARGIN)
        ):
            return self.color_mode
        return self.white_mode

    @property
    def narrow_white(self) -> bool:
        """Whether some white rows of the grid are shown with the colour LEDs."""
        return any(self.white_mode_at(k) != self.white_mode for k in WHITE_KELVINS)

    def white_color(self, kelvin: float, mode: str) -> tuple[float, ...]:
        """The colour of a white in a colour mode (hs or channel ratios)."""
        rgb = color_util.color_temperature_to_rgb(kelvin)
        if mode == "hs":
            return self.hs_in_gamut(*color_util.color_RGB_to_hs(*rgb))
        channels = _normalise(tuple(float(c) for c in _rgb_to_channels(rgb, mode, self)), mode)
        return self.balance(channels, mode)

    @classmethod
    def from_modes(
        cls,
        modes: list[str] | set[str] | None,
        min_kelvin: int | None = None,
        max_kelvin: int | None = None,
        floor: int = 0,
        **adapters: Any,
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
            **adapters,
        )

    def hs_in_gamut(self, hue: float, sat: float) -> tuple[float, float]:
        """The hue and saturation the lamp really shows, clamped to its gamut."""
        if not self.gamut:
            return hue, sat
        gamut = color_util.GamutType(*(color_util.XYPoint(*p) for p in self.gamut))
        x, y = color_util.color_hs_to_xy(hue, sat, gamut)
        return color_util.color_xy_to_hs(x, y)

    def balance(self, color: tuple[float, ...], mode: str) -> tuple[float, ...]:
        """Divide channel ratios by the channel gains, renormalised."""
        gain = self.channel_gain
        if not gain or len(gain) != len(color):
            return color
        return _normalise(tuple(c / g for c, g in zip(color, gain, strict=True)), mode)

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


def _command_mode(cmd: dict[str, Any], modes: list[str]) -> str | None:
    """The first of these modes a command is given in (by its colour attribute)."""
    for mode in modes:
        attr = MODE_ATTR.get(mode, "")
        if attr is None or attr in cmd:
            return mode
    return None


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


SOURCE_USER = "user"
SOURCE_MODEL = "model"
SOURCE_DEFAULT = "default"
SOURCE_INFERRED = "inferred"


def quick_keys(caps: SourceCaps) -> list[str]:
    """The five cells of the quick calibration, in order.

    White at 1, 20 and 100 % fix the brightness curve; two more cells fix the
    colour: red and blue for colour lamps, the warm and cold ends for
    colour-temperature lamps. Inference spreads them over the grid.
    """
    if caps.white_mode is None:
        return []
    if caps.white_mode == "brightness":
        return [cell_key(KIND_WHITE, DIM_KELVIN, lvl) for lvl in BRIGHTNESS_LEVELS]
    keys = [cell_key(KIND_WHITE, 2700, lvl) for lvl in (1, 20, 100)]
    if caps.color_mode is not None:
        keys += [cell_key(KIND_COLOR, "red", 20), cell_key(KIND_COLOR, "blue", 20)]
    else:
        keys += [cell_key(KIND_WHITE, 6500, 20), cell_key(KIND_WHITE, 2200, 20)]
    return keys


class Profile:
    """A lamp's calibration in layers: defaults < model profile < user cells.

    ``cells`` are the user's own calibrated cells; ``model_cells`` come from
    the lamp's model profile in the database, already converted to this
    lamp's command modes (see :meth:`convert_cells`).
    """

    def __init__(
        self,
        caps: SourceCaps,
        cells: dict[str, dict[str, Any]] | None = None,
        fallback: str = BEHAVIOUR_SMART,
        identity: bool = False,
        model_cells: dict[str, dict[str, Any]] | None = None,
        infer: bool = True,
        white_color: str = WHITE_COLOR_AUTO,
    ) -> None:
        """Create a profile for a lamp with these capabilities."""
        self.caps = caps
        self.fallback = fallback
        self.identity = identity
        self.infer = infer
        self.white_color = white_color
        self._correction: dict[str, list[tuple[float, float]]] | None = None
        self.cells = dict(cells or {})
        self.model_cells = dict(model_cells or {})

    @property
    def cells(self) -> dict[str, dict[str, Any]]:
        """The user's own calibrated cells."""
        return self._cells

    @cells.setter
    def cells(self, value: dict[str, dict[str, Any]]) -> None:
        self._cells = value
        self._correction = None

    @property
    def model_cells(self) -> dict[str, dict[str, Any]]:
        """Cells from the model profile, in this lamp's modes."""
        return self._model_cells

    @model_cells.setter
    def model_cells(self, value: dict[str, dict[str, Any]]) -> None:
        self._model_cells = value
        self._correction = None

    def _layer(self, cells: dict[str, dict[str, Any]], key: str) -> dict[str, Any] | None:
        """A layer's command for a cell, if it is in the cell's current mode."""
        cmd = cells.get(key)
        if cmd is None or self.identity:
            return None
        try:
            mode = self.cell_mode(key)
        except ValueError:
            return None
        if mode is None or not isinstance(cmd, dict):
            return None
        return cmd if _command_mode(cmd, [mode]) == mode else None

    def stored(self, key: str) -> dict[str, Any] | None:
        """The command stored for a cell: the user's, else the model profile's."""
        return self._layer(self.cells, key) or self._layer(self.model_cells, key)

    def cell_source(self, key: str) -> str:
        """Where a cell's command comes from: user, model, inferred or default."""
        if self._layer(self.cells, key) is not None:
            return SOURCE_USER
        if self._layer(self.model_cells, key) is not None:
            return SOURCE_MODEL
        if self._inferring:
            return SOURCE_INFERRED
        return SOURCE_DEFAULT

    # -- inference -------------------------------------------------------------

    @property
    def _inferring(self) -> bool:
        return self.infer and not self.identity and bool(self.cells)

    def _base_point(self, key: str) -> Point:
        """A cell's point below the user's layer: model cell, else default."""
        kind, anchor, level = parse_cell_key(key)
        mode = self.cell_mode(key)
        if mode is not None and (model := self._layer(self.model_cells, key)) is not None:
            return command_to_point(model, mode)
        value = COLOR_ANCHORS[anchor] if kind == KIND_COLOR else float(anchor)
        return self._default_point(kind, value, level, mode)

    def _corrections(self) -> dict[str, list[tuple[float, float]]]:
        """Per kind: (level %, lightness ratio user / base) from the user's cells."""
        if self._correction is not None:
            return self._correction
        ratios: dict[str, dict[float, list[float]]] = {KIND_COLOR: {}, KIND_WHITE: {}}
        for key, cmd in self.cells.items():
            try:
                kind, _anchor, level = parse_cell_key(key)
            except ValueError:
                continue
            mode = self.cell_mode(key)
            if mode is None or self._layer(self.cells, key) is None:
                continue
            base = lightness(self._base_point(key).level / 255)
            if base <= _EPS:
                continue
            user = lightness(command_to_point(cmd, mode).level / 255)
            ratios[kind].setdefault(level, []).append(user / base)
        self._correction = {
            kind: sorted((lvl, sum(vals) / len(vals)) for lvl, vals in by_level.items())
            for kind, by_level in ratios.items()
        }
        return self._correction

    def _correction_at(self, kind: str, level: float) -> float:
        """The lightness ratio at a level: interpolated, constant beyond the ends."""
        table = self._corrections()
        curve = table.get(kind) or table.get(KIND_WHITE if kind == KIND_COLOR else KIND_COLOR) or []
        if not curve:
            return 1.0
        if level <= curve[0][0]:
            return curve[0][1]
        if level >= curve[-1][0]:
            return curve[-1][1]
        for (l0, r0), (l1, r1) in zip(curve, curve[1:], strict=False):
            if l0 <= level <= l1:
                return r0 + (r1 - r0) * _perceptual_t(level, l0, l1)
        return 1.0

    def _inferred_point(self, kind: str, anchor_name: str | int, level: float, base: Point) -> Point:
        """An uncalibrated cell estimated from the user's cells.

        Colour: the same anchor's calibrated cell at the nearest level.
        Brightness: the base level scaled by the user's lightness ratio at this
        level, learned from all calibrated cells of the same kind.
        """
        color = base.color
        nearest: tuple[float, Point] | None = None
        for lvl in BRIGHTNESS_LEVELS:
            key = cell_key(kind, anchor_name, lvl)
            cmd = self._layer(self.cells, key)
            mode = self.cell_mode(key)
            if cmd is None or mode is None:
                continue
            dist = abs(lightness(lvl / 100) - lightness(level / 100))
            if nearest is None or dist < nearest[0]:
                nearest = (dist, command_to_point(cmd, mode))
        if nearest is not None and nearest[1].mode == base.mode:
            color = nearest[1].color
        ratio = self._correction_at(kind, level)
        new_level = luminance(lightness(base.level / 255) * ratio) * 255
        return Point(base.mode, max(0.0, min(255.0, new_level)), color)

    def convert_cells(
        self, cells: dict[str, Any], modes: dict[str, Any]
    ) -> tuple[dict[str, dict[str, Any]], int]:
        """Convert cells given in other command modes to this lamp's modes.

        Returns (converted cells, number skipped).
        """
        valid = self.valid_keys()
        cell_modes = modes.get("cells") if isinstance(modes.get("cells"), dict) else {}
        converted: dict[str, dict[str, Any]] = {}
        skipped = 0
        for key, cmd in cells.items():
            try:
                kind, anchor, level = parse_cell_key(key)
            except ValueError:
                skipped += 1
                continue
            src = cell_modes.get(key) or modes.get(kind)
            # a white shown with the colour LEDs was stored in the colour mode
            if isinstance(cmd, dict) and src in MODE_ATTR and MODE_ATTR[src] not in cmd and MODE_ATTR[src] is not None:
                src = modes.get(KIND_COLOR, src)
            if kind == KIND_WHITE and not self.identity and key in valid:
                # a white keeps the mode it was calibrated in when this lamp has it
                allowed = self.white_modes(float(anchor))
                dst = src if src in allowed else self.default_white_mode(float(anchor), level)
            else:
                dst = self.cell_mode(key)
            if key not in valid or dst is None or src not in MODE_ATTR:
                skipped += 1
                continue
            try:
                clean = validate_command(cmd, src)
                if kind == KIND_WHITE and dst == "color_temp" and src != dst:
                    # a white made with the colour LEDs: the cell's colour temperature at its level
                    k = max(self.caps.min_kelvin, min(self.caps.max_kelvin, float(anchor)))
                    level = command_to_point(clean, src).level
                    result = point_to_command(Point(dst, level, (1e6 / k,)), self.caps.floor)
                else:
                    result = convert_command(clean, src, dst, self.caps)
                if result is None:
                    raise ValueError("cannot convert")
                converted[key] = validate_command(result, dst)
            except (ValueError, KeyError, TypeError):
                skipped += 1
        return converted, skipped

    # -- cells ---------------------------------------------------------------

    def mode_for(self, kind: str, anchor: str | float | None = None) -> str | None:
        """The command mode of colour or white cells (of one white, given its kelvin)."""
        if kind == KIND_COLOR:
            return self.caps.color_mode
        if anchor is not None and not self.identity:
            return self.caps.white_mode_at(float(anchor))
        return self.caps.white_mode

    @property
    def _colour_dims_lower(self) -> bool:
        """Whether whites with the colour LEDs can go below the floor (sub-floor trick)."""
        return self.caps.color_mode in CHANNEL_MODES and self._subfloor and self.caps.floor > 0

    def white_modes(self, kelvin: float) -> list[str]:
        """The modes a white cell can be calibrated in: the lamp's white first, then its colour LEDs."""
        caps = self.caps
        if self.identity:
            return [caps.white_mode] if caps.white_mode else []
        base = caps.white_mode_at(kelvin)
        if base is None:
            return []
        if base == "color_temp" and caps.color_mode is not None:
            return [base, caps.color_mode]
        return [base]

    def default_white_mode(self, kelvin: float, level: float) -> str | None:
        """The mode of an uncalibrated white cell, by the white colour option.

        auto: the colour LEDs only where the default brightness is below the
        lamp's floor, which the colour channels can dim under; always: the
        colour LEDs for every white; never: the colour temperature (whites
        outside the lamp's range always use the colour LEDs).
        """
        caps = self.caps
        if self.identity:
            return caps.white_mode
        base = caps.white_mode_at(kelvin)
        if base != "color_temp" or caps.color_mode is None:
            return base
        if self.white_color == WHITE_COLOR_ALWAYS:
            return caps.color_mode
        if (
            self.white_color == WHITE_COLOR_AUTO
            and self._colour_dims_lower
            and self._default_level(level) < caps.floor
        ):
            return caps.color_mode
        return base

    def cell_mode(self, key: str) -> str | None:
        """The command mode of one cell.

        A white cell is in the mode its calibration (user, else model profile)
        was saved in, when the lamp has that mode; else the default mode.
        """
        kind, anchor, level = parse_cell_key(key)
        if kind == KIND_COLOR or self.identity:
            return self.mode_for(kind)
        kelvin = float(anchor)
        allowed = self.white_modes(kelvin)
        if len(allowed) > 1:
            for layer in (self.cells, self.model_cells):
                cmd = layer.get(key)
                if isinstance(cmd, dict) and (mode := _command_mode(cmd, allowed)) is not None:
                    return mode
        return self.default_white_mode(kelvin, level)

    def colour_whites(self) -> bool:
        """Whether any white cell is shown with the colour LEDs of a ct lamp."""
        caps = self.caps
        if self.identity or caps.white_mode != "color_temp" or caps.color_mode is None:
            return False
        return any(
            self.cell_mode(cell_key(KIND_WHITE, k, lvl)) != caps.white_mode
            for k in caps.white_kelvins
            for lvl in BRIGHTNESS_LEVELS
        )

    def valid_keys(self) -> set[str]:
        """Keys of the cells this lamp's grid has."""
        return {cell["key"] for cell in grid_cells(self.caps)}

    def set_cell(self, key: str, cmd: dict[str, Any], mode: str | None = None) -> dict[str, Any]:
        """Store a calibrated cell; validates the key, the mode and the command.

        A white cell can be saved in any of its modes (``mode``, else the mode
        the command is in, else the cell's current mode).
        """
        if key not in self.valid_keys():
            raise ValueError(f"this lamp has no cell {key!r}")
        kind, anchor, _level = parse_cell_key(key)
        current = self.cell_mode(key)
        assert current is not None
        allowed = [current]
        if kind == KIND_WHITE:
            allowed += [m for m in self.white_modes(float(anchor)) if m != current]
        if mode is None:
            mode = _command_mode(cmd, allowed) if isinstance(cmd, dict) else None
            mode = mode or current
        elif mode not in allowed:
            raise ValueError(f"cell {key!r} cannot be calibrated in {mode!r}")
        clean = validate_command(cmd, mode)
        self.cells = {**self.cells, key: clean}
        return clean

    def reset_cell(self, key: str) -> bool:
        """Forget a calibrated cell; True if there was one."""
        had = key in self.cells
        self.cells = {k: v for k, v in self.cells.items() if k != key}
        return had

    def default_command(
        self, kind: str, anchor: float, level: float, mode: str | None = None
    ) -> dict[str, Any]:
        """The fallback command for a target colour hue / white kelvin at a level (in a mode)."""
        return point_to_command(
            self._default_point(kind, anchor, level, mode), self.caps.floor, self._subfloor
        )

    def cell_command_in(self, key: str, mode: str) -> dict[str, Any]:
        """A white cell's command in one of its modes, for switching modes in the panel.

        In the cell's own mode this is its command. In the other mode it is the
        model profile's cell when that is in this mode, else the cell's command
        converted at the same light level: the colour LEDs mix the white of the
        cell's colour temperature, below the floor with the channels scaled
        down at the floor brightness.
        """
        current = self.cell_mode(key)
        if mode == current:
            return self.cell_command(key)
        kind, anchor, _level = parse_cell_key(key)
        if kind != KIND_WHITE or mode not in self.white_modes(float(anchor)):
            raise ValueError(f"cell {key!r} has no mode {mode!r}")
        model = self.model_cells.get(key)
        if isinstance(model, dict) and _command_mode(model, [mode]) == mode:
            return dict(model)
        assert current is not None
        point = command_to_point(self.cell_command(key), current)
        caps = self.caps
        if mode == "color_temp":
            kelvin = max(caps.min_kelvin, min(caps.max_kelvin, float(anchor)))
            other = Point(mode, point.level, (1e6 / kelvin,))
        else:
            kelvin = 1e6 / point.color[0] if point.mode == "color_temp" else float(anchor)
            other = Point(mode, point.level, caps.white_color(kelvin, mode))
        return point_to_command(other, caps.floor, caps.subfloor and not self.identity)

    def cell_command(self, key: str) -> dict[str, Any]:
        """The command of a cell: calibrated if stored, else inferred or the default."""
        if (stored := self.stored(key)) is not None:
            return dict(stored)
        kind, anchor, level = parse_cell_key(key)
        value = COLOR_ANCHORS[anchor] if kind == KIND_COLOR else float(anchor)
        if self._inferring and self.mode_for(kind) is not None:
            point = self._cell_point(kind, anchor if kind == KIND_COLOR else int(anchor), value, level)
            return point_to_command(point, self.caps.floor, self._subfloor)
        return self.default_command(kind, value, level)

    def copy_model(self, key: str = "*") -> int:
        """Copy model profile cells into the user's cells; returns cells copied.

        With ``*``, every model cell the user has not calibrated is copied.
        """
        keys = [k for k in self.model_cells if k not in self.cells] if key == "*" else [key]
        copied = 0
        for k in keys:
            if (cmd := self.model_cells.get(k)) is not None:
                self.cells = {**self.cells, k: dict(cmd)}
                copied += 1
        return copied

    @property
    def _subfloor(self) -> bool:
        return self.fallback == BEHAVIOUR_SMART and not self.identity and self.caps.subfloor

    def _default_level(self, level: float) -> float:
        """The default brightness (0-255) for a level: the output curve when known, else linear."""
        if self.fallback == BEHAVIOUR_SMART and not self.identity:
            from_curve = self.caps.curve_level(level)
            if from_curve is not None:
                return from_curve
        return level * 255 / 100

    def _default_point(self, kind: str, anchor: float, level: float, mode: str | None = None) -> Point:
        caps = self.caps
        if mode is None:
            mode = self.mode_for(kind) if kind == KIND_COLOR else self.default_white_mode(anchor, level)
        target_level = self._default_level(level)
        if mode is None:
            return Point("brightness", target_level)
        if kind == KIND_COLOR:
            if mode in CHANNEL_MODES:
                rgb = color_util.color_hs_to_RGB(anchor, 100.0)
                base = tuple(float(c) for c in rgb)
                color = base + (0.0,) * (CHANNEL_MODES[mode] - 3)
                return Point(mode, target_level, caps.balance(_normalise(color, mode), mode))
            return Point("hs", target_level, caps.hs_in_gamut(anchor, 100.0))
        # white
        if mode == "color_temp":
            kelvin = max(caps.min_kelvin, min(caps.max_kelvin, anchor))
            return Point(mode, target_level, (1e6 / kelvin,))
        if mode == "brightness":
            return Point(mode, target_level)
        return Point(mode, target_level, caps.white_color(anchor, mode))

    def _cell_point(self, kind: str, anchor_name: str | int, anchor: float, level: float) -> Point:
        key = cell_key(kind, anchor_name, level)
        mode = self.cell_mode(key)
        if mode is not None and (stored := self.stored(key)) is not None:
            return command_to_point(stored, mode)
        base = self._default_point(kind, anchor, level, mode)
        if self._inferring:
            return self._inferred_point(kind, anchor_name, level, base)
        return base

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
                p0 = self._cell_point(kind, anchor_name, anchor, low)
                p1 = self._cell_point(kind, anchor_name, anchor, high)
                if p0.mode != p1.mode:
                    return self._mix_white(p0, p1, t, anchor)
                return mix_points(p0, p1, t)
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
        p1 = self._level_point(kind, k1, float(k1), target.brightness)
        if p0.mode != p1.mode:
            k = max(self.caps.min_kelvin, min(self.caps.max_kelvin, kelvin))
            return self._mix_white(p0, p1, t, kelvin, 1e6 / k)
        return mix_points(p0, p1, t)

    def _mix_white(self, p0: Point, p1: Point, t: float, kelvin: float, mired: float | None = None) -> Point:
        """Blend a colour-temperature white with a white shown by the colour LEDs.

        Where the lamp's own white can show the result (inside its ct range and
        at or above its floor) it is used at the blended level, with ``mired``
        (default: the ct point's). Otherwise the ct white is converted to the
        colour mode and blended there, so levels below the floor stay reachable.
        """
        caps = self.caps
        if p0.mode != "color_temp" and p1.mode != "color_temp":
            # two colour modes cannot happen on one lamp; keep the nearer point
            return p0 if t < 0.5 else p1
        ct, other = (p0, p1) if p0.mode == "color_temp" else (p1, p0)
        level = _mix_level(p0.level, p1.level, t)
        below = level < caps.floor and self._colour_dims_lower
        if caps.white_mode_at(kelvin) == "color_temp" and not below:
            return Point("color_temp", level, (mired if mired is not None else ct.color[0],))
        converted = Point(other.mode, ct.level, caps.white_color(1e6 / ct.color[0], other.mode))
        return mix_points(converted, p1, t) if p0 is ct else mix_points(p0, converted, t)

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
        if key is not None and (stored := self.stored(key)) is not None:
            return dict(stored)
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
        elif sat < 95 and self.colour_whites():
            # a colour-LED white of a ct lamp (below the floor, a narrow ct range or by
            # choice) can be warm, so allow more saturation but a closer match; compare
            # at about the right brightness, where the cell modes are what they are
            kelvins = self.caps.white_kelvins
            mid = float(kelvins[len(kelvins) // 2])
            near = self._solve_brightness(lambda x: Target(KIND_WHITE, x, kelvin=mid), level)
            white = self._inverse_white_by_hs(level, hue, sat, 15.0 if sat < 20 else 6.0, near)
            if white is not None and self.forward_point(white).mode != "color_temp":
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

            def dist(k: float) -> float:
                point = self.forward_point(Target(KIND_WHITE, 50.0, kelvin=k))
                if point.mode != "color_temp":  # shown with the colour LEDs: the nearest such
                    return 1e3 + abs(1e6 / k - actual)
                # the small second term breaks ties where the lamp's range clamps
                return abs(point.color[0] - actual) + 1e-4 * abs(1e6 / k - actual)

            target_k = _scan(dist, kelvins[0], kelvins[-1])
        elif len(kelvins) == 1:
            target_k = float(kelvins[0])
        b = self._solve_brightness(lambda x: Target(KIND_WHITE, x, kelvin=target_k), level)
        return Target(KIND_WHITE, b, kelvin=round(target_k))

    def _inverse_white_by_hs(
        self, level: float, hue: float, sat: float, max_dist: float = 15.0, at: float = 50.0
    ) -> Target | None:
        kelvins = self.caps.white_kelvins
        if len(kelvins) < 2:
            return None
        at = max(0.5, at)

        def dist(k: float) -> float:
            fh, fs = point_hs(self.forward_point(Target(KIND_WHITE, at, kelvin=k)))
            return _hs_distance(fh, fs, hue, sat)

        target_k = _scan(dist, kelvins[0], kelvins[-1])
        if dist(target_k) > max_dist:
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

        written: dict[str, dict[str, Any]] = {}
        for cell in grid_cells(self.caps):
            if self.cell_source(cell["key"]) not in (SOURCE_DEFAULT, SOURCE_INFERRED):
                continue
            anchor = cell["hue"] if cell["kind"] == KIND_COLOR else float(cell["kelvin"])
            mapped = max(0.1, min(100.0, remap(cell["brightness"])))
            written[cell["key"]] = self.default_command(cell["kind"], anchor, mapped)
        self.cells = {**self.cells, **written}
        return len(written)

    def to_json(
        self, model: str | None = None, meta: dict[str, Any] | None = None, effective: bool = False
    ) -> dict[str, Any]:
        """Export the profile so it can be shared.

        ``effective`` exports the model profile's cells under the user's own;
        ``meta`` adds the portable metadata (device, capabilities, provenance).
        """
        cells = {**self.model_cells, **self.cells} if effective else self.cells
        modes: dict[str, Any] = {"color": self.caps.color_mode, "white": self.caps.white_mode}
        # whites saved with the colour LEDs record their mode per cell
        cell_modes: dict[str, str] = {}
        for key, cmd in cells.items():
            try:
                kind, anchor, _level = parse_cell_key(key)
            except ValueError:
                continue
            if kind == KIND_WHITE and isinstance(cmd, dict):
                mode = _command_mode(cmd, self.white_modes(float(anchor)))
                if mode is not None and mode != self.caps.white_mode:
                    cell_modes[key] = mode
        if cell_modes:
            modes["cells"] = dict(sorted(cell_modes.items()))
        data: dict[str, Any] = {
            "format": PROFILE_FORMAT,
            "version": PROFILE_VERSION,
            "app_version": APP_VERSION,
            "modes": modes,
            "floor": self.caps.floor,
            "cells": {key: dict(cmd) for key, cmd in sorted(cells.items())},
        }
        if model:
            data["model"] = model
        if meta:
            data.update(meta)
        data["format_version"] = written_format(data)
        return data

    def import_json(self, data: Any, replace: bool = True) -> tuple[int, int]:
        """Load cells from an exported profile, converting modes if needed.

        Returns (cells imported, cells skipped). Raises ValueError if the
        data is not a profile.
        """
        if not isinstance(data, dict) or data.get("format") != PROFILE_FORMAT:
            raise ValueError("not a Light Match profile")
        try:
            version = int(data.get("version", 0))
        except (TypeError, ValueError) as err:
            raise ValueError("bad profile version") from err
        if version > PROFILE_VERSION:
            raise ValueError("profile is from a newer version of Light Match")
        data = migrate(data)  # NewerFormatError for a newer format
        modes = data.get("modes") or {}
        cells = data.get("cells") or {}
        if not isinstance(cells, dict) or not isinstance(modes, dict):
            raise ValueError("profile cells and modes must be objects")
        imported, skipped = self.convert_cells(cells, modes)
        if replace:
            self.cells = imported
        else:
            self.cells = {**self.cells, **imported}
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
