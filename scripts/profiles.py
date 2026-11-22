"""Check the lamp profile database and keep its index current.

    python scripts/profiles.py check   # schema, rules, duplicates, index
    python scripts/profiles.py index   # rewrite profiles/index.json
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from custom_components.light_match.database import (  # noqa: E402
    INDEX_FILE,
    INDEX_FORMAT,
    PROFILES_DIR,
    SCHEMA_FILE,
    STATS_FIELDS,
    asset_name,
    norm,
    profile_id_for,
    valid_profile_id,
    validate_db_profile,
    validate_index,
)

INDEX_SCHEMA_FILE = PROFILES_DIR / "index.schema.json"


def profile_files() -> list[Path]:
    """Every profile file, sorted."""
    return sorted(PROFILES_DIR.glob("*/*.json"))


def current_stats() -> dict[str, dict]:
    """The stats fields of the index on disk, by profile id (kept on rewrite)."""
    try:
        old = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {e["id"]: {k: e[k] for k in STATS_FIELDS if k in e} for e in old.get("profiles", [])}


def structural_entry(path: Path) -> dict:
    """The index fields that follow from a profile file alone."""
    raw = path.read_bytes()
    data = json.loads(raw)
    pid = profile_id_for(path)
    prov = data.get("provenance", {})
    return {
        "id": pid,
        "path": path.relative_to(PROFILES_DIR).as_posix(),
        "asset": asset_name(pid, data.get("revision", 1)),
        "revision": data.get("revision"),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "name": data.get("name"),
        "manufacturer": data.get("device", {}).get("manufacturer"),
        "models": data.get("device", {}).get("models", []),
        "method": prov.get("method"),
        "verified": bool(prov.get("verified")),
        "contributors": sorted({c["nickname"] for c in data.get("contributors", [])}),
    }


def build_index(stats: dict[str, dict] | None = None) -> dict:
    """The index of all profiles; stats (ratings, downloads...) come from `stats`."""
    stats = current_stats() if stats is None else stats
    entries = []
    for path in profile_files():
        entry = structural_entry(path)
        entry.update(stats.get(entry["id"], {}))
        entries.append(entry)
    return {"format": INDEX_FORMAT, "version": 1, "profiles": entries}


def index_text(index: dict | None = None) -> str:
    """The index as it is written to disk."""
    return json.dumps(index or build_index(), indent=2, ensure_ascii=False) + "\n"


def _structure(index: dict) -> list[dict]:
    return [{k: v for k, v in e.items() if k not in STATS_FIELDS} for e in index.get("profiles", [])]


def check_index() -> list[str]:
    """Problems with the index: schema, and structural fields that drifted from the files."""
    import jsonschema  # noqa: PLC0415

    try:
        index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        return [f"profiles/index.json not readable: {err}"]
    schema = json.loads(INDEX_SCHEMA_FILE.read_text(encoding="utf-8"))
    problems = [f"index: schema: {e.message}" for e in jsonschema.Draft202012Validator(schema).iter_errors(index)]
    try:
        validate_index(index)
    except Exception as err:  # noqa: BLE001
        problems.append(f"index: {err}")
    if _structure(index) != _structure(build_index({})):
        problems.append("profiles/index.json is out of date: run python scripts/profiles.py index")
    return problems


def check() -> list[str]:
    """All problems found in the database."""
    import jsonschema  # noqa: PLC0415

    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
    )
    problems: list[str] = []
    seen: dict[tuple[str, str, str], str] = {}
    for path in profile_files():
        pid = profile_id_for(path)
        if not valid_profile_id(pid):
            problems.append(f"{pid}: file names must be lower case: <manufacturer>/<model>.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as err:
            problems.append(f"{pid}: not valid JSON: {err}")
            continue
        problems += [f"{pid}: schema: {e.message}" for e in validator.iter_errors(data)]
        try:
            validate_db_profile(data)
        except Exception as err:  # noqa: BLE001
            problems.append(f"{pid}: {err}")
            continue
        device = data["device"]
        manufacturers = {norm(device["manufacturer"]), *map(norm, device.get("manufacturer_aliases", []))}
        integrations = tuple(sorted(device.get("integrations") or [])) or ("*",)
        for manufacturer in manufacturers:
            for model in device["models"]:
                for integration in integrations:
                    key = (manufacturer, norm(model) if model != "*" else "*", integration)
                    if key in seen and seen[key] != pid:
                        problems.append(f"{pid}: model {model!r} is also in {seen[key]}")
                    seen[key] = pid
    return problems + check_index()


def main() -> int:
    """Run a command."""
    command = sys.argv[1] if len(sys.argv) > 1 else "check"
    if command == "index":
        INDEX_FILE.write_text(index_text(), encoding="utf-8")
        print(f"wrote {INDEX_FILE.relative_to(ROOT)}")
        return 0
    problems = check()
    for problem in problems:
        print(problem)
    print(f"{len(profile_files())} profiles, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
