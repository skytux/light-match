"""The user's own saved calibrations and lamp groups.

Kept in their own store (``light_match.library``), apart from the profiles
shipped with the integration and the community ones, so they stay easy to
find. Groups are either automatic (lamps of the same make and model, worked
out live from the device registry) or made by hand and stored here.
"""

from __future__ import annotations

from datetime import date
import re
import secrets
import string
from typing import TYPE_CHECKING, Any
import uuid

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, PROFILE_FORMAT, PROFILE_VERSION
from .database import ModelProfile, norm
from .formats import (
    FORMAT_VERSION,
    NewerFormatError,
    clean_notes,
    format_version,
    good_practice,
    is_newer,
    migrate,
    written_format,
)

if TYPE_CHECKING:
    from . import LightMatchData

LIBRARY_VERSION = 1
NAME_MAX = 120
TOKEN_CHARS = string.ascii_lowercase + string.digits

# Section names of the profile browser.
SECTION_MINE = "mine"
SECTION_BUILTIN = "builtin"
SECTION_COMMUNITY = "community"


def _slug(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9.-]+", "_", value or "").strip("_.")


def profile_name(
    make: str | None, model: str | None, day: date | None = None, token: str | None = None
) -> str:
    """``<make>_<model>_<YYYY-MM-DD>_<random>``, safe as a file name."""
    parts = [p for p in (_slug(make), _slug(model)) if p] or ["profile"]
    day = day or dt_util.now().date()
    token = token or "".join(secrets.choice(TOKEN_CHARS) for _ in range(5))
    return "_".join([*parts, day.isoformat(), token])[:NAME_MAX]


def profile_filename(name: str) -> str:
    """The file name of an exported profile."""
    return f"{_slug(name) or 'profile'}.light-match.json"


def make_model(data: LightMatchData) -> tuple[str | None, str | None]:
    """Make and model of a twin's lamp: the device registry, else the model profile."""
    dev = data.device_info()
    make, model = dev.manufacturer, dev.model or dev.model_id
    if not (make and model):
        prof = data.model_profile()
        if prof is not None:
            device = prof.data.get("device") or {}
            make = make or device.get("manufacturer")
            models = [m for m in device.get("models") or [] if m != "*"]
            model = model or (models[0] if models else None)
    return make or None, model or None


def check_profile(data: Any, allow_newer: bool = False) -> dict[str, Any]:
    """An exported profile with at least one cell, or ValueError.

    A profile of a newer format raises NewerFormatError, unless ``allow_newer``
    (the library keeps it read-only).
    """
    if not isinstance(data, dict) or data.get("format") != PROFILE_FORMAT:
        raise ValueError("not a Light Match profile")
    version = format_version(data)
    if version > FORMAT_VERSION:
        if not allow_newer:
            raise NewerFormatError(version)
        return data
    try:
        version = int(data.get("version", 0))
    except (TypeError, ValueError) as err:
        raise ValueError("bad profile version") from err
    if version > PROFILE_VERSION:
        raise ValueError("profile is from a newer version of Light Match")
    cells = data.get("cells")
    if not isinstance(cells, dict) or not cells:
        raise ValueError("the profile has no calibrated cells")
    return data


def _text(value: Any, limit: int = NAME_MAX) -> str | None:
    text = str(value or "").strip()[:limit]
    return text or None


