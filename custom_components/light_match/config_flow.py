"""Config and options flow for Light Match."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er, selector
import voluptuous as vol

from .const import (
    BEHAVIOURS,
    CONF_AUTO_FLOOR,
    CONF_BEHAVIOUR,
    CONF_FLOOR,
    CONF_HIDE_SOURCE,
    CONF_NAME,
    CONF_PROFILE,
    CONF_REFERENCE,
    CONF_SOURCE,
    CONF_WHITE_COLOR,
    DEFAULT_OPTIONS,
    DOMAIN,
    PROFILE_AUTO,
    PROFILE_NONE,
    WHITE_COLORS,
)


def _describe(prof: Any) -> str:
    prov = prof.data["provenance"]
    method = {"spec": "spec-based", "by_eye": "calibrated by eye", "lux_meter": "measured with a lux meter",
              "colorimeter": "measured with a colorimeter",
              "imported": "imported, partly calibrated"}[prov["method"]]
    verified = "verified" if prov["verified"] else "unverified"
    return f"{prof.name} ({method}, {verified})"


async def _profile_selector(hass: Any, source: str) -> tuple[selector.SelectSelector, str, str]:
    """A select of the model profiles, and descriptions of the device and its match."""
    from . import async_get_database, device_info_for  # noqa: PLC0415

    db = await async_get_database(hass)
    device = device_info_for(hass, source)
    match = db.match(device)
    auto_label = f"Detected: {_describe(match)}" if match else "Detect (no profile found for this lamp)"
    options = [
        selector.SelectOptionDict(value=PROFILE_AUTO, label=auto_label),
        selector.SelectOptionDict(value=PROFILE_NONE, label="No model profile"),
    ]
    # every profile that fits this lamp, then the hand-written rest; imported
    # profiles of other models are left out (there are hundreds)
    fitting = {prof.id for prof in db.candidates(device)}
    options += [
        selector.SelectOptionDict(value=pid, label=_describe(prof))
        for pid, prof in sorted(db.all().items())
        if pid in fitting or prof.data["provenance"]["method"] != "imported"
    ]
    dev = " ".join(v for v in (device.manufacturer, device.model, device.model_id) if v) or "unknown"
    if device.integration:
        dev += f" ({device.integration})"
    return (
        selector.SelectSelector(selector.SelectSelectorConfig(options=options)),
        dev,
        _describe(match) if match else "none",
    )


def _own_twins(hass: Any) -> list[str]:
    registry = er.async_get(hass)
    return [e.entity_id for e in registry.entities.values() if e.platform == DOMAIN]


class LightMatchConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create one calibrated twin for a source light."""

    VERSION = 1
    _source: str
    _title: str

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick the source light and a name."""
        errors: dict[str, str] = {}
        twins = _own_twins(self.hass)
        if user_input is not None:
            source = user_input[CONF_SOURCE]
            if source in twins:
                errors[CONF_SOURCE] = "own_twin"
            elif self.hass.states.get(source) is None and er.async_get(self.hass).async_get(source) is None:
                errors[CONF_SOURCE] = "not_found"
            else:
                await self.async_set_unique_id(source)
                self._abort_if_unique_id_configured()
                name = (user_input.get(CONF_NAME) or "").strip()
                if not name:
                    state = self.hass.states.get(source)
                    base = state.name if state else source.split(".", 1)[-1]
                    name = f"{base} calibrated"
                self._source = source
                self._title = name
                return await self.async_step_profile()
        schema = vol.Schema(
            {
                vol.Required(CONF_SOURCE): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="light", exclude_entities=twins)
                ),
                vol.Optional(CONF_NAME): selector.TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def async_step_profile(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the model profile matched to the lamp and allow another choice."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._title,
                data={CONF_SOURCE: self._source},
                options={**DEFAULT_OPTIONS, CONF_PROFILE: user_input[CONF_PROFILE]},
            )
        select, device, match = await _profile_selector(self.hass, self._source)
        return self.async_show_form(
            step_id="profile",
            data_schema=vol.Schema({vol.Required(CONF_PROFILE, default=PROFILE_AUTO): select}),
            description_placeholders={"device": device, "match": match},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options for a twin."""
        return LightMatchOptionsFlow()


class LightMatchOptionsFlow(OptionsFlow):
    """Behaviour, reference lamp, brightness floor and hiding the source."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show and save the options."""
        if user_input is not None:
            options = dict(DEFAULT_OPTIONS)
            options.update(user_input)
            options[CONF_FLOOR] = int(options.get(CONF_FLOOR) or 0)
            if not options.get(CONF_REFERENCE):
                options.pop(CONF_REFERENCE, None)
            return self.async_create_entry(data=options)

        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        profile_select, device, match = await _profile_selector(
            self.hass, self.config_entry.data[CONF_SOURCE]
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_PROFILE): profile_select,
                vol.Required(CONF_BEHAVIOUR): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=BEHAVIOURS,
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key=CONF_BEHAVIOUR,
                    )
                ),
                vol.Optional(CONF_REFERENCE): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="light",
                        exclude_entities=[self.config_entry.data[CONF_SOURCE]],
                    )
                ),
                vol.Optional(CONF_WHITE_COLOR): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=WHITE_COLORS,
                        mode=selector.SelectSelectorMode.LIST,
                        translation_key=CONF_WHITE_COLOR,
                    )
                ),
                vol.Required(CONF_AUTO_FLOOR): selector.BooleanSelector(),
                vol.Required(CONF_FLOOR): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=255, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Required(CONF_HIDE_SOURCE): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, current),
            description_placeholders={"device": device, "match": match},
        )
