"""Websocket commands, services and storage."""

from __future__ import annotations

import json
import re

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest

from custom_components.light_match.const import DOMAIN

from .conftest import add_twin, twin_id


async def test_storage_roundtrip(hass: HomeAssistant, sources, hass_storage) -> None:
    entry = await add_twin(hass, "light.bulb_rgbw")
    cell = {"brightness": 26, "rgbw_color": [12, 0, 0, 0]}
    await entry.runtime_data.async_set_cell("color:red:1", cell)
    assert hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"] == {"cells": {"color:red:1": cell}}

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.cells == {"color:red:1": cell}

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{entry.entry_id}" not in hass_storage


async def test_storage_loads_existing(hass: HomeAssistant, sources, hass_storage) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, title="Twin", data={"source": "light.bulb_hs"}, unique_id="light.bulb_hs")
    hass_storage[f"{DOMAIN}.{entry.entry_id}"] = {
        "version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}",
        "data": {"cells": {"white:2700:5": {"brightness": 9, "color_temp_kelvin": 2600}}},
    }
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.cells["white:2700:5"]["brightness"] == 9


async def test_websocket_commands(hass: HomeAssistant, sources, hass_ws_client) -> None:
    entry = await add_twin(hass, "light.bulb_rgbw", auto_floor=False, brightness_floor=26)
    ref = await add_twin(hass, "light.bulb_hs", title="Reference", behaviour="reference")
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": f"{DOMAIN}/entries"})
    res = await client.receive_json()
    assert res["success"]
    entries = {e["entry_id"]: e for e in res["result"]["entries"]}
    assert entries[entry.entry_id]["modes"] == {"color": "rgbw", "white": "color_temp"}
    assert entries[entry.entry_id]["total"] == 60
    assert entries[entry.entry_id]["floor"] == 26
    assert entries[ref.entry_id]["behaviour"] == "reference"

    await client.send_json_auto_id({"type": f"{DOMAIN}/profile", "entry_id": entry.entry_id})
    res = await client.receive_json()
    cells = {c["key"]: c for c in res["result"]["cells"]}
    assert cells["color:red:1"]["command"] == {"brightness": 26, "rgbw_color": [25, 0, 0, 0]}
    assert cells["color:red:1"]["calibrated"] is False
    # auto: a white below the floor (26) is shown with the colour LEDs, above it in ct
    assert cells["white:2700:1"]["mode"] == "rgbw"
    assert cells["white:2700:1"]["default_mode"] == "rgbw"
    assert cells["white:2700:1"]["modes"] == ["color_temp", "rgbw"]
    assert cells["white:2700:1"]["command"]["brightness"] == 26
    assert set(cells["white:2700:1"]["commands"]) == {"color_temp", "rgbw"}
    assert "color_temp_kelvin" in cells["white:2700:1"]["commands"]["color_temp"]
    assert cells["white:2700:20"]["mode"] == "color_temp"
    assert "modes" not in cells["color:red:1"]

    # a white saved in its other mode switches that cell
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/cell/set", "entry_id": entry.entry_id, "key": "white:4000:20",
         "mode": "rgbw", "command": {"brightness": 51, "rgbw_color": [40, 30, 0, 255]}}
    )
    res = await client.receive_json()
    assert res["success"], res
    await client.send_json_auto_id({"type": f"{DOMAIN}/profile", "entry_id": entry.entry_id})
    cells = {c["key"]: c for c in (await client.receive_json())["result"]["cells"]}
    assert cells["white:4000:20"]["mode"] == "rgbw"
    assert cells["white:4000:20"]["default_mode"] == "color_temp"
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/cell/set", "entry_id": entry.entry_id, "key": "white:4000:20",
         "mode": "hs", "command": {"brightness": 51, "hs_color": [30, 50]}}
    )
    assert not (await client.receive_json())["success"]
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/cell/reset", "entry_id": entry.entry_id, "key": "white:4000:20"}
    )
    assert (await client.receive_json())["success"]

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/cell/set", "entry_id": entry.entry_id, "key": "color:red:1",
         "command": {"brightness": 26, "rgbw_color": [12, 0, 0, 0]}}
    )
    res = await client.receive_json()
    assert res["success"], res
    assert entry.runtime_data.cells["color:red:1"]["rgbw_color"] == [12, 0, 0, 0]

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/cell/set", "entry_id": entry.entry_id, "key": "color:red:1",
         "command": {"brightness": 26, "hs_color": [0, 100]}}
    )
    res = await client.receive_json()
    assert not res["success"] and res["error"]["code"] == "invalid_format"

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/preview", "entry_id": entry.entry_id,
         "command": {"brightness": 40, "rgbw_color": [0, 0, 200, 0], "effect": "x"}}
    )
    res = await client.receive_json()
    assert res["success"], res
    assert sources["light.bulb_rgbw"].calls[-1][1] == {"brightness": 40, "rgbw_color": (0, 0, 200, 0)}

    # a twin reference shows the target through its own profile (pass-through here)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/show_target", "entity_id": twin_id(hass, ref), "key": "color:orange:20"}
    )
    res = await client.receive_json()
    assert res["success"], res
    assert res["result"]["sent"] == {"brightness": 51, "hs_color": [30.0, 100.0]}
    assert sources["light.bulb_hs"].calls[-1][1]["hs_color"] == (30.0, 100.0)

    # any other light gets the plain command
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/show_target", "entity_id": "light.bulb_dim", "key": "white:4000:50"}
    )
    res = await client.receive_json()
    assert res["success"], res
    assert sources["light.bulb_dim"].calls[-1][1]["brightness"] == 128

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/show_target", "entity_id": "light.bulb_dim", "key": "bad"}
    )
    assert not (await client.receive_json())["success"]

    await client.send_json_auto_id({"type": f"{DOMAIN}/profile/export", "entry_id": entry.entry_id, "model": "X"})
    res = await client.receive_json()
    exported = res["result"]["profile"]
    assert exported["model"] == "X" and "color:red:1" in exported["cells"]

    await client.send_json_auto_id({"type": f"{DOMAIN}/cell/reset", "entry_id": entry.entry_id, "key": "color:red:1"})
    res = await client.receive_json()
    assert res["result"] == {"removed": True}
    assert entry.runtime_data.cells == {}

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/profile/import", "entry_id": ref.entry_id, "profile": exported}
    )
    res = await client.receive_json()
    assert res["result"] == {"imported": 1, "skipped": 0}
    assert ref.runtime_data.cells["color:red:1"]["hs_color"][0] == pytest.approx(0.0)

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/profile/import", "entry_id": ref.entry_id, "profile": {"format": "nope"}}
    )
    assert not (await client.receive_json())["success"]

    await client.send_json_auto_id({"type": f"{DOMAIN}/profile", "entry_id": "missing"})
    res = await client.receive_json()
    assert res["error"]["code"] == "not_found"


