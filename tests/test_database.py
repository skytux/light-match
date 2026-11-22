"""The lamp profile database: validation, matching, adapters and layering."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest
import voluptuous as vol

from custom_components.light_match.database import (
    DeviceInfo,
    ModelProfile,
    ProfileDatabase,
    firmware_in_range,
    gamut_points,
    load_bundled,
    validate_db_profile,
)
from custom_components.light_match.profile import (
    KIND_COLOR,
    Profile,
    SourceCaps,
    Target,
)

ROOT = Path(__file__).resolve().parent.parent

BASE = {
    "format": "light_match_profile",
    "version": 2,
    "revision": 1,
    "name": "Example",
    "license": "CC-BY-4.0",
    "device": {"manufacturer": "Example Co", "models": ["X1", "X1-B"], "integrations": ["example"]},
    "provenance": {"method": "by_eye", "verified": False},
}


@pytest.fixture(scope="module")
def db() -> ProfileDatabase:
    return ProfileDatabase(bundled=load_bundled(), loaded=True)


@pytest.fixture(scope="module")
def hand_db(db: ProfileDatabase) -> ProfileDatabase:
    """The hand-written profiles only (no profiles/imported/)."""
    return ProfileDatabase(
        bundled={pid: p for pid, p in db.bundled.items() if not pid.startswith("imported/")}, loaded=True
    )


def test_bundled_profiles_load(db: ProfileDatabase, hand_db: ProfileDatabase) -> None:
    assert set(hand_db.bundled) == {
        "wiz/generic", "signify/hue-gamut-a", "signify/hue-gamut-b", "signify/hue-gamut-c",
    }
    imported = [p for pid, p in db.bundled.items() if pid.startswith("imported/")]
    assert len(imported) > 400
    for prof in imported:
        prov = prof.data["provenance"]
        assert prov["method"] == "imported" and prov["verified"] is False and prov["imports"]
        assert not prof.cells
    for prof in hand_db.bundled.values():
        prov = prof.data["provenance"]
        # seeds come only from public specs, unverified, with sources
        assert prov["method"] == "spec" and prov["verified"] is False and prov["sources"]
        assert not prof.cells
    assert db.get("signify/hue-gamut-c").summary()["reference"] is True


def test_database_script_finds_no_problems() -> None:
    spec = importlib.util.spec_from_file_location("profiles_script", ROOT / "scripts" / "profiles.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.check() == []


@pytest.mark.parametrize(
    ("device", "expected"),
    [
        (DeviceInfo("WiZ", "ESP01_SHRGB_03", integration="wiz"), "wiz/generic"),
        (DeviceInfo(None, None, integration="wiz"), "wiz/generic"),
        (DeviceInfo("WiZ", "ESP01_SHRGB_03", integration="zha"), None),
        (DeviceInfo("Signify Netherlands B.V.", "Hue color lamp", "LCT015", "hue"), "signify/hue-gamut-c"),
        (DeviceInfo("Philips", "LCT001", integration="zha"), "signify/hue-gamut-b"),
        (DeviceInfo("Signify Netherlands B.V.", "lst-001", integration="hue"), "signify/hue-gamut-a"),
        (DeviceInfo("Signify Netherlands B.V.", "Hue go", "LLC999", "hue"), None),
        (DeviceInfo("Other", "LCT015", integration="hue"), None),
        (DeviceInfo(), None),
    ],
)
def test_matching(hand_db: ProfileDatabase, device: DeviceInfo, expected: str | None) -> None:
    found = hand_db.match(device)
    assert (found.id if found else None) == expected


def test_exact_model_beats_generic_and_firmware_range() -> None:
    generic = copy.deepcopy(BASE) | {"name": "All"}
    generic["device"] = {"manufacturer": "Example Co", "models": ["*"]}
    exact = copy.deepcopy(BASE)
    exact["device"]["firmware"] = {"min": "2.0", "max": "2.9"}
    db = ProfileDatabase(
        bundled={
            "example/all": ModelProfile("example/all", validate_db_profile(generic)),
            "example/x1": ModelProfile("example/x1", validate_db_profile(exact)),
        }
    )
    assert db.match(DeviceInfo("Example Co", "X1", integration="example", firmware="2.4.1")).id == "example/x1"
    assert db.match(DeviceInfo("example co", "x1b", integration="example")).id == "example/x1"
    assert db.match(DeviceInfo("Example Co", "X1", integration="example", firmware="3.0")).id == "example/all"
    assert db.match(DeviceInfo("Example Co", "Z9")).id == "example/all"
    assert firmware_in_range("v1.2", {"min": "1.0"}) and not firmware_in_range("0.9", {"min": "1.0"})
    assert firmware_in_range("unknown", {"min": "1.0"})


def test_online_profile_wins_only_with_higher_revision(db: ProfileDatabase) -> None:
    local = ProfileDatabase(bundled=dict(db.bundled))
    newer = copy.deepcopy(db.bundled["wiz/generic"].data) | {"revision": 2, "name": "WiZ newer"}
    older = copy.deepcopy(db.bundled["signify/hue-gamut-a"].data) | {"name": "old"}
    local.set_online({"wiz/generic": newer, "signify/hue-gamut-a": older})
    assert local.get("wiz/generic").name == "WiZ newer"
    assert local.get("wiz/generic").origin == "online"
    assert local.get("signify/hue-gamut-a").origin == "bundled"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"provenance": {"method": "spec", "verified": False}}, "cite its sources"),
        ({"provenance": {"method": "spec", "verified": False, "sources": ["https://example.com/a"]},
          "modes": {"color": "hs"}, "cells": {"color:red:5": {"brightness": 9, "hs_color": [0, 100]}}},
         "no calibrated cells"),
        ({"capabilities": {"gamut": "D"}}, "gamut"),
        ({"capabilities": {"min_kelvin": 6500, "max_kelvin": 2700}}, "min_kelvin"),
        ({"modes": {"color": "hs"}, "cells": {"color:red:7": {"brightness": 9, "hs_color": [0, 100]}}},
         "bad cell key"),
        ({"modes": {"color": "rgb"}, "cells": {"color:red:5": {"brightness": 9, "hs_color": [0, 100]}}},
         "needs rgb_color"),
        ({"device": {"manufacturer": "X", "models": []}}, "models"),
    ],
)
def test_db_profile_validation(change: dict, message: str) -> None:
    with pytest.raises(vol.Invalid, match=message):
        validate_db_profile(copy.deepcopy(BASE) | change)


def test_valid_profile_with_cells() -> None:
    data = copy.deepcopy(BASE) | {
        "modes": {"color": "rgbw", "white": "color_temp"},
        "cells": {"color:red:1": {"brightness": 26, "rgbw_color": [12, 0, 0, 0]}},
        "capabilities": {"gamut": {"red": [0.7, 0.3], "green": [0.17, 0.7], "blue": [0.15, 0.05]}},
    }
    clean = validate_db_profile(data)
    assert clean["target"] == "lm-ref-1"
    assert gamut_points(clean["capabilities"]["gamut"])[0] == (0.7, 0.3)
    assert gamut_points("B")[0] == (0.675, 0.322)


def test_layering_user_over_model_over_default() -> None:
    caps = SourceCaps.from_modes(["rgbw", "color_temp"], 2700, 6500, floor=26)
    profile = Profile(caps, {"color:red:5": {"brightness": 30, "rgbw_color": [200, 0, 0, 0]}})
    model, skipped = profile.convert_cells(
        {
            "color:red:5": {"brightness": 40, "hs_color": [0, 100]},
            "color:blue:5": {"brightness": 40, "hs_color": [240, 100]},
            "color:blue:7": {"brightness": 40, "hs_color": [240, 100]},
        },
        {"color": "hs"},
    )
    assert skipped == 1
    profile.model_cells = model
    assert profile.cell_source("color:red:5") == "user"
    assert profile.cell_source("color:blue:5") == "model"
    assert profile.cell_source("color:green:5") == "inferred"
    assert Profile(caps, model_cells=model).cell_source("color:green:5") == "default"
    assert profile.cell_command("color:red:5")["rgbw_color"] == [200, 0, 0, 0]
    assert profile.cell_command("color:blue:5") == {"brightness": 40, "rgbw_color": [0, 0, 255, 0]}
    assert profile.forward(Target(KIND_COLOR, 5.0, hue=240.0)) == {"brightness": 40, "rgbw_color": [0, 0, 255, 0]}
    # the reference behaviour ignores both layers
    identity = Profile(caps, profile.cells, identity=True, model_cells=model)
    assert identity.cell_source("color:blue:5") == "default"
    # the export can include the model layer
    assert set(profile.to_json(effective=True)["cells"]) == {"color:red:5", "color:blue:5"}
    assert set(profile.to_json()["cells"]) == {"color:red:5"}


def test_gamut_adapter_clamps_and_inverts() -> None:
    hs_b = SourceCaps.from_modes(["hs", "color_temp"], 2000, 6500, gamut=gamut_points("B"))
    profile = Profile(hs_b)
    red = profile.cell_command("color:red:100")
    # gamut B's reddest red is xy 0.675,0.322: hue about 16
    assert red["hs_color"][0] == pytest.approx(16.47, abs=0.2)
    # the lamp reports that red back; the twin shows red
    target = profile.inverse({"brightness": 255, "color_mode": "xy", "hs_color": red["hs_color"]})
    assert target.kind == KIND_COLOR and min(target.hue, 360 - target.hue) < 1.5


def test_channel_gain_and_subfloor_adapters() -> None:
    plain = Profile(SourceCaps.from_modes(["rgbw"])).cell_command("color:pink:100")["rgbw_color"]
    gain = SourceCaps.from_modes(["rgbw"], channel_gain=(2.0, 1.0, 1.0, 1.0))
    pink = Profile(gain).cell_command("color:pink:100")["rgbw_color"]
    # the twice as bright red channel is driven at about half its share
    assert pink[0] / max(pink[2], 1) == pytest.approx(plain[0] / max(plain[2], 1) / 2, rel=0.1)
    nosub = SourceCaps.from_modes(["rgbw"], floor=26, subfloor=False)
    assert Profile(nosub).cell_command("color:red:1") == {"brightness": 3, "rgbw_color": [255, 0, 0, 0]}
    sub = SourceCaps.from_modes(["rgbw"], floor=26)
    assert Profile(sub).cell_command("color:red:1") == {"brightness": 26, "rgbw_color": [25, 0, 0, 0]}


def test_contributors_and_license() -> None:
    data = copy.deepcopy(BASE) | {
        "contributors": [
            {"nickname": "lamp fan", "role": "calibrated", "date": "2026-11-20"},
            {"nickname": "m", "github": "some-user", "role": "verified", "date": "2026-11-21"},
        ]
    }
    prof = ModelProfile("example/x1", validate_db_profile(data))
    assert prof.summary()["contributors"][1] == {
        "nickname": "m", "github": "some-user", "role": "verified", "date": "2026-11-21",
    }
    assert prof.summary()["license"] == "CC-BY-4.0"
    for bad in (
        {"nickname": "me@example.com", "role": "calibrated", "date": "2026-11-20"},
        {"nickname": "x", "role": "owner", "date": "2026-11-20"},
        {"nickname": "x", "role": "edited", "date": "20.11.2026"},
        {"role": "edited", "date": "2026-11-20"},
    ):
        with pytest.raises(vol.Invalid):
            validate_db_profile(copy.deepcopy(BASE) | {"contributors": [bad]})
    for lic in (None, "MIT"):
        broken = copy.deepcopy(BASE)
        if lic:
            broken["license"] = lic
        else:
            del broken["license"]
        with pytest.raises(vol.Invalid):
            validate_db_profile(broken)


def _variant(pid: str, **change) -> ModelProfile:
    data = copy.deepcopy(BASE)
    for key, value in change.items():
        data["provenance"][key] = value
    if data["provenance"]["method"] == "spec":
        data["provenance"]["sources"] = ["https://example.com/spec"]
    return ModelProfile(pid, validate_db_profile(data))


def test_ranking_prefers_verified_then_measured() -> None:
    profiles = [
        _variant("example/a-spec", method="spec"),
        _variant("example/b-eye", method="by_eye"),
        _variant("example/c-lux", method="lux_meter"),
        _variant("example/d-eye-verified", method="by_eye", verified=True),
    ]
    db = ProfileDatabase(bundled={p.id: p for p in profiles})
    order = [p.id for p in db.candidates(DeviceInfo("Example Co", "X1", integration="example"))]
    assert order == ["example/d-eye-verified", "example/c-lux", "example/b-eye", "example/a-spec"]


def test_copy_from_model() -> None:
    caps = SourceCaps.from_modes(["hs", "color_temp"], 2000, 6500)
    model = {"color:red:5": {"brightness": 9, "hs_color": [2.0, 100.0]},
             "color:blue:5": {"brightness": 9, "hs_color": [238.0, 100.0]}}
    profile = Profile(caps, {"color:red:5": {"brightness": 12, "hs_color": [0.0, 100.0]}}, model_cells=model)
    assert profile.copy_model("color:blue:5") == 1
    assert profile.cells["color:blue:5"]["hs_color"] == [238.0, 100.0]
    assert profile.copy_model("color:green:5") == 0
    profile.reset_cell("color:blue:5")
    assert profile.copy_model() == 1  # the user's red stays
    assert profile.cells["color:red:5"]["brightness"] == 12
