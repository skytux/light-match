"""Format versions, migrations, read-only newer files, calibration details and GitHub rating."""

from __future__ import annotations

import json
from unittest.mock import patch

from homeassistant.core import HomeAssistant
import pytest
import voluptuous as vol

from custom_components.light_match.const import DOMAIN
from custom_components.light_match.database import (
    ModelProfile,
    ProfileDatabase,
    accept_db_profile,
    validate_db_profile,
)
from custom_components.light_match.formats import (
    APP_VERSION,
    FORMAT_VERSION,
    NOTES_MAX,
    NewerFormatError,
    clean_calibration,
    clean_notes,
    format_version,
    good_practice,
    migrate,
)
from custom_components.light_match.github import DEVICE_CODE_URL, GRAPHQL_URL, TOKEN_URL, GitHubRating
from custom_components.light_match.library import Library, browse

from .conftest import add_twin

V1 = {
    "format": "light_match_profile",
    "version": 2,
    "modes": {"color": "rgbw", "white": "color_temp"},
    "floor": 0,
    "cells": {"color:red:5": {"brightness": 26, "rgbw_color": [255, 0, 0, 0]}},
}
NEWER = {**V1, "format_version": FORMAT_VERSION + 1, "name": "Future", "something_new": {"x": 1}}


def _db_data(**extra) -> dict:
    return {
        "format": "light_match_profile",
        "version": 2,
        "revision": 1,
        "name": "p",
        "license": "CC-BY-4.0",
        "device": {"manufacturer": "Acme", "models": ["X1"]},
        "provenance": {"method": "by_eye", "verified": False, "sources": ["https://example.com/x1"]},
        **extra,
    }


def test_migrate_v1_to_current() -> None:
    assert format_version(V1) == 1
    out = migrate(V1)
    assert out["format_version"] == FORMAT_VERSION == 2
    assert out["calibration"] == {"method": "by_eye", "screen_conditions_confirmed": False}
    assert "format_version" not in V1  # a copy
    # the method of a database profile comes from its provenance
    assert migrate({**V1, "provenance": {"method": "lux_meter"}})["calibration"]["method"] == "lux_meter"
    # already current: unchanged apart from the copy
    assert migrate(out) == out
    with pytest.raises(NewerFormatError, match="update Light Match"):
        migrate(NEWER)
    with pytest.raises(ValueError):
        format_version({"format_version": "2"})
    with pytest.raises(ValueError):
        format_version({"format_version": True})


def test_calibration_and_notes_validation() -> None:
    cal = clean_calibration(
        {"method": "colorimeter", "screen_conditions_confirmed": True, "screen_matched": True,
         "reference_device": " Pixel 8 ", "instrument": ""}
    )
    assert cal == {"method": "colorimeter", "screen_conditions_confirmed": True, "screen_matched": True,
                   "reference_device": "Pixel 8"}
    for bad in ({"method": "guess"}, {"reference_device": "x" * 81}, {"unknown": 1}, {"screen_matched": "yes"}):
        with pytest.raises(ValueError):
            clean_calibration(bad)
    assert clean_notes(None) is None and clean_notes("  ") is None
    assert clean_notes("x" * NOTES_MAX) == "x" * NOTES_MAX
    with pytest.raises(ValueError):
        clean_notes("x" * (NOTES_MAX + 1))
    assert good_practice({"calibration": {"screen_conditions_confirmed": True}})
    assert not good_practice({"calibration": {"screen_conditions_confirmed": "yes"}})


def test_db_schema_and_read_only() -> None:
    data = validate_db_profile(_db_data(notes="Diffuser on", calibration={"screen_conditions_confirmed": True}))
    assert data["format_version"] == FORMAT_VERSION and data["notes"] == "Diffuser on"
    with pytest.raises(vol.Invalid):
        validate_db_profile(_db_data(notes="x" * 501))
    with pytest.raises(vol.Invalid):
        validate_db_profile(_db_data(format_version=FORMAT_VERSION + 1))
    newer, read_only = accept_db_profile(_db_data(format_version=FORMAT_VERSION + 1, future_field=[1]))
    assert read_only and newer["future_field"] == [1]
    _, read_only = accept_db_profile(_db_data())
    assert not read_only

    db = ProfileDatabase()
    db.bundled = {"acme/a": ModelProfile("acme/a", validate_db_profile(_db_data()))}
    db.set_online({"acme/new": newer}, {"acme/new"})
    assert set(db.all()) == {"acme/a"}  # never applied
    assert [p.id for p in db.read_only()] == ["acme/new"]


def test_good_practice_ranks_a_little_higher() -> None:
    db = ProfileDatabase()
    plain = ModelProfile("acme/a", validate_db_profile(_db_data()))
    good = ModelProfile("acme/b", validate_db_profile(_db_data(calibration={"screen_conditions_confirmed": True})))
    assert db.rank(good) > db.rank(plain)
    assert db.summary(good)["good_practice"] and not db.summary(plain)["good_practice"]


async def test_library_keeps_newer_read_only_and_converts_old(hass: HomeAssistant) -> None:
    lib = Library(hass)
    old = await lib.async_add(V1, name="Old")
    new = await lib.async_add(NEWER)
    items = {i["id"]: i for i in lib.list()}
    assert items[old["id"]]["convertible"] and not items[old["id"]]["read_only"]
    assert items[new["id"]]["read_only"] and items[new["id"]]["format_version"] == FORMAT_VERSION + 1
    converted = await lib.async_convert(old["id"])
    assert converted["profile"]["format_version"] == FORMAT_VERSION
    assert not Library.summary(converted)["convertible"]
    with pytest.raises(NewerFormatError):
        await lib.async_convert(new["id"])
    newer_db = ModelProfile("acme/new", accept_db_profile(_db_data(format_version=9))[0], "online", True)
    sections = browse(lib, {}, [newer_db])
    assert sections["community"][0]["read_only"]


