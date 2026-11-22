"""File downloads over authenticated HTTP.

Phone browsers and the companion app's WebView ignore blob downloads, so the
panel signs one of these paths (``auth/sign_path``) and opens it, the way Home
Assistant downloads backups.
"""

from __future__ import annotations

import json
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
import voluptuous as vol

from .const import DOMAIN
from .library import library, profile_filename
from .share import suggested_path

EXPORT_URL = f"/api/{DOMAIN}/export/{{entry_id}}"
SHARE_URL = f"/api/{DOMAIN}/share/{{entry_id}}"
LIBRARY_URL = f"/api/{DOMAIN}/library/{{item_id}}"


def _attachment(data: dict[str, Any], filename: str) -> web.Response:
    return web.Response(
        body=json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        content_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


def _require_admin(request: web.Request) -> None:
    user = request.get("hass_user")
    if user is None or not user.is_admin:
        raise web.HTTPForbidden


class _ProfileView(HomeAssistantView):
    requires_auth = True

    def _data(self, request: web.Request, entry_id: str) -> Any:
        from . import data_for_entry  # noqa: PLC0415

        _require_admin(request)
        data = data_for_entry(request.app["hass"], entry_id)
        if data is None:
            raise web.HTTPNotFound
        return data


class LightMatchExportView(_ProfileView):
    """The profile of one twin as a JSON file (``?make=&model=&name=`` override the auto name)."""

    url = EXPORT_URL
    name = f"api:{DOMAIN}:export"

    async def get(self, request: web.Request, entry_id: str) -> web.Response:
        data = self._data(request, entry_id)
        q = request.query
        profile = data.export(
            q.get("model", "").strip()[:100] or None,
            make=q.get("make", "").strip()[:100] or None,
            name=q.get("name", "").strip()[:120] or None,
        )
        return _attachment(profile, profile_filename(profile["name"]))


class LightMatchLibraryView(HomeAssistantView):
    """A profile saved in the user's library as a JSON file."""

    requires_auth = True
    url = LIBRARY_URL
    name = f"api:{DOMAIN}:library"

    async def get(self, request: web.Request, item_id: str) -> web.Response:
        _require_admin(request)
        item = library(request.app["hass"]).get(item_id)
        if item is None:
            raise web.HTTPNotFound
        return _attachment(item["profile"], profile_filename(item["name"]))


class LightMatchShareView(_ProfileView):
    """The portable profile for the lamp database as a JSON file."""

    url = SHARE_URL
    name = f"api:{DOMAIN}:share"

    async def get(self, request: web.Request, entry_id: str) -> web.Response:
        data = self._data(request, entry_id)
        q = request.query
        try:
            profile = data.share(
                q.get("reference") or None,
                nickname=q.get("nickname") or None,
                github=q.get("github") or None,
                notes=q.get("notes") or None,
            )
        except vol.Invalid as err:
            raise web.HTTPBadRequest(text=str(err)) from err
        return _attachment(profile, suggested_path(data.device_info()).replace("/", "-"))


@callback
def async_register_views(hass: HomeAssistant) -> None:
    """Register the download views."""
    if hass.http is None:
        return
    hass.http.register_view(LightMatchExportView)
    hass.http.register_view(LightMatchShareView)
    hass.http.register_view(LightMatchLibraryView)
