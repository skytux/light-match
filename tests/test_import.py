"""The importer of public lamp databases and how imported profiles are used."""

from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path
import sys

from homeassistant.core import HomeAssistant
import pytest
import voluptuous as vol

from custom_components.light_match import async_get_database
from custom_components.light_match.database import (
    PROFILES_DIR,
    DeviceInfo,
    ModelProfile,
    ProfileDatabase,
    merge_capabilities,
    validate_db_profile,
)
from custom_components.light_match.profile import KIND_WHITE, Profile, SourceCaps, luminance

from .conftest import add_twin

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "import"))
import importer  # noqa: E402

IMPORTED = PROFILES_DIR / "imported"


# ---------------------------------------------------------------------------
# Curve normalisation and floor inference
# ---------------------------------------------------------------------------


def test_floor_from_flat_power_start() -> None:
    # WiZ 9290022656 in Powercalc: 0.79 W from 1 to 26 (one 0.78 reading), then rising
    wiz = [(1, 0.79), (6, 0.79), (11, 0.79), (16, 0.79), (21, 0.78), (26, 0.79), (31, 0.85), (36, 0.93), (255, 8.5)]
    assert importer.infer_floor(wiz) == 26
    # Hue LCT015: flat only from 1 to 11 -> meter resolution, not a floor
    hue = [(1, 1.07), (6, 1.07), (11, 1.07), (16, 1.12), (21, 1.2), (255, 9.0)]
    assert importer.infer_floor(hue) == 0
    # rising from the start, or flat everywhere: no floor
    assert importer.infer_floor([(1, 1.0), (6, 1.2), (11, 1.4), (16, 1.6), (255, 9.0)]) == 0
    assert importer.infer_floor([(1, 1.0), (16, 1.0), (31, 1.0), (255, 1.0)]) == 0


def test_output_curve_is_normalised_and_monotonic() -> None:
    points = [(1, 0.9), (51, 2.0), (101, 1.95), (151, 5.0), (201, 7.0), (255, 10.4)]
    curve = importer.output_curve(points, standby=0.4)
    assert curve[0][0] == 1 and curve[-1] == [255, 1.0]
    assert curve[0][1] == pytest.approx((0.9 - 0.4) / 10.0, abs=1e-4)  # standby subtracted
    outputs = [y for _, y in curve]
    assert outputs == sorted(outputs)  # the 1.95 W dip does not make the curve fall
    assert all(0 <= y <= 1 for y in outputs)
    validate_db_profile(_imported(capabilities={"output_curve": curve}))
    # with a floor the curve starts at it
    floored = importer.output_curve(points, 0.4, floor=26)
    assert floored[0][0] == 26
    assert importer.output_curve([(1, 0.4), (255, 0.4)], standby=0.4) == []


def test_lumens_and_kelvin_helpers() -> None:
    assert importer.lumens_from_text("Hue white and color ambiance E26/E27 (800/806 lm)") == 806
    assert importer.lumens_from_text("Smart bulb") is None
    assert importer.kelvin(153) == 6540 and importer.kelvin(500) == 2000


# ---------------------------------------------------------------------------
# Joins across sources, on a small fake source tree
# ---------------------------------------------------------------------------


