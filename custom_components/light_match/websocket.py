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
from .const import CONF_BEHAVIOUR, CONF_PROFILE, CONF_REFERENCE, CONF_WHITE_COLOR, DOMAIN
from .library import auto_groups, browse, check_profile, library, make_model, profile_filename
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
        ws_copy_profile,
        ws_apply,
        ws_library,
        ws_library_save,
        ws_library_get,
        ws_library_rename,
        ws_library_delete,
        ws_groups,
        ws_group_set,
        ws_group_delete,
        ws_browse,
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
    make, model_name = make_model(data)
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
        "white_color": data.option(CONF_WHITE_COLOR),
        "make": make,
        "model": model_name,
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
        mode = profile.cell_mode(key)
        item = {
            **cell,
            "calibrated": key in data.cells,
            "source": profile.cell_source(key),
            "target": target_of(key),
            "model_command": profile.model_cells.get(key),
            "command": profile.cell_command(key),
            "default": profile.default_command(cell["kind"], anchor, cell["brightness"], mode),
            "mode": mode,
        }
        if cell["kind"] == "white":
            # a white of a ct lamp with colour LEDs can be calibrated in either mode
            modes = profile.white_modes(anchor)
            item["modes"] = modes
            item["default_mode"] = profile.default_white_mode(anchor, cell["brightness"])
            item["commands"] = {m: profile.cell_command_in(key, m) for m in modes}
            item["defaults"] = {
                m: profile.default_command("white", anchor, cell["brightness"], m) for m in modes
            }
        cells.append(item)
    connection.send_result(
        msg["id"], {**_summary(data), "cells": cells, "quick": quick_keys(profile.caps)}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/cell/set",
        vol.Required("entry_id"): str,
        vol.Required("key"): str,
        vol.Required("command"): dict,
        vol.Optional("mode"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set_cell(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Save a calibrated cell."""
    if (data := _get(hass, connection, msg)) is None:
        return
    try:
        command = await data.async_set_cell(msg["key"], msg["command"], msg.get("mode"))
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
        vol.Optional("make"): str,
        vol.Optional("name"): str,
    }
)
@callback
def ws_export(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """The profile as shareable JSON, with its file name."""
    if (data := _get(hass, connection, msg)) is None:
        return
    profile = data.export(msg.get("model"), make=msg.get("make"), name=msg.get("name"))
    connection.send_result(msg["id"], {"profile": profile, "filename": profile_filename(profile["name"])})


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
        vol.Required("type"): f"{DOMAIN}/profile/copy",
        vol.Required("entry_id"): str,
        vol.Required("from_entry_id"): str,
        vol.Optional("merge", default=False): bool,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_copy_profile(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Reuse another twin's calibration on this one."""
    if (data := _get(hass, connection, msg)) is None:
        return
    source = data_for_entry(hass, msg["from_entry_id"])
    if source is None or source is data:
        connection.send_error(msg["id"], "not_found", "Choose another calibrated lamp to copy from")
        return
    try:
        imported, skipped = await data.async_copy_from(source, msg["merge"])
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


def _source_profile(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """The profile to apply: given, another twin's, a saved one or a database one.

    Raises ValueError (not found, not a profile, or no cells).
    """
    if "profile" in msg:
        return check_profile(msg["profile"])
    if "from_entry_id" in msg:
        source = data_for_entry(hass, msg["from_entry_id"])
        if source is None:
            raise ValueError("no such lamp to copy from")
        if not source.cells:
            raise ValueError("that lamp has no calibrated cells to copy")
        return source.profile().to_json()
    if "library_id" in msg:
        item = library(hass).get(msg["library_id"])
        if item is None:
            raise ValueError("no such saved profile")
        return check_profile(item["profile"])
    if "db_id" in msg:
        prof = database(hass).get(msg["db_id"])
        if prof is None:
            raise ValueError("no such profile in the database")
        return check_profile(prof.data)
    raise ValueError("nothing to apply")


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/profile/apply",
        vol.Required("entry_ids"): vol.All([str], vol.Length(min=1)),
        vol.Optional("merge", default=False): bool,
        vol.Exclusive("profile", "source"): dict,
        vol.Exclusive("from_entry_id", "source"): str,
        vol.Exclusive("library_id", "source"): str,
        vol.Exclusive("db_id", "source"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_apply(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Apply one profile to several lamps, each converted for its own capabilities.

    An empty source is refused before any lamp is touched; when replacing, a
    lamp none of whose cells fit is left alone.
    """
    try:
        source = _source_profile(hass, msg)
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    results = []
    for entry_id in dict.fromkeys(msg["entry_ids"]):
        data = data_for_entry(hass, entry_id)
        result: dict[str, Any] = {"entry_id": entry_id, "title": data.entry.title if data else None}
        if data is None:
            result["error"] = "no such lamp"
        elif entry_id == msg.get("from_entry_id"):
            result["error"] = "this is the lamp copied from"
        else:
            try:
                result["imported"], result["skipped"] = await data.async_import(
                    source, msg["merge"], refuse_empty=True
                )
            except ValueError as err:
                result["error"] = str(err)
        results.append(result)
    connection.send_result(msg["id"], {"results": results})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/library"})
@websocket_api.require_admin
@callback
def ws_library(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """The saved profiles (without their cells)."""
    connection.send_result(msg["id"], {"profiles": library(hass).list()})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/library/save",
        vol.Exclusive("entry_id", "source"): str,
        vol.Exclusive("profile", "source"): dict,
        vol.Optional("name"): vol.Any(None, str),
        vol.Optional("make"): vol.Any(None, str),
        vol.Optional("model"): vol.Any(None, str),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_library_save(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Save a lamp's calibration, or a given (imported) profile, to the library."""
    source = None
    if "entry_id" in msg:
        if (data := _get(hass, connection, msg)) is None:
            return
        if not data.cells:
            connection.send_error(msg["id"], "invalid_format", "This lamp has no calibrated cells to save")
            return
        profile = data.export(msg.get("model"), make=msg.get("make"), name=msg.get("name"))
        source = data.entry.title
    elif "profile" in msg:
        profile = msg["profile"]
    else:
        connection.send_error(msg["id"], "invalid_format", "Give entry_id or profile")
        return
    try:
        item = await library(hass).async_add(
            profile, name=msg.get("name"), make=msg.get("make"), model=msg.get("model"), source=source
        )
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    connection.send_result(msg["id"], {"profile": library(hass).summary(item)})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/library/get", vol.Required("item_id"): str})
@websocket_api.require_admin
@callback
def ws_library_get(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """A saved profile with its cells."""
    item = library(hass).get(msg["item_id"])
    if item is None:
        connection.send_error(msg["id"], "not_found", "No such saved profile")
        return
    connection.send_result(msg["id"], {"profile": item["profile"], "filename": profile_filename(item["name"])})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/library/rename", vol.Required("item_id"): str, vol.Required("name"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_library_rename(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Rename a saved profile."""
    try:
        item = await library(hass).async_rename(msg["item_id"], msg["name"])
    except KeyError:
        connection.send_error(msg["id"], "not_found", "No such saved profile")
        return
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    connection.send_result(msg["id"], {"profile": library(hass).summary(item)})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/library/delete", vol.Required("item_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_library_delete(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Forget a saved profile."""
    try:
        await library(hass).async_delete(msg["item_id"])
    except KeyError:
        connection.send_error(msg["id"], "not_found", "No such saved profile")
        return
    connection.send_result(msg["id"], {})


def _groups(hass: HomeAssistant) -> dict[str, Any]:
    twins = [e.runtime_data for e in loaded_entries(hass)]
    return {"auto": auto_groups(twins), "manual": library(hass).manual_groups()}


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/groups"})
@callback
def ws_groups(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Lamp groups: automatic (same make and model) and hand-made."""
    connection.send_result(msg["id"], _groups(hass))


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/groups/set",
        vol.Optional("group_id"): vol.Any(None, str),
        vol.Required("name"): str,
        vol.Required("entry_ids"): [str],
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_group_set(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Create (no group_id) or change a hand-made group."""
    try:
        await library(hass).async_set_group(msg.get("group_id"), msg["name"], msg["entry_ids"])
    except KeyError:
        connection.send_error(msg["id"], "not_found", "No such group")
        return
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_format", str(err))
        return
    connection.send_result(msg["id"], _groups(hass))


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/groups/delete", vol.Required("group_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_group_delete(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Remove a hand-made group."""
    try:
        await library(hass).async_delete_group(msg["group_id"])
    except KeyError:
        connection.send_error(msg["id"], "not_found", "No such group")
        return
    connection.send_result(msg["id"], _groups(hass))


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/profiles/browse"})
@callback
def ws_browse(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Profiles in three separate sections: my calibrations, built-in, community."""
    connection.send_result(msg["id"], browse(library(hass), database(hass).all()))