async def test_websocket_writes_need_admin(hass: HomeAssistant, sources, hass_ws_client, hass_read_only_access_token) -> None:
    entry = await add_twin(hass, "light.bulb_rgbw")
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/cell/set", "entry_id": entry.entry_id, "key": "color:red:1",
         "command": {"brightness": 26, "rgbw_color": [12, 0, 0, 0]}}
    )
    res = await client.receive_json()
    assert res["error"]["code"] == "unauthorized"


async def test_services(hass: HomeAssistant, sources) -> None:
    entry = await add_twin(hass, "light.bulb_rgbw")
    twin = twin_id(hass, entry)

    await hass.services.async_call(
        DOMAIN, "calibrate_cell",
        {"entity_id": twin, "cell": "color:blue:5", "brightness": 26, "rgbw_color": [0, 0, 60, 0]},
        blocking=True,
    )
    assert entry.runtime_data.cells["color:blue:5"] == {"brightness": 26, "rgbw_color": [0, 0, 60, 0]}

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "calibrate_cell",
            {"entity_id": twin, "cell": "color:blue:7", "brightness": 26, "rgbw_color": [0, 0, 60, 0]},
            blocking=True,
        )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "export_profile", {"entity_id": "light.bulb_hs"}, blocking=True, return_response=True
        )

    res = await hass.services.async_call(
        DOMAIN, "export_profile", {"entity_id": twin, "model": "M"}, blocking=True, return_response=True
    )
    profile = res["profile"]
    assert profile["cells"] == {"color:blue:5": {"brightness": 26, "rgbw_color": [0, 0, 60, 0]}}

    await hass.services.async_call(DOMAIN, "reset_cell", {"entity_id": twin, "cell": "*"}, blocking=True)
    assert entry.runtime_data.cells == {}

    res = await hass.services.async_call(
        DOMAIN, "import_profile", {"entity_id": twin, "profile": json.dumps(profile)},
        blocking=True, return_response=True,
    )
    assert res == {"imported": 1, "skipped": 0}
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "import_profile", {"entity_id": twin, "profile": "{not json"}, blocking=True
        )

    res = await hass.services.async_call(
        DOMAIN, "import_brightness_curve", {"entity_id": twin, "curve": {"10": 30}},
        blocking=True, return_response=True,
    )
    assert res == {"written": 59}
    # the twin shows the new calibration count
    assert hass.states.get(twin).attributes["calibrated_cells"] == 60