def _write_sources(src: Path) -> None:
    pc = src / "powercalc/profile_library"
    (pc / "signify/LCT999").mkdir(parents=True)
    (pc / "wiz/ESPTEST").mkdir(parents=True)
    library = {
        "manufacturers": [
            {
                "name": "signify",
                "full_name": "Signify",
                "dir_name": "signify",
                "aliases": ["Philips", "Signify Netherlands B.V."],
                "models": [
                    {
                        "id": "LCT999",
                        "aliases": ["9290099999"],
                        "name": "Hue colour test bulb",
                        "device_type": "light",
                        "color_modes": ["color_temp", "hs"],
                        "standby_power": 0.4,
                        "authors": [{"name": "Someone", "email": "someone@example.com"}],
                    }
                ],
            },
            {
                "name": "WiZ",
                "dir_name": "wiz",
                "models": [{"id": "ESPTEST", "name": "Test", "device_type": "light", "standby_power": 0.3}],
            },
        ]
    }
    (pc / "library.json").write_text(json.dumps(library))
    rows = ["bri,mired,watt"]
    for mired in (153, 300, 454):
        for bri in range(1, 256, 5):
            rows.append(f"{bri},{mired},{0.4 + 9.0 * (bri / 255) ** 2:.3f}")
    (pc / "signify/LCT999/color_temp.csv.gz").write_bytes(gzip.compress("\n".join(rows).encode()))
    flat = ["bri,watt"] + [f"{b},{0.8 if b <= 26 else 0.8 + (b - 26) * 0.03:.3f}" for b in range(1, 256, 5)]
    (pc / "wiz/ESPTEST/brightness.csv").write_text("\n".join(flat))
    (src / "powercalc/LICENSE").write_text("MIT License\n\nCopyright (c) 2021 Example Author\n")

    (src / "z2m").mkdir()
    lights = [
        {"model": "9290099999", "vendor": "Philips", "description": "Hue test bulb 1100 lm",
         "zigbeeModel": ["LCT999", "LCT999B"], "whiteLabel": [], "brightness": True,
         "color_temp": [153, 500], "color_xy": True, "color_hs": False},
    ]
    (src / "z2m/lights.json").write_text(json.dumps({"version": "1.0.0", "lights": lights}))

    (src / "deconz/devices/philips").mkdir(parents=True)
    (src / "deconz/devices/philips/light_zll_C.json").write_text(json.dumps({"modelid": ["LCT999"]}))
    (src / "diyhue/BridgeEmulator/lights").mkdir(parents=True)
    (src / "diyhue/BridgeEmulator/lights/light_types.py").write_text(
        'lightTypes["LCT999"]["v1_static"]["capabilities"] = {"control": {"colorgamuttype": "B", '
        '"ct": {"min": 153, "max": 500}, "maxlumen": 806, "mindimlevel": 1000}}\n'
    )
    (src / "pywizlight/pywizlight/tests").mkdir(parents=True)
    (src / "pywizlight/pywizlight/tests/fake_bulb.py").write_text(
        'BULBS = {\n    ("ESP99_TEST_01", "1.0"): {\n        "cctRange": [2200, 2700, 6500, 6500],\n    },\n}\n'
    )
    (src / "yeelight/yeelight").mkdir(parents=True)
    (src / "yeelight/yeelight/main.py").write_text(
        '_MODEL_SPECS = {"testlamp": {"color_temp": {"min": 2700, "max": 6500}, "night_light": False}, '
        '"other": {"color_temp": {"min": BAD, "max": 1}}}\n'
    )


@pytest.fixture
def built(tmp_path: Path) -> tuple[dict, list[str], Path]:
    src = tmp_path / "src"
    _write_sources(src)
    sources = copy.deepcopy(importer.SOURCES)
    try:
        profiles, log = importer.build_all(src)
    finally:
        importer.SOURCES.clear()
        importer.SOURCES.update(sources)
    return profiles, log, src


def test_joins_sources_into_one_profile(built) -> None:
    profiles, log, _ = built
    hue = profiles["signify__lct999"]
    validate_db_profile(hue)
    assert hue["device"]["manufacturer"] == "Signify"
    assert set(hue["device"]["manufacturer_aliases"]) >= {"Philips", "Signify Netherlands B.V."}
    # Powercalc id + alias, then the z2m model and Zigbee model ids
    assert hue["device"]["models"] == ["LCT999", "9290099999", "LCT999B"]
    caps = hue["capabilities"]
    assert caps["color_modes"] == ["color_temp", "hs"]  # Powercalc first
    assert (caps["min_kelvin"], caps["max_kelvin"]) == (2200, 6540)  # measured mired range 153-454
    assert caps["lumens"] == 1100  # z2m description (Powercalc had none), before diyHue's maxlumen
    assert caps["gamut"] == "C"  # deCONZ wins over diyHue's B
    assert any("diyHue says gamut B, deCONZ C" in line for line in log)
    assert "brightness_floor" not in caps
    assert caps["output_curve"][-1] == [255, 1.0]
    assert "someone" not in json.dumps(hue) and "@" not in json.dumps(hue)  # authors never copied


