"""The twin entity: forwarding to the source, state mirroring, inverse mapping."""

from __future__ import annotations

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest

from .conftest import add_twin, twin_id


async def _on(hass: HomeAssistant, entity_id: str, **data) -> None:
    await hass.services.async_call("light", "turn_on", {"entity_id": entity_id, **data}, blocking=True)
    await hass.async_block_till_done()


def _sent(src) -> dict:
    """The last command a fake source got (without HA's legacy mired copy)."""
    return {k: v for k, v in src.calls[-1][1].items() if k != "color_temp"}


async def test_twin_forwards_colour_with_subfloor(hass: HomeAssistant, sources) -> None:
    entry = await add_twin(hass, "light.bulb_rgbw", auto_floor=False, brightness_floor=26)
    twin = twin_id(hass, entry)
    src = sources["light.bulb_rgbw"]

    state = hass.states.get(twin)
    assert state.state == STATE_OFF
    assert set(state.attributes["supported_color_modes"]) == {"hs", "color_temp"}
    assert state.attributes["min_color_temp_kelvin"] == 2200

    await _on(hass, twin, hs_color=[0, 100], brightness=3, transition=2)
    kind, kwargs = src.calls[-1]
    assert kind == "on"
    assert kwargs["brightness"] == 26
    assert kwargs["rgbw_color"][0] < 40 and kwargs["rgbw_color"][1:] == (0, 0, 0)
    assert kwargs["transition"] == 2

    state = hass.states.get(twin)
    assert state.state == STATE_ON
    assert state.attributes["color_mode"] == "hs"
    assert state.attributes["brightness"] == 3
    assert tuple(state.attributes["hs_color"]) == (0.0, 100.0)


async def test_twin_forwards_white_brightness_and_off(hass: HomeAssistant, sources) -> None:
    entry = await add_twin(hass, "light.bulb_hs")
    twin = twin_id(hass, entry)
    src = sources["light.bulb_hs"]

    await _on(hass, twin, color_temp_kelvin=4000, brightness=128)
    assert _sent(src) == {"brightness": 128, "color_temp_kelvin": 4000}
    state = hass.states.get(twin)
    assert state.attributes["color_mode"] == "color_temp"
    assert state.attributes["color_temp_kelvin"] == 4000

    # brightness only keeps the white
    await _on(hass, twin, brightness=51)
    assert _sent(src) == {"brightness": 51, "color_temp_kelvin": 4000}

    await hass.services.async_call("light", "turn_off", {"entity_id": twin, "transition": 1}, blocking=True)
    await hass.async_block_till_done()
    assert src.calls[-1] == ("off", {"transition": 1})
    assert hass.states.get(twin).state == STATE_OFF


async def test_twin_uses_calibrated_cell(hass: HomeAssistant, sources) -> None:
    entry = await add_twin(hass, "light.bulb_hs")
    twin = twin_id(hass, entry)
    await entry.runtime_data.async_set_cell("color:red:50", {"brightness": 90, "hs_color": [8, 95]})
    await _on(hass, twin, hs_color=[0, 100], brightness=round(255 * 0.5))
    # 128/255 is 50.2 %, a hair above the 50 % cell: interpolated, close to the cell
    kwargs = sources["light.bulb_hs"].calls[-1][1]
    assert kwargs["hs_color"][0] == pytest.approx(8, abs=0.5)
    assert kwargs["brightness"] == pytest.approx(90, abs=2)
    state = hass.states.get(twin)
    assert state.attributes["brightness"] == 128
    assert state.attributes["calibrated_cells"] == 1


async def test_twin_inverse_maps_external_change(hass: HomeAssistant, sources) -> None:
    entry = await add_twin(hass, "light.bulb_hs")
    twin = twin_id(hass, entry)
    await entry.runtime_data.async_set_cell("color:green:50", {"brightness": 128, "hs_color": [110, 100]})
    # someone sets the real lamp directly
    await _on(hass, "light.bulb_hs", hs_color=[110, 100], brightness=128)
    state = hass.states.get(twin)
    assert state.attributes["color_mode"] == "hs"
    assert state.attributes["hs_color"][0] == pytest.approx(120, abs=1.5)
    assert state.attributes["brightness"] == pytest.approx(128, abs=2)


async def test_twin_mirrors_availability(hass: HomeAssistant, sources) -> None:
    entry = await add_twin(hass, "light.bulb_rgbw")
    twin = twin_id(hass, entry)
    sources["light.bulb_rgbw"].set_available(False)
    await hass.async_block_till_done()
    assert hass.states.get(twin).state == STATE_UNAVAILABLE
    sources["light.bulb_rgbw"].set_available(True)
    await hass.async_block_till_done()
    assert hass.states.get(twin).state == STATE_OFF


async def test_brightness_only_twin(hass: HomeAssistant, sources) -> None:
    entry = await add_twin(hass, "light.bulb_dim")
    twin = twin_id(hass, entry)
    assert hass.states.get(twin).attributes["supported_color_modes"] == ["brightness"]
    await _on(hass, twin, brightness=100)
    assert sources["light.bulb_dim"].calls[-1][1] == {"brightness": 100}


async def test_hide_source_and_restore_on_remove(hass: HomeAssistant, sources) -> None:
    registry = er.async_get(hass)
    entry = await add_twin(hass, "light.bulb_rgbw", hide_source=True)
    assert registry.async_get("light.bulb_rgbw").hidden_by is er.RegistryEntryHider.INTEGRATION
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get("light.bulb_rgbw").hidden_by is None


async def test_user_hidden_source_is_left_alone(hass: HomeAssistant, sources) -> None:
    registry = er.async_get(hass)
    registry.async_update_entity("light.bulb_hs", hidden_by=er.RegistryEntryHider.USER)
    entry = await add_twin(hass, "light.bulb_hs", hide_source=True)
    assert await hass.config_entries.async_remove(entry.entry_id)
    assert registry.async_get("light.bulb_hs").hidden_by is er.RegistryEntryHider.USER


async def test_floor_detection_by_platform(hass: HomeAssistant, sources) -> None:
    registry = er.async_get(hass)
    registry.async_get_or_create("light", "wiz", "abc123", suggested_object_id="wiz_bulb")
    entry = await add_twin(hass, "light.wiz_bulb")
    assert entry.runtime_data.floor() == 26
    other = await add_twin(hass, "light.bulb_rgbw", title="Other")
    assert other.runtime_data.floor() == 0


async def test_source_rename_is_followed(hass: HomeAssistant, sources) -> None:
    entry = await add_twin(hass, "light.bulb_rgbw")
    er.async_get(hass).async_update_entity("light.bulb_rgbw", new_entity_id="light.renamed")
    await hass.async_block_till_done()
    assert entry.data["source"] == "light.renamed"
