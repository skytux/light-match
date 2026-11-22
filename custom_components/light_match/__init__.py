"""Light Match: calibrated twin lights that look the same across brands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    BEHAVIOUR_REFERENCE,
    BEHAVIOUR_SMART,
    CONF_AUTO_FLOOR,
    CONF_BEHAVIOUR,
    CONF_FLOOR,
    CONF_HIDE_SOURCE,
    CONF_PROFILE,
    CONF_SOURCE,
    DEFAULT_OPTIONS,
    DOMAIN,
    PROFILE_AUTO,
    PROFILE_NONE,
    STORAGE_VERSION,
)
from .database import (
    SUBFLOOR_NONE,
    DeviceInfo,
    ModelProfile,
    ProfileDatabase,
    gamut_points,
    load_bundled,
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

    def device_info(self) -> DeviceInfo:
        """How Home Assistant identifies the source lamp."""
        return device_info_for(self.hass, self.source)

    def model_profile(self) -> ModelProfile | None:
        """The model profile in use: chosen in the options, or matched."""
        choice = self.option(CONF_PROFILE) or PROFILE_AUTO
        if choice == PROFILE_NONE:
            return None
        db = database(self.hass)
        if choice != PROFILE_AUTO:
            return db.get(choice)
        return db.match(self.device_info())

    def floor(self) -> int:
        """The source's brightness floor (0-255): configured or from the model profile."""
        if not self.option(CONF_AUTO_FLOOR):
            return int(self.option(CONF_FLOOR) or 0)
        model = self.model_profile()
        return int(model.capabilities.get("brightness_floor", 0)) if model else 0

    def transition_supported(self) -> bool:
        """Whether transitions are passed to the source."""
        model = self.model_profile()
        override = model.capabilities.get("transition") if model else None
        if override is not None:
            return bool(override)
        state = self.hass.states.get(self.source)
        if state is None:
            return True
        return bool(int(state.attributes.get("supported_features", 0)) & 32)

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
        model = self.model_profile()
        mcaps = model.capabilities if model else {}
        modes = attrs.get("supported_color_modes") or mcaps.get("color_modes")
        gain = mcaps.get("channel_gain")
        return SourceCaps.from_modes(
            modes,
            mcaps.get("min_kelvin") or attrs.get("min_color_temp_kelvin"),
            mcaps.get("max_kelvin") or attrs.get("max_color_temp_kelvin"),
            self.floor(),
            gamut=gamut_points(mcaps.get("gamut")),
            subfloor=mcaps.get("subfloor") != SUBFLOOR_NONE,
            channel_gain=tuple(gain) if gain else None,
        )

    def profile(self) -> Profile:
        """A profile object for the current cells, options and capabilities."""
        behaviour = self.option(CONF_BEHAVIOUR) or BEHAVIOUR_SMART
        profile = Profile(
            self.caps(),
            self.cells,
            fallback=behaviour,
            identity=behaviour == BEHAVIOUR_REFERENCE,
        )
        model = self.model_profile()
        if model is not None and model.cells:
            profile.model_cells, _ = profile.convert_cells(model.cells, model.modes)
        return profile

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


def database(hass: HomeAssistant) -> ProfileDatabase:
    """The profile database (empty until the integration is set up)."""
    return hass.data.setdefault(DOMAIN, {}).setdefault("db", ProfileDatabase())


async def async_get_database(hass: HomeAssistant) -> ProfileDatabase:
    """The profile database, reading the bundled profiles the first time."""
    db = database(hass)
    if not db.loaded:
        db.bundled = await hass.async_add_executor_job(load_bundled)
        db.loaded = True
    return db


def device_info_for(hass: HomeAssistant, entity_id: str) -> DeviceInfo:
    """Manufacturer, model, integration and firmware of a light's device."""
    reg = er.async_get(hass).async_get(entity_id)
    if reg is None:
        return DeviceInfo()
    device = dr.async_get(hass).async_get(reg.device_id) if reg.device_id else None
    if device is None:
        return DeviceInfo(integration=reg.platform)
    return DeviceInfo(
        manufacturer=device.manufacturer,
        model=device.model,
        model_id=getattr(device, "model_id", None),
        integration=reg.platform,
        firmware=device.sw_version,
    )


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

    await async_get_database(hass)
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
