"""Community stats in the index, rating in the ranking, and the optional online update."""

from __future__ import annotations

import copy
import hashlib
import json

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest
import voluptuous as vol

from custom_components.light_match import async_get_database, online_updater
from custom_components.light_match.const import DOMAIN
from custom_components.light_match.database import (
    INDEX_FILE,
    PROFILES_DIR,
    DeviceInfo,
    ModelProfile,
    ProfileDatabase,
    asset_name,
    load_bundled_index,
    validate_db_profile,
    validate_index,
)
from custom_components.light_match.online import ASSET_URL, INDEX_URL

from .conftest import add_twin

HUE_C = "signify/hue-gamut-c"


def _entry(pid: str, raw: bytes, revision: int, **stats) -> dict:
    data = json.loads(raw)
    return {
        "id": pid,
        "path": f"{pid}.json",
        "asset": asset_name(pid, revision),
        "revision": revision,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "name": data["name"],
        "manufacturer": data["device"]["manufacturer"],
        "models": data["device"]["models"],
        "method": data["provenance"]["method"],
        "verified": data["provenance"]["verified"],
        "contributors": [],
        **stats,
    }


def _hue_c(revision: int) -> bytes:
    data = json.loads((PROFILES_DIR / f"{HUE_C}.json").read_text())
    data["revision"] = revision
    data["contributors"] = [{"nickname": "lampfan", "role": "verified", "date": "2026-11-20"}]
    return json.dumps(data).encode()


def test_bundled_index_has_stats_defaults() -> None:
    index = load_bundled_index()
    assert index is not None
    entry = next(e for e in index["profiles"] if e["id"] == HUE_C)
    assert entry["rating"] == {"up": 0, "down": 0}
    assert entry["downloads"] == 0
    assert entry["asset"] == "signify__hue-gamut-c__r1.json"
    assert entry["contributors"] == []
    assert json.loads(INDEX_FILE.read_text())["format"] == "light_match_profile_index"


@pytest.mark.parametrize(
    "change",
    [
        {"sha256": "abc"},
        {"rating": {"up": -1, "down": 0}},
        {"asset": "../evil.json"},
        {"method": "guess"},
        {"discussion": "not a url"},
    ],
)
def test_index_rejects_bad_entries(change: dict) -> None:
    raw = _hue_c(2)
    index = {"format": "light_match_profile_index", "version": 1, "profiles": [{**_entry(HUE_C, raw, 2), **change}]}
    with pytest.raises(vol.Invalid):
        validate_index(index)


def _profile(pid: str, *, verified: bool = False, method: str = "by_eye") -> ModelProfile:
    data = validate_db_profile(
        {
            "format": "light_match_profile",
            "version": 2,
            "revision": 1,
            "name": pid,
            "license": "CC-BY-4.0",
            "device": {"manufacturer": "Acme", "models": ["X1"]},
            "provenance": {"method": method, "verified": verified, "sources": ["https://example.com/x1"]},
        }
    )
    return ModelProfile(pid, data)


def _stats(pid: str, up: int, down: int) -> dict:
    return {
        "id": pid, "path": f"{pid}.json", "asset": asset_name(pid, 1), "revision": 1,
        "sha256": "0" * 64, "name": pid, "manufacturer": "Acme", "models": ["X1"],
        "method": "by_eye", "verified": False, "contributors": [],
        "rating": {"up": up, "down": down}, "downloads": 7,
    }


def test_rating_breaks_ties_after_verified_and_method() -> None:
    db = ProfileDatabase()
    db.bundled = {
        "acme/a": _profile("acme/a"),
        "acme/b": _profile("acme/b"),
        "acme/c": _profile("acme/c", method="spec"),
        "acme/d": _profile("acme/d", verified=True, method="spec"),
    }
    db.set_stats(validate_index({
        "format": "light_match_profile_index", "version": 1,
        "profiles": [_stats("acme/a", 1, 0), _stats("acme/b", 5, 1), _stats("acme/c", 99, 0), _stats("acme/d", 0, 9)],
    }))
    device = DeviceInfo(manufacturer="Acme", model="X1")
    assert [p.id for p in db.candidates(device)] == ["acme/d", "acme/b", "acme/a", "acme/c"]
    summary = db.summary(db.bundled["acme/b"])
    assert summary["rating"] == {"up": 5, "down": 1} and summary["downloads"] == 7
    assert summary["rate_url"].endswith("discussions_q=acme/b")


