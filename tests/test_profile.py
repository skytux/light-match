"""Translation math: defaults, sub-floor, interpolation, inverse mapping."""

from __future__ import annotations

from typing import Any

from homeassistant.util import color as color_util
import pytest

from custom_components.light_match.const import BEHAVIOUR_PLAIN, BRIGHTNESS_LEVELS
from custom_components.light_match.profile import (
    KIND_COLOR,
    KIND_WHITE,
    Profile,
    SourceCaps,
    Target,
    cell_target,
    convert_command,
    grid_cells,
    lightness,
    luminance,
    nominal_command,
    parse_cell_key,
    validate_command,
)

RGBW = SourceCaps.from_modes(["rgbw", "color_temp"], 2700, 6500, floor=26)
RGBW_NOCT = SourceCaps.from_modes(["rgbw"], floor=26)
HS = SourceCaps.from_modes(["hs", "color_temp"], 2000, 6500)
DIM = SourceCaps.from_modes(["brightness"])


def state_from(cmd: dict[str, Any], mode: str) -> dict[str, Any]:
    """What a light would report after receiving cmd."""
    state: dict[str, Any] = {"brightness": cmd["brightness"], "color_mode": mode}
    if mode == "rgbw":
        state["rgbw_color"] = cmd["rgbw_color"]
        state["hs_color"] = color_util.color_RGB_to_hs(*color_util.color_rgbw_to_rgb(*cmd["rgbw_color"]))
    elif mode == "hs":
        state["hs_color"] = cmd["hs_color"]
    elif mode == "color_temp":
        state["color_temp_kelvin"] = cmd["color_temp_kelvin"]
    return state


def hue_diff(a: float, b: float) -> float:
    return abs(((a - b + 180) % 360) - 180)


def test_caps_from_modes() -> None:
    assert (RGBW.color_mode, RGBW.white_mode) == ("rgbw", "color_temp")
    assert (RGBW_NOCT.color_mode, RGBW_NOCT.white_mode) == ("rgbw", "rgbw")
    assert (HS.color_mode, HS.white_mode) == ("hs", "color_temp")
    assert SourceCaps.from_modes(["xy"]).color_mode == "hs"
    assert (DIM.color_mode, DIM.white_mode) == (None, "brightness")
    assert SourceCaps.from_modes(["onoff"]).white_mode is None
    assert SourceCaps.from_modes(["rgb"], floor=999).floor == 255


def test_grid() -> None:
    cells = grid_cells(RGBW)
    assert len(cells) == (4 + 8) * 5
    keys = {c["key"] for c in cells}
    assert "color:red:1" in keys and "white:6500:100" in keys
    assert len(grid_cells(DIM)) == 5
    assert grid_cells(SourceCaps.from_modes(["onoff"])) == []
    with pytest.raises(ValueError):
        parse_cell_key("color:magenta:5")
    with pytest.raises(ValueError):
        parse_cell_key("white:2700:7")
    with pytest.raises(ValueError):
        parse_cell_key("nonsense")
    assert cell_target("color:green:20") == Target(KIND_COLOR, 20.0, hue=120.0)
    assert cell_target("white:4000:50") == Target(KIND_WHITE, 50.0, kelvin=4000.0)


def test_lightness_roundtrip() -> None:
    for y in (0.0, 0.001, 0.01, 0.2, 0.5, 1.0):
        assert luminance(lightness(y)) == pytest.approx(y, abs=1e-9)
    assert lightness(1.0) == pytest.approx(100.0)


def test_subfloor_trick_scales_channels_below_floor() -> None:
    profile = Profile(RGBW)
    cmd = profile.forward(Target(KIND_COLOR, 1.0, hue=0.0))
    # 1 % is 2.55/255; the floor is 26, so the floor is sent and red scaled down
    assert cmd["brightness"] == 26
    assert cmd["rgbw_color"] == [round(255 * 2.55 / 26), 0, 0, 0]
    # above the floor the channels are full
    cmd = profile.forward(Target(KIND_COLOR, 50.0, hue=0.0))
    assert cmd == {"brightness": 128, "rgbw_color": [255, 0, 0, 0]}