def test_provenance_lists_each_source(built) -> None:
    profiles, _, _ = built
    imports = profiles["signify__lct999"]["provenance"]["imports"]
    by = {(i["source"], i["quality"]): i for i in imports}
    pc = by[("Powercalc profile library", "power-proxy")]
    assert pc["fields"] == ["output_curve"] and pc["licence"] == "MIT"
    assert pc["copyright"] == "Copyright (c) 2021 Example Author"  # read from the licence file
    assert by[("deCONZ REST plugin device description files", "spec")]["licence"] == "BSD-3-Clause"
    z2m = by[("zigbee-herdsman-converters (Zigbee2MQTT)", "spec")]
    assert set(z2m["fields"]) == {"models", "lumens"}
    assert ("diyHue BridgeEmulator light types", "spec") not in by  # nothing of diyHue was used
    assert profiles["signify__lct999"]["provenance"]["method"] == "imported"
    assert profiles["signify__lct999"]["provenance"]["verified"] is False


def test_floor_and_standalone_profiles(built) -> None:
    profiles, _, _ = built
    wiz = profiles["wiz__esptest"]
    assert wiz["capabilities"]["brightness_floor"] == 26
    assert wiz["capabilities"]["output_curve"][0][0] == 26  # the curve starts at the floor
    estimated = [i for i in wiz["provenance"]["imports"] if i["quality"] == "estimated"]
    assert estimated and estimated[0]["fields"] == ["brightness_floor"]
    module = profiles["wiz__esp99_test_01"]
    assert module["capabilities"] == {"min_kelvin": 2200, "max_kelvin": 6500}
    assert module["provenance"]["imports"][0]["licence"] == "MIT"
    lamp = profiles["yeelight__testlamp"]
    assert lamp["provenance"]["imports"][0]["licence"] == "BSD-2-Clause"
    assert "yeelight__other" not in profiles  # unreadable spec skipped
    for prof in profiles.values():
        validate_db_profile(prof)


def test_shared_model_ids_are_dropped() -> None:
    def prof(*models: str) -> dict:
        return {"device": {"manufacturer": "Acme", "models": list(models)}}

    profiles = {"a": prof("A1", "Z9"), "b": prof("B1", "Z9"), "c": prof("C1", "A1"), "d": prof("D1", "D2"), "e": prof("D1")}
    primary = {"a": {"a1"}, "b": {"b1"}, "c": {"c1"}, "d": {"d1", "d2"}, "e": {"d1"}}
    importer.drop_shared_models(profiles, primary)
    assert profiles["a"]["device"]["models"] == ["A1"]  # Z9 was added to two profiles: dropped
    assert profiles["c"]["device"]["models"] == ["C1"]  # A1 is a's own id
    assert profiles["d"]["device"]["models"] == ["D2"]  # D1 is the own id of two profiles
    assert "e" not in profiles  # nothing left


def test_write_ships_licences_and_notice(built, tmp_path: Path) -> None:
    _, _, src = built
    out = tmp_path / "out"
    sources = copy.deepcopy(importer.SOURCES)
    try:
        count, log = importer.write(src, out)
    finally:
        importer.SOURCES.clear()
        importer.SOURCES.update(sources)
    assert count == len(list(out.glob("*.json")))
    assert (out / "licenses/powercalc.txt").exists()
    assert any("licence file missing for deconz" in line for line in log)
    notice = (out / "NOTICE.md").read_text()
    assert "CC BY 4.0" in notice and "BSD-3-Clause" in notice and "python-kasa" in notice