async def _setup(hass: HomeAssistant) -> None:
    await add_twin(hass, "light.bulb_hs")


async def test_online_update_off_by_default(hass: HomeAssistant, sources, aioclient_mock) -> None:
    await _setup(hass)
    updater = online_updater(hass)
    assert updater.enabled is False
    assert await updater.async_update() == {"enabled": False}
    assert aioclient_mock.call_count == 0
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "update_profiles", {}, blocking=True, return_response=True)


async def test_online_update_downloads_newer_revision(
    hass: HomeAssistant, sources, aioclient_mock, hass_storage, hass_ws_client
) -> None:
    entry = await add_twin(hass, "light.bulb_rgbw")
    await entry.runtime_data.async_set_cell("color:red:1", {"brightness": 26, "rgbw_color": [12, 0, 0, 0]})
    newer = _hue_c(2)
    gamut_a = (PROFILES_DIR / "signify/hue-gamut-a.json").read_bytes()
    index = {
        "format": "light_match_profile_index",
        "version": 1,
        "profiles": [
            _entry(HUE_C, newer, 2, rating={"up": 3, "down": 0}, downloads=12),
            _entry("signify/hue-gamut-a", gamut_a, 1),  # same as bundled: never downloaded
        ],
    }
    aioclient_mock.get(INDEX_URL, text=json.dumps(index), headers={"ETag": '"v1"'})
    aioclient_mock.get(ASSET_URL.format(asset=asset_name(HUE_C, 2)), content=newer)

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/settings/set", "online_update": True})
    assert (await client.receive_json())["result"]["online_update"] is True
    await client.send_json_auto_id({"type": f"{DOMAIN}/profiles/update"})
    res = (await client.receive_json())["result"]
    assert res["result"]["downloaded"] == [HUE_C]
    assert res["online_profiles"] == [HUE_C]
    assert aioclient_mock.call_count == 2

    db = await async_get_database(hass)
    assert db.get(HUE_C).revision == 2 and db.get(HUE_C).origin == "online"
    assert db.summary(db.get(HUE_C))["rating"] == {"up": 3, "down": 0}
    assert hass_storage["light_match.settings"]["data"] == {"online_update": True}
    assert HUE_C in hass_storage["light_match.profiles_cache"]["data"]["profiles"]
    # the user's cells are never touched
    assert entry.runtime_data.cells == {"color:red:1": {"brightness": 26, "rgbw_color": [12, 0, 0, 0]}}

    # unchanged index: one 304, no asset download, cached profile kept
    aioclient_mock.clear_requests()
    aioclient_mock.get(INDEX_URL, status=304)
    result = await online_updater(hass).async_update()
    assert result["index"] == "unchanged" and result["kept"] == 1 and result["downloaded"] == []
    assert aioclient_mock.call_count == 1
    assert aioclient_mock.mock_calls[0][3] == {"If-None-Match": '"v1"'}

    # off again: the online layer is dropped
    await client.send_json_auto_id({"type": f"{DOMAIN}/settings/set", "online_update": False})
    assert (await client.receive_json())["result"]["online_profiles"] == []
    assert db.get(HUE_C).revision == 1
    online_updater(hass).async_unload()