def test_subfloor_never_sends_all_zero_channels() -> None:
    profile = Profile(RGBW)
    cmd = profile.forward(Target(KIND_COLOR, 0.05, hue=240.0))
    assert cmd["brightness"] == 26
    assert max(cmd["rgbw_color"]) == 1


def test_plain_and_no_floor() -> None:
    plain = Profile(RGBW, fallback=BEHAVIOUR_PLAIN)
    assert plain.forward(Target(KIND_COLOR, 1.0, hue=0.0)) == {"brightness": 3, "rgbw_color": [255, 0, 0, 0]}
    nofloor = Profile(SourceCaps.from_modes(["rgb"]))
    assert nofloor.forward(Target(KIND_COLOR, 1.0, hue=0.0)) == {"brightness": 3, "rgb_color": [255, 0, 0]}
    # below brightness 1 the channels carry the rest
    cmd = nofloor.forward(Target(KIND_COLOR, 0.2, hue=0.0))
    assert cmd == {"brightness": 1, "rgb_color": [round(255 * 0.51), 0, 0]}


def test_identity_ignores_cells() -> None:
    profile = Profile(RGBW, {"color:red:50": {"brightness": 10, "rgbw_color": [1, 2, 3, 4]}}, identity=True)
    assert profile.forward(Target(KIND_COLOR, 50.0, hue=0.0)) == {"brightness": 128, "rgbw_color": [255, 0, 0, 0]}
    assert profile.forward(Target(KIND_COLOR, 1.0, hue=0.0))["brightness"] == 3


def test_white_defaults() -> None:
    assert Profile(RGBW).forward(Target(KIND_WHITE, 20.0, kelvin=4000)) == {
        "brightness": 51,
        "color_temp_kelvin": 4000,
    }
    # outside a narrow ct range the colour LEDs show the white (see test_narrow_kelvin_range)
    assert "rgbw_color" in Profile(RGBW).forward(Target(KIND_WHITE, 20.0, kelvin=2200))
    # a ct-only lamp and the reference lamp clamp to the range
    ct_only = SourceCaps.from_modes(["color_temp"], 2700, 6500)
    assert Profile(ct_only).forward(Target(KIND_WHITE, 20.0, kelvin=2200))["color_temp_kelvin"] == 2700
    assert Profile(RGBW, identity=True).forward(Target(KIND_WHITE, 20.0, kelvin=2200))["color_temp_kelvin"] == 2700
    # a lamp without colour temperature gets white from its channels, W used
    cmd = Profile(RGBW_NOCT).forward(Target(KIND_WHITE, 100.0, kelvin=6500))
    assert cmd["rgbw_color"][3] == 255
    assert Profile(DIM).forward(Target(KIND_WHITE, 50.0)) == {"brightness": 128}
    assert Profile(DIM).forward(Target(KIND_COLOR, 50.0, hue=10)) == {"brightness": 128}
    assert Profile(SourceCaps.from_modes(["onoff"])).forward(Target(KIND_WHITE, 50.0)) == {}


def test_calibrated_cell_is_used_verbatim() -> None:
    cell = {"brightness": 200, "rgbw_color": [255, 20, 0, 0]}
    profile = Profile(RGBW, {"color:red:50": cell})
    assert profile.forward(Target(KIND_COLOR, 50.0, hue=0.0)) == cell
    assert profile.cell_command("color:red:50") == cell
    plain = Profile(RGBW, {"color:red:50": cell}, infer=False)
    assert plain.cell_command("color:red:20") == plain.default_command(KIND_COLOR, 0.0, 20.0)
    # with inference the neighbour takes the calibrated colour and brightness ratio
    inferred = profile.cell_command("color:red:20")
    assert inferred["rgbw_color"] == [255, 20, 0, 0]
    assert inferred["brightness"] > profile.default_command(KIND_COLOR, 0.0, 20.0)["brightness"]
    assert profile.cell_source("color:red:20") == "inferred"