async def test_panel_registered_with_frontend(hass: HomeAssistant, sources) -> None:
    """With the frontend loaded the panel and its module are registered."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from custom_components.light_match import panel

    hass.config.components.add("frontend")
    static = AsyncMock()
    with (
        patch.object(hass, "http", MagicMock(async_register_static_paths=static)),
        patch("homeassistant.components.panel_custom.async_register_panel", AsyncMock()) as reg,
    ):
        assert await panel.async_register_panel(hass) is True
    assert static.call_args[0][0][0].url_path == "/light_match_static"
    kwargs = reg.call_args.kwargs
    assert kwargs["webcomponent_name"] == "light-match-panel"
    assert kwargs["frontend_url_path"] == "light-match"
    assert kwargs["require_admin"] is True
    assert kwargs["module_url"].startswith("/light_match_static/light-match-panel.js?v=")
    assert panel.FRONTEND_DIR.joinpath("light-match-panel.js").is_file()


async def test_copy_model_and_quick_keys(hass: HomeAssistant, sources, hass_ws_client) -> None:
    from custom_components.light_match import async_get_database
    from custom_components.light_match.database import ModelProfile, validate_db_profile

    data = validate_db_profile({
        "format": "light_match_profile", "version": 2, "revision": 1, "name": "Test lamp",
        "license": "CC-BY-4.0", "device": {"manufacturer": "Nobody", "models": ["N1"]},
        "provenance": {"method": "by_eye", "verified": False},
        "modes": {"color": "hs", "white": "color_temp"},
        "cells": {"color:red:5": {"brightness": 9, "hs_color": [3, 100]}},
    })
    (await async_get_database(hass)).bundled["nobody/n1"] = ModelProfile("nobody/n1", data)
    entry = await add_twin(hass, "light.bulb_hs", profile="nobody/n1")
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/profile", "entry_id": entry.entry_id})
    res = (await client.receive_json())["result"]
    assert res["quick"][:3] == ["white:2700:1", "white:2700:20", "white:2700:100"]
    red5 = next(c for c in res["cells"] if c["key"] == "color:red:5")
    assert red5["source"] == "model" and red5["model_command"]["hs_color"] == [3.0, 100.0]
    assert res["model_profile"]["id"] == "nobody/n1"
    await client.send_json_auto_id({"type": f"{DOMAIN}/cell/copy_model", "entry_id": entry.entry_id})
    assert (await client.receive_json())["result"] == {"copied": 1}
    assert entry.runtime_data.cells["color:red:5"]["brightness"] == 9


async def test_export_download_view(
    hass: HomeAssistant, sources, hass_client, hass_client_no_auth, hass_access_token, hass_read_only_access_token
) -> None:
    from datetime import timedelta

    from homeassistant.components.http.auth import async_sign_path

    entry = await add_twin(hass, "light.bulb_rgbw", title="Desk lamp")
    entry.runtime_data.cells["color:red:1"] = {"brightness": 26, "rgbw_color": [12, 0, 0, 0]}
    path = f"/api/{DOMAIN}/export/{entry.entry_id}"

    assert (await (await hass_client_no_auth()).get(path)).status == 401
    assert (await (await hass_client(hass_read_only_access_token)).get(path)).status == 403
    client = await hass_client()
    assert (await client.get(f"/api/{DOMAIN}/export/nope")).status == 404

    resp = await client.get(path, params={"make": "Acme", "model": "Bulb 9"})
    assert resp.status == 200
    assert re.fullmatch(
        r'attachment; filename="Acme_Bulb_9_\d{4}-\d\d-\d\d_[a-z0-9]{5}\.light-match\.json"',
        resp.headers["Content-Disposition"],
    )
    assert resp.content_type == "application/json"
    body = json.loads(await resp.text())
    assert body["format"] == "light_match_profile" and body["model"] == "Bulb 9" and body["make"] == "Acme"
    assert body["name"].startswith("Acme_Bulb_9_") and re.fullmatch(r"\d{4}-\d\d-\d\d", body["date"])
    assert body["cells"] == {"color:red:1": {"brightness": 26, "rgbw_color": [12, 0, 0, 0]}}

    # a chosen name; a lamp without make and model falls back to "profile"
    resp = await client.get(path, params={"name": "My lamp"})
    assert resp.headers["Content-Disposition"] == 'attachment; filename="My_lamp.light-match.json"'
    resp = await client.get(path)
    assert re.search(r'filename="profile_\d{4}-\d\d-\d\d_[a-z0-9]{5}\.light-match\.json"', resp.headers["Content-Disposition"])

    # the panel signs the path (auth/sign_path) and opens it without a bearer token
    token = hass.auth.async_validate_access_token(hass_access_token)
    signed = async_sign_path(hass, f"{path}?model=X1", timedelta(seconds=60), refresh_token_id=token.id)
    resp = await (await hass_client_no_auth()).get(signed)
    assert resp.status == 200 and json.loads(await resp.text())["model"] == "X1"
    # the signature covers the query
    resp = await (await hass_client_no_auth()).get(signed.replace("model=X1", "model=X2"))
    assert resp.status == 401


async def test_share_download_view(hass: HomeAssistant, sources, hass_client) -> None:
    entry = await add_twin(hass, "light.bulb_rgbw")
    entry.runtime_data.cells["color:red:1"] = {"brightness": 26, "rgbw_color": [12, 0, 0, 0]}
    client = await hass_client()
    resp = await client.get(f"/api/{DOMAIN}/share/{entry.entry_id}", params={"nickname": "tester"})
    assert resp.status == 200
    assert resp.headers["Content-Disposition"].startswith('attachment; filename="')
    assert resp.headers["Content-Disposition"].endswith('.json"')
    body = json.loads(await resp.text())
    assert body["cells"]["color:red:1"]["rgbw_color"] == [12, 0, 0, 0]
    assert "tester" in json.dumps(body)
    resp = await client.get(f"/api/{DOMAIN}/share/{entry.entry_id}", params={"nickname": "me@example.com"})
    assert resp.status == 400


async def test_copy_calibration_between_twins(hass: HomeAssistant, sources, hass_ws_client) -> None:
    source = await add_twin(hass, "light.bulb_rgbw", title="Source")
    target = await add_twin(hass, "light.bulb_hs", title="Target")
    source.runtime_data.cells["color:red:5"] = {"brightness": 26, "rgbw_color": [255, 0, 0, 0]}
    target.runtime_data.cells["color:blue:5"] = {"brightness": 30, "hs_color": [240, 100]}
    client = await hass_ws_client(hass)

    async def copy(**extra):
        await client.send_json_auto_id(
            {"type": f"{DOMAIN}/profile/copy", "entry_id": target.entry_id, "from_entry_id": source.entry_id, **extra}
        )
        return await client.receive_json()

    res = await copy(merge=True)
    assert res["result"] == {"imported": 1, "skipped": 0}
    cells = target.runtime_data.cells
    assert set(cells) == {"color:red:5", "color:blue:5"}
    # rgbw converted to the hs lamp's colour mode
    assert "hs_color" in cells["color:red:5"] and "rgbw_color" not in cells["color:red:5"]
    assert cells["color:red:5"]["hs_color"][1] > 90

    res = await copy()
    assert res["result"] == {"imported": 1, "skipped": 0}
    assert set(target.runtime_data.cells) == {"color:red:5"}

    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/profile/copy", "entry_id": target.entry_id, "from_entry_id": target.entry_id}
    )
    assert (await client.receive_json())["error"]["code"] == "not_found"

    # an uncalibrated source never empties the target
    source.runtime_data.cells.clear()
    res = await copy()
    assert res["error"]["code"] == "invalid_format"
    assert set(target.runtime_data.cells) == {"color:red:5"}


async def test_copy_calibration_needs_admin(hass: HomeAssistant, sources, hass_ws_client, hass_read_only_access_token) -> None:
    source = await add_twin(hass, "light.bulb_rgbw", title="Source")
    target = await add_twin(hass, "light.bulb_hs", title="Target")
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json_auto_id(
        {"type": f"{DOMAIN}/profile/copy", "entry_id": target.entry_id, "from_entry_id": source.entry_id}
    )
    assert (await client.receive_json())["error"]["code"] == "unauthorized"
