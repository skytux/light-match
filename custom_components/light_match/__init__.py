"""Light Match: calibrated twin lights that look the same across brands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    BEHAVIOUR_REFERENCE,
    BEHAVIOUR_SMART,
    CONF_AUTO_FLOOR,
    CONF_BEHAVIOUR,
    CONF_FLOOR,
    CONF_HIDE_SOURCE,
    CONF_SOURCE,
    DEFAULT_OPTIONS,
    DOMAIN,
    KNOWN_FLOORS,
    STORAGE_VERSION,
)
from .profile import Profile, SourceCaps

if TYPE_CHECKING:
    from .light import CalibratedLight

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.LIGHT]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type LightMatchConfigEntry = ConfigEntry[LightMatchData]


@dataclass
class LightMatchData:
    """Runtime data of one twin: its source, profile cells and store."""

    hass: HomeAssistant
    entry: ConfigEntry
    store: Store
    cells: dict[str, dict[str, Any]] = field(default_factory=dict)
    entity: CalibratedLight | None = None
    _caps_attrs: dict[str, Any] = field(default_factory=dict)
    _listeners: list[Callable[[], None]] = field(default_factory=list)

    @property
    def source(self) -> str:
        """Entity id of the source light."""
        return self.entry.data[CONF_SOURCE]

    def option(self, key: str) -> Any:
        """An option of the entry, or its default."""
        return self.entry.options.get(key, DEFAULT_OPTIONS.get(key))

    async def async_load(self) -> None:
        """Load the stored profile cells."""
        stored = await self.store.async_load()
        if isinstance(stored, dict):
            self.cells = dict(stored.get("cells") or {})

    async def async_save(self) -> None:
        """Save the profile cells and refresh the twin's state."""
        await self.store.async_save({"cells": self.cells})
        for listener in list(self._listeners):
            listener()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call listener when the profile changes."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def remember_capabilities(self, attrs: dict[str, Any]) -> None:
        """Keep the capability attributes of the source's last known state."""
        if attrs.get("supported_color_modes"):
            self._caps_attrs = {
                "supported_color_modes": list(attrs["supported_color_modes"]),
                "min_color_temp_kelvin": attrs.get("min_color_temp_kelvin"),
                "max_color_temp_kelvin": attrs.get("max_color_temp_kelvin"),
            }

    def _registry_entry(self) -> er.RegistryEntry | None:
        return er.async_get(self.hass).async_get(self.source)

    def floor(self) -> int:
        """The source's brightness floor (0-255): configured or detected."""
        if not self.option(CONF_AUTO_FLOOR):
            return int(self.option(CONF_FLOOR) or 0)
        reg = self._registry_entry()
        return KNOWN_FLOORS.get(reg.platform, 0) if reg else 0

    def caps(self) -> SourceCaps:
        """The source's capabilities from its state, else the registry."""
        attrs = self._caps_attrs
        if not attrs:
            state = self.hass.states.get(self.source)
            if state is not None and state.attributes.get("supported_color_modes"):
                self.remember_capabilities(dict(state.attributes))
                attrs = self._caps_attrs
        if not attrs:
            reg = self._registry_entry()
            attrs = dict(reg.capabilities or {}) if reg else {}
        return SourceCaps.from_modes(
            attrs.get("supported_color_modes"),
            attrs.get("min_color_temp_kelvin"),
            attrs.get("max_color_temp_kelvin"),
            self.floor(),
        )

    def profile(self) -> Profile:
        """A profile object for the current cells, options and capabilities."""
        behaviour = self.option(CONF_BEHAVIOUR) or BEHAVIOUR_SMART
        return Profile(
            self.caps(),
            self.cells,
            fallback=behaviour,
            identity=behaviour == BEHAVIOUR_REFERENCE,
        )

    async def async_set_cell(self, key: str, command: dict[str, Any]) -> dict[str, Any]:
        """Store a calibrated cell."""
        profile = self.profile()
        clean = profile.set_cell(key, command)
        self.cells = profile.cells
        await self.async_save()
        return clean

    async def async_reset_cell(self, key: str) -> bool:
        """Forget a calibrated cell (or all with key '*')."""
        if key == "*":
            had = bool(self.cells)
            self.cells = {}
        else:
            had = self.cells.pop(key, None) is not None
        await self.async_save()
        return had

    def export(self, model: str | None = None) -> dict[str, Any]:
        """The profile as shareable JSON."""
        return self.profile().to_json(model)

    async def async_import(self, data: Any, merge: bool = False) -> tuple[int, int]:
        """Import an exported profile; returns (imported, skipped)."""
        profile = self.profile()
        result = profile.import_json(data, replace=not merge)
        self.cells = profile.cells
        await self.async_save()
        return result

    async def async_import_curve(self, curve: dict[Any, Any]) -> int:
        """Fill uncalibrated cells from a brightness curve (e.g. Lightener)."""
        profile = self.profile()
        written = profile.apply_brightness_curve({float(k): float(v) for k, v in curve.items()})
        self.cells = profile.cells
        await self.async_save()
        return written


