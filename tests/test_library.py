"""Profile names, the user's library, groups, the browser and multi-lamp apply."""

from __future__ import annotations

from datetime import date
import re

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_match.const import DOMAIN
from custom_components.light_match.database import ModelProfile
from custom_components.light_match.library import (
    SECTION_BUILTIN,
    SECTION_COMMUNITY,
    SECTION_MINE,
    Library,
    browse,
    make_model,
    profile_filename,
    profile_name,
)

from .conftest import add_twin

PROFILE = {
    "format": "light_match_profile",
    "version": 2,
    "modes": {"color": "rgbw", "white": "color_temp"},
    "floor": 0,
    "cells": {"color:red:5": {"brightness": 26, "rgbw_color": [255, 0, 0, 0]}},
}


def test_profile_name() -> None:
    assert profile_name("WiZ", "ESP01 SHRGB/03", date(2026, 11, 22), "ab12c") == "WiZ_ESP01_SHRGB_03_2026-11-22_ab12c"
    assert profile_name(None, None, date(2026, 1, 2), "zzzzz") == "profile_2026-01-02_zzzzz"
    assert re.fullmatch(r"Acme_X_\d{4}-\d\d-\d\d_[a-z0-9]{5}", profile_name("Acme", "X"))
    assert profile_name("A", "B") != profile_name("A", "B")
    assert profile_filename("WiZ_A_2026-11-22_ab12c") == "WiZ_A_2026-11-22_ab12c.light-match.json"
    assert profile_filename("../x y") == "x_y.light-match.json"


def _device(hass: HomeAssistant, entity_id: str, make: str, model: str, ident: str) -> None:
    owner = MockConfigEntry(domain="test")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("test", ident)}, manufacturer=make, model=model
    )
    er.async_get(hass).async_update_entity(entity_id, device_id=device.id)


async def test_make_model_from_registry_then_model_profile(hass: HomeAssistant, sources) -> None:
    from custom_components.light_match import async_get_database
    from custom_components.light_match.database import validate_db_profile

    _device(hass, "light.bulb_rgbw", "Acme", "Glow 9", "a1")
    rgbw = await add_twin(hass, "light.bulb_rgbw")
    assert make_model(rgbw.runtime_data) == ("Acme", "Glow 9")

    data = validate_db_profile({
        "format": "light_match_profile", "version": 2, "revision": 1, "name": "Nobody N1",
        "license": "CC-BY-4.0", "device": {"manufacturer": "Nobody", "models": ["N1"]},
        "provenance": {"method": "by_eye", "verified": False},
        "modes": {"color": "hs", "white": "color_temp"}, "cells": {},
    })
    (await async_get_database(hass)).bundled["nobody/n1"] = ModelProfile("nobody/n1", data)
    hs = await add_twin(hass, "light.bulb_hs", profile="nobody/n1")
    assert make_model(hs.runtime_data) == ("Nobody", "N1")


