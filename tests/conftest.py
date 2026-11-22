"""Fixtures: a fake source light platform and a set-up twin."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ColorMode, LightEntity, LightEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_integration,
    mock_platform,
)

from custom_components.light_match.const import DEFAULT_OPTIONS, DOMAIN

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
    """Load custom_components in every test."""
    return


class FakeLight(LightEntity):
    """A source light that records the commands it gets."""

    _attr_should_poll = False

    def __init__(self, name: str, modes: set[ColorMode], unique_id: str) -> None:
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._attr_supported_color_modes = modes
        self._attr_supported_features = LightEntityFeature.TRANSITION
        self._attr_color_mode = sorted(modes)[0] if len(modes) == 1 else next(
            m for m in (ColorMode.RGBW, ColorMode.RGB, ColorMode.HS, ColorMode.COLOR_TEMP, ColorMode.BRIGHTNESS) if m in modes
        )
        self._attr_is_on = False
        self._attr_brightness = 255
        self._attr_min_color_temp_kelvin = 2700
        self._attr_max_color_temp_kelvin = 6500
        self._attr_color_temp_kelvin = 2700
        self._attr_hs_color = (0, 0)
        self._attr_rgb_color = (255, 255, 255)
        self._attr_rgbw_color = (0, 0, 0, 255)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.calls.append(("on", dict(kwargs)))
        self._attr_is_on = True
        if "brightness" in kwargs:
            self._attr_brightness = kwargs["brightness"]
        for attr, mode in (
            ("rgbw_color", ColorMode.RGBW),
            ("rgb_color", ColorMode.RGB),
            ("hs_color", ColorMode.HS),
            ("color_temp_kelvin", ColorMode.COLOR_TEMP),
        ):
            if attr in kwargs:
                setattr(self, f"_attr_{attr}", kwargs[attr])
                self._attr_color_mode = mode
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.calls.append(("off", dict(kwargs)))
        self._attr_is_on = False
        self.async_write_ha_state()

    def set_available(self, available: bool) -> None:
        self._attr_available = available
        self.async_write_ha_state()


@pytest.fixture
async def sources(hass: HomeAssistant) -> dict[str, FakeLight]:
    """Three fake lamps: rgbw + color_temp, hs + color_temp, brightness only."""
    lights = {
        "light.bulb_rgbw": FakeLight("Bulb RGBW", {ColorMode.RGBW, ColorMode.COLOR_TEMP}, "rgbw1"),
        "light.bulb_hs": FakeLight("Bulb HS", {ColorMode.HS, ColorMode.COLOR_TEMP}, "hs1"),
        "light.bulb_dim": FakeLight("Bulb Dim", {ColorMode.BRIGHTNESS}, "dim1"),
    }

    async def setup_platform(hass, config, async_add_entities, discovery_info=None):
        async_add_entities(list(lights.values()))

    mock_integration(hass, MockModule("test"))
    mock_platform(hass, "test.light", MockPlatform(async_setup_platform=setup_platform))
    assert await async_setup_component(hass, "light", {"light": [{"platform": "test"}]})
    await hass.async_block_till_done()
    for entity_id in lights:
        assert hass.states.get(entity_id) is not None, entity_id
    return lights


async def add_twin(
    hass: HomeAssistant, source: str, title: str = "Twin", **options: Any
) -> MockConfigEntry:
    """Set up a twin for a source light."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={"source": source},
        options={**DEFAULT_OPTIONS, **options},
        unique_id=source,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def twin_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Entity id of the twin of an entry."""
    from homeassistant.helpers import entity_registry as er

    entity_id = er.async_get(hass).async_get_entity_id("light", DOMAIN, entry.entry_id)
    assert entity_id
    return entity_id