def test_inference_learns_brightness_curve_and_colour() -> None:
    # the lamp is too bright: every calibrated white is at about half the L* of the default
    ct = SourceCaps.from_modes(["color_temp"], 2200, 6500)
    half = {
        f"white:2700:{lvl}": {
            "brightness": max(1, round(luminance(lightness(lvl / 100) * 0.5) * 255)),
            "color_temp_kelvin": 2900,
        }
        for lvl in (1, 20, 100)
    }
    profile = Profile(ct, half)
    for lvl in (5, 50):
        cmd = profile.cell_command(f"white:2700:{lvl}")
        expected = luminance(lightness(lvl / 100) * 0.5) * 255
        assert cmd["brightness"] == pytest.approx(expected, abs=1.5)
        assert cmd["color_temp_kelvin"] == 2900  # same anchor: the calibrated tint
    # another white row gets the brightness correction but keeps its own colour
    cold = profile.cell_command("white:6500:50")
    assert cold["color_temp_kelvin"] == 6500
    assert cold["brightness"] == pytest.approx(luminance(lightness(0.5) * 0.5) * 255, abs=1.5)
    # forward() between cells follows the inferred cells
    assert profile.forward(Target(KIND_WHITE, 50.0, kelvin=2700.0)) == profile.cell_command("white:2700:50")
    # beyond the calibrated levels the ratio is held: 100 % gets the 100 % ratio
    assert profile._correction_at(KIND_WHITE, 100.0) == pytest.approx(0.5, abs=0.02)
    # colour cells borrow the white correction when no colour cell is calibrated
    rgbw = Profile(RGBW, {"white:2700:50": {"brightness": 64, "color_temp_kelvin": 2700}})
    assert rgbw.cell_command("color:red:50")["brightness"] < 128


def test_quick_keys() -> None:
    from custom_components.light_match.profile import quick_keys

    assert quick_keys(RGBW) == ["white:2700:1", "white:2700:20", "white:2700:100", "color:red:20", "color:blue:20"]
    ct = SourceCaps.from_modes(["color_temp"], 2200, 6500)
    assert quick_keys(ct)[3:] == ["white:6500:20", "white:2200:20"]
    assert quick_keys(DIM) == [f"white:2700:{lvl}" for lvl in (1, 5, 20, 50, 100)]
    for caps in (RGBW, RGBW_NOCT, HS, DIM, ct):
        keys = {c["key"] for c in grid_cells(caps)}
        assert set(quick_keys(caps)) <= keys


def test_brightness_interpolates_perceptually_and_monotonically() -> None:
    profile = Profile(HS, {"color:red:20": {"brightness": 40, "hs_color": [0, 100]},
                           "color:red:50": {"brightness": 160, "hs_color": [0, 100]}})
    levels = [profile.forward_point(Target(KIND_COLOR, b, hue=0.0)).level for b in range(1, 101)]
    assert all(b >= a for a, b in zip(levels, levels[1:], strict=False))
    mid = profile.forward_point(Target(KIND_COLOR, 35.0, hue=0.0)).level
    assert 40 < mid < 160
    # perceptual blend: the L* of the result sits at the L* fraction of 35 % between 20 % and 50 %
    t = (lightness(0.35) - lightness(0.2)) / (lightness(0.5) - lightness(0.2))
    expected = lightness(40 / 255) + (lightness(160 / 255) - lightness(40 / 255)) * t
    assert lightness(mid / 255) == pytest.approx(expected, abs=1e-6)


def test_below_lowest_level_is_proportional() -> None:
    profile = Profile(HS, {"color:red:1": {"brightness": 6, "hs_color": [0, 100]}})
    assert profile.forward_point(Target(KIND_COLOR, 0.5, hue=0.0)).level == pytest.approx(3.0)