async def test_library_crud(hass: HomeAssistant, hass_storage) -> None:
    lib = Library(hass)
    await lib.async_load()
    item = await lib.async_add(PROFILE, make="Acme", model="Glow 9")
    assert re.fullmatch(r"Acme_Glow_9_\d{4}-\d\d-\d\d_[a-z0-9]{5}", item["name"])
    assert item["profile"]["name"] == item["name"] and item["profile"]["make"] == "Acme"
    assert lib.list()[0]["cells"] == 1 and "profile" not in lib.list()[0]

    await lib.async_rename(item["id"], "Kitchen spots")
    assert lib.get(item["id"])["profile"]["name"] == "Kitchen spots"
    # survives a reload from the store
    again = Library(hass)
    await again.async_load()
    assert again.list()[0]["name"] == "Kitchen spots"
    assert hass_storage[f"{DOMAIN}.library"]["data"]["profiles"][0]["id"] == item["id"]

    await again.async_delete(item["id"])
    assert again.list() == []
    for bad in ({}, {**PROFILE, "cells": {}}, {**PROFILE, "version": 99}):
        try:
            await again.async_add(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def _db(pid: str, origin: str = "bundled") -> ModelProfile:
    return ModelProfile(pid, {
        "name": pid, "device": {"manufacturer": "M", "models": ["X"]},
        "provenance": {"method": "spec", "verified": False}, "cells": {},
    }, origin)


async def test_browser_sections_are_separate(hass: HomeAssistant) -> None:
    lib = Library(hass)
    await lib.async_add(PROFILE, name="Mine")
    profiles = {p.id: p for p in (_db("wiz/a"), _db("imported/b"), _db("signify/c", "online"))}
    sections = browse(lib, profiles)
    assert [p["name"] for p in sections[SECTION_MINE]] == ["Mine"]
    assert [p["id"] for p in sections[SECTION_BUILTIN]] == ["wiz/a"]
    assert [p["id"] for p in sections[SECTION_COMMUNITY]] == ["imported/b", "signify/c"]


async def test_browse_and_library_websocket(hass: HomeAssistant, sources, hass_ws_client) -> None:
    rgbw = await add_twin(hass, "light.bulb_rgbw", title="Desk")
    client = await hass_ws_client(hass)

    async def ws(**msg):
        await client.send_json_auto_id(msg)
        return await client.receive_json()

    # an uncalibrated lamp is not saved
    res = await ws(type=f"{DOMAIN}/library/save", entry_id=rgbw.entry_id)
    assert res["error"]["code"] == "invalid_format"
    rgbw.runtime_data.cells["color:red:5"] = {"brightness": 26, "rgbw_color": [255, 0, 0, 0]}
    saved = (await ws(type=f"{DOMAIN}/library/save", entry_id=rgbw.entry_id, make="Acme", model="G9"))["result"]["profile"]
    assert saved["name"].startswith("Acme_G9_") and saved["source"] == "Desk" and saved["cells"] == 1
    imported = (await ws(type=f"{DOMAIN}/library/save", profile=PROFILE, name="From a file"))["result"]["profile"]

    sections = (await ws(type=f"{DOMAIN}/profiles/browse"))["result"]
    assert {p["id"] for p in sections["mine"]} == {saved["id"], imported["id"]}
    assert sections["builtin"] and all(not p["id"].startswith("imported/") for p in sections["builtin"])
    assert any(p["id"].startswith("imported/") for p in sections["community"])

    res = await ws(type=f"{DOMAIN}/library/rename", item_id=imported["id"], name="Renamed")
    assert res["result"]["profile"]["name"] == "Renamed"
    got = (await ws(type=f"{DOMAIN}/library/get", item_id=imported["id"]))["result"]
    assert got["filename"] == "Renamed.light-match.json" and got["profile"]["cells"] == PROFILE["cells"]
    assert (await ws(type=f"{DOMAIN}/library/delete", item_id=imported["id"]))["success"]
    assert (await ws(type=f"{DOMAIN}/library/delete", item_id=imported["id"]))["error"]["code"] == "not_found"
    assert [p["id"] for p in (await ws(type=f"{DOMAIN}/library"))["result"]["profiles"]] == [saved["id"]]


async def test_library_download_view(hass: HomeAssistant, sources, hass_client, hass_client_no_auth) -> None:
    from custom_components.light_match.library import library

    await add_twin(hass, "light.bulb_rgbw")
    item = await library(hass).async_add(PROFILE, name="Saved one")
    path = f"/api/{DOMAIN}/library/{item['id']}"
    assert (await (await hass_client_no_auth()).get(path)).status == 401
    client = await hass_client()
    resp = await client.get(path)
    assert resp.status == 200
    assert resp.headers["Content-Disposition"] == 'attachment; filename="Saved_one.light-match.json"'
    assert (await resp.json())["cells"] == PROFILE["cells"]
    assert (await client.get(f"/api/{DOMAIN}/library/nope")).status == 404


async def test_groups_auto_and_manual(hass: HomeAssistant, sources, hass_ws_client) -> None:
    _device(hass, "light.bulb_rgbw", "Acme", "Glow 9", "a1")
    _device(hass, "light.bulb_hs", "ACME", "Glow 9", "a2")
    _device(hass, "light.bulb_dim", "Other", "D1", "a3")
    rgbw = await add_twin(hass, "light.bulb_rgbw", title="A")
    hs = await add_twin(hass, "light.bulb_hs", title="B")
    dim = await add_twin(hass, "light.bulb_dim", title="C")
    client = await hass_ws_client(hass)

    async def ws(**msg):
        await client.send_json_auto_id(msg)
        return await client.receive_json()

    groups = (await ws(type=f"{DOMAIN}/groups"))["result"]
    # same make and model (any case) form a live group; a lone lamp does not
    assert [(g["name"], sorted(g["entry_ids"]), g["auto"]) for g in groups["auto"]] == [
        ("Acme Glow 9", sorted([rgbw.entry_id, hs.entry_id]), True)
    ]
    assert groups["manual"] == []

    groups = (await ws(type=f"{DOMAIN}/groups/set", name="Living room", entry_ids=[hs.entry_id, dim.entry_id]))["result"]
    group = groups["manual"][0]
    assert group["name"] == "Living room" and group["entry_ids"] == [hs.entry_id, dim.entry_id] and not group["auto"]
    groups = (await ws(type=f"{DOMAIN}/groups/set", group_id=group["id"], name="Lounge", entry_ids=[dim.entry_id]))["result"]
    assert groups["manual"] == [{**group, "name": "Lounge", "entry_ids": [dim.entry_id]}]
    assert (await ws(type=f"{DOMAIN}/groups/set", name="  ", entry_ids=[]))["error"]["code"] == "invalid_format"
    assert (await ws(type=f"{DOMAIN}/groups/set", group_id="nope", name="X", entry_ids=[]))["error"]["code"] == "not_found"
    groups = (await ws(type=f"{DOMAIN}/groups/delete", group_id=group["id"]))["result"]
    assert groups["manual"] == []


async def test_apply_to_many_lamps(hass: HomeAssistant, sources, hass_ws_client) -> None:
    from custom_components.light_match.library import library

    rgbw = await add_twin(hass, "light.bulb_rgbw", title="RGBW")
    hs = await add_twin(hass, "light.bulb_hs", title="HS")
    dim = await add_twin(hass, "light.bulb_dim", title="Dim")
    dim.runtime_data.cells["white:2700:50"] = {"brightness": 120}
    hs.runtime_data.cells["color:blue:5"] = {"brightness": 30, "hs_color": [240, 100]}
    client = await hass_ws_client(hass)
    targets = [rgbw.entry_id, hs.entry_id, dim.entry_id, "nope"]

    async def apply(**src):
        await client.send_json_auto_id({"type": f"{DOMAIN}/profile/apply", "entry_ids": targets, **src})
        return await client.receive_json()

    res = (await apply(profile=PROFILE, merge=True))["result"]["results"]
    by_title = {r["title"]: r for r in res}
    # each lamp converted for its own capabilities
    assert by_title["RGBW"]["imported"] == 1 and rgbw.runtime_data.cells["color:red:5"]["rgbw_color"] == [255, 0, 0, 0]
    assert by_title["HS"]["imported"] == 1 and "hs_color" in hs.runtime_data.cells["color:red:5"]
    assert set(hs.runtime_data.cells) == {"color:red:5", "color:blue:5"}  # merged
    # a brightness-only lamp has no colour cells: refused, its own cells kept
    assert "error" in by_title["Dim"] and dim.runtime_data.cells == {"white:2700:50": {"brightness": 120}}
    assert res[-1] == {"entry_id": "nope", "title": None, "error": "no such lamp"}

    # replace, from a saved profile
    item = await library(hass).async_add(PROFILE, name="Saved")
    res = (await apply(library_id=item["id"]))["result"]["results"]
    assert set(hs.runtime_data.cells) == {"color:red:5"}
    assert dim.runtime_data.cells == {"white:2700:50": {"brightness": 120}}

    # copy from one lamp to the others; the source itself is left out
    rgbw.runtime_data.cells["color:green:20"] = {"brightness": 60, "rgbw_color": [0, 255, 0, 0]}
    res = (await apply(from_entry_id=rgbw.entry_id))["result"]["results"]
    assert res[0]["error"] == "this is the lamp copied from"
    assert set(hs.runtime_data.cells) == {"color:red:5", "color:green:20"}


async def test_apply_refuses_an_empty_source(hass: HomeAssistant, sources, hass_ws_client) -> None:
    rgbw = await add_twin(hass, "light.bulb_rgbw", title="RGBW")
    hs = await add_twin(hass, "light.bulb_hs", title="HS")
    hs.runtime_data.cells["color:blue:5"] = {"brightness": 30, "hs_color": [240, 100]}
    client = await hass_ws_client(hass)
    for src in (
        {"from_entry_id": rgbw.entry_id},
        {"profile": {**PROFILE, "cells": {}}},
        {"library_id": "nope"},
        {},
    ):
        await client.send_json_auto_id({"type": f"{DOMAIN}/profile/apply", "entry_ids": [hs.entry_id], **src})
        res = await client.receive_json()
        assert res["error"]["code"] == "invalid_format", src
    assert hs.runtime_data.cells == {"color:blue:5": {"brightness": 30, "hs_color": [240, 100]}}
