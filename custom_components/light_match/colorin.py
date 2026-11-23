"""Any colour input for a twin, normalised to a target in the calibrated space.

Other systems may send a twin raw channels (rgb, rgbw, rgbww) or an xy
chromaticity instead of hue/saturation or a colour temperature. They are
turned into one neutral look:

- **rgb**: the channels are taken as sRGB, linearised and converted to an xy
  chromaticity. Their intensity, the strongest channel / 255, is folded into
  the brightness: ``[25, 0, 0]`` at brightness 255 is red at about 10 %.
- **rgbw / rgbww**: as rgb, plus white channels. W is a neutral white, the
  sRGB white point (as Home Assistant's own rgbw conversion assumes, where W
  adds equally to r, g and b); WW and CW are whites at :data:`WW_KELVIN` and
  :data:`CW_KELVIN`. At 255 each white channel gives as much light as the
  sRGB white. The channels are added as light (in linear space) and the
  intensity is again the strongest channel / 255.
- **xy**: used directly, at the brightness given.

Hue and saturation follow Home Assistant's own conventions, so that a twin
treats an input the way Home Assistant would for an hs lamp: for channels the
hue/saturation of the combined (sRGB) colour, as ``color_RGB_to_hs`` gives
(``[255, 0, 0]`` is hue 0, the grid's red), and for xy ``color_xy_to_hs``.

A chromaticity close to the Planckian locus (``|Duv|`` at most
:data:`WHITE_DUV` and between :data:`WHITE_MIN_KELVIN` and
:data:`WHITE_MAX_KELVIN`) becomes a white at that colour temperature;
anything else becomes a colour at hue/saturation. Only white channels lit
(rgbw or rgbww with r, g and b at 0) is always a white: a warm and cold mix
lies a little below the locus. Both then go through the
calibration grid like hs and colour-temperature commands.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from homeassistant.util.color import color_RGB_to_hs, color_xy_to_hs

from .const import TWIN_MAX_KELVIN, TWIN_MIN_KELVIN

INPUT_MODES = ("rgb", "rgbw", "rgbww", "xy")
CHANNELS = {"rgb": 3, "rgbw": 4, "rgbww": 5}

WW_KELVIN = 2700  # the warm white channel of rgbww
CW_KELVIN = 6500  # the cold white channel of rgbww
WHITE_DUV = 0.006  # how far from the Planckian locus a colour still counts as white
WHITE_MIN_KELVIN = 1900
WHITE_MAX_KELVIN = 8000

D65 = (0.3127, 0.3290)
SRGB_TO_XYZ = (
    (0.4124, 0.3576, 0.1805),
    (0.2126, 0.7152, 0.0722),
    (0.0193, 0.1192, 0.9505),
)
XYZ_TO_SRGB = (
    (3.2406, -1.5372, -0.4986),
    (-0.9689, 1.8758, 0.0415),
    (0.0557, -0.2040, 1.0570),
)


@dataclass(frozen=True)
class Translated:
    """A colour input in the calibrated space: white (kelvin) or colour (hue/sat), and its intensity 0-1."""

    kind: str  # "white" or "color"
    intensity: float
    xy: tuple[float, float]
    hue: float = 0.0
    sat: float = 0.0
    kelvin: float = 0.0


def srgb_to_linear(v8: float) -> float:
    """An 8-bit sRGB value as linear light 0-1."""
    c = max(0.0, min(255.0, float(v8))) / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear_to_srgb(c: float) -> float:
    """Linear light 0-1 as an sRGB value 0-255."""
    c = max(0.0, min(1.0, c))
    return 255 * (c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055)


def planck_xy(kelvin: float) -> tuple[float, float]:
    """The chromaticity of a black body (Kim et al. cubic approximation, 1667-25000 K)."""
    t = max(1667.0, min(25000.0, float(kelvin)))
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
    return x, y


def _uv(xy: tuple[float, float]) -> tuple[float, float]:
    """CIE 1960 uv."""
    x, y = xy
    d = -2 * x + 12 * y + 3
    return 4 * x / d, 6 * y / d


def cct_duv(xy: tuple[float, float]) -> tuple[float, float]:
    """Correlated colour temperature and Duv (signed distance from the locus in uv)."""
    u, v = _uv(xy)

    def dist(mired: float) -> float:
        pu, pv = _uv(planck_xy(1e6 / mired))
        return math.hypot(u - pu, v - pv)

    lo, hi = 40.0, 600.0  # 25000 K .. 1667 K
    for _ in range(80):  # golden-section search on the mired scale
        m1 = hi - (hi - lo) / 1.618034
        m2 = lo + (hi - lo) / 1.618034
        if dist(m1) < dist(m2):
            hi = m2
        else:
            lo = m1
    mired = (lo + hi) / 2
    pu, pv = _uv(planck_xy(1e6 / mired))
    sign = 1.0 if v >= pv else -1.0
    return 1e6 / mired, sign * math.hypot(u - pu, v - pv)


def _xyz_of_xy(xy: tuple[float, float], big_y: float) -> tuple[float, float, float]:
    x, y = xy
    if y <= 0:
        return 0.0, 0.0, 0.0
    return x * big_y / y, big_y, (1 - x - y) * big_y / y


def channels_to_linear(mode: str, values: tuple[float, ...] | list[float]) -> tuple[list[float], float]:
    """Linear sRGB light of raw channels (white channels added as light) and their intensity."""
    vals = [max(0.0, min(255.0, float(v))) for v in values]
    if len(vals) != CHANNELS[mode]:
        raise ValueError(f"{mode} needs {CHANNELS[mode]} values")
    lin = [srgb_to_linear(v) for v in vals[:3]]
    whites: list[tuple[tuple[float, float], float]] = []
    if mode == "rgbw":
        whites.append((D65, srgb_to_linear(vals[3])))
    elif mode == "rgbww":
        whites.append((planck_xy(CW_KELVIN), srgb_to_linear(vals[3])))
        whites.append((planck_xy(WW_KELVIN), srgb_to_linear(vals[4])))
    for xy, amount in whites:
        add = _xyz_of_xy(xy, amount)
        lin = [c + sum(row[i] * add[i] for i in range(3)) for c, row in zip(lin, XYZ_TO_SRGB, strict=True)]
    return [max(0.0, c) for c in lin], max(vals) / 255


def linear_to_xy(lin: list[float]) -> tuple[float, float]:
    """The chromaticity of linear sRGB light (D65 for black)."""
    xyz = [sum(row[i] * lin[i] for i in range(3)) for row in SRGB_TO_XYZ]
    total = sum(xyz)
    return (xyz[0] / total, xyz[1] / total) if total > 0 else D65


def channels_to_xy(mode: str, values: tuple[float, ...] | list[float]) -> tuple[tuple[float, float], float]:
    """xy chromaticity and intensity (strongest channel / 255) of raw channels."""
    lin, intensity = channels_to_linear(mode, values)
    return linear_to_xy(lin), intensity


def _white(xy: tuple[float, float], intensity: float) -> Translated | None:
    kelvin, duv = cct_duv(xy)
    if abs(duv) <= WHITE_DUV and WHITE_MIN_KELVIN <= kelvin <= WHITE_MAX_KELVIN:
        kelvin = max(TWIN_MIN_KELVIN, min(TWIN_MAX_KELVIN, kelvin))
        return Translated("white", intensity, xy, kelvin=round(kelvin))
    return None


def translate_xy(xy: tuple[float, float], intensity: float = 1.0) -> Translated:
    """A white near the Planckian locus, else a colour at hue/saturation."""
    x, y = float(xy[0]), float(xy[1])
    if (white := _white((x, y), intensity)) is not None:
        return white
    hue, sat = color_xy_to_hs(x, y)
    return Translated("color", intensity, (x, y), hue=float(hue), sat=float(sat))


def translate(mode: str, value: tuple[float, ...] | list[float]) -> Translated:
    """Normalise an rgb, rgbw, rgbww or xy input."""
    if mode == "xy":
        return translate_xy((float(value[0]), float(value[1])), 1.0)
    lin, intensity = channels_to_linear(mode, value)
    xy = linear_to_xy(lin)
    if (white := _white(xy, intensity)) is not None:
        return white
    if mode != "rgb" and not any(float(v) > 0 for v in value[:3]) and any(float(v) > 0 for v in value[3:]):
        # only the white channels: always a white, even where a warm/cold mix dips below the locus
        kelvin = max(TWIN_MIN_KELVIN, min(TWIN_MAX_KELVIN, cct_duv(xy)[0]))
        return Translated("white", intensity, xy, kelvin=round(kelvin))
    top = max(lin) or 1.0
    rgb = [linear_to_srgb(c / top) for c in lin]
    hue, sat = color_RGB_to_hs(*rgb)
    return Translated("color", intensity, xy, hue=float(hue), sat=float(sat))