class Library:
    """Saved calibrations and manual groups."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store: Store = Store(hass, LIBRARY_VERSION, f"{DOMAIN}.library")
        self.profiles: dict[str, dict[str, Any]] = {}
        self.groups: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        """Read the store."""
        stored = await self.store.async_load() or {}
        self.profiles = {p["id"]: p for p in stored.get("profiles") or [] if isinstance(p, dict) and p.get("id")}
        self.groups = {g["id"]: g for g in stored.get("groups") or [] if isinstance(g, dict) and g.get("id")}

    async def async_save(self) -> None:
        """Write the store."""
        await self.store.async_save(
            {"profiles": list(self.profiles.values()), "groups": list(self.groups.values())}
        )

    # profiles

    @staticmethod
    def summary(item: dict[str, Any]) -> dict[str, Any]:
        """A saved profile without its cells."""
        prof = item["profile"]
        try:
            version = format_version(prof)
        except ValueError:
            version = 1
        return {
            "id": item["id"],
            "name": item["name"],
            "make": item.get("make"),
            "model": item.get("model"),
            "date": item.get("date"),
            "source": item.get("source"),
            "cells": len(prof.get("cells") or {}) if isinstance(prof.get("cells"), dict) else 0,
            "modes": {k: v for k, v in (prof.get("modes") or {}).items() if k != "cells"},
            "format_version": version,
            "read_only": version > FORMAT_VERSION,
            "convertible": version <= FORMAT_VERSION and version < written_format(prof),
            "good_practice": good_practice(prof),
            "notes": prof.get("notes") if isinstance(prof.get("notes"), str) else None,
            "calibration": prof.get("calibration") if isinstance(prof.get("calibration"), dict) else {},
        }

    def list(self) -> list[dict[str, Any]]:
        """Every saved profile, newest first."""
        items = sorted(self.profiles.values(), key=lambda p: (p.get("date") or "", p["name"]), reverse=True)
        return [self.summary(p) for p in items]

    def get(self, item_id: str) -> dict[str, Any] | None:
        """A saved profile with its cells."""
        return self.profiles.get(item_id)

    async def async_add(
        self,
        profile: Any,
        name: str | None = None,
        make: str | None = None,
        model: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        """Save a profile as it is (a newer format stays read-only).

        Raises ValueError if it is not a profile, has no cells or bad notes.
        """
        profile = dict(check_profile(profile, allow_newer=True))
        if not is_newer(profile):
            clean_notes(profile.get("notes"))
        make = _text(make) or _text(profile.get("make"))
        model = _text(model) or _text(profile.get("model"))
        name = _text(name) or _text(profile.get("name")) or profile_name(make, model)
        day = dt_util.now().date().isoformat()
        profile.update({"name": name, "date": profile.get("date") or day})
        if make:
            profile["make"] = make
        if model:
            profile["model"] = model
        item = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "make": make,
            "model": model,
            "date": day,
            "source": _text(source),
            "profile": profile,
        }
        self.profiles[item["id"]] = item
        await self.async_save()
        return item

    async def async_rename(self, item_id: str, name: str) -> dict[str, Any]:
        """Rename a saved profile; KeyError if unknown, ValueError if the name is empty."""
        item = self.profiles[item_id]
        if not (clean := _text(name)):
            raise ValueError("the name is empty")
        item["name"] = clean
        item["profile"]["name"] = clean
        await self.async_save()
        return item

    async def async_convert(self, item_id: str) -> dict[str, Any]:
        """Rewrite a saved profile in the current format.

        KeyError if unknown, NewerFormatError if it is newer than this code.
        """
        item = self.profiles[item_id]
        item["profile"] = migrate(item["profile"])
        await self.async_save()
        return item

    async def async_delete(self, item_id: str) -> None:
        """Forget a saved profile; KeyError if unknown."""
        del self.profiles[item_id]
        await self.async_save()

    # groups

    def manual_groups(self) -> list[dict[str, Any]]:
        """The hand-made groups, by name."""
        return sorted(
            ({"id": g["id"], "name": g["name"], "entry_ids": list(g["entry_ids"]), "auto": False} for g in self.groups.values()),
            key=lambda g: g["name"].lower(),
        )

    async def async_set_group(self, group_id: str | None, name: str, entry_ids: list[str]) -> dict[str, Any]:
        """Create (no id) or change a manual group."""
        if not (clean := _text(name, 60)):
            raise ValueError("the group name is empty")
        if group_id is not None and group_id not in self.groups:
            raise KeyError(group_id)
        group = {"id": group_id or uuid.uuid4().hex[:12], "name": clean, "entry_ids": list(dict.fromkeys(entry_ids))}
        self.groups[group["id"]] = group
        await self.async_save()
        return group

    async def async_delete_group(self, group_id: str) -> None:
        """Remove a manual group; KeyError if unknown."""
        del self.groups[group_id]
        await self.async_save()


def library(hass: HomeAssistant) -> Library:
    """The library (loaded in the integration's setup)."""
    data = hass.data.setdefault(DOMAIN, {})
    if "library" not in data:
        data["library"] = Library(hass)
    return data["library"]


def auto_groups(twins: list[LightMatchData]) -> list[dict[str, Any]]:
    """Lamps of the same make and model, worked out live (two or more lamps)."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for data in twins:
        make, model = make_model(data)
        if not (make and model):
            continue
        key = (norm(make), norm(model))
        group = found.setdefault(
            key, {"id": f"auto:{key[0]}:{key[1]}", "name": f"{make} {model}", "entry_ids": [], "auto": True}
        )
        group["entry_ids"].append(data.entry.entry_id)
    return sorted((g for g in found.values() if len(g["entry_ids"]) > 1), key=lambda g: g["name"].lower())


def section_of(prof: ModelProfile) -> str:
    """Built-in (shipped calibrations) or community (imported data and online profiles)."""
    if prof.origin == "online" or prof.id.startswith("imported/"):
        return SECTION_COMMUNITY
    return SECTION_BUILTIN


def db_entry(prof: ModelProfile) -> dict[str, Any]:
    """A database profile for the browser."""
    device = prof.data.get("device") or {}
    prov = prof.data.get("provenance") or {}
    return {
        "id": prof.id,
        "name": prof.name,
        "make": device.get("manufacturer"),
        "models": list(device.get("models") or []),
        "cells": len(prof.cells),
        "method": prov.get("method"),
        "verified": bool(prov.get("verified")),
        "origin": prof.origin,
        "read_only": prof.read_only,
        "good_practice": good_practice(prof.data),
        "notes": prof.data.get("notes") if isinstance(prof.data.get("notes"), str) else None,
        "discussion": prof.data.get("discussion") if isinstance(prof.data.get("discussion"), str) else None,
    }


def browse(
    lib: Library, profiles: dict[str, ModelProfile], read_only: list[ModelProfile] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """The three separate sections of the profile browser (newer formats listed read-only)."""
    sections: dict[str, list[dict[str, Any]]] = {SECTION_MINE: lib.list(), SECTION_BUILTIN: [], SECTION_COMMUNITY: []}
    for prof in sorted([*profiles.values(), *(read_only or [])], key=lambda p: (p.id, p.read_only)):
        sections[section_of(prof)].append(db_entry(prof))
    return sections