async def test_import_export_and_calibration_websocket(hass: HomeAssistant, sources, hass_ws_client) -> None:
    rgbw = await add_twin(hass, "light.bulb_rgbw", title="Desk")
    client = await hass_ws_client(hass)

    async def ws(**msg):
        await client.send_json_auto_id(msg)
        return await client.receive_json()

    # a newer file is refused with the update message, its cells untouched
    res = await ws(type=f"{DOMAIN}/profile/import", entry_id=rgbw.entry_id, profile=NEWER)
    assert not res["success"] and "update Light Match" in res["error"]["message"]
    assert rgbw.runtime_data.cells == {}
    res = await ws(type=f"{DOMAIN}/profile/apply", entry_ids=[rgbw.entry_id], profile=NEWER)
    assert "update Light Match" in json.dumps(res)

    # a format 1 file imports
    res = await ws(type=f"{DOMAIN}/profile/import", entry_id=rgbw.entry_id, profile=V1)
    assert res["success"] and "color:red:5" in rgbw.runtime_data.cells

    # calibration details
    res = await ws(
        type=f"{DOMAIN}/calibration/set", entry_id=rgbw.entry_id,
        calibration={"screen_conditions_confirmed": True, "screen_matched": True, "reference_device": "Pixel 8",
                     "instrument": "", "method": "by_eye"},
        notes="Frosted globe",
    )
    assert res["result"]["calibration"]["screen_matched"] is True and res["result"]["notes"] == "Frosted globe"
    res = await ws(type=f"{DOMAIN}/calibration/set", entry_id=rgbw.entry_id, calibration={}, notes="x" * 501)
    assert res["error"]["code"] == "invalid_format"
    res = await ws(type=f"{DOMAIN}/calibration/set", entry_id=rgbw.entry_id, calibration={"method": "nope"})
    assert res["error"]["code"] == "invalid_format"

    exported = (await ws(type=f"{DOMAIN}/profile/export", entry_id=rgbw.entry_id))["result"]
    profile = exported.get("profile", exported)
    assert profile["format_version"] == FORMAT_VERSION and profile["app_version"] == APP_VERSION
    assert profile["calibration"]["screen_conditions_confirmed"] is True
    assert profile["calibration"]["reference_device"] == "Pixel 8" and profile["notes"] == "Frosted globe"
    entries = (await ws(type=f"{DOMAIN}/entries"))["result"]["entries"]
    assert entries[0]["calibration"]["screen_matched"] and entries[0]["format_version"] == FORMAT_VERSION

    # the details are kept across a reload
    await hass.config_entries.async_reload(rgbw.entry_id)
    await hass.async_block_till_done()
    assert rgbw.runtime_data.calibration["reference_device"] == "Pixel 8"
    assert rgbw.runtime_data.notes == "Frosted globe"

    # github: hidden without a client id
    res = await ws(type=f"{DOMAIN}/github/status")
    assert res["result"] == {"enabled": False, "signed_in": False, "login": None}
    assert (await ws(type=f"{DOMAIN}/github/login"))["error"]["code"] == "github"


async def test_github_device_flow_and_reaction(hass: HomeAssistant, aioclient_mock, hass_storage) -> None:
    gh = GitHubRating(hass, client_id="Iv1.test")
    await gh.async_load()
    assert gh.status() == {"enabled": True, "signed_in": False, "login": None}

    aioclient_mock.post(DEVICE_CODE_URL, json={
        "device_code": "dev", "user_code": "ABCD-1234", "verification_uri": "https://github.com/login/device",
        "interval": 5, "expires_in": 900,
    })
    start = await gh.async_start()
    assert start["user_code"] == "ABCD-1234" and "device_code" not in start

    aioclient_mock.post(TOKEN_URL, json={"error": "authorization_pending"})
    assert (await gh.async_poll())["status"] == "pending"
    aioclient_mock.clear_requests()
    aioclient_mock.post(TOKEN_URL, json={"access_token": "tok", "token_type": "bearer"})
    aioclient_mock.post(GRAPHQL_URL, json={"data": {"viewer": {"login": "octo"}}})
    assert await gh.async_poll() == {"status": "done", "login": "octo"}
    assert hass_storage[f"{DOMAIN}.github"]["data"] == {"token": "tok", "login": "octo"}
    assert "token" not in gh.status() and gh.status()["signed_in"]

    calls = []

    async def graphql(query, variables):
        calls.append((query, variables))
        if "discussion(number" in query:
            return {"repository": {"discussion": {"id": "D_1"}}}
        return {}

    with patch.object(gh, "_graphql", side_effect=graphql):
        assert await gh.async_react("https://github.com/acme/profiles/discussions/7", up=True) == "THUMBS_UP"
    assert calls[0][1] == {"owner": "acme", "repo": "profiles", "number": 7}
    assert "addReaction" in calls[1][0] and calls[1][1] == {"id": "D_1", "content": "THUMBS_UP"}
    assert "removeReaction" in calls[2][0] and calls[2][1]["content"] == "THUMBS_DOWN"

    from custom_components.light_match.github import GitHubError

    with pytest.raises(GitHubError):
        await gh.async_react("https://example.com/nope", up=False)

    # an expired token is forgotten
    aioclient_mock.clear_requests()
    aioclient_mock.post(GRAPHQL_URL, status=401)
    with pytest.raises(GitHubError, match="expired"):
        await gh.async_react("https://github.com/acme/profiles/discussions/7", up=True)
    assert not gh.status()["signed_in"]
    await gh.async_logout()
    assert gh.status()["login"] is None
