"""Websocket API used by the calibration panel."""

from __future__ import annotations

from typing import Any

from homeassistant.components import websocket_api
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.loader import async_get_integration
import voluptuous as vol

from . import (
    LightMatchData,
    data_for_entity,
    data_for_entry,
    database,
    device_info_for,
    loaded_entries,
    online_updater,
)
from .const import CONF_BEHAVIOUR, CONF_PROFILE, CONF_REFERENCE, DOMAIN
from .profile import MODE_ATTR, cell_target, grid_cells, nominal_command, quick_keys
from .reference import reference_command, target_of
from .share import repo_url, submission_url, suggested_path

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
        ws_share,
        ws_copy_model,
        ws_reference,
        ws_settings,
        ws_settings_set,
        ws_profiles_update,
    ):
        websocket_api.async_register_command(hass, handler)


def _summary(data: LightMatchData) -> dict[str, Any]:
    caps = data.caps()
    total = len(grid_cells(caps))
    model = data.model_profile()
    return {
        "device": data.device_info().as_dict(),
        "model_profile": database(data.hass).summary(model) if model else None,
        "profile_choice": data.option(CONF_PROFILE),
        "transition": data.transition_supported(),
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
                "source": profile.cell_source(key),
                "target": target_of(key),
                "model_command": profile.model_cells.get(key),
                "command": profile.cell_command(key),
                "default": profile.default_command(cell["kind"], anchor, cell["brightness"]),
                "mode": profile.mode_for(cell["kind"]),
            }
        )
    connection.send_result(
        msg["id"], {**_summary(data), "cells": cells, "quick": quick_keys(profile.caps)}
    )


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
            found = database(hass).candidates(device_info_for(hass, msg["entity_id"]))
            if any(p.data.get("reference") for p in found):
                command = reference_command(target)
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


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/profile/share",
        vol.Required("entry_id"): str,
        vol.Optional("nickname"): vol.Any(None, str),
        vol.Optional("github"): vol.Any(None, str),
        vol.Optional("notes"): vol.Any(None, str),
        vol.Optional("reference"): vol.Any(None, str),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_share(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """The portable profile and a prefilled link to submit it to the database."""
    if (data := _get(hass, connection, msg)) is None:
        return
    try:
        profile = data.share(
            msg.get("reference") or None,
            nickname=msg.get("nickname") or None,
            github=msg.get("github") or None,
            notes=msg.get("notes") or None,
        )
    except vol.Invalid as err:
        connection.send_error(msg["id"], "invalid_format", f"Nickname or GitHub name not accepted: {err}")
        return
    integration = await async_get_integration(hass, DOMAIN)
    repo = repo_url(integration.manifest)
    path = suggested_path(data.device_info())
    url, json_in_url = submission_url(repo, profile, path)
    connection.send_result(
        msg["id"],
        {
            "profile": profile,
            "path": path,
            "filename": path.replace("/", "-"),
            "url": url,
            "json_in_url": json_in_url,
            "target": profile["target"],
            "cells": len(profile["cells"]),
        },
    )


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/reference", vol.Required("key"): str})
@callback
def ws_reference(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """The absolute lm-ref-1 target of a cell."""
    try:
        connection.send_result(msg["id"], target_of(msg["key"]))
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/cell/copy_model",
        vol.Required("entry_id"): str,
        vol.Optional("key", default="*"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_copy_model(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Copy one model profile cell, or all the user has not calibrated ('*')."""
    if (data := _get(hass, connection, msg)) is None:
        return
    connection.send_result(msg["id"], {"copied": await data.async_copy_model(msg["key"])})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/settings"})
@callback
def ws_settings(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """The online update setting and the last check."""
    connection.send_result(msg["id"], online_updater(hass).status())


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/settings/set", vol.Required("online_update"): bool}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_settings_set(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Turn the online profile update on or off."""
    updater = online_updater(hass)
    await updater.async_set_enabled(msg["online_update"])
    connection.send_result(msg["id"], updater.status())


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/profiles/update"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_profiles_update(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Check the online database now (only when the online update is on)."""
    updater = online_updater(hass)
    if not updater.enabled:
        connection.send_error(msg["id"], "not_enabled", "Turn the online update on first")
        return
    result = await updater.async_update()
    _refresh_twins(hass)
    connection.send_result(msg["id"], {**updater.status(), "result": result})


def _refresh_twins(hass: HomeAssistant) -> None:
    for entry in loaded_entries(hass):
        entry.runtime_data.refresh()
