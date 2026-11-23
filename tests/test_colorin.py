"""Colour inputs (rgb, rgbw, rgbww, xy) normalised to the calibrated space."""

from __future__ import annotations

import pytest

from custom_components.light_match.colorin import (
    CW_KELVIN,
    D65,
    WW_KELVIN,
    XYZ_TO_SRGB,
    cct_duv,
    channels_to_xy,
    linear_to_srgb,
    planck_xy,
    srgb_to_linear,
    translate,
)


def test_linearisation_and_locus() -> None:
    assert srgb_to_linear(0) == 0 and srgb_to_linear(255) == pytest.approx(1)
    assert srgb_to_linear(128) == pytest.approx(0.2158, abs=1e-3)
    for kelvin in (2200, 2700, 4000, 6500):
        found, duv = cct_duv(planck_xy(kelvin))
        assert found == pytest.approx(kelvin, rel=0.005) and abs(duv) < 1e-4
    kelvin, duv = cct_duv(D65)
    assert kelvin == pytest.approx(6504, abs=40) and duv == pytest.approx(0.0032, abs=0.0005)


def test_rgb() -> None:
    red = translate("rgb", [255, 0, 0])
    assert red.kind == "color" and red.intensity == 1
    assert red.xy == pytest.approx((0.64, 0.33), abs=0.002)
    assert red.hue == pytest.approx(0, abs=0.5) and red.sat == pytest.approx(100, abs=0.5)
    blue = translate("rgb", (0, 0, 255))
    assert blue.kind == "color" and blue.hue == pytest.approx(240, abs=1)
    # intensity is the strongest channel / 255, the chromaticity does not change
    dim = translate("rgb", [25, 0, 0])
    assert dim.intensity == pytest.approx(25 / 255) and dim.xy == pytest.approx(red.xy, abs=1e-6)
    white = translate("rgb", [255, 255, 255])
    assert white.kind == "white" and white.kelvin == 6500
    x, y = planck_xy(2700)
    lin = [sum(r[i] * c for i, c in enumerate((x / y, 1, (1 - x - y) / y))) for r in XYZ_TO_SRGB]
    warm = translate("rgb", [round(linear_to_srgb(c / max(lin))) for c in lin])
    assert warm.kind == "white" and warm.kelvin == pytest.approx(2700, abs=40)
    pastel = translate("rgb", [255, 128, 200])
    assert pastel.kind == "color" and 0 < pastel.sat < 100


def test_rgbw_and_rgbww() -> None:
    assert translate("rgbw", [12, 0, 0, 0]).intensity == pytest.approx(12 / 255)
    assert translate("rgbw", [12, 0, 0, 0]).hue == pytest.approx(0, abs=0.5)
    # W alone is the neutral (sRGB) white
    w = translate("rgbw", [0, 0, 0, 255])
    assert w.kind == "white" and w.xy == pytest.approx(D65, abs=1e-4)
    # red plus white: a desaturated red, as bright as the strongest channel
    pink = translate("rgbw", [255, 0, 0, 128])
    assert pink.kind == "color" and pink.hue == pytest.approx(0, abs=3) and pink.sat < 90 and pink.intensity == 1
    ww = translate("rgbww", [0, 0, 0, 0, 255])
    assert WW_KELVIN == 2200 and ww.kind == "white" and ww.kelvin == 2200
    cw = translate("rgbww", [0, 0, 0, 255, 0])
    assert cw.kind == "white" and cw.kelvin == pytest.approx(CW_KELVIN, abs=50)
    both = translate("rgbww", [0, 0, 0, 255, 255])
    assert both.kind == "white" and WW_KELVIN < both.kelvin < CW_KELVIN
    assert translate("rgbww", [0, 0, 0, 0, 51]).intensity == pytest.approx(0.2)
    with pytest.raises(ValueError):
        channels_to_xy("rgbw", [1, 2, 3])


def test_xy() -> None:
    assert translate("xy", [0.3127, 0.329]).kind == "white"
    near = translate("xy", planck_xy(3000))
    assert near.kind == "white" and near.kelvin == pytest.approx(3000, abs=30) and near.intensity == 1
    # well off the locus: a colour, even at a "white" temperature
    green = translate("xy", [0.3, 0.6])
    assert green.kind == "color" and 90 < green.hue < 150
    # a white warmer than the twin's range is clamped to it
    candle = translate("xy", planck_xy(2000))
    assert candle.kind == "white" and candle.kelvin == 2200
