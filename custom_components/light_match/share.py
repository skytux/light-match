"""Portable profile export and the prefilled GitHub submission link.

The shared file uses the database format (see ``profiles/profile.schema.json``).
It names the lamp only as Home Assistant's device registry does (manufacturer,
model, model id, integration); never entity ids, names, areas or addresses.
Contributors are credited by the nickname they choose.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlencode

from .database import (
    CONTRIBUTOR_SCHEMA,
    DATA_LICENSE,
    METHOD_BY_EYE,
    TARGET_HOUSE,
    TARGET_REFERENCE,
    DeviceInfo,
    norm,
)

ISSUE_TEMPLATE = "new-lamp-profile.yml"
MAX_URL = 7500


def repo_url(manifest: dict[str, Any]) -> str:
    """The GitHub repository from the manifest."""
    url = str(manifest.get("issue_tracker") or manifest.get("documentation") or "")
    if url.endswith("/issues"):
        url = url[: -len("/issues")]
    return url.rstrip("/")


def suggested_path(device: DeviceInfo) -> str:
    """Where the profile would go in the database."""
    maker = norm(device.manufacturer) or "unknown"
    for long, short in (("signifynetherlandsbv", "signify"), ("philips", "signify")):
        maker = short if maker.startswith(long) else maker
    model = norm(device.model_id or device.model) or "model"
    return f"{maker}/{model}.json"


def portable_profile(
    exported: dict[str, Any],
    device: DeviceInfo,
    capabilities: dict[str, Any],
    target: str,
    *,
    nickname: str | None = None,
    github: str | None = None,
    notes: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """The database form of a twin's own cells.

    Raises ``vol.Invalid`` (a ValueError) if the nickname or GitHub handle is
    not acceptable.
    """
    contributors = []
    if nickname:
        entry = {"nickname": nickname.strip(), "role": "calibrated",
                 "date": (today or date.today()).isoformat()}
        if github:
            entry["github"] = github.strip().lstrip("@")
        contributors.append(CONTRIBUTOR_SCHEMA(entry))
    models = [m for m in dict.fromkeys((device.model, device.model_id)) if m]
    name = " ".join(v for v in (device.manufacturer, device.model or device.model_id) if v) or "Unknown lamp"
    dev: dict[str, Any] = {"manufacturer": device.manufacturer or "unknown", "models": models or ["*"]}
    if device.integration:
        dev["integrations"] = [device.integration]
    provenance: dict[str, Any] = {"method": METHOD_BY_EYE, "verified": False}
    note_parts = [n for n in (notes or "").strip().splitlines() if n.strip()]
    if device.firmware:
        note_parts.append(f"Calibrated on firmware {device.firmware}.")
    if note_parts:
        provenance["notes"] = " ".join(note_parts)
    profile: dict[str, Any] = {
        "$schema": "../profile.schema.json",
        "format": exported["format"],
        "version": exported["version"],
        "revision": 1,
        "name": name,
        "license": DATA_LICENSE,
        "contributors": contributors,
        "target": target,
        "device": dev,
        "capabilities": {k: v for k, v in capabilities.items() if v not in (None, [], "")},
        "provenance": provenance,
        "modes": exported["modes"],
        "cells": exported["cells"],
    }
    return profile


def submission_url(repo: str, profile: dict[str, Any], path: str) -> tuple[str, bool]:
    """A prefilled 'new lamp profile' issue link; whether the JSON fits in it."""
    import json  # noqa: PLC0415

    dev = profile["device"]
    nick = profile["contributors"][0]["nickname"] if profile["contributors"] else ""
    params = {
        "template": ISSUE_TEMPLATE,
        "title": f"Lamp profile: {profile['name']}",
        "labels": "lamp-profile",
        "manufacturer": dev["manufacturer"],
        "models": ", ".join(dev["models"]),
        "integration": ", ".join(dev.get("integrations", [])),
        "method": profile["provenance"]["method"],
        "target": profile["target"],
        "path": path,
        "nickname": nick,
    }
    with_json = {**params, "profile": "```json\n" + json.dumps(profile, indent=1) + "\n```"}
    url = f"{repo}/issues/new?{urlencode(with_json)}"
    if len(url) <= MAX_URL:
        return url, True
    return f"{repo}/issues/new?{urlencode({**params, 'profile': 'Attached as a file.'})}", False


__all__ = [
    "TARGET_HOUSE",
    "TARGET_REFERENCE",
    "portable_profile",
    "repo_url",
    "submission_url",
    "suggested_path",
]
