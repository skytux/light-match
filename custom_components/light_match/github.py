"""Optional thumbs up / down on a profile's GitHub Discussion, from the panel.

Uses GitHub's OAuth device flow (as HACS does): the user signs in to GitHub
once from the panel, and the token is kept in this integration's store
(``.storage/light_match.github``); it is never sent to the browser. A rating
is a reaction on the profile's discussion, added through the GraphQL API.

The feature needs the client id of a GitHub OAuth app with the device flow
enabled. It is hidden while :data:`CLIENT_ID` is empty (see CONTRIBUTING.md).
"""

from __future__ import annotations

import contextlib
import re
import time
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import DOMAIN

# Set when publishing: the client id of the project's GitHub OAuth app.
CLIENT_ID = ""
# Adding a reaction to a public repository's discussion needs public_repo.
SCOPE = "public_repo"

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
GRAPHQL_URL = "https://api.github.com/graphql"
TIMEOUT = aiohttp.ClientTimeout(total=20)
_DISCUSSION_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/discussions/(\d+)/?$")

UP = "THUMBS_UP"
DOWN = "THUMBS_DOWN"


class GitHubError(Exception):
    """GitHub refused or could not be reached."""


def discussion_parts(url: str | None) -> tuple[str, str, int] | None:
    """Owner, repository and number of a discussion URL."""
    m = _DISCUSSION_RE.match(url or "")
    return (m.group(1), m.group(2), int(m.group(3))) if m else None


class GitHubRating:
    """The device-flow sign-in and the reactions."""

    def __init__(self, hass: HomeAssistant, client_id: str | None = None) -> None:
        self.hass = hass
        self._client_id = client_id
        self.store: Store = Store(hass, 1, f"{DOMAIN}.github")
        self.token: str | None = None
        self.login: str | None = None
        self._pending: dict[str, Any] | None = None
        self._loaded = False

    @property
    def client_id(self) -> str:
        """The OAuth app's client id (empty: feature off)."""
        return self._client_id if self._client_id is not None else CLIENT_ID

    @property
    def enabled(self) -> bool:
        """Whether in-panel rating is available."""
        return bool(self.client_id)

    async def async_load(self) -> None:
        """Read the stored token once."""
        if self._loaded:
            return
        stored = await self.store.async_load() or {}
        self.token, self.login = stored.get("token"), stored.get("login")
        self._loaded = True

    def status(self) -> dict[str, Any]:
        """For the panel; never includes the token."""
        return {"enabled": self.enabled, "signed_in": bool(self.token), "login": self.login}

    async def _post(self, url: str, data: dict[str, Any]) -> dict[str, Any]:
        session = async_get_clientsession(self.hass)
        try:
            async with session.post(url, data=data, headers={"Accept": "application/json"}, timeout=TIMEOUT) as resp:
                if resp.status != 200:
                    raise GitHubError(f"GitHub answered HTTP {resp.status}")
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise GitHubError(f"GitHub not reachable: {err}") from err

    async def async_start(self) -> dict[str, Any]:
        """Start the device flow: the code the user enters on GitHub."""
        if not self.enabled:
            raise GitHubError("rating from the panel is not set up in this release")
        res = await self._post(DEVICE_CODE_URL, {"client_id": self.client_id, "scope": SCOPE})
        if "device_code" not in res:
            raise GitHubError(res.get("error_description") or res.get("error") or "no device code")
        interval = int(res.get("interval", 5))
        self._pending = {
            "device_code": res["device_code"],
            "interval": interval,
            "expires": time.monotonic() + int(res.get("expires_in", 900)),
        }
        return {
            "user_code": res["user_code"],
            "verification_uri": res.get("verification_uri", "https://github.com/login/device"),
            "interval": interval,
            "expires_in": int(res.get("expires_in", 900)),
        }

    async def async_poll(self) -> dict[str, Any]:
        """Ask once whether the user has entered the code: pending, slow_down, done, expired or denied."""
        pending = self._pending
        if pending is None:
            return {"status": "expired"}
        if time.monotonic() > pending["expires"]:
            self._pending = None
            return {"status": "expired"}
        res = await self._post(
            TOKEN_URL,
            {
                "client_id": self.client_id,
                "device_code": pending["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        if token := res.get("access_token"):
            self._pending = None
            self.token = token
            self.login = await self._viewer()
            await self.store.async_save({"token": self.token, "login": self.login})
            return {"status": "done", "login": self.login}
        error = res.get("error")
        if error == "authorization_pending":
            return {"status": "pending", "interval": pending["interval"]}
        if error == "slow_down":
            pending["interval"] = int(res.get("interval", pending["interval"] + 5))
            return {"status": "slow_down", "interval": pending["interval"]}
        self._pending = None
        return {"status": "denied" if error == "access_denied" else "expired"}

    async def async_logout(self) -> None:
        """Forget the token (it can also be revoked on GitHub)."""
        self.token = self.login = None
        self._pending = None
        await self.store.async_remove()

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if not self.token:
            raise GitHubError("sign in to GitHub first")
        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                GRAPHQL_URL,
                json={"query": query, "variables": variables},
                headers={"Authorization": f"bearer {self.token}", "Accept": "application/json"},
                timeout=TIMEOUT,
            ) as resp:
                if resp.status == 401:
                    self.token = self.login = None
                    await self.store.async_remove()
                    raise GitHubError("the GitHub sign-in has expired; sign in again")
                if resp.status != 200:
                    raise GitHubError(f"GitHub answered HTTP {resp.status}")
                body = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise GitHubError(f"GitHub not reachable: {err}") from err
        if body.get("errors"):
            raise GitHubError("; ".join(str(e.get("message")) for e in body["errors"]))
        return body.get("data") or {}

    async def _viewer(self) -> str | None:
        try:
            data = await self._graphql("query { viewer { login } }", {})
        except GitHubError:
            return None
        return (data.get("viewer") or {}).get("login")

    async def async_react(self, discussion_url: str | None, up: bool) -> str:
        """Thumbs up or down on a discussion (and take back the opposite one)."""
        parts = discussion_parts(discussion_url)
        if parts is None:
            raise GitHubError("this profile has no discussion to rate yet")
        owner, repo, number = parts
        data = await self._graphql(
            "query($owner:String!,$repo:String!,$number:Int!)"
            "{ repository(owner:$owner,name:$repo) { discussion(number:$number) { id } } }",
            {"owner": owner, "repo": repo, "number": number},
        )
        subject = ((data.get("repository") or {}).get("discussion") or {}).get("id")
        if not subject:
            raise GitHubError("discussion not found")
        content, other = (UP, DOWN) if up else (DOWN, UP)
        await self._graphql(
            "mutation($id:ID!,$content:ReactionContent!)"
            "{ addReaction(input:{subjectId:$id,content:$content}) { reaction { content } } }",
            {"id": subject, "content": content},
        )
        with contextlib.suppress(GitHubError):  # there may be no opposite reaction
            await self._graphql(
                "mutation($id:ID!,$content:ReactionContent!)"
                "{ removeReaction(input:{subjectId:$id,content:$content}) { reaction { content } } }",
                {"id": subject, "content": other},
            )
        return content


def github(hass: HomeAssistant) -> GitHubRating:
    """The rating helper (one per Home Assistant)."""
    data = hass.data.setdefault(DOMAIN, {})
    if "github" not in data:
        data["github"] = GitHubRating(hass)
    return data["github"]
