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
from homeassistant.util import dt as dt_util

from .const import (
    BEHAVIOUR_REFERENCE,
    BEHAVIOUR_SMART,
    CONF_AUTO_FLOOR,
    CONF_BEHAVIOUR,
    CONF_FLOOR,
    CONF_HIDE_SOURCE,
    CONF_PROFILE,
    CONF_REFERENCE,
    CONF_SOURCE,
    CONF_WHITE_COLOR,
    DEFAULT_OPTIONS,
    DOMAIN,
    PROFILE_AUTO,
    PROFILE_NONE,
    STORAGE_VERSION,
    WHITE_COLOR_AUTO,
)
from .database import (
    SUBFLOOR_NONE,
    TARGET_HOUSE,
    TARGET_REFERENCE,
    DeviceInfo,
    ModelProfile,
    ProfileDatabase,
    gamut_points,
    load_bundled,
    merge_capabilities,
    trusted,
)
from .formats import CAL_SCREEN, CALIBRATION_KEYS, clean_calibration, clean_notes, migrate
from .profile import Profile, SourceCaps
from .share import portable_profile

if TYPE_CHECKING:
    from .light import CalibratedLight
    from .online import OnlineUpdater

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
    # how the cells were calibrated (formats.CALIBRATION_SCHEMA) and a free note
    calibration: dict[str, Any] = field(default_factory=dict)
    notes: str | None = None
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
            try:
                self.calibration = clean_calibration(stored.get("calibration"))
                self.notes = clean_notes(stored.get("notes"))
            except ValueError:
                self.calibration, self.notes = {}, None

    async def async_save(self) -> None:
        """Save the profile cells and refresh the twin's state."""
        stored: dict[str, Any] = {"cells": self.cells}
        if self.calibration:
            stored["calibration"] = self.calibration
        if self.notes:
            stored["notes"] = self.notes
        await self.store.async_save(stored)
        for listener in list(self._listeners):
            listener()

    @callback
    def refresh(self) -> None:
        """Tell the twin its model profile may have changed."""
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
            # some integrations expose the colour gamut; use it when they do
            for key in ("gamut_type", "gamut"):
                if attrs.get(key) is not None:
                    self._caps_attrs[key] = attrs[key]

    def _registry_entry(self) -> er.RegistryEntry | None:
        return er.async_get(self.hass).async_get(self.source)

    def device_info(self) -> DeviceInfo:
        """How Home Assistant identifies the source lamp."""
        return device_info_for(self.hass, self.source)

    def model_profiles(self) -> list[ModelProfile]:
        """The model profiles in use, best first: the chosen or best match, then other matches."""
        choice = self.option(CONF_PROFILE) or PROFILE_AUTO
        if choice == PROFILE_NONE:
            return []
        db = database(self.hass)
        found = db.candidates(self.device_info())
        if choice == PROFILE_AUTO:
            return found
        chosen = db.get(choice)
        return [chosen, *(p for p in found if p.id != chosen.id)] if chosen else []

    def model_profile(self) -> ModelProfile | None:
        """The model profile in use: chosen in the options, or matched."""
        found = self.model_profiles()
        return found[0] if found else None

    def model_capabilities(self) -> tuple[dict[str, Any], dict[str, ModelProfile]]:
        """Capabilities of every matching profile, best first, and where each came from."""
        return merge_capabilities(self.model_profiles())

    def floor(self) -> int:
        """The source's brightness floor (0-255): configured or from the model profiles."""
        if not self.option(CONF_AUTO_FLOOR):
            return int(self.option(CONF_FLOOR) or 0)
        return int(self.model_capabilities()[0].get("brightness_floor", 0))

    def transition_supported(self) -> bool:
        """Whether transitions are passed to the source."""
        override = self.model_capabilities()[0].get("transition")
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
        mcaps, origin = self.model_capabilities()

        def measured(key: str) -> bool:
            return key in origin and trusted(origin[key])

        # What the lamp reports wins over spec sheets and imported data;
        # only a measured or verified community profile overrides it.
        modes = attrs.get("supported_color_modes") or mcaps.get("color_modes")
        lo, hi = attrs.get("min_color_temp_kelvin"), attrs.get("max_color_temp_kelvin")
        if measured("min_kelvin") or not (lo and hi):
            lo, hi = mcaps.get("min_kelvin") or lo, mcaps.get("max_kelvin") or hi
        gamut = gamut_points(mcaps.get("gamut")) if measured("gamut") else None
        if gamut is None:
            gamut = _runtime_gamut(attrs) or gamut_points(mcaps.get("gamut"))
        gain = mcaps.get("channel_gain")
        curve = mcaps.get("output_curve")
        return SourceCaps.from_modes(
            modes,
            lo,
            hi,
            self.floor(),
            gamut=gamut,
            subfloor=mcaps.get("subfloor") != SUBFLOOR_NONE,
            channel_gain=tuple(gain) if gain else None,
            output_curve=tuple((float(b), float(y)) for b, y in curve) if curve else None,
        )

    def profile(self) -> Profile:
        """A profile object for the current cells, options and capabilities."""
        behaviour = self.option(CONF_BEHAVIOUR) or BEHAVIOUR_SMART
        profile = Profile(
            self.caps(),
            self.cells,
            fallback=behaviour,
            identity=behaviour == BEHAVIOUR_REFERENCE,
            white_color=self.option(CONF_WHITE_COLOR) or WHITE_COLOR_AUTO,
        )
        model = next((p for p in self.model_profiles() if p.cells), None)
        if model is not None:
            profile.model_cells, _ = profile.convert_cells(model.cells, model.modes)
        return profile

    async def async_set_cell(
        self, key: str, command: dict[str, Any], mode: str | None = None
    ) -> dict[str, Any]:
        """Store a calibrated cell (a white in one of its modes)."""
        profile = self.profile()
        clean = profile.set_cell(key, command, mode)
        self.cells = profile.cells
        await self.async_save()
        return clean

    async def async_copy_model(self, key: str = "*") -> int:
        """Copy model profile cells into the user's cells."""
        profile = self.profile()
        copied = profile.copy_model(key)
        if copied:
            self.cells = profile.cells
            await self.async_save()
        return copied

    async def async_reset_cell(self, key: str) -> bool:
        """Forget a calibrated cell (or all with key '*')."""
        if key == "*":
            had = bool(self.cells)
            self.cells = {}
        else:
            had = self.cells.pop(key, None) is not None
        await self.async_save()
        return had

    def export(
        self, model: str | None = None, make: str | None = None, name: str | None = None
    ) -> dict[str, Any]:
        """The profile as shareable JSON, named ``<make>_<model>_<date>_<random>``.

        Make and model default to the lamp's (device registry, else model profile).
        """
        from .library import make_model, profile_name  # noqa: PLC0415

        auto_make, auto_model = make_model(self)
        make = (make or "").strip() or auto_make
        model = (model or "").strip() or auto_model
        meta: dict[str, Any] = {
            "name": (name or "").strip()[:120] or profile_name(make, model),
            "date": dt_util.now().date().isoformat(),
        }
        if make:
            meta["make"] = make
        meta.update(self.calibration_meta())
        return self.profile().to_json(model, meta=meta)

    def calibration_meta(self) -> dict[str, Any]:
        """The calibration details and notes that go into exported files."""
        meta: dict[str, Any] = {"calibration": {"method": "by_eye", **self.calibration}}
        meta["calibration"].setdefault(CAL_SCREEN, False)
        if self.notes:
            meta["notes"] = self.notes
        return meta

    async def async_set_calibration(self, calibration: Any, notes: Any) -> None:
        """Store the calibration details and note; ValueError if not acceptable."""
        cal, text = clean_calibration(calibration), clean_notes(notes)  # both valid, or nothing changes
        self.calibration, self.notes = cal, text
        await self.async_save()

    def share_target(self, reference: str | None = None) -> str:
        """lm-ref-1 if the cells were matched to a reference-space lamp, else house."""
        ref = reference or self.option(CONF_REFERENCE)
        if not ref:
            return TARGET_HOUSE
        twin = data_for_entity(self.hass, ref)
        source = twin.source if twin is not None else ref
        found = database(self.hass).candidates(device_info_for(self.hass, source))
        return TARGET_REFERENCE if any(p.data.get("reference") for p in found) else TARGET_HOUSE

    def share_capabilities(self) -> dict[str, Any]:
        """What Home Assistant knows of the lamp, for a shared profile."""
        caps = self.caps()
        modes = self._caps_attrs.get("supported_color_modes") or []
        out: dict[str, Any] = {"color_modes": sorted(str(m) for m in modes)}
        if caps.white_mode == "color_temp":
            out["min_kelvin"], out["max_kelvin"] = caps.min_kelvin, caps.max_kelvin
        if caps.floor:
            out["brightness_floor"] = caps.floor
        return out

    def share(self, reference: str | None = None, **contributor: Any) -> dict[str, Any]:
        """The portable profile of the user's own cells."""
        return portable_profile(
            self.profile().to_json(meta=self.calibration_meta()),
            self.device_info(),
            self.share_capabilities(),
            self.share_target(reference),
            **contributor,
        )

    async def async_import(
        self, data: Any, merge: bool = False, refuse_empty: bool = False
    ) -> tuple[int, int]:
        """Import an exported profile; returns (imported, skipped).

        ``refuse_empty`` leaves the lamp alone (ValueError) when no cell fits it.
        When replacing, the lamp takes the profile's calibration details and note.
        """
        profile = self.profile()
        result = profile.import_json(data, replace=not merge)
        if refuse_empty and not result[0]:
            raise ValueError("no cell of the profile fits this lamp")
        self.cells = profile.cells
        if not merge:
            current = migrate(data)
            try:
                self.calibration = clean_calibration(
                    {k: v for k, v in (current.get("calibration") or {}).items() if k in CALIBRATION_KEYS}
                )
                self.notes = clean_notes(current.get("notes"))
            except ValueError:
                self.calibration, self.notes = {}, None
        await self.async_save()
        return result

    async def async_copy_from(self, source: LightMatchData, merge: bool = False) -> tuple[int, int]:
        """Reuse another twin's calibrated cells, converted like an import."""
        if not source.cells:
            raise ValueError("That lamp has no calibrated cells to copy")
        return await self.async_import(source.profile().to_json(meta=source.calibration_meta()), merge)

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


