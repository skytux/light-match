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
    PROFILES_DIR,
    SCHEMA_FILE,
    norm,
    profile_id_for,
    valid_profile_id,
    validate_db_profile,
)

INDEX_FORMAT = "light_match_profile_index"


def profile_files() -> list[Path]:
    """Every profile file, sorted."""
    return sorted(PROFILES_DIR.glob("*/*.json"))


def build_index() -> dict:
    """The index of all profiles: id, path, revision and checksum."""
    entries = []
    for path in profile_files():
        raw = path.read_bytes()
        data = json.loads(raw)
        entries.append(
            {
                "id": profile_id_for(path),
                "path": path.relative_to(PROFILES_DIR).as_posix(),
                "revision": data.get("revision"),
                "name": data.get("name"),
                "manufacturer": data.get("device", {}).get("manufacturer"),
                "models": data.get("device", {}).get("models", []),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return {"format": INDEX_FORMAT, "version": 1, "profiles": entries}


def index_text() -> str:
    """The index as it is written to disk."""
    return json.dumps(build_index(), indent=2, ensure_ascii=False) + "\n"


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
    if not INDEX_FILE.exists() or INDEX_FILE.read_text(encoding="utf-8") != index_text():
        problems.append("profiles/index.json is out of date: run python scripts/profiles.py index")
    return problems


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
