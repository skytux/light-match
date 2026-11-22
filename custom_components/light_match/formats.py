"""Profile file format versions, migrations and calibration details.

``format_version`` (an integer) says which layout a profile file has; files
without it are format 1. ``version`` stays at 2 so Light Match releases from
before ``format_version`` still read the files. Every format change adds a
step to :data:`MIGRATIONS`, so old files are converted when they are imported
or loaded from the database. A file of a newer format than this code knows is
shown but never applied (:class:`NewerFormatError`).

Format 2 adds ``app_version`` (the Light Match release that wrote the file),
``calibration`` (how it was calibrated) and ``notes`` (free text).

Format 3 adds the rough camera calibration: method ``camera`` and the
``camera_locked``, ``camera_reference`` and ``camera_corrected`` details. A
file is written with the lowest format that holds it (:func:`written_format`),
so a profile calibrated by eye stays format 2 and older releases still use it.
"""

from __future__ import annotations

from collections.abc import Callable
import copy
import json
from pathlib import Path
from typing import Any

import voluptuous as vol

FORMAT_VERSION = 3
BASE_FORMAT = 2  # the format of files without camera details
NOTES_MAX = 500
DEVICE_MAX = 80

CAL_SCREEN = "screen_conditions_confirmed"
CAL_MATCHED = "screen_matched"  # the screen brightness was matched to the lamp at 100 %
CAL_DEVICE = "reference_device"
CAL_INSTRUMENT = "instrument"
CAL_METHOD = "method"
CAL_CAMERA_LOCKED = "camera_locked"  # {"wb": bool, "exposure": bool}: what the browser could lock
CAL_CAMERA_REFERENCE = "camera_reference"  # "lamp" (a reference lamp in the frame) or "absolute"
CAL_CAMERA_CORRECTED = "camera_corrected"  # a camera -> sRGB matrix from screen patches was used
CAMERA_REFERENCES = ["lamp", "absolute"]
METHOD_CAMERA = "camera"  # rough: a phone camera, white balance and exposure locked where possible
CAL_METHODS = ["by_eye", "lux_meter", "colorimeter", METHOD_CAMERA]
CAMERA_KEYS = (CAL_CAMERA_LOCKED, CAL_CAMERA_REFERENCE, CAL_CAMERA_CORRECTED)

try:
    APP_VERSION: str = json.loads(Path(__file__).with_name("manifest.json").read_text(encoding="utf-8"))["version"]
except (OSError, ValueError, KeyError):  # pragma: no cover - the manifest ships with the code
    APP_VERSION = "unknown"


def _short(limit: int) -> Callable[[Any], str]:
    def clean(value: Any) -> str:
        if not isinstance(value, str):
            raise vol.Invalid("expected text")
        text = value.strip()
        if len(text) > limit:
            raise vol.Invalid(f"at most {limit} characters")
        return text

    return clean


NOTES = vol.All(str, _short(NOTES_MAX))

CALIBRATION_SCHEMA = vol.Schema(
    {
        vol.Optional(CAL_METHOD): vol.In(["spec", "imported", *CAL_METHODS]),
        vol.Optional(CAL_SCREEN): bool,
        vol.Optional(CAL_MATCHED): bool,
        vol.Optional(CAL_DEVICE): vol.All(str, _short(DEVICE_MAX)),
        vol.Optional(CAL_INSTRUMENT): vol.All(str, _short(DEVICE_MAX)),
        vol.Optional(CAL_CAMERA_LOCKED): {vol.Required("wb"): bool, vol.Required("exposure"): bool},
        vol.Optional(CAL_CAMERA_REFERENCE): vol.In(CAMERA_REFERENCES),
        vol.Optional(CAL_CAMERA_CORRECTED): bool,
    }
)


CALIBRATION_KEYS = (CAL_METHOD, CAL_SCREEN, CAL_MATCHED, CAL_DEVICE, CAL_INSTRUMENT, *CAMERA_KEYS)


class NewerFormatError(ValueError):
    """The profile was written by a newer Light Match than this one."""

    def __init__(self, version: int) -> None:
        super().__init__(
            f"this profile has format {version}, newer than this Light Match reads "
            f"({FORMAT_VERSION}); update Light Match to use this profile"
        )
        self.version = version


def format_version(data: Any) -> int:
    """The format of a profile file (1 when it does not say)."""
    if not isinstance(data, dict):
        raise ValueError("not a Light Match profile")
    value = data.get("format_version", 1)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("bad format_version")
    return value


def is_newer(data: Any) -> bool:
    """Whether a profile is of a format this code does not know (read-only)."""
    try:
        return format_version(data) > FORMAT_VERSION
    except ValueError:
        return False


def _v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Format 1 had the method only in the database's provenance."""
    prov = data.get("provenance") if isinstance(data.get("provenance"), dict) else {}
    calibration = dict(data.get("calibration") or {}) if isinstance(data.get("calibration"), dict) else {}
    calibration.setdefault(CAL_METHOD, prov.get("method") or "by_eye")
    calibration.setdefault(CAL_SCREEN, False)
    data["calibration"] = calibration
    return data


def _v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
    """Format 3 only adds optional camera details."""
    return data


# MIGRATIONS[n] turns format n into format n + 1.
MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {1: _v1_to_v2, 2: _v2_to_v3}


def uses_camera(data: dict[str, Any]) -> bool:
    """Whether a profile holds format 3 camera details."""
    cal = data.get("calibration")
    prov = data.get("provenance")
    return bool(
        (isinstance(cal, dict) and (cal.get(CAL_METHOD) == METHOD_CAMERA or any(k in cal for k in CAMERA_KEYS)))
        or (isinstance(prov, dict) and prov.get("method") == METHOD_CAMERA)
    )


def written_format(data: dict[str, Any]) -> int:
    """The lowest format that holds a (current) profile."""
    return FORMAT_VERSION if uses_camera(data) else BASE_FORMAT


def migrate(data: Any) -> dict[str, Any]:
    """A copy of a profile in the current format.

    Raises :class:`NewerFormatError` for a newer format, ValueError for junk.
    """
    version = format_version(data)
    if version > FORMAT_VERSION:
        raise NewerFormatError(version)
    out = copy.deepcopy(data)
    while version < FORMAT_VERSION:
        out = MIGRATIONS[version](out)
        version += 1
    out["format_version"] = written_format(out)
    return out


def clean_calibration(value: Any) -> dict[str, Any]:
    """Validated calibration details (empty texts dropped); ValueError if bad."""
    try:
        cal = CALIBRATION_SCHEMA(value or {})
    except vol.Invalid as err:
        raise ValueError(f"calibration: {err}") from err
    return {k: v for k, v in cal.items() if v != ""}


def clean_notes(value: Any) -> str | None:
    """Validated notes (None when empty); ValueError if too long."""
    if value is None:
        return None
    try:
        text = NOTES(value)
    except vol.Invalid as err:
        raise ValueError(f"notes: {err}") from err
    return text or None


def good_practice(data: dict[str, Any]) -> bool:
    """Calibrated under the recommended screen conditions (as the calibrator says)."""
    cal = data.get("calibration")
    return bool(isinstance(cal, dict) and cal.get(CAL_SCREEN) is True)
