"""Services of Light Match."""

from __future__ import annotations

import json
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from . import LightMatchData, data_for_entity, loaded_entries, online_updater
from .const import DOMAIN

ATTR_CELL = "cell"
ATTR_PROFILE = "profile"
ATTR_MERGE = "merge"
ATTR_MODEL = "model"
ATTR_CURVE = "curve"

COMMAND_FIELDS = {
    vol.Required("brightness"): vol.All(vol.Coerce(int), vol.Range(min=1, max=255)),
    vol.Optional("hs_color"): vol.All(vol.ExactSequence((vol.Coerce(float), vol.Coerce(float)))),
    vol.Optional("rgb_color"): vol.All([vol.Coerce(int)], vol.Length(min=3, max=3)),
    vol.Optional("rgbw_color"): vol.All([vol.Coerce(int)], vol.Length(min=4, max=4)),
    vol.Optional("rgbww_color"): vol.All([vol.Coerce(int)], vol.Length(min=5, max=5)),
    vol.Optional("color_temp_kelvin"): vol.Coerce(int),
}

CALIBRATE_SCHEMA = vol.Schema(
    {vol.Required("entity_id"): cv.entity_id, vol.Required(ATTR_CELL): cv.string, **COMMAND_FIELDS}
)
RESET_SCHEMA = vol.Schema({vol.Required("entity_id"): cv.entity_id, vol.Required(ATTR_CELL): cv.string})
EXPORT_SCHEMA = vol.Schema(
    {vol.Required("entity_id"): cv.entity_id, vol.Optional(ATTR_MODEL): cv.string}
)
IMPORT_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Required(ATTR_PROFILE): vol.Any(dict, cv.string),
        vol.Optional(ATTR_MERGE, default=False): cv.boolean,
    }
)
CURVE_SCHEMA = vol.Schema(
    {vol.Required("entity_id"): cv.entity_id, vol.Required(ATTR_CURVE): vol.Any(dict, cv.string)}
)


def _data(hass: HomeAssistant, call: ServiceCall) -> LightMatchData:
    data = data_for_entity(hass, call.data["entity_id"])
    if data is None:
        raise ServiceValidationError(
            f"{call.data['entity_id']} is not a Light Match light",
            translation_domain=DOMAIN,
            translation_key="not_a_twin",
            translation_placeholders={"entity_id": call.data["entity_id"]},
        )
    return data


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError as err:
            raise ServiceValidationError(f"not valid JSON: {err}") from err
    return value


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register the services."""

    async def calibrate_cell(call: ServiceCall) -> None:
        data = _data(hass, call)
        command = {k: v for k, v in call.data.items() if k not in ("entity_id", ATTR_CELL)}
        try:
            await data.async_set_cell(call.data[ATTR_CELL], command)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def reset_cell(call: ServiceCall) -> None:
        await _data(hass, call).async_reset_cell(call.data[ATTR_CELL])

    async def export_profile(call: ServiceCall) -> ServiceResponse:
        return {"profile": _data(hass, call).export(call.data.get(ATTR_MODEL))}

    async def import_profile(call: ServiceCall) -> ServiceResponse:
        data = _data(hass, call)
        try:
            imported, skipped = await data.async_import(
                _json(call.data[ATTR_PROFILE]), call.data[ATTR_MERGE]
            )
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err
        return {"imported": imported, "skipped": skipped}

    async def import_brightness_curve(call: ServiceCall) -> ServiceResponse:
        data = _data(hass, call)
        curve = _json(call.data[ATTR_CURVE])
        try:
            written = await data.async_import_curve(curve)
        except (ValueError, TypeError, AttributeError) as err:
            raise ServiceValidationError(f"bad curve: {err}") from err
        return {"written": written}

    async def update_profiles(call: ServiceCall) -> ServiceResponse:
        updater = online_updater(hass)
        if not updater.enabled:
            raise ServiceValidationError(
                "The online profile update is off; turn it on in the Light Match panel",
                translation_domain=DOMAIN,
                translation_key="online_off",
            )
        result = await updater.async_update()
        for entry in loaded_entries(hass):
            entry.runtime_data.refresh()
        return result

    hass.services.async_register(
        DOMAIN, "update_profiles", update_profiles, vol.Schema({}), SupportsResponse.OPTIONAL
    )
    hass.services.async_register(DOMAIN, "calibrate_cell", calibrate_cell, CALIBRATE_SCHEMA)
    hass.services.async_register(DOMAIN, "reset_cell", reset_cell, RESET_SCHEMA)
    hass.services.async_register(
        DOMAIN, "export_profile", export_profile, EXPORT_SCHEMA, SupportsResponse.ONLY
    )
    hass.services.async_register(
        DOMAIN, "import_profile", import_profile, IMPORT_SCHEMA, SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN,
        "import_brightness_curve",
        import_brightness_curve,
        CURVE_SCHEMA,
        SupportsResponse.OPTIONAL,
    )