def online_updater(hass: HomeAssistant) -> OnlineUpdater:
    """The online updater (created at setup)."""
    return hass.data[DOMAIN]["online"]


def _runtime_gamut(attrs: dict[str, Any]) -> tuple[tuple[float, float], ...] | None:
    """A colour gamut the integration exposes, if any (a letter or corner points)."""
    for key in ("gamut_type", "gamut"):
        value = attrs.get(key)
        if isinstance(value, str) and value.upper() in ("A", "B", "C"):
            return gamut_points(value.upper())
        if isinstance(value, (list, tuple)) and len(value) == 3:
            try:
                return tuple((float(p[0]), float(p[1])) for p in value)
            except (TypeError, ValueError, IndexError):
                continue
    return None


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
    from .library import library  # noqa: PLC0415
    from .online import OnlineUpdater  # noqa: PLC0415
    from .panel import async_register_panel  # noqa: PLC0415
    from .services import async_register_services  # noqa: PLC0415
    from .views import async_register_views  # noqa: PLC0415
    from .websocket import async_register_websocket  # noqa: PLC0415

    db = await async_get_database(hass)
    updater = OnlineUpdater(hass, db)
    hass.data[DOMAIN]["online"] = updater
    await updater.async_load()
    await library(hass).async_load()
    async_register_websocket(hass)
    async_register_services(hass)
    async_register_views(hass)
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