def test_hue_interpolates_on_the_circle() -> None:
    profile = Profile(HS, {
        "color:pink:50": {"brightness": 128, "hs_color": [320, 100]},
        "color:red:50": {"brightness": 128, "hs_color": [5, 100]},
    })
    point = profile.forward_point(Target(KIND_COLOR, 50.0, hue=340.0))
    assert point.color[0] == pytest.approx(342.5)
    # defaults are identity in hue
    assert Profile(HS).forward_point(Target(KIND_COLOR, 50.0, hue=200.0)).color[0] == pytest.approx(200.0)


def test_rgbw_hue_between_anchors() -> None:
    cmd = Profile(RGBW).forward(Target(KIND_COLOR, 100.0, hue=15.0))
    r, g, b, w = cmd["rgbw_color"]
    assert r == 255 and 0 < g < 255 and b == 0 and w == 0


def test_saturation_blends_towards_white() -> None:
    hs = Profile(HS).forward(Target(KIND_COLOR, 100.0, hue=120.0, sat=50.0))
    assert hs["hs_color"] == [120.0, 50.0]
    rgbw = Profile(RGBW).forward(Target(KIND_COLOR, 100.0, hue=120.0, sat=50.0))
    assert rgbw["rgbw_color"][1] == 255 and rgbw["rgbw_color"][3] == 255


def test_kelvin_interpolates_in_mired() -> None:
    profile = Profile(HS, {
        "white:2700:50": {"brightness": 128, "color_temp_kelvin": 2500},
        "white:4000:50": {"brightness": 128, "color_temp_kelvin": 4200},
    })
    point = profile.forward_point(Target(KIND_WHITE, 50.0, kelvin=3300))
    t = (1e6 / 3300 - 1e6 / 2700) / (1e6 / 4000 - 1e6 / 2700)
    assert point.color[0] == pytest.approx(1e6 / 2500 + (1e6 / 4200 - 1e6 / 2500) * t)


@pytest.mark.parametrize("caps,mode", [(RGBW, "rgbw"), (HS, "hs")])
@pytest.mark.parametrize("hue", [0.0, 15.0, 100.0, 200.0, 300.0, 350.0])
@pytest.mark.parametrize("brightness", [2.0, 10.0, 35.0, 80.0])
def test_inverse_color_roundtrip(caps: SourceCaps, mode: str, hue: float, brightness: float) -> None:
    profile = Profile(caps)
    target = Target(KIND_COLOR, brightness, hue=hue)
    found = profile.inverse(state_from(profile.forward(target), mode))
    assert found is not None and found.kind == KIND_COLOR
    assert hue_diff(found.hue, hue) < 3
    assert found.brightness == pytest.approx(brightness, rel=0.12, abs=0.4)


def test_inverse_through_calibrated_cells() -> None:
    profile = Profile(HS, {
        "color:red:50": {"brightness": 90, "hs_color": [8, 100]},
        "color:orange:50": {"brightness": 100, "hs_color": [40, 100]},
    })
    target = Target(KIND_COLOR, 50.0, hue=15.0)
    found = profile.inverse(state_from(profile.forward(target), "hs"))
    assert hue_diff(found.hue, 15.0) < 1
    assert found.brightness == pytest.approx(50.0, abs=1.0)


@pytest.mark.parametrize("kelvin", [2700, 3300, 4000, 5500])
def test_inverse_white_roundtrip(kelvin: int) -> None:
    profile = Profile(RGBW)
    target = Target(KIND_WHITE, 40.0, kelvin=kelvin)
    found = profile.inverse(state_from(profile.forward(target), "color_temp"))
    assert found.kind == KIND_WHITE
    assert abs(found.kelvin - kelvin) < 60
    assert found.brightness == pytest.approx(40.0, abs=0.5)


