"""The calibrated twin light entity."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from . import LightMatchConfigEntry, LightMatchData
from .const import TWIN_MAX_KELVIN, TWIN_MIN_KELVIN
from .profile import CHANNEL_MODES, KIND_COLOR, KIND_WHITE, MODE_ATTR, Target

_LOGGER = logging.getLogger(__name__)
DEFAULT_TARGET = Target(KIND_WHITE, 100.0, kelvin=2700)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LightMatchConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the twin of this entry."""
    async_add_entities([CalibratedLight(entry.runtime_data)])


def _close(sent: dict[str, Any], attrs: dict[str, Any]) -> bool:
    """Whether a source state still shows the command we last sent."""
    brightness = attrs.get("brightness")
    if brightness is None or abs(int(brightness) - int(sent.get("brightness", 0))) > 3:
        return False
    for mode, attr in MODE_ATTR.items():
        if attr is None or attr not in sent:
            continue
        value = attrs.get(attr)
        if value is None:
            return False
        if mode in CHANNEL_MODES:
            return all(abs(a - b) <= 3 for a, b in zip(sent[attr], value, strict=False))
        if mode == "hs":
            dh = abs(((sent[attr][0] - value[0] + 180) % 360) - 180)
            return dh <= 3 and abs(sent[attr][1] - value[1]) <= 3
        return abs(sent[attr] - value) <= 75
    return True


class CalibratedLight(LightEntity):
    """A light that translates uniform commands into its source's own."""

    _attr_should_poll = False
    _attr_supported_features = LightEntityFeature.TRANSITION
    _attr_min_color_temp_kelvin = TWIN_MIN_KELVIN
    _attr_max_color_temp_kelvin = TWIN_MAX_KELVIN

    def __init__(self, data: LightMatchData) -> None:
        """Create the twin of a source light."""
        self._data = data
        self._attr_unique_id = data.entry.entry_id
        self._attr_name = data.entry.title
        self._attr_available = False
        self._attr_is_on = False
        self._target: Target | None = None
        self._sent: dict[str, Any] | None = None
        self._sent_context: str | None = None

    # -- lifecycle -----------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Follow the source light."""
        self._data.entity = self
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._data.source], self._async_source_changed
            )
        )
        self.async_on_remove(self._data.async_add_listener(self._async_profile_changed))
        self._sync(self.hass.states.get(self._data.source), None)

    async def async_will_remove_from_hass(self) -> None:
        """Forget the entity reference."""
        if self._data.entity is self:
            self._data.entity = None

    @callback
    def _async_source_changed(self, event: Event[EventStateChangedData]) -> None:
        self._sync(event.data["new_state"], event.context)
        self.async_write_ha_state()

    @callback
    def _async_profile_changed(self) -> None:
        """Re-read the source state through the changed profile."""
        self._sent = None
        self._sync(self.hass.states.get(self._data.source), None)
        self.async_write_ha_state()

    @callback
    def _sync(self, state: State | None, context: Context | None) -> None:
        """Mirror availability and on/off; inverse-map the source's look."""
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._attr_available = False
            return
        self._attr_available = True
        attrs = dict(state.attributes)
        self._data.remember_capabilities(attrs)
        self._attr_is_on = state.state == STATE_ON
        if not self._attr_is_on:
            return
        ours = context is not None and context.id == self._sent_context
        if self._sent is not None and self._target is not None and (ours or _close(self._sent, attrs)):
            return
        self._sent = None
        target = self._data.profile().inverse(attrs)
        if target is not None:
            self._target = target

    # -- state ---------------------------------------------------------------

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        """Hs and colour temperature when the source can show them."""
        caps = self._data.caps()
        modes: set[ColorMode] = set()
        if caps.color_mode is not None:
            modes.add(ColorMode.HS)
        if caps.white_mode == "color_temp" or caps.color_mode is not None:
            modes.add(ColorMode.COLOR_TEMP)
        if not modes:
            modes.add(ColorMode.BRIGHTNESS if caps.white_mode else ColorMode.ONOFF)
        return modes

    @property
    def color_mode(self) -> ColorMode:
        """The mode of the current look."""
        modes = self.supported_color_modes
        target = self._target or DEFAULT_TARGET
        if target.kind == KIND_COLOR and ColorMode.HS in modes:
            return ColorMode.HS
        if ColorMode.COLOR_TEMP in modes:
            return ColorMode.COLOR_TEMP
        return next(iter(modes))

    @property
    def brightness(self) -> int | None:
        """Brightness in the calibrated space."""
        if self.color_mode == ColorMode.ONOFF:
            return None
        target = self._target or DEFAULT_TARGET
        return max(1, min(255, round(target.brightness * 255 / 100)))

    @property
    def hs_color(self) -> tuple[float, float] | None:
        """Colour in the calibrated space."""
        if self._target is None or self._target.kind != KIND_COLOR:
            return None
        return (self._target.hue, self._target.sat)

    @property
    def color_temp_kelvin(self) -> int | None:
        """Colour temperature in the calibrated space."""
        target = self._target or DEFAULT_TARGET
        if target.kind != KIND_WHITE:
            return None
        return max(TWIN_MIN_KELVIN, min(TWIN_MAX_KELVIN, round(target.kelvin)))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Where the twin points and how much of it is calibrated."""
        return {
            "source_entity": self._data.source,
            "calibrated_cells": len(self._data.cells),
        }

    # -- commands ------------------------------------------------------------

    def _new_target(self, kwargs: dict[str, Any]) -> Target:
        base = self._target or DEFAULT_TARGET
        if ATTR_BRIGHTNESS in kwargs:
            brightness = max(1, int(kwargs[ATTR_BRIGHTNESS])) * 100 / 255
        else:
            brightness = base.brightness
        if ATTR_HS_COLOR in kwargs:
            hue, sat = kwargs[ATTR_HS_COLOR]
            return Target(KIND_COLOR, brightness, hue=float(hue), sat=float(sat))
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            kelvin = max(TWIN_MIN_KELVIN, min(TWIN_MAX_KELVIN, int(kwargs[ATTR_COLOR_TEMP_KELVIN])))
            return Target(KIND_WHITE, brightness, kelvin=kelvin)
        return Target(base.kind, brightness, hue=base.hue, sat=base.sat, kelvin=base.kelvin)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Translate the uniform command and send it to the source."""
        target = self._new_target(kwargs)
        command = self._data.profile().forward(target)
        await self.async_send_to_source(command, kwargs.get(ATTR_TRANSITION), target)

    async def async_send_to_source(
        self, command: dict[str, Any], transition: float | None, target: Target | None
    ) -> None:
        """Send a source command, remembering it to map the state back."""
        service_data: dict[str, Any] = {ATTR_ENTITY_ID: self._data.source, **command}
        if transition is not None:
            service_data[ATTR_TRANSITION] = transition
        context = self._context or Context()
        self._target = target
        self._sent = dict(command) if target is not None else None
        self._sent_context = context.id
        await self.hass.services.async_call(
            "light", "turn_on", service_data, blocking=True, context=context
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the source off."""
        service_data: dict[str, Any] = {ATTR_ENTITY_ID: self._data.source}
        if ATTR_TRANSITION in kwargs:
            service_data[ATTR_TRANSITION] = kwargs[ATTR_TRANSITION]
        await self.hass.services.async_call(
            "light", "turn_off", service_data, blocking=True, context=self._context
        )