# ---------------------------------------------------------------------------
# The generated profiles in the repository
# ---------------------------------------------------------------------------


def test_generated_profiles_are_clean() -> None:
    files = sorted(IMPORTED.glob("*.json"))
    assert len(files) > 400
    total = sum(f.stat().st_size for f in files)
    assert total < 5 * 1024 * 1024
    licences = {"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0"}
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "@" not in text, path.name  # no author emails
        data = validate_db_profile(json.loads(text))
        assert not data["cells"]
        assert {i["licence"] for i in data["provenance"]["imports"]} <= licences
        curve_quality = {i["quality"] for i in data["provenance"]["imports"] if "output_curve" in i["fields"]}
        assert curve_quality <= {"power-proxy"}
    for key in importer.SOURCES:
        assert (IMPORTED / "licenses" / f"{key}.txt").read_text().strip(), key
    assert "CC BY 4.0" in (IMPORTED / "NOTICE.md").read_text()


# ---------------------------------------------------------------------------
# Schema, ranking, merging and use of the curve
# ---------------------------------------------------------------------------


def _imported(**extra) -> dict:
    data = {
        "format": "light_match_profile",
        "version": 2,
        "revision": 1,
        "name": "Acme X1",
        "license": "CC-BY-4.0",
        "device": {"manufacturer": "Acme", "models": ["X1"]},
        "provenance": {
            "method": "imported",
            "verified": False,
            "imports": [
                {
                    "source": "Example DB",
                    "source_url": "https://example.com/db",
                    "licence": "MIT",
                    "copyright": "Copyright (c) Example",
                    "fields": ["min_kelvin"],
                    "quality": "spec",
                }
            ],
        },
    }
    data.update(extra)
    return data


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["provenance"].update(imports=[]),
        lambda d: d["provenance"].update(verified=True),
        lambda d: d["provenance"]["imports"][0].update(licence="GPL-3.0"),
        lambda d: d["provenance"]["imports"][0].update(quality="calibrated"),
        lambda d: d.update(cells={"white:2700:50": {"brightness": 100}}),
        lambda d: d["provenance"].update(method="by_eye"),  # imports only on imported profiles
        lambda d: d.update(capabilities={"output_curve": [[1, 0.5], [255, 0.4]]}),
        lambda d: d.update(capabilities={"output_curve": [[100, 0.1], [50, 1.0]]}),
        lambda d: d.update(capabilities={"output_curve": [[0, 0.1], [255, 1.0]]}),
    ],
)
def test_imported_schema_rules(change) -> None:
    data = _imported()
    validate_db_profile(copy.deepcopy(data))
    change(data)
    with pytest.raises(vol.Invalid):
        validate_db_profile(data)


def _prof(pid: str, method: str, models: list[str], **caps) -> ModelProfile:
    data = _imported(capabilities=caps) if method == "imported" else {
        "format": "light_match_profile", "version": 2, "revision": 1, "name": pid, "license": "CC-BY-4.0",
        "device": {"manufacturer": "Acme", "models": models},
        "capabilities": caps,
        "provenance": {"method": method, "verified": False, "sources": ["https://example.com/x"]},
    }
    data["device"]["models"] = models
    return ModelProfile(pid, validate_db_profile(data))


def test_ranking_measured_then_imported_then_spec() -> None:
    db = ProfileDatabase(
        bundled={
            "acme/spec": _prof("acme/spec", "spec", ["X1"], subfloor="none"),
            "imported/acme__x1": _prof("imported/acme__x1", "imported", ["X1"], min_kelvin=2200, max_kelvin=6500),
            "acme/all": _prof("acme/all", "by_eye", ["*"], min_kelvin=3000, max_kelvin=5000, brightness_floor=5),
        }
    )
    found = db.candidates(DeviceInfo("Acme", "X1"))
    # a measured community profile first, even a generic one; then imported over spec
    assert [p.id for p in found] == ["acme/all", "imported/acme__x1", "acme/spec"]
    caps, origin = merge_capabilities(found)
    assert (caps["min_kelvin"], caps["max_kelvin"]) == (3000, 5000) and origin["min_kelvin"].id == "acme/all"
    assert caps["subfloor"] == "none" and origin["subfloor"].id == "acme/spec"  # gaps filled
    # the kelvin range is taken as a pair
    half = ModelProfile("acme/half", {**found[0].data, "capabilities": {"max_kelvin": 4000}})
    caps, _ = merge_capabilities([half, found[1]])
    assert (caps["min_kelvin"], caps["max_kelvin"]) == (2200, 6500)


