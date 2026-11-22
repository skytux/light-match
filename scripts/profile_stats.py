"""Refresh the community stats in profiles/index.json (run by a scheduled workflow).

    python scripts/profile_stats.py assets DIR   # copy profiles to DIR under their release asset names
    python scripts/profile_stats.py stats        # ratings, downloads, discussions, dates -> index.json

`stats` needs GITHUB_TOKEN and GITHUB_REPOSITORY (owner/name), both set in
GitHub Actions. It only reads public GitHub data:

- rating: thumbs up / thumbs down reactions on the profile's Discussion in the
  "Lamp profiles" category (the Discussion title is the profile id; missing
  ones are created, so every profile has a place to be rated and reported);
- downloads: the download counts of the profile's assets in the "profiles"
  release, summed over its revisions (each revision is its own asset);
- updated: the date of the last commit that touched the file.

Home Assistant sends nothing for any of this: downloads are counted only when
a user turned the online update on and Light Match fetched a profile file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from profiles import build_index, index_text, profile_files, structural_entry  # noqa: E402

from custom_components.light_match.database import INDEX_FILE, validate_index  # noqa: E402

API = "https://api.github.com"
CATEGORY = "Lamp profiles"
RELEASE_TAG = "profiles"


def asset_downloads(assets: list[dict[str, Any]]) -> dict[str, int]:
    """Downloads per profile id from release assets named '<manufacturer>__<model>__r<N>.json'."""
    totals: dict[str, int] = {}
    for asset in assets:
        name = asset.get("name", "")
        if not name.endswith(".json") or "__r" not in name:
            continue
        stem = name[: -len(".json")].rsplit("__r", 1)[0]
        pid = stem.replace("__", "/", 1)
        totals[pid] = totals.get(pid, 0) + int(asset.get("download_count", 0))
    return totals


def discussion_stats(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Rating and URL per profile id from Discussion nodes (title = profile id)."""
    out: dict[str, dict[str, Any]] = {}
    for node in nodes:
        groups = {g["content"]: g["reactors"]["totalCount"] for g in node.get("reactionGroups", [])}
        out[node["title"].strip()] = {
            "discussion": node["url"],
            "rating": {"up": groups.get("THUMBS_UP", 0), "down": groups.get("THUMBS_DOWN", 0)},
        }
    return out


def merge_stats(
    ids: list[str],
    discussions: dict[str, dict[str, Any]],
    downloads: dict[str, int],
    updated: dict[str, str | None],
) -> dict[str, dict[str, Any]]:
    """The stats fields for every profile id."""
    return {
        pid: {
            "updated": updated.get(pid),
            "discussion": discussions.get(pid, {}).get("discussion"),
            "rating": discussions.get(pid, {}).get("rating", {"up": 0, "down": 0}),
            "downloads": downloads.get(pid, 0),
        }
        for pid in ids
    }


class GitHub:
    """Minimal GitHub REST + GraphQL client (stdlib only)."""

    def __init__(self, token: str, repo: str) -> None:
        self.token = token
        self.owner, self.name = repo.split("/", 1)

    def _request(self, url: str, body: dict[str, Any] | None = None) -> Any:
        req = urllib.request.Request(  # noqa: S310
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "light-match-profile-stats",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            return json.loads(resp.read())

    def graphql(self, query: str, **variables: Any) -> dict[str, Any]:
        res = self._request(f"{API}/graphql", {"query": query, "variables": variables})
        if res.get("errors"):
            raise RuntimeError(res["errors"])
        return res["data"]

    def release_assets(self) -> list[dict[str, Any]]:
        try:
            release = self._request(f"{API}/repos/{self.owner}/{self.name}/releases/tags/{RELEASE_TAG}")
        except urllib.error.HTTPError as err:
            if err.code == 404:
                return []
            raise
        return release.get("assets", [])

    def discussions(self) -> tuple[str, str, list[dict[str, Any]]]:
        """Repository id, category id and every Discussion in the category."""
        data = self.graphql(
            """query($owner:String!,$name:String!){repository(owner:$owner,name:$name){id
               discussionCategories(first:50){nodes{id name}}}}""",
            owner=self.owner,
            name=self.name,
        )["repository"]
        category = next((c["id"] for c in data["discussionCategories"]["nodes"] if c["name"] == CATEGORY), None)
        if category is None:
            raise RuntimeError(f'Create the Discussions category "{CATEGORY}" first')
        nodes: list[dict[str, Any]] = []
        cursor = None
        while True:
            page = self.graphql(
                """query($owner:String!,$name:String!,$cat:ID!,$after:String){repository(owner:$owner,name:$name){
                   discussions(first:100,after:$after,categoryId:$cat){pageInfo{hasNextPage endCursor}
                   nodes{title url reactionGroups{content reactors{totalCount}}}}}}""",
                owner=self.owner,
                name=self.name,
                cat=category,
                after=cursor,
            )["repository"]["discussions"]
            nodes += page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                return data["id"], category, nodes
            cursor = page["pageInfo"]["endCursor"]

    def create_discussion(self, repo_id: str, category: str, title: str, body: str) -> dict[str, Any]:
        return self.graphql(
            """mutation($repo:ID!,$cat:ID!,$title:String!,$body:String!){createDiscussion(input:
               {repositoryId:$repo,categoryId:$cat,title:$title,body:$body}){discussion{title url reactionGroups{
               content reactors{totalCount}}}}}""",
            repo=repo_id,
            cat=category,
            title=title,
            body=body,
        )["createDiscussion"]["discussion"]


def last_commit_date(path: Path) -> str | None:
    """YYYY-MM-DD of the last commit touching a file."""
    out = subprocess.run(  # noqa: S603
        ["git", "log", "-1", "--format=%cs", "--", str(path)],  # noqa: S607
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    ).stdout.strip()
    return out or None


def copy_assets(target: Path) -> list[str]:
    """Copy every profile under its release asset name."""
    target.mkdir(parents=True, exist_ok=True)
    names = []
    for path in profile_files():
        entry = structural_entry(path)
        shutil.copyfile(path, target / entry["asset"])
        names.append(entry["asset"])
    return names


def refresh_stats() -> None:
    """Fetch the stats and rewrite the index."""
    gh = GitHub(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"])
    repo_id, category, nodes = gh.discussions()
    discussions = discussion_stats(nodes)
    ids = [structural_entry(p)["id"] for p in profile_files()]
    for pid in ids:
        if pid not in discussions:
            body = (
                f"Rate the lamp profile `{pid}` with a thumbs up or down on this post, and tell us in a comment "
                "whether it works for your lamp (model, firmware, integration).\n\n"
                f"File: `custom_components/light_match/profiles/{pid}.json`"
            )
            discussions.update(discussion_stats([gh.create_discussion(repo_id, category, pid, body)]))
    updated = {structural_entry(p)["id"]: last_commit_date(p) for p in profile_files()}
    stats = merge_stats(ids, discussions, asset_downloads(gh.release_assets()), updated)
    index = build_index(stats)
    validate_index(index)
    INDEX_FILE.write_text(index_text(index), encoding="utf-8")
    print(f"stats for {len(ids)} profiles written to {INDEX_FILE.relative_to(ROOT)}")


def main() -> int:
    """Run a command."""
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "assets" and len(sys.argv) > 2:
        names = copy_assets(Path(sys.argv[2]))
        print(f"{len(names)} assets in {sys.argv[2]}")
        return 0
    if command == "stats":
        refresh_stats()
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