def loaded_entries(hass: HomeAssistant) -> list[LightMatchConfigEntry]:
    """Config entries of this integration that are set up."""
    return [
        entry
        for entry in hass.config_entries.async_loaded_entries(DOMAIN)
        if isinstance(getattr(entry, "runtime_data", None), LightMatchData)
    ]


def data_for_entry(hass: HomeAssistant, entry_id: str) -> LightMatchData | None:
    """Runtime data of a loaded entry, by entry id."""
    for entry in loaded_entries(hass):
        if entry.entry_id == entry_id:
            return entry.runtime_data
    return None


def data_for_entity(hass: HomeAssistant, entity_id: str) -> LightMatchData | None:
    """Runtime data of the twin with this entity id."""
    reg = er.async_get(hass).async_get(entity_id)
    if reg is None or reg.platform != DOMAIN or reg.config_entry_id is None:
        return None
    return data_for_entry(hass, reg.config_entry_id)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the websocket API, services and the calibration panel."""
    from .panel import async_register_panel  # noqa: PLC0415
    from .services import async_register_services  # noqa: PLC0415
    from .websocket import async_register_websocket  # noqa: PLC0415

    async_register_websocket(hass)
    async_register_services(hass)
    await async_register_panel(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LightMatchConfigEntry) -> bool:
    """Set up one calibrated twin."""
    store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
    data = LightMatchData(hass, entry, store)
    await data.async_load()
    entry.runtime_data = data

    _apply_hidden(hass, entry)

    @callback
    def _registry_updated(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        """Follow a rename of the source entity."""
        if event.data["action"] != "update" or "old_entity_id" not in event.data:
            return
        if event.data["old_entity_id"] != entry.data[CONF_SOURCE]:
            return
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_SOURCE: event.data["entity_id"]}
        )
        hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _registry_updated)
    )
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: LightMatchConfigEntry) -> bool:
    """Unload a twin."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Restore the source entity and delete the stored profile."""
    _set_source_hidden(hass, entry.data[CONF_SOURCE], False)
    store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
    await store.async_remove()


@callback
def _apply_hidden(hass: HomeAssistant, entry: ConfigEntry) -> None:
    hide = entry.options.get(CONF_HIDE_SOURCE, DEFAULT_OPTIONS[CONF_HIDE_SOURCE])
    _set_source_hidden(hass, entry.data[CONF_SOURCE], bool(hide))


@callback
def _set_source_hidden(hass: HomeAssistant, entity_id: str, hidden: bool) -> None:
    """Hide or restore the source; never touches a user's own hiding."""
    registry = er.async_get(hass)
    reg = registry.async_get(entity_id)
    if reg is None:
        return
    if hidden and reg.hidden_by is None:
        registry.async_update_entity(entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION)
    elif not hidden and reg.hidden_by is er.RegistryEntryHider.INTEGRATION:
        registry.async_update_entity(entity_id, hidden_by=None)