CURVE = ((1, 0.0852), (8, 0.0852), (13, 0.0892), (20, 0.0952), (51, 0.125), (128, 0.3082), (200, 0.6685), (255, 1.0))


def test_output_curve_shapes_default_brightness() -> None:
    caps = SourceCaps.from_modes({"color_temp"}, 2200, 6500, output_curve=CURVE)
    plain = SourceCaps.from_modes({"color_temp"}, 2200, 6500)
    levels = [0.5, 1, 3, 5, 10, 20, 35, 50, 75, 100]
    curve = [caps.curve_level(lvl) for lvl in levels]
    assert curve == sorted(curve) and curve[-1] == pytest.approx(255)
    assert plain.curve_level(50) is None
    # the target output luminance(L*) is met on the corrected curve (lowest reading = overhead)
    b50 = caps.curve_level(50)
    rel = (0.125 + (0.3082 - 0.125) * (b50 - 51) / (128 - 51) - 0.0852) / (1 - 0.0852)
    assert rel == pytest.approx(luminance(50), abs=1e-3)
    # continuous where the linear part joins the curve
    assert caps.curve_level(9.79) == pytest.approx(caps.curve_level(9.81), abs=0.5)

    with_curve = Profile(caps).default_command(KIND_WHITE, 2700, 50)
    without = Profile(plain).default_command(KIND_WHITE, 2700, 50)
    assert without["brightness"] == 128 and with_curve["brightness"] == round(b50)
    # plain behaviour and the reference lamp keep the linear mapping
    assert Profile(caps, fallback="plain").default_command(KIND_WHITE, 2700, 50)["brightness"] == 128
    assert Profile(caps, identity=True).default_command(KIND_WHITE, 2700, 50)["brightness"] == 128


async def test_runtime_kelvin_beats_imported_spec(hass: HomeAssistant, sources, wiz_device) -> None:
    entry = await add_twin(hass, "light.bulb_rgbw")
    data = entry.runtime_data
    assert data.model_profile().id == "imported/wiz__esp01_shrgb_03"  # says 2200-6500 K
    caps = data.caps()
    assert (caps.min_kelvin, caps.max_kelvin) == (2700, 6500)  # what the lamp reports
    db = await async_get_database(hass)
    db.bundled["wiz/measured"] = ModelProfile(
        "wiz/measured",
        validate_db_profile({
            "format": "light_match_profile", "version": 2, "revision": 1, "name": "WiZ measured",
            "license": "CC-BY-4.0", "device": {"manufacturer": "WiZ", "models": ["ESP01_SHRGB_03"]},
            "capabilities": {"min_kelvin": 3000, "max_kelvin": 5000},
            "provenance": {"method": "lux_meter", "verified": False},
        }),
    )
    caps = data.caps()
    assert (caps.min_kelvin, caps.max_kelvin) == (3000, 5000)  # a measured profile overrides the lamp
    del db.bundled["wiz/measured"]


async def test_runtime_gamut_attribute_is_used(hass: HomeAssistant, sources) -> None:
    src = sources["light.bulb_hs"]
    src._attr_extra_state_attributes = {"gamut_type": "B"}
    src.async_write_ha_state()
    entry = await add_twin(hass, "light.bulb_hs")
    assert entry.runtime_data.caps().gamut == ((0.675, 0.322), (0.4091, 0.518), (0.167, 0.04))
