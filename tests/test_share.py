"""The reference space and the portable share flow."""

from __future__ import annotations

from datetime import date
import importlib.util
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import color as color_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.light_match.const import DOMAIN
from custom_components.light_match.database import DeviceInfo, validate_db_profile
from custom_components.light_match.profile import Target
from custom_components.light_match.reference import (
    REFERENCE_GAMUT,
    planck_xy,
    reference_command,
    target_of,
)
from custom_components.light_match.share import (
    portable_profile,
    repo_url,
    submission_url,
    suggested_path,
)

from .conftest import add_twin

ROOT = Path(__file__).resolve().parent.parent


def test_reference_targets() -> None:
    red = target_of("color:red:100")
    assert red["xy"] == pytest.approx([0.64, 0.33], abs=0.002)  # sRGB red
    assert red["relative_output"] == 1.0
    assert target_of("color:red:50")["relative_output"] == pytest.approx(0.184, abs=0.001)
    # Planckian locus: 2700 K and 6500 K
    assert planck_xy(2700) == pytest.approx((0.4599, 0.4106), abs=0.002)
    assert planck_xy(6500) == pytest.approx((0.3135, 0.3237), abs=0.002)
    for key in ("color:blue:20", "color:green:20", "color:purple:5"):
        x, y = target_of(key)["xy"]
        assert color_util.check_point_in_lamps_reach((x, y), REFERENCE_GAMUT)
    assert reference_command(Target("color", 20.0, hue=0.0, sat=100.0)) == {
        "brightness": 51, "xy_color": list(target_of("color:red:20")["xy"]),
    }
    assert reference_command(Target("white", 50.0, kelvin=4000.0)) == {
        "brightness": 128, "color_temp_kelvin": 4000,
    }


def test_reference_doc_is_current() -> None:
    spec = importlib.util.spec_from_file_location("reference_doc", ROOT / "scripts" / "reference_doc.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (ROOT / "docs" / "reference-space.md").read_text(encoding="utf-8") == module.text()


EXPORTED = {
    "format": "light_match_profile",
    "version": 2,
    "modes": {"color": "rgbw", "white": "color_temp"},
    "floor": 26,
    "cells": {"color:red:1": {"brightness": 26, "rgbw_color": [12, 0, 0, 0]}},
}
DEVICE = DeviceInfo("WiZ", "ESP01_SHRGB_03", None, "wiz", "1.25.0")


def test_portable_profile_metadata() -> None:
    prof = portable_profile(
        EXPORTED, DEVICE, {"color_modes": ["rgbw", "color_temp"], "brightness_floor": 26}, "lm-ref-1",
        nickname=" lamp fan ", github="@some-user", notes="Daylight.", today=date(2026, 11, 22),
    )
    assert prof["contributors"] == [
        {"nickname": "lamp fan", "github": "some-user", "role": "calibrated", "date": "2026-11-22"}
    ]
    assert prof["license"] == "CC-BY-4.0"
    assert prof["device"] == {"manufacturer": "WiZ", "models": ["ESP01_SHRGB_03"], "integrations": ["wiz"]}
    assert prof["provenance"] == {
        "method": "by_eye", "verified": False, "notes": "Daylight. Calibrated on firmware 1.25.0.",
    }
    # a reference-space profile is a valid database entry as it is
    validate_db_profile(prof)
    house = portable_profile(EXPORTED, DEVICE, {}, "house")
    assert house["contributors"] == []
    with pytest.raises(vol.Invalid, match="lm-ref-1"):
        validate_db_profile(house)
    with pytest.raises(vol.Invalid):
        portable_profile(EXPORTED, DEVICE, {}, "lm-ref-1", nickname="me@example.com")


def test_submission_link() -> None:
    repo = repo_url({"issue_tracker": "https://github.com/someone/ha-light-match/issues"})
    assert repo == "https://github.com/someone/ha-light-match"
    prof = portable_profile(EXPORTED, DEVICE, {}, "lm-ref-1", nickname="n")
    path = suggested_path(DEVICE)
    assert path == "wiz/esp01shrgb03.json"
    assert suggested_path(DeviceInfo("Signify Netherlands B.V.", "Hue color lamp", "LCT015")) == "signify/lct015.json"
    url, inline = submission_url(repo, prof, path)
    assert inline
    query = parse_qs(urlparse(url).query)
    assert query["template"] == ["new-lamp-profile.yml"]
    assert query["manufacturer"] == ["WiZ"] and query["nickname"] == ["n"]
    assert '"color:red:1"' in query["profile"][0]
    big = dict(prof, cells={f"white:{k}:{lvl}": {"brightness": 200, "color_temp_kelvin": 2700}
                            for k in range(2000, 7000, 50) for lvl in (1, 5, 20, 50, 100)})
    url, inline = submission_url(repo, big, path)
    assert not inline and len(url) < 7500


async def test_share_websocket(hass: HomeAssistant, sources, hass_ws_client) -> None:
    owner = MockConfigEntry(domain="test")
    owner.add_to_hass(hass)
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    hue = devices.async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("hue", "x")},
        manufacturer="Signify Netherlands B.V.", model="Hue color lamp", model_id="LCT015",
    )
    entities.async_get_or_create("light", "hue", "x", suggested_object_id="hue_ref", device_id=hue.id,
                                 config_entry=owner)
    lamp = devices.async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("test", "rgbw")}, manufacturer="Acme", model="A1",
    )
    entities.async_update_entity("light.bulb_rgbw", device_id=lamp.id)
    entry = await add_twin(hass, "light.bulb_rgbw")
    await entry.runtime_data.async_set_cell("color:red:1", {"brightness": 26, "rgbw_color": [12, 0, 0, 0]})
    client = await hass_ws_client(hass)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/profile/share", "entry_id": entry.entry_id, "nickname": "tester",
         "reference": "light.hue_ref"}
    )
    res = (await client.receive_json())["result"]
    assert res["target"] == "lm-ref-1" and res["cells"] == 1
    assert res["path"] == "acme/a1.json" and res["json_in_url"]
    assert res["url"].startswith("https://github.com/skytux/ha-light-match/issues/new?")
    text = str(res["profile"])
    # nothing from the house: no entity ids, names or titles
    for private in ("light.", "bulb", "calibrated", entry.title):
        assert private not in text.replace("'calibrated'", "")
    validate_db_profile(res["profile"])

    await client.send_json_auto_id({"type": f"{DOMAIN}/profile/share", "entry_id": entry.entry_id})
    assert (await client.receive_json())["result"]["target"] == "house"

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/profile/share", "entry_id": entry.entry_id, "nickname": "a@b.c"}
    )
    assert not (await client.receive_json())["success"]

    # a reference-space lamp as the reference gets xy targets
    await client.send_json_auto_id({"type": f"{DOMAIN}/show_target", "entity_id": "light.hue_ref",
                                    "key": "color:blue:20"})
    res = await client.receive_json()
    assert res["success"], res
    assert "xy_color" in res["result"]["sent"]

    await client.send_json_auto_id({"type": f"{DOMAIN}/reference", "key": "white:2700:50"})
    assert (await client.receive_json())["result"]["xy"] == list(planck_xy(2700))
