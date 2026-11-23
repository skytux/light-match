"""The calibrated twin light entity."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_EFFECT_LIST,
    ATTR_HS_COLOR,
    ATTR_RGB_COLOR,
    ATTR_RGBW_COLOR,
    ATTR_RGBWW_COLOR,
    ATTR_TRANSITION,
    ATTR_XY_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from . import LightMatchConfigEntry, LightMatchData
from .colorin import translate
from .const import TWIN_MAX_KELVIN, TWIN_MIN_KELVIN
from .profile import CHANNEL_MODES, KIND_COLOR, KIND_WHITE, MODE_ATTR, Target

_LOGGER = logging.getLogger(__name__)
DEFAULT_TARGET = Target(KIND_WHITE, 100.0, kelvin=2700)
NO_EFFECT = {None, "off", "none", "None"}
# colour inputs other systems may send, translated to the calibrated space and echoed back as sent
INPUT_ATTRS = {
    ATTR_RGB_COLOR: ColorMode.RGB,
    ATTR_RGBW_COLOR: ColorMode.RGBW,
    ATTR_RGBWW_COLOR: ColorMode.RGBWW,
    ATTR_XY_COLOR: ColorMode.XY,
}
INPUT_MODES = {ColorMode.HS, ColorMode.RGB, ColorMode.RGBW, ColorMode.RGBWW, ColorMode.XY}


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
        self._effects: list[str] | None = None
        self._effect: str | None = None
        # the colour input as the caller sent it (rgb, rgbw, rgbww or xy) and its brightness, echoed in the state
        self._input: tuple[ColorMode, tuple[float, ...]] | None = None
        self._input_brightness: int = 255

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
        effects = attrs.get(ATTR_EFFECT_LIST)
        self._effects = [str(e) for e in effects] if effects else None
        self._effect = attrs.get(ATTR_EFFECT) if self._effects else None
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
            self._input = None  # changed elsewhere: reported in the calibrated space

    # -- state ---------------------------------------------------------------

    @property
    def supported_features(self) -> LightEntityFeature:
        """Transition when the source takes it (or its model profile says so)."""
        features = LightEntityFeature(0)
        if self._data.transition_supported():
            features |= LightEntityFeature.TRANSITION
        if self._effects:
            features |= LightEntityFeature.EFFECT
        return features

    @property
    def effect_list(self) -> list[str] | None:
        """The source's effects, passed through unchanged."""
        return self._effects

    @property
    def effect(self) -> str | None:
        """The source's running effect."""
        return self._effect

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        """Every colour input (hs, rgb, rgbw, rgbww, xy) and colour temperature when the source shows colour.

        The twin translates whatever it gets, so the input modes do not depend
        on the source's own mode.
        """
        caps = self._data.caps()
        modes: set[ColorMode] = set()
        if caps.color_mode is not None:
            modes |= INPUT_MODES
        if caps.white_mode == "color_temp" or caps.color_mode is not None:
            modes.add(ColorMode.COLOR_TEMP)
        if not modes:
            modes.add(ColorMode.BRIGHTNESS if caps.white_mode else ColorMode.ONOFF)
        return modes

    @property
    def color_mode(self) -> ColorMode:
        """The mode of the current look."""
        modes = self.supported_color_modes
        if self._input is not None and self._input[0] in modes:
            return self._input[0]
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
        if self._input is not None:
            return self._input_brightness
        target = self._target or DEFAULT_TARGET
        return max(1, min(255, round(target.brightness * 255 / 100)))

    @property
    def hs_color(self) -> tuple[float, float] | None:
        """Colour in the calibrated space."""
        if self._target is None or self._target.kind != KIND_COLOR:
            return None
        return (self._target.hue, self._target.sat)

    def _echo(self, mode: ColorMode) -> tuple[Any, ...] | None:
        if self._input is None or self._input[0] != mode:
            return None
        values = self._input[1]
        return tuple(float(v) for v in values) if mode == ColorMode.XY else tuple(int(round(v)) for v in values)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """The rgb input as sent."""
        return self._echo(ColorMode.RGB)

    @property
    def rgbw_color(self) -> tuple[int, int, int, int] | None:
        """The rgbw input as sent."""
        return self._echo(ColorMode.RGBW)

    @property
    def rgbww_color(self) -> tuple[int, int, int, int, int] | None:
        """The rgbww input as sent."""
        return self._echo(ColorMode.RGBWW)

    @property
    def xy_color(self) -> tuple[float, float] | None:
        """The xy input as sent."""
        return self._echo(ColorMode.XY)

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
        attrs: dict[str, Any] = {
            "source_entity": self._data.source,
            "calibrated_cells": len(self._data.cells),
        }
        if self._target is not None and self._attr_is_on:
            t = self._target
            match: dict[str, Any] = {"kind": t.kind, "level": round(t.brightness, 2)}
            if t.kind == KIND_COLOR:
                match.update(hue=round(t.hue, 2), sat=round(t.sat, 2))
            else:
                match["kelvin"] = round(t.kelvin)
            attrs["match_target"] = match
        return attrs

    # -- commands ------------------------------------------------------------

    def _new_target(self, kwargs: dict[str, Any]) -> Target:
        base = self._target or DEFAULT_TARGET
        if ATTR_BRIGHTNESS in kwargs:
            brightness = max(1, int(kwargs[ATTR_BRIGHTNESS])) * 100 / 255
        elif self._input is not None:
            brightness = self._input_brightness * 100 / 255
        else:
            brightness = base.brightness
        for attr, mode in INPUT_ATTRS.items():
            if attr in kwargs:
                self._input = (mode, tuple(float(v) for v in kwargs[attr]))
                break
        else:
            if ATTR_HS_COLOR in kwargs or ATTR_COLOR_TEMP_KELVIN in kwargs:
                self._input = None
        if self._input is not None and not (ATTR_HS_COLOR in kwargs or ATTR_COLOR_TEMP_KELVIN in kwargs):
            self._input_brightness = max(1, min(255, round(brightness * 255 / 100)))
            look = translate(self._input[0].value, self._input[1])
            # the input's own intensity (strongest channel / 255) folds into the brightness
            level = brightness * max(look.intensity, 1 / 255)
            if look.kind == KIND_COLOR:
                return Target(KIND_COLOR, level, hue=look.hue, sat=look.sat)
            return Target(KIND_WHITE, level, kelvin=look.kelvin)
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
        effect = kwargs.get(ATTR_EFFECT)
        if effect is not None and self._effects and effect in self._effects and effect not in NO_EFFECT:
            # an effect is the lamp's own show: pass it through with a calibrated brightness only
            command: dict[str, Any] = {ATTR_EFFECT: effect}
            if ATTR_BRIGHTNESS in kwargs:
                forward = self._data.profile().forward(target)
                if "brightness" in forward:
                    command[ATTR_BRIGHTNESS] = forward["brightness"]
            await self.async_send_to_source(command, kwargs.get(ATTR_TRANSITION), None)
            return
        command = self._data.profile().forward(target)
        if effect is not None and self._effects and effect in self._effects:
            command = {**command, ATTR_EFFECT: effect}  # "off" / "none": stop the effect
        await self.async_send_to_source(command, kwargs.get(ATTR_TRANSITION), target)

    async def async_send_to_source(
        self, command: dict[str, Any], transition: float | None, target: Target | None
    ) -> None:
        """Send a source command, remembering it to map the state back."""
        service_data: dict[str, Any] = {ATTR_ENTITY_ID: self._data.source, **command}
        if transition is not None and self._data.transition_supported():
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
        if ATTR_TRANSITION in kwargs and self._data.transition_supported():
            service_data[ATTR_TRANSITION] = kwargs[ATTR_TRANSITION]
        await self.hass.services.async_call(
            "light", "turn_off", service_data, blocking=True, context=self._context
        )