def test_inverse_white_on_channel_lamp() -> None:
    profile = Profile(RGBW_NOCT)
    found = profile.inverse(state_from(profile.forward(Target(KIND_WHITE, 60.0, kelvin=6500)), "rgbw"))
    assert found.kind == KIND_WHITE
    assert found.brightness == pytest.approx(60.0, abs=1.0)


def test_inverse_subfloor_state() -> None:
    profile = Profile(RGBW)
    found = profile.inverse({"brightness": 26, "color_mode": "rgbw", "rgbw_color": [12, 0, 0, 0], "hs_color": (0, 100)})
    # 26 * 12 / 255 = 1.22 of 255 -> about 0.48 %
    assert found.brightness == pytest.approx(100 * 26 * 12 / 255 / 255, rel=0.05)
    assert Profile(DIM).inverse({"brightness": 128, "color_mode": "brightness"}).brightness == pytest.approx(50.2, abs=0.3)
    assert profile.inverse({"color_mode": "rgbw"}) is None


def test_validate_command() -> None:
    assert validate_command({"brightness": "10", "rgbw_color": [1, 0, 0, 0]}, "rgbw") == {
        "brightness": 10, "rgbw_color": [1, 0, 0, 0]}
    for bad, mode in [
        ({"rgbw_color": [1, 0, 0, 0]}, "rgbw"),
        ({"brightness": 0, "rgbw_color": [1, 0, 0, 0]}, "rgbw"),
        ({"brightness": 5, "rgbw_color": [0, 0, 0, 0]}, "rgbw"),
        ({"brightness": 5, "rgbw_color": [1, 0, 0]}, "rgbw"),
        ({"brightness": 5, "hs_color": [400, 10]}, "hs"),
        ({"brightness": 5, "color_temp_kelvin": 100}, "color_temp"),
        ({"brightness": 5}, "hs"),
        ("x", "hs"),
    ]:
        with pytest.raises(ValueError):
            validate_command(bad, mode)


def test_set_cell_validates_key_and_mode() -> None:
    profile = Profile(DIM)
    with pytest.raises(ValueError):
        profile.set_cell("color:red:5", {"brightness": 5})
    assert profile.set_cell("white:2700:5", {"brightness": 7}) == {"brightness": 7}
    assert profile.reset_cell("white:2700:5") is True
    assert profile.reset_cell("white:2700:5") is False


def test_export_import_roundtrip_and_conversion() -> None:
    source = Profile(RGBW, {
        "color:green:20": {"brightness": 40, "rgbw_color": [0, 255, 0, 0]},
        "white:4000:50": {"brightness": 120, "color_temp_kelvin": 4100},
    })
    data = source.to_json(model="Example A60")
    assert data["model"] == "Example A60" and data["modes"] == {"color": "rgbw", "white": "color_temp"}

    same = Profile(RGBW)
    assert same.import_json(data) == (2, 0)
    assert same.cells == source.cells

    hs = Profile(HS)
    assert hs.import_json(data) == (2, 0)
    assert hs.cells["color:green:20"]["hs_color"][0] == pytest.approx(120.0)
    assert hs.cells["white:4000:50"]["color_temp_kelvin"] == 4100

    dim = Profile(DIM)
    imported, skipped = dim.import_json(data)
    assert (imported, skipped) == (0, 2)  # no colour cells, 4000 K row does not exist

    merged = Profile(RGBW, {"color:red:1": {"brightness": 26, "rgbw_color": [12, 0, 0, 0]}})
    merged.import_json(data, replace=False)
    assert len(merged.cells) == 3

    with pytest.raises(ValueError):
        Profile(RGBW).import_json({"format": "other"})
    with pytest.raises(ValueError):
        Profile(RGBW).import_json({"format": "light_match_profile", "version": 99})


def test_convert_command() -> None:
    assert convert_command({"brightness": 10, "hs_color": [0, 100]}, "hs", "color_temp", HS) is None
    cmd = convert_command({"brightness": 100, "color_temp_kelvin": 3000}, "color_temp", "rgb", RGBW)
    assert cmd["rgb_color"][0] == 255 and cmd["rgb_color"][2] < 255
    assert convert_command({"brightness": 100, "rgb_color": [255, 0, 0]}, "rgb", "brightness", RGBW) == {"brightness": 100}


