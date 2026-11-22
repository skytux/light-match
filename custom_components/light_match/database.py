"""The lamp profile database: model profiles by manufacturer and model.

A *model profile* describes one lamp model (or a family): how Home Assistant
identifies it (manufacturer, model ids, integration, firmware range), its
capabilities as adapters (brightness floor, sub-floor method, kelvin range,
xy gamut, channel gains, transition support) and, optionally, calibrated
cells in the shared reference space. Profiles ship with the integration in
``profiles/<manufacturer>/<model>.json`` and can be refreshed from the
online database (see :mod:`.online`).

This module holds no Home Assistant state apart from reading files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote

import voluptuous as vol

from .const import PROFILE_FORMAT, PROFILE_VERSION
from .profile import parse_cell_key, validate_command

_LOGGER = logging.getLogger(__name__)

PROFILES_DIR = Path(__file__).parent / "profiles"
SCHEMA_FILE = PROFILES_DIR / "profile.schema.json"
INDEX_FILE = PROFILES_DIR / "index.json"

METHOD_SPEC = "spec"
METHOD_BY_EYE = "by_eye"
METHOD_LUX = "lux_meter"
METHOD_COLORIMETER = "colorimeter"
METHOD_IMPORTED = "imported"  # generated from a public database: partly calibrated, unverified
METHODS = [METHOD_SPEC, METHOD_IMPORTED, METHOD_BY_EYE, METHOD_LUX, METHOD_COLORIMETER]
MEASURED_METHODS = {METHOD_BY_EYE, METHOD_LUX, METHOD_COLORIMETER}
# Licences of the sources imported data may come from (SPDX ids).
IMPORT_LICENCES = ["MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0"]
# How good an imported field is: measured by the source (e.g. a power meter
# sweep), a published spec, derived from power ("power-proxy") or estimated.
IMPORT_QUALITIES = ["measured-by-source", "spec", "power-proxy", "estimated", "derived"]

SUBFLOOR_CHANNEL_SCALE = "channel_scale"
SUBFLOOR_NONE = "none"

DATA_LICENSE = "CC-BY-4.0"
CONTRIBUTOR_ROLES = ["calibrated", "measured", "verified", "edited"]
# A contributor is credited by a nickname they choose; never a real name or email.
_NICK_RE = r"^[^@<>\s][^@<>]{0,38}[^@<>\s]$|^[^@<>\s]$"
_GITHUB_RE = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$"
METHOD_RANK = {METHOD_COLORIMETER: 4, METHOD_LUX: 4, METHOD_BY_EYE: 3, METHOD_IMPORTED: 2, METHOD_SPEC: 1}

DISCUSSIONS_URL = "https://github.com/skytux/ha-light-match/discussions/categories/lamp-profiles"

TARGET_REFERENCE = "lm-ref-1"
TARGET_HOUSE = "house"

# Philips Hue colour gamuts (CIE xy of the red, green and blue corners).
# Source: Philips Hue developer documentation, as published in the MIT
# licensed rgbxy converter: https://github.com/benknight/hue-python-rgb-converter
NAMED_GAMUTS: dict[str, tuple[tuple[float, float], ...]] = {
    "A": ((0.704, 0.296), (0.2151, 0.7106), (0.138, 0.08)),
    "B": ((0.675, 0.322), (0.4091, 0.518), (0.167, 0.04)),
    "C": ((0.692, 0.308), (0.17, 0.7), (0.153, 0.048)),
}

COLOR_MODES = ["onoff", "brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"]
COMMAND_MODES = ["rgb", "rgbw", "rgbww", "hs", "color_temp", "brightness"]

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_.-]*$")


def _xy(value: Any) -> list[float]:
    point = vol.Schema(vol.All([vol.Coerce(float)], vol.Length(min=2, max=2)))(value)
    if not all(0.0 <= v <= 1.0 for v in point):
        raise vol.Invalid("xy values must be 0-1")
    return point


GAMUT_SCHEMA = vol.Any(
    vol.In(list(NAMED_GAMUTS)),
    vol.Schema({vol.Required("red"): _xy, vol.Required("green"): _xy, vol.Required("blue"): _xy}),
)

def _output_curve(value: Any) -> list[list[float]]:
    """[[brightness 1-255, relative output 0-1], ...]: brightness rising, output not falling."""
    points = vol.Schema(vol.All([vol.All([vol.Coerce(float)], vol.Length(min=2, max=2))], vol.Length(min=2, max=64)))(
        value
    )
    for b, y in points:
        if not (1 <= b <= 255 and 0 <= y <= 1):
            raise vol.Invalid("curve points are [brightness 1-255, output 0-1]")
    for (b0, y0), (b1, y1) in zip(points, points[1:], strict=False):
        if b1 <= b0 or y1 < y0:
            raise vol.Invalid("curve brightness must rise and output must not fall")
    if points[-1][1] <= points[0][1]:
        raise vol.Invalid("curve output must rise")
    return points


CAPABILITIES_SCHEMA = vol.Schema(
    {
        vol.Optional("color_modes"): [vol.In(COLOR_MODES)],
        vol.Optional("brightness_floor"): vol.All(int, vol.Range(min=0, max=255)),
        vol.Optional("min_kelvin"): vol.All(int, vol.Range(min=1000, max=20000)),
        vol.Optional("max_kelvin"): vol.All(int, vol.Range(min=1000, max=20000)),
        vol.Optional("gamut"): GAMUT_SCHEMA,
        vol.Optional("subfloor"): vol.In([SUBFLOOR_CHANNEL_SCALE, SUBFLOOR_NONE]),
        vol.Optional("channel_gain"): vol.All(
            [vol.All(vol.Coerce(float), vol.Range(min=0.05, max=20))], vol.Length(min=3, max=5)
        ),
        vol.Optional("transition"): vol.Any(None, bool),
        vol.Optional("lumens"): vol.All(int, vol.Range(min=1, max=100000)),
        # Estimated relative light output by brightness (imported: from power readings).
        vol.Optional("output_curve"): _output_curve,
    }
)

IMPORT_SCHEMA = vol.Schema(
    {
        vol.Required("source"): vol.All(str, vol.Length(min=1)),
        vol.Required("source_url"): vol.Url(),
        vol.Required("licence"): vol.In(IMPORT_LICENCES),
        vol.Required("copyright"): vol.All(str, vol.Length(min=1)),
        vol.Required("fields"): vol.All([str], vol.Length(min=1)),
        vol.Required("quality"): vol.In(IMPORT_QUALITIES),
    }
)

DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required("manufacturer"): vol.All(str, vol.Length(min=1)),
        vol.Optional("manufacturer_aliases", default=list): [str],
        vol.Required("models"): vol.All([vol.All(str, vol.Length(min=1))], vol.Length(min=1)),
        vol.Optional("integrations", default=list): [vol.Match(r"^[a-z0-9_]+$")],
        vol.Optional("firmware"): vol.Schema({vol.Optional("min"): str, vol.Optional("max"): str}),
    }
)

PROVENANCE_SCHEMA = vol.Schema(
    {
        vol.Required("method"): vol.In(METHODS),
        vol.Optional("sources", default=list): [vol.Url()],
        vol.Required("verified"): bool,
        vol.Optional("notes"): str,
        vol.Optional("imports", default=list): [IMPORT_SCHEMA],
    }
)

CONTRIBUTOR_SCHEMA = vol.Schema(
    {
        vol.Required("nickname"): vol.All(str, vol.Match(_NICK_RE)),
        vol.Optional("github"): vol.All(str, vol.Match(_GITHUB_RE)),
        vol.Required("role"): vol.In(CONTRIBUTOR_ROLES),
        vol.Required("date"): vol.Match(r"^\d{4}-\d{2}-\d{2}$"),
    }
)

MODES_SCHEMA = vol.Schema(
    {
        vol.Optional("color"): vol.Any(None, vol.In(COMMAND_MODES)),
        vol.Optional("white"): vol.Any(None, vol.In(COMMAND_MODES)),
    }
)


def _provenance_rules(data: dict[str, Any]) -> dict[str, Any]:
    prov = data["provenance"]
    if prov["method"] == METHOD_SPEC and not prov["sources"]:
        raise vol.Invalid("a spec-based profile must cite its sources")
    if prov["method"] in (METHOD_SPEC, METHOD_IMPORTED) and data.get("cells"):
        raise vol.Invalid(f"a {prov['method']} profile has no calibrated cells")
    if prov["method"] == METHOD_IMPORTED:
        if not prov["imports"]:
            raise vol.Invalid("an imported profile must list its sources in provenance.imports")
        if prov["verified"]:
            raise vol.Invalid("an imported profile is unverified")
    elif prov["imports"]:
        raise vol.Invalid("provenance.imports is only for imported profiles")
    caps = data.get("capabilities") or {}
    if "min_kelvin" in caps and "max_kelvin" in caps and caps["min_kelvin"] > caps["max_kelvin"]:
        raise vol.Invalid("min_kelvin is above max_kelvin")
    if data.get("cells"):
        if data.get("target") != TARGET_REFERENCE:
            raise vol.Invalid(f"database cells must be in the reference space ({TARGET_REFERENCE})")
        modes = data.get("modes") or {}
        for key, cmd in data["cells"].items():
            try:
                kind = parse_cell_key(key)[0]
                validate_command(cmd, modes.get(kind) or "")
            except ValueError as err:
                raise vol.Invalid(f"cell {key}: {err}") from err
    return data


# A profile in the database (bundled or online). Exports from the panel use
# the same format with fewer required fields (see Profile.import_json).
DB_PROFILE_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Optional("$schema"): str,
            vol.Required("format"): PROFILE_FORMAT,
            vol.Required("version"): vol.All(int, vol.Range(min=2, max=PROFILE_VERSION)),
            vol.Required("revision"): vol.All(int, vol.Range(min=1)),
            vol.Required("name"): vol.All(str, vol.Length(min=1)),
            vol.Required("license"): DATA_LICENSE,
            vol.Optional("contributors", default=list): [CONTRIBUTOR_SCHEMA],
            vol.Optional("discussion"): vol.Url(),
            vol.Optional("target", default=TARGET_REFERENCE): vol.In([TARGET_REFERENCE]),
            vol.Optional("reference", default=False): bool,
            vol.Required("device"): DEVICE_SCHEMA,
            vol.Optional("capabilities", default=dict): CAPABILITIES_SCHEMA,
            vol.Required("provenance"): PROVENANCE_SCHEMA,
            vol.Optional("modes", default=dict): MODES_SCHEMA,
            vol.Optional("cells", default=dict): {str: dict},
        }
    ),
    _provenance_rules,
)


INDEX_FORMAT = "light_match_profile_index"
STATS_FIELDS = ("updated", "discussion", "rating", "downloads")
_SHA_RE = r"^[0-9a-f]{64}$"

INDEX_ENTRY_SCHEMA = vol.Schema(
    {
        vol.Required("id"): vol.Match(_ID_RE.pattern),
        vol.Required("path"): vol.Match(r"^[a-z0-9][a-z0-9_/.-]*\.json$"),
        vol.Required("asset"): vol.Match(r"^[a-z0-9_.-]+\.json$"),
        vol.Required("revision"): vol.All(int, vol.Range(min=1)),
        vol.Required("sha256"): vol.Match(_SHA_RE),
        vol.Required("name"): str,
        vol.Required("manufacturer"): str,
        vol.Required("models"): [str],
        vol.Required("method"): vol.In(METHODS),
        vol.Required("verified"): bool,
        vol.Required("contributors"): [str],
        vol.Optional("updated", default=None): vol.Any(None, vol.Match(r"^\d{4}-\d{2}-\d{2}$")),
        vol.Optional("discussion", default=None): vol.Any(None, vol.Url()),
        vol.Optional("rating", default=lambda: {"up": 0, "down": 0}): {
            vol.Required("up"): vol.All(int, vol.Range(min=0)),
            vol.Required("down"): vol.All(int, vol.Range(min=0)),
        },
        vol.Optional("downloads", default=0): vol.All(int, vol.Range(min=0)),
    },
    extra=vol.REMOVE_EXTRA,
)

INDEX_SCHEMA = vol.Schema(
    {
        vol.Required("format"): INDEX_FORMAT,
        vol.Required("version"): 1,
        vol.Optional("generated"): vol.Any(None, str),
        vol.Required("profiles"): [INDEX_ENTRY_SCHEMA],
    },
    extra=vol.REMOVE_EXTRA,
)


def asset_name(profile_id: str, revision: int) -> str:
    """The release asset of one revision of a profile (never replaced, so downloads add up)."""
    return f"{profile_id.replace('/', '__')}__r{revision}.json"


def validate_index(data: Any) -> dict[str, Any]:
    """Validate a profile index; return the cleaned copy or raise vol.Invalid."""
    return INDEX_SCHEMA(data)


def validate_db_profile(data: Any) -> dict[str, Any]:
    """Validate a database profile; return the cleaned copy or raise vol.Invalid."""
    return DB_PROFILE_SCHEMA(data)


def gamut_points(value: Any) -> tuple[tuple[float, float], ...] | None:
    """The corners of a gamut given by name or as an object."""
    if value is None:
        return None
    if isinstance(value, str):
        return NAMED_GAMUTS.get(value)
    return tuple(tuple(value[c]) for c in ("red", "green", "blue"))  # type: ignore[misc]


def norm(value: Any) -> str:
    """Normalise a manufacturer or model name for matching."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", value)[:4])


