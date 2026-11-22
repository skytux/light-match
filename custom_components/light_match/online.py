"""Optional online update of the profile database.

Off by default. When the user turns it on, Light Match fetches the profile
index from the project's GitHub repository (with an ETag, so an unchanged
index costs one 304), and downloads only the profiles whose revision is newer
than the bundled one and whose checksum changed, from the release assets of
the "profiles" release. Each file is checked against the index's sha256 and
validated before it is used. Downloaded profiles are cached and only ever
fill the model layer: the user's own cells are never touched.

Nothing else is sent: no device data, no ids, no counting. The only trace a
user leaves is GitHub's own download counter on the release assets, which is
what the community stats show.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
import hashlib
import json
import logging
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import DOMAIN
from .database import ProfileDatabase, accept_db_profile, load_bundled_index, validate_index

_LOGGER = logging.getLogger(__name__)

REPO = "skytux/ha-light-match"
INDEX_URL = f"https://raw.githubusercontent.com/{REPO}/main/custom_components/light_match/profiles/index.json"
ASSET_URL = f"https://github.com/{REPO}/releases/download/profiles/{{asset}}"
UPDATE_INTERVAL = timedelta(hours=24)
TIMEOUT = aiohttp.ClientTimeout(total=30)
MAX_PROFILE_BYTES = 256 * 1024

SETTINGS_KEY = f"{DOMAIN}.settings"
CACHE_KEY = f"{DOMAIN}.profiles_cache"
DEFAULT_SETTINGS = {"online_update": False}


class OnlineUpdater:
    """Settings, cache and fetching of the online profile layer."""

    def __init__(self, hass: HomeAssistant, db: ProfileDatabase) -> None:
        self.hass = hass
        self.db = db
        self.settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.cache: dict[str, Any] = {"etag": None, "index": None, "profiles": {}, "hashes": {}, "checked": None}
        self._settings_store: Store = Store(hass, 1, SETTINGS_KEY)
        self._cache_store: Store = Store(hass, 1, CACHE_KEY)
        self._unsub = None
        self.last_result: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        """Whether the user turned the online update on."""
        return bool(self.settings.get("online_update"))

    async def async_load(self) -> None:
        """Read settings and the bundled stats; apply the cache when enabled."""
        self.settings = {**DEFAULT_SETTINGS, **(await self._settings_store.async_load() or {})}
        bundled = await self.hass.async_add_executor_job(load_bundled_index)
        if bundled:
            self.db.set_stats(bundled)
        if self.enabled:
            self.cache = {**self.cache, **(await self._cache_store.async_load() or {})}
            self._apply_cache()
            self._schedule()

    def _apply_cache(self) -> None:
        profiles: dict[str, dict[str, Any]] = {}
        read_only: set[str] = set()
        for pid, data in (self.cache.get("profiles") or {}).items():
            try:
                profiles[pid], newer = accept_db_profile(data)
            except vol.Invalid as err:
                _LOGGER.warning("Dropping cached profile %s: %s", pid, err)
                continue
            if newer:
                read_only.add(pid)
        self.db.set_online(profiles, read_only)
        if index := self.cache.get("index"):
            with contextlib.suppress(vol.Invalid):
                self.db.set_stats(validate_index(index))

    def _schedule(self) -> None:
        if self._unsub is None:
            self._unsub = async_track_time_interval(
                self.hass, self._interval, UPDATE_INTERVAL, name="Light Match profile update"
            )

    async def _interval(self, _now: Any) -> None:
        await self.async_update()

    def async_unload(self) -> None:
        """Stop the periodic update."""
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def async_set_enabled(self, enabled: bool) -> None:
        """Turn the online update on or off; off drops the online layer."""
        self.settings["online_update"] = bool(enabled)
        await self._settings_store.async_save(self.settings)
        if enabled:
            self._schedule()
            return
        self.async_unload()
        self.db.set_online({})
        self.cache = {"etag": None, "index": None, "profiles": {}, "hashes": {}, "checked": None}
        await self._cache_store.async_remove()
        bundled = await self.hass.async_add_executor_job(load_bundled_index)
        self.db.set_stats(bundled or {"profiles": []})

    async def _get(self, session: aiohttp.ClientSession, url: str, etag: str | None = None) -> tuple[int, bytes, str | None]:
        headers = {"If-None-Match": etag} if etag else {}
        async with session.get(url, headers=headers, timeout=TIMEOUT) as resp:
            if resp.status == 304:
                return 304, b"", etag
            if resp.status != 200:
                return resp.status, b"", None
            body = await resp.content.read(MAX_PROFILE_BYTES * 64 + 1)
            return 200, body, resp.headers.get("ETag")

    async def async_update(self, *, force: bool = False) -> dict[str, Any]:
        """Fetch the index and changed profiles; return what happened."""
        if not (self.enabled or force):
            return {"enabled": False}
        session = async_get_clientsession(self.hass)
        result: dict[str, Any] = {"enabled": True, "index": "unchanged", "downloaded": [], "rejected": [], "kept": 0}
        try:
            status, body, etag = await self._get(session, INDEX_URL, self.cache.get("etag"))
        except (aiohttp.ClientError, TimeoutError) as err:
            result["error"] = f"index not fetched: {err}"
            self.last_result = result
            return result
        if status == 200:
            try:
                index = validate_index(json.loads(body))
            except (ValueError, vol.Invalid) as err:
                result["error"] = f"index rejected: {err}"
                self.last_result = result
                return result
            self.cache["index"] = index
            self.cache["etag"] = etag
            result["index"] = "updated"
        elif status != 304 or not self.cache.get("index"):
            result["error"] = f"index not fetched: HTTP {status}"
            self.last_result = result
            return result
        index = validate_index(self.cache["index"])

        cached: dict[str, dict[str, Any]] = self.cache.get("profiles") or {}
        hashes: dict[str, str] = self.cache.get("hashes") or {}
        fresh: dict[str, dict[str, Any]] = {}
        fresh_hashes: dict[str, str] = {}
        for entry in index["profiles"]:
            pid = entry["id"]
            bundled = self.db.bundled.get(pid)
            if bundled and bundled.revision >= entry["revision"]:
                continue
            if pid in cached and hashes.get(pid) == entry["sha256"]:
                fresh[pid], fresh_hashes[pid] = cached[pid], entry["sha256"]
                result["kept"] += 1
                continue
            try:
                status, body, _ = await self._get(session, ASSET_URL.format(asset=entry["asset"]))
            except (aiohttp.ClientError, TimeoutError) as err:
                status, body = 0, b""
                _LOGGER.debug("Profile %s not fetched: %s", pid, err)
            reason = None
            if status != 200:
                reason = f"HTTP {status}"
            elif len(body) > MAX_PROFILE_BYTES:
                reason = "too large"
            elif hashlib.sha256(body).hexdigest() != entry["sha256"]:
                reason = "checksum mismatch"
            else:
                try:
                    data, _newer = accept_db_profile(json.loads(body))
                except (ValueError, vol.Invalid) as err:
                    reason = f"invalid: {err}"
                else:
                    if data["revision"] != entry["revision"]:
                        reason = "revision differs from the index"
            if reason:
                result["rejected"].append({"id": pid, "reason": reason})
                if pid in cached:  # keep the last good copy
                    fresh[pid], fresh_hashes[pid] = cached[pid], hashes.get(pid, "")
                continue
            fresh[pid], fresh_hashes[pid] = json.loads(body), entry["sha256"]
            result["downloaded"].append(pid)

        self.cache["profiles"] = fresh
        self.cache["hashes"] = fresh_hashes
        self.cache["checked"] = dt_util.utcnow().isoformat()
        await self._cache_store.async_save(self.cache)
        self._apply_cache()
        result["checked"] = self.cache["checked"]
        self.last_result = result
        return result

    def status(self) -> dict[str, Any]:
        """Settings and the last check, for the panel."""
        return {
            "online_update": self.enabled,
            "checked": self.cache.get("checked"),
            "online_profiles": sorted(self.db.online),
            "last_result": self.last_result,
            "index_url": INDEX_URL,
        }
