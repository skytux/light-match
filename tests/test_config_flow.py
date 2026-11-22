"""Config and options flow."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
import pytest

from custom_components.light_match.const import DOMAIN

from .conftest import add_twin, twin_id


async def test_user_flow_creates_entry(hass: HomeAssistant, sources) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"source": "light.bulb_rgbw"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "profile"
    assert result["description_placeholders"]["match"] == "none"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"profile": "auto"})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Bulb RGBW calibrated"
    assert result["data"] == {"source": "light.bulb_rgbw"}
    assert result["options"]["behaviour"] == "smart"
    assert hass.states.get("light.bulb_rgbw_calibrated") is not None


async def test_user_flow_custom_name_and_duplicate(hass: HomeAssistant, sources) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"source": "light.bulb_hs", "name": "Desk lamp"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"profile": "none"})
    await hass.async_block_till_done()
    assert result["options"]["profile"] == "none"
    assert result["title"] == "Desk lamp"

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"source": "light.bulb_hs"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_rejects_own_twin_and_missing(hass: HomeAssistant, sources) -> None:
    entry = await add_twin(hass, "light.bulb_rgbw")
    twin = twin_id(hass, entry)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    # the selector itself excludes the twins
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(result["flow_id"], {"source": twin})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"source": "light.nope"})
    assert result["errors"] == {"source": "not_found"}


async def test_options_flow(hass: HomeAssistant, sources) -> None:
    entry = await add_twin(hass, "light.bulb_rgbw")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "profile": "signify/hue-gamut-b",
            "behaviour": "plain",
            "reference_entity": "light.bulb_hs",
            "auto_floor": False,
            "brightness_floor": 26,
            "hide_source": False,
            "white_color": "always",
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["white_color"] == "always"
    assert entry.options["behaviour"] == "plain"
    assert entry.options["brightness_floor"] == 26
    assert entry.options["reference_entity"] == "light.bulb_hs"
    assert entry.runtime_data.floor() == 26
    assert entry.runtime_data.model_profile().id == "signify/hue-gamut-b"
    assert entry.runtime_data.caps().gamut[0] == (0.675, 0.322)


async def test_user_flow_shows_detected_profile(hass: HomeAssistant, sources, wiz_device) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"source": "light.bulb_rgbw"})
    placeholders = result["description_placeholders"]
    assert placeholders["device"] == "WiZ ESP01_SHRGB_03 (test)"
    # the integration is not wiz: only the imported profile (no integration limit) fits
    assert placeholders["match"] == "WiZ ESP01_SHRGB_03 (imported, partly calibrated, unverified)"

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"source": "light.wiz_bulb"})
    assert result["description_placeholders"]["match"] == "WiZ ESP01_SHRGB_03 (imported, partly calibrated, unverified)"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"profile": "auto"})
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(result["result"].entry_id)
    assert entry.runtime_data.model_profile().id == "imported/wiz__esp01_shrgb_03"
    assert entry.runtime_data.floor() == 26  # from wiz/generic, merged