def firmware_in_range(firmware: str | None, rng: dict[str, str] | None) -> bool:
    """Whether a firmware version lies in a profile's range (unknown = yes)."""
    if not rng or not firmware or not re.search(r"\d", firmware):
        return True
    version = _version_tuple(firmware)
    if "min" in rng and version < _version_tuple(rng["min"]):
        return False
    return not ("max" in rng and version > _version_tuple(rng["max"]))


@dataclass(frozen=True)
class DeviceInfo:
    """How Home Assistant identifies a source lamp."""

    manufacturer: str | None = None
    model: str | None = None
    model_id: str | None = None
    integration: str | None = None
    firmware: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """The fields that are known."""
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass
class ModelProfile:
    """One validated database profile."""

    id: str
    data: dict[str, Any]
    origin: str = "bundled"  # bundled or online

    @property
    def name(self) -> str:
        """Human-readable name."""
        return self.data["name"]

    @property
    def revision(self) -> int:
        """Revision, raised by every change to the file."""
        return int(self.data["revision"])

    @property
    def capabilities(self) -> dict[str, Any]:
        """The adapter data."""
        return self.data.get("capabilities") or {}

    @property
    def cells(self) -> dict[str, dict[str, Any]]:
        """Calibrated cells in the reference space."""
        return self.data.get("cells") or {}

    @property
    def modes(self) -> dict[str, Any]:
        """Command modes of the cells."""
        return self.data.get("modes") or {}

    @property
    def generic(self) -> bool:
        """A profile for every model of a manufacturer."""
        return "*" in self.data["device"]["models"]

    def summary(self) -> dict[str, Any]:
        """Short description for the panel and the config flow."""
        prov = self.data["provenance"]
        return {
            "id": self.id,
            "name": self.name,
            "revision": self.revision,
            "origin": self.origin,
            "method": prov["method"],
            "verified": prov["verified"],
            "reference": bool(self.data.get("reference")),
            "sources": list(prov.get("sources") or []),
            "imports": [
                {k: i[k] for k in ("source", "source_url", "licence", "quality", "fields")}
                for i in prov.get("imports") or []
            ],
            "license": self.data["license"],
            "contributors": [
                {k: c[k] for k in ("nickname", "github", "role", "date") if k in c}
                for c in self.data.get("contributors") or []
            ],
            "discussion": self.data.get("discussion"),
            "cells": len(self.cells),
        }

    def match_score(self, device: DeviceInfo) -> int:
        """How well this profile fits a device; 0 for no match."""
        dev = self.data["device"]
        if device.integration and dev["integrations"] and device.integration not in dev["integrations"]:
            return 0
        if not firmware_in_range(device.firmware, dev.get("firmware")):
            return 0
        manufacturers = {norm(dev["manufacturer"]), *(norm(a) for a in dev["manufacturer_aliases"])}
        if device.manufacturer:
            if norm(device.manufacturer) not in manufacturers:
                return 0
        elif not (device.integration and device.integration in dev["integrations"]):
            return 0
        integration_bonus = 1 if device.integration and device.integration in dev["integrations"] else 0
        models = {norm(m) for m in dev["models"]}
        if {norm(device.model), norm(device.model_id)} & models - {""}:
            return 10 + integration_bonus
        if self.generic:
            return 1 + integration_bonus
        return 0


