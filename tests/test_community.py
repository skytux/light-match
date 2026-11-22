"""The community files stay in step with the code that fills them."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from custom_components.light_match.share import ISSUE_TEMPLATE, submission_url

ROOT = Path(__file__).resolve().parent.parent


def test_issue_form_has_the_fields_the_share_link_fills() -> None:
    form = yaml.safe_load((ROOT / ".github/ISSUE_TEMPLATE" / ISSUE_TEMPLATE).read_text())
    ids = {item["id"] for item in form["body"] if "id" in item}
    profile = {
        "name": "Acme X1",
        "device": {"manufacturer": "Acme", "models": ["X1"], "integrations": ["zha"]},
        "contributors": [{"nickname": "lampfan"}],
        "provenance": {"method": "by_eye"},
        "target": "lm-ref-1",
    }
    url, _ = submission_url("https://github.com/example/repo", profile, "profiles/acme/x1.json")
    query = parse_qs(urlparse(url).query)
    filled = set(query) - {"template", "title", "labels"}
    assert filled <= ids, filled - ids
    assert query["labels"] == form["labels"]
    dropdowns = {item["id"]: item["attributes"]["options"] for item in form["body"] if item["type"] == "dropdown"}
    assert query["method"][0] in dropdowns["method"] and query["target"][0] in dropdowns["target"]


def test_workflows_are_valid_yaml() -> None:
    names = sorted(p.name for p in (ROOT / ".github/workflows").glob("*.yml"))
    assert names == ["profiles-release.yml", "profiles.yml", "tests.yml", "validate.yml"]
    for name in names:
        data = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        assert data["jobs"] and data[True]  # "on:" loads as True in YAML 1.1
