"""Websocket API used by the calibration panel."""

from __future__ import annotations

from typing import Any

from homeassistant.components import websocket_api
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
import voluptuous as vol

from . import LightMatchData, data_for_entity, data_for_entry, loaded_entries
from .const import CONF_BEHAVIOUR, CONF_REFERENCE, DOMAIN
from .profile import MODE_ATTR, cell_target, grid_cells, nominal_command

COMMAND_KEYS = {"brightness", *(a for a in MODE_ATTR.values() if a)}


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    """Register all commands."""
    for handler in (
        ws_entries,
        ws_profile,
        ws_set_cell,
        ws_reset_cell,
        ws_preview,
        ws_show_target,
        ws_export,
        ws_import,
    ):
        websocket_api.async_register_command(hass, handler)


def _summary(data: LightMatchData) -> dict[str, Any]:
    caps = data.caps()
    total = len(grid_cells(caps))
    return {
        "entry_id": data.entry.entry_id,
        "title": data.entry.title,
        "entity_id": data.entity.entity_id if data.entity else None,
        "source": data.source,
        "reference_entity": data.option(CONF_REFERENCE),
        "behaviour": data.option(CONF_BEHAVIOUR),
        "modes": {"color": caps.color_mode, "white": caps.white_mode},
        "min_kelvin": caps.min_kelvin,
        "max_kelvin": caps.max_kelvin,
        "floor": caps.floor,
        "calibrated": len([k for k in data.cells if k in {c["key"] for c in grid_cells(caps)}]),
        "total": total,
    }


def _get(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> LightMatchData | None:
    data = data_for_entry(hass, msg["entry_id"])
    if data is None:
        connection.send_error(msg["id"], "not_found", "No such Light Match entry")
    return data


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/entries"})
@callback
def ws_entries(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """List the twins."""
    connection.send_result(
        msg["id"], {"entries": [_summary(e.runtime_data) for e in loaded_entries(hass)]}
    )


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/profile", vol.Required("entry_id"): str}
)
@callback
def ws_profile(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """The grid of a twin with each cell's command and whether it is calibrated."""
    if (data := _get(hass, connection, msg)) is None:
        return
    profile = data.profile()
    cells = []
    for cell in grid_cells(profile.caps):
        key = cell["key"]
        anchor = cell["hue"] if cell["kind"] == "color" else float(cell["kelvin"])
        cells.append(
            {
                **cell,
                "calibrated": key in data.cells,
                "command": profile.cell_command(key),
                "default": profile.default_command(cell["kind"], anchor, cell["brightness"]),
                "mode": profile.mode_for(cell["kind"]),
            }
        )
    connection.send_result(msg["id"], {**_summary(data), "cells": cells})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/cell/set",
        vol.Required("entry_id"): str,
        vol.Required("key"): str,
        vol.Required("command"): dict,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set_cell(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Save a calibrated cell."""
    if (data := _get(hass, connection, msg)) is None:
        return
    try:
        command = await data.async_set_cell(msg["key"], msg["command"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    connection.send_result(msg["id"], {"key": msg["key"], "command": command})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/cell/reset",
        vol.Required("entry_id"): str,
        vol.Required("key"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_reset_cell(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Forget a calibrated cell; key '*' forgets all."""
    if (data := _get(hass, connection, msg)) is None:
        return
    removed = await data.async_reset_cell(msg["key"])
    connection.send_result(msg["id"], {"removed": removed})


async def _turn_on(
    hass: HomeAssistant, entity_id: str, command: dict[str, Any], connection: Any, msg: dict[str, Any]
) -> None:
    await hass.services.async_call(
        "light",
        "turn_on",
        {ATTR_ENTITY_ID: entity_id, **command},
        blocking=True,
        context=connection.context(msg),
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/preview",
        vol.Required("entry_id"): str,
        vol.Required("command"): dict,
        vol.Optional("transition"): vol.Coerce(float),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_preview(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Send a raw command to the source of the lamp under test."""
    if (data := _get(hass, connection, msg)) is None:
        return
    command = {k: v for k, v in msg["command"].items() if k in COMMAND_KEYS}
    if "transition" in msg:
        command["transition"] = msg["transition"]
    try:
        await _turn_on(hass, data.source, command, connection, msg)
    except (HomeAssistantError, vol.Invalid) as err:
        connection.send_error(msg["id"], "preview_failed", str(err))
        return
    connection.send_result(msg["id"], {"sent": command})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/show_target",
        vol.Required("entity_id"): str,
        vol.Required("key"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_show_target(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Make the reference lamp show the target look of a cell.

    A Light Match twin shows it through its own profile; any other light gets
    the plain Home Assistant command.
    """
    try:
        target = cell_target(msg["key"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    twin = data_for_entity(hass, msg["entity_id"])
    try:
        if twin is not None:
            command = twin.profile().forward(target)
            if twin.entity is not None:
                await twin.entity.async_send_to_source(command, None, target)
            else:
                await _turn_on(hass, twin.source, command, connection, msg)
        else:
            command = nominal_command(target)
            await _turn_on(hass, msg["entity_id"], command, connection, msg)
    except (HomeAssistantError, vol.Invalid) as err:
        connection.send_error(msg["id"], "show_failed", str(err))
        return
    connection.send_result(msg["id"], {"sent": command})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/profile/export",
        vol.Required("entry_id"): str,
        vol.Optional("model"): str,
    }
)
@callback
def ws_export(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """The profile as shareable JSON."""
    if (data := _get(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], {"profile": data.export(msg.get("model"))})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/profile/import",
        vol.Required("entry_id"): str,
        vol.Required("profile"): dict,
        vol.Optional("merge", default=False): bool,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_import(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Load an exported profile."""
    if (data := _get(hass, connection, msg)) is None:
        return
    try:
        imported, skipped = await data.async_import(msg["profile"], msg["merge"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    connection.send_result(msg["id"], {"imported": imported, "skipped": skipped})