async def test_online_update_rejects_checksum_mismatch(hass: HomeAssistant, sources, aioclient_mock) -> None:
    await _setup(hass)
    newer = _hue_c(2)
    tampered = copy.deepcopy(json.loads(newer))
    tampered["capabilities"]["brightness_floor"] = 200
    index = {"format": "light_match_profile_index", "version": 1, "profiles": [_entry(HUE_C, newer, 2)]}
    aioclient_mock.get(INDEX_URL, text=json.dumps(index))
    aioclient_mock.get(ASSET_URL.format(asset=asset_name(HUE_C, 2)), text=json.dumps(tampered))
    updater = online_updater(hass)
    await updater.async_set_enabled(True)
    result = await updater.async_update()
    assert result["rejected"] == [{"id": HUE_C, "reason": "checksum mismatch"}]
    assert (await async_get_database(hass)).get(HUE_C).revision == 1
    updater.async_unload()


async def test_online_update_rejects_bad_index(hass: HomeAssistant, sources, aioclient_mock) -> None:
    await _setup(hass)
    aioclient_mock.get(INDEX_URL, text=json.dumps({"format": "something else"}))
    updater = online_updater(hass)
    await updater.async_set_enabled(True)
    result = await updater.async_update()
    assert result["error"].startswith("index rejected")
    assert aioclient_mock.call_count == 1
    updater.async_unload()


async def test_online_cache_applies_at_start(hass: HomeAssistant, sources, hass_storage) -> None:
    newer = _hue_c(2)
    hass_storage["light_match.settings"] = {"version": 1, "key": "light_match.settings", "data": {"online_update": True}}
    hass_storage["light_match.profiles_cache"] = {
        "version": 1,
        "key": "light_match.profiles_cache",
        "data": {"etag": '"v1"', "index": None, "profiles": {HUE_C: json.loads(newer)}, "hashes": {}, "checked": None},
    }
    await _setup(hass)
    assert (await async_get_database(hass)).get(HUE_C).revision == 2
    online_updater(hass).async_unload()


def test_stats_script_aggregates_github_data(tmp_path) -> None:
    import sys

    from custom_components.light_match.database import STATS_FIELDS

    sys.path.insert(0, str(PROFILES_DIR.parents[2] / "scripts"))
    import profile_stats  # noqa: PLC0415
    import profiles  # noqa: PLC0415

    downloads = profile_stats.asset_downloads(
        [
            {"name": "signify__hue-gamut-c__r1.json", "download_count": 4},
            {"name": "signify__hue-gamut-c__r2.json", "download_count": 6},
            {"name": "wiz__generic__r1.json", "download_count": 1},
            {"name": "notes.txt", "download_count": 99},
        ]
    )
    assert downloads == {HUE_C: 10, "wiz/generic": 1}
    discussions = profile_stats.discussion_stats(
        [
            {
                "title": HUE_C,
                "url": "https://github.com/skytux/ha-light-match/discussions/7",
                "reactionGroups": [
                    {"content": "THUMBS_UP", "reactors": {"totalCount": 5}},
                    {"content": "THUMBS_DOWN", "reactors": {"totalCount": 1}},
                    {"content": "HEART", "reactors": {"totalCount": 2}},
                ],
            }
        ]
    )
    stats = profile_stats.merge_stats([HUE_C, "wiz/generic"], discussions, downloads, {HUE_C: "2026-11-20"})
    assert stats[HUE_C] == {
        "updated": "2026-11-20",
        "discussion": "https://github.com/skytux/ha-light-match/discussions/7",
        "rating": {"up": 5, "down": 1},
        "downloads": 10,
    }
    index = validate_index(profiles.build_index(stats))
    entry = next(e for e in index["profiles"] if e["id"] == HUE_C)
    assert {k: entry[k] for k in STATS_FIELDS} == stats[HUE_C]
    # stats never make the structural check fail
    assert profiles._structure(index) == profiles._structure(profiles.build_index({}))

    names = profile_stats.copy_assets(tmp_path)
    assert "signify__hue-gamut-c__r1.json" in names
    assert (tmp_path / "wiz__generic__r1.json").read_bytes() == (PROFILES_DIR / "wiz/generic.json").read_bytes()