@dataclass
class ProfileDatabase:
    """Bundled profiles plus the ones fetched from the online database."""

    bundled: dict[str, ModelProfile] = field(default_factory=dict)
    online: dict[str, ModelProfile] = field(default_factory=dict)
    stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    loaded: bool = False

    def set_stats(self, index: dict[str, Any]) -> None:
        """Take ratings, downloads and discussion links from a validated index."""
        self.stats = {e["id"]: {k: e.get(k) for k in STATS_FIELDS} for e in index["profiles"]}

    def rating(self, profile_id: str) -> int:
        """Thumbs up minus thumbs down in the profile's discussion."""
        rating = (self.stats.get(profile_id) or {}).get("rating") or {}
        return int(rating.get("up", 0)) - int(rating.get("down", 0))

    def summary(self, prof: ModelProfile) -> dict[str, Any]:
        """A profile's summary with its community stats."""
        stats = self.stats.get(prof.id) or {}
        out = prof.summary()
        out["rating"] = stats.get("rating") or {"up": 0, "down": 0}
        out["downloads"] = stats.get("downloads") or 0
        out["updated"] = stats.get("updated")
        out["discussion"] = prof.data.get("discussion") or stats.get("discussion")
        out["rate_url"] = out["discussion"] or (
            f"{DISCUSSIONS_URL}?discussions_q={quote(prof.id)}"
        )
        return out

    def all(self) -> dict[str, ModelProfile]:
        """Every profile; an online one wins over a bundled one of a lower revision."""
        merged = dict(self.bundled)
        for pid, prof in self.online.items():
            if pid not in merged or prof.revision > merged[pid].revision:
                merged[pid] = prof
        return merged

    def get(self, profile_id: str | None) -> ModelProfile | None:
        """A profile by id."""
        return self.all().get(profile_id) if profile_id else None

    def rank(self, prof: ModelProfile) -> tuple[int, ...]:
        """Order among profiles that fit equally well.

        Verified first, then measured, then the community rating, then revision.
        """
        prov = prof.data["provenance"]
        return (int(prov["verified"]), METHOD_RANK[prov["method"]], self.rating(prof.id), prof.revision)

    def candidates(self, device: DeviceInfo) -> list[ModelProfile]:
        """Every profile that fits a device, best first.

        Measured or verified community profiles come first (even generic
        ones), then the closest match, then :meth:`rank`; so an imported
        profile beats a spec one for the same model, and a measured one beats
        both.
        """
        scored = [(prof.match_score(device), prof) for prof in self.all().values()]
        scored = [(score, prof) for score, prof in scored if score]
        scored.sort(key=lambda sp: (-trusted(sp[1]), -sp[0], tuple(-v for v in self.rank(sp[1])), sp[1].id))
        return [prof for _, prof in scored]

    def match(self, device: DeviceInfo) -> ModelProfile | None:
        """The best profile for a device, if any."""
        found = self.candidates(device)
        return found[0] if found else None

    def set_online(self, profiles: dict[str, dict[str, Any]]) -> None:
        """Replace the online layer with validated profiles."""
        self.online = {pid: ModelProfile(pid, data, "online") for pid, data in profiles.items()}