def test_brightness_curve() -> None:
    profile = Profile(HS, {"color:red:50": {"brightness": 1, "hs_color": [0, 100]}})
    written = profile.apply_brightness_curve({"10": 30, "100": 100})
    assert written == len(grid_cells(HS)) - 1
    assert profile.cells["color:red:50"]["brightness"] == 1  # calibrated cell kept
    # 5 % -> 15 % (linear from 0,0 to 10,30)
    assert profile.cells["white:2700:5"]["brightness"] == round(15 * 2.55)
    assert Profile(HS).apply_brightness_curve({}) == 0


def test_nominal_command() -> None:
    assert nominal_command(Target(KIND_COLOR, 1.0, hue=30.0)) == {"brightness": 3, "hs_color": [30.0, 100.0]}
    assert nominal_command(Target(KIND_WHITE, 100.0, kelvin=2700)) == {"brightness": 255, "color_temp_kelvin": 2700}
    assert set(BRIGHTNESS_LEVELS) == {1.0, 5.0, 20.0, 50.0, 100.0}


def test_narrow_kelvin_range() -> None:
    profile = Profile(RGBW)
    assert RGBW.narrow_white and not HS.narrow_white
    assert profile.cell_mode("white:2200:50") == "rgbw" and profile.cell_mode("white:2700:50") == "color_temp"
    warm = profile.forward(Target(KIND_WHITE, 50.0, kelvin=2200))
    r, g, b, _w = warm["rgbw_color"]
    assert r >= g >= b  # a warm white from the colour LEDs
    # inside the range the lamp's own white is used, also between a colour-LED row and a ct row
    assert profile.forward(Target(KIND_WHITE, 50.0, kelvin=2800))["color_temp_kelvin"] == 2800
    between = profile.forward(Target(KIND_WHITE, 50.0, kelvin=2400))
    assert "rgbw_color" in between
    # a 2200 cell is calibrated in the colour mode; an old ct cell there is ignored
    with pytest.raises(ValueError):
        profile.set_cell("white:2200:50", {"brightness": 100, "color_temp_kelvin": 2700})
    cell = profile.set_cell("white:2200:50", {"brightness": 100, "rgbw_color": [255, 120, 20, 0]})
    assert profile.forward(Target(KIND_WHITE, 50.0, kelvin=2200)) == cell
    old = Profile(RGBW, {"white:2200:50": {"brightness": 100, "color_temp_kelvin": 2700}})
    assert old.cell_source("white:2200:50") != "user"
    assert "rgbw_color" in old.forward(Target(KIND_WHITE, 50.0, kelvin=2200))
    # export/import keeps colour-LED whites
    copy, skipped = Profile(RGBW).convert_cells(profile.cells, {"color": "rgbw", "white": "color_temp"})
    assert skipped == 0 and copy["white:2200:50"] == cell
    # the state of a colour-LED white maps back to that white
    state = {"brightness": warm["brightness"], "color_mode": "rgbw", "rgbw_color": warm["rgbw_color"],
             "hs_color": list(color_util.color_RGB_to_hs(*color_util.color_rgbw_to_rgb(*warm["rgbw_color"])))}
    back = profile.inverse(state)
    assert back.kind == KIND_WHITE and back.kelvin < 2600
    # a full-saturation orange stays a colour
    orange = profile.forward(Target(KIND_COLOR, 50.0, hue=30.0))
    hs = color_util.color_RGB_to_hs(*color_util.color_rgbw_to_rgb(*orange["rgbw_color"]))
    state = {"brightness": orange["brightness"], "color_mode": "rgbw", "rgbw_color": orange["rgbw_color"], "hs_color": list(hs)}
    assert profile.inverse(state).kind == KIND_COLOR
