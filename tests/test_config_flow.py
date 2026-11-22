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
    await hass.async_block_till_done()
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
            "behaviour": "plain",
            "reference_entity": "light.bulb_hs",
            "auto_floor": False,
            "brightness_floor": 26,
            "hide_source": False,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["behaviour"] == "plain"
    assert entry.options["brightness_floor"] == 26
    assert entry.options["reference_entity"] == "light.bulb_hs"
    assert entry.runtime_data.floor() == 26