def trusted(prof: ModelProfile) -> bool:
    """A measured or verified profile (not a spec sheet or an import)."""
    prov = prof.data["provenance"]
    return bool(prov["verified"]) or prov["method"] in MEASURED_METHODS


# Capabilities taken together from one profile.
_CAP_GROUPS = (("min_kelvin", "max_kelvin"),)


def merge_capabilities(profiles: list[ModelProfile]) -> tuple[dict[str, Any], dict[str, ModelProfile]]:
    """Capabilities of several profiles, best first: each field from the first that has it.

    Returns the merged capabilities and, per field, the profile it came from.
    The kelvin range is taken as a pair.
    """
    caps: dict[str, Any] = {}
    origin: dict[str, ModelProfile] = {}
    grouped = {k: g for g in _CAP_GROUPS for k in g}
    for prof in profiles:
        own = prof.capabilities
        for key in own:
            if key in caps:
                continue
            group = grouped.get(key, (key,))
            if not all(k in own for k in group) or any(k in caps for k in group):
                continue
            for k in group:
                caps[k], origin[k] = own[k], prof
    return caps, origin


def profile_id_for(path: Path, root: Path = PROFILES_DIR) -> str:
    """The id of a profile file: '<manufacturer>/<model>'."""
    rel = path.relative_to(root).with_suffix("")
    return rel.as_posix()


def load_bundled(root: Path = PROFILES_DIR) -> dict[str, ModelProfile]:
    """Read and validate the bundled profiles (blocking: run in an executor)."""
    profiles: dict[str, ModelProfile] = {}
    for path in sorted(root.glob("*/*.json")):
        pid = profile_id_for(path, root)
        if not _ID_RE.match(pid):
            _LOGGER.warning("Skipping profile with a bad file name: %s", pid)
            continue
        try:
            data = validate_db_profile(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, vol.Invalid) as err:
            _LOGGER.warning("Skipping invalid profile %s: %s", pid, err)
            continue
        profiles[pid] = ModelProfile(pid, data)
    return profiles


def load_bundled_index(path: Path = INDEX_FILE) -> dict[str, Any] | None:
    """The bundled index with its stats snapshot (blocking)."""
    try:
        return validate_index(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, vol.Invalid) as err:
        _LOGGER.warning("Bundled profile index not usable: %s", err)
        return None


def valid_profile_id(value: str) -> bool:
    """Whether a string is a well-formed profile id."""
    return bool(_ID_RE.match(value))
