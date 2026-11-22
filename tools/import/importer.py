"""Build imported lamp profiles from public, permissively licensed databases.

    python tools/import/importer.py SOURCES_DIR [OUT_DIR]

SOURCES_DIR is filled by fetch.sh and z2m.sh (see run.sh, which runs all of
it in containers). OUT_DIR defaults to custom_components/light_match/profiles/imported.

What is used, and how (nothing here is a colour calibration):

- Powercalc profile library (MIT): one profile per light model, with its
  model ids, aliases and legacy ids, colour modes, lumens and the mired range
  it was measured over. Its brightness->power table becomes an *estimated*
  output curve (standby power subtracted, normalised to 1 at full
  brightness; quality "power-proxy": electrical power is not light), and a
  brightness floor is estimated where the power stays flat at the bottom
  (quality "estimated"). Author emails are never read into the output.
- zigbee-herdsman-converters (MIT): Zigbee model ids (what ZHA reports),
  Zigbee2MQTT model ids, colour modes, mired range, lumens from the
  description, joined on the model id.
- deCONZ device files (BSD-3-Clause): Hue model id -> colour gamut A/B/C,
  from the file name; cross-checked against diyHue (Apache-2.0), which also
  gives the mired range and maximum lumens. On disagreement deCONZ wins.
- pywizlight test fixtures (MIT): WiZ module -> white kelvin range.
- python-yeelight model specs (BSD-2-Clause): Yeelight model -> kelvin range.

Each profile lists every source it used in provenance.imports, with the
licence, copyright, fields and quality. The cells stay empty.
"""

from __future__ import annotations

import ast
import csv
import gzip
import io
import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "custom_components" / "light_match" / "profiles" / "imported"

# Brightness points kept from a power table (roughly even in perceived brightness).
CURVE_POINTS = (1, 3, 5, 8, 13, 20, 26, 33, 41, 51, 64, 77, 92, 110, 128, 150, 175, 200, 228, 255)
FLOOR_MIN = 16  # a flat start shorter than this is measurement resolution, not a floor
DATE = "2026-11-22"

SOURCES: dict[str, dict[str, str]] = {
    "powercalc": {
        "name": "Powercalc profile library",
        "url": "https://github.com/bramstroker/homeassistant-powercalc/tree/master/profile_library",
        "licence": "MIT",
        "copyright": "Copyright (c) 2021 Bram Gerritsen and the Powercalc contributors",
        "license_file": "powercalc/LICENSE",
    },
    "z2m": {
        "name": "zigbee-herdsman-converters (Zigbee2MQTT)",
        "url": "https://github.com/Koenkk/zigbee-herdsman-converters",
        "licence": "MIT",
        "copyright": "Copyright (c) 2018 Koen Kanters and the zigbee-herdsman-converters contributors",
        "license_file": "z2m/LICENSE",
    },
    "deconz": {
        "name": "deCONZ REST plugin device description files",
        "url": "https://github.com/dresden-elektronik/deconz-rest-plugin/tree/master/devices/philips",
        "licence": "BSD-3-Clause",
        "copyright": "Copyright (c) dresden elektronik ingenieurtechnik gmbh",
        "license_file": "deconz/LICENSE.txt",
    },
    "diyhue": {
        "name": "diyHue BridgeEmulator light types",
        "url": "https://github.com/diyhue/diyHue/blob/master/BridgeEmulator/lights/light_types.py",
        "licence": "Apache-2.0",
        "copyright": "Copyright the diyHue contributors",
        "license_file": "diyhue/LICENSE.md",
    },
    "pywizlight": {
        "name": "pywizlight test fixtures",
        "url": "https://github.com/sbidy/pywizlight/blob/master/pywizlight/tests/fake_bulb.py",
        "licence": "MIT",
        "copyright": "Copyright (c) 2020 Stephan Traub and the pywizlight contributors",
        "license_file": "pywizlight/LICENSE",
    },
    "yeelight": {
        "name": "python-yeelight model specs",
        "url": "https://gitlab.com/stavros/python-yeelight/-/blob/master/yeelight/main.py",
        "licence": "BSD-2-Clause",
        "copyright": "Copyright (c) 2016 Stavros Korokithakis",
        "license_file": "yeelight/LICENSE",
    },
}

HUE_MANUFACTURER = "Signify Netherlands B.V."
COLOR_MODES = {"onoff", "brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def norm(value: Any) -> str:
    """Normalise a model or manufacturer for joining (as the integration matches)."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def slug(value: str) -> str:
    """A file-name-safe lower-case slug."""
    out = re.sub(r"[^a-z0-9._-]+", "-", value.casefold()).strip("-._")
    return out or "x"


def kelvin(mired: float) -> int:
    """Mired to kelvin, rounded to 10 K."""
    return int(round(1e6 / mired / 10) * 10)


def lumens_from_text(text: str | None) -> int | None:
    """The lumen figure in a product description ('... 806 lm'); the highest when several."""
    found = re.findall(r"(\d{2,5}(?:/\d{2,5})*)\s?lm\b", text or "", re.I)
    values = [int(v) for group in found for v in group.split("/")]
    return max(values) if values else None


def source_entry(key: str, fields: list[str], quality: str) -> dict[str, Any]:
    """One provenance.imports entry."""
    src = SOURCES[key]
    return {
        "source": src["name"],
        "source_url": src["url"],
        "licence": src["licence"],
        "copyright": src["copyright"],
        "fields": sorted(set(fields)),
        "quality": quality,
    }


# ---------------------------------------------------------------------------
# Power table -> estimated output curve and floor
# ---------------------------------------------------------------------------


def read_lut(path: Path) -> list[dict[str, str]]:
    """Rows of a Powercalc csv(.gz) table."""
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))


def bri_to_watt(rows: list[dict[str, str]]) -> tuple[list[tuple[int, float]], list[int] | None]:
    """Brightness -> power at one colour temperature, and the measured mired range.

    For a colour temperature table the mired closest to the middle of the
    measured range is used.
    """
    if not rows:
        return [], None
    every = sorted({int(r["mired"]) for r in rows if r.get("mired")})
    # 50-1000 mired = 1000-20000 K; some tables hold other numbers in that column
    mireds = [m for m in every if 50 <= m <= 1000]
    use = None
    if every:
        pick = mireds or every
        middle = (pick[0] + pick[-1]) / 2
        use = min(pick, key=lambda m: (abs(m - middle), m))
    points: dict[int, float] = {}
    for r in rows:
        if use is not None and int(r["mired"]) != use:
            continue
        points[int(r["bri"])] = float(r["watt"])
    return sorted(points.items()), ([mireds[0], mireds[-1]] if len(mireds) > 1 else None)


def infer_floor(points: list[tuple[int, float]], min_flat: int = FLOOR_MIN) -> int:
    """The brightness below which the power does not change (0 when there is no such floor).

    The power must stay at its first reading, within meter noise (0.015 W or
    0.3 % of the power range, whichever is larger), up to at least
    ``min_flat`` and over three samples or more; shorter flat starts are
    meter resolution, not a floor.
    """
    if len(points) < 4:
        return 0
    first = points[0][1]
    tolerance = max(0.015, 0.003 * (max(w for _, w in points) - min(w for _, w in points)))
    run = []
    for b, w in points:
        if abs(w - first) > tolerance:
            break
        run.append(b)
    if len(run) < 3 or run[-1] < min_flat or len(run) == len(points):
        return 0
    return run[-1]


def output_curve(points: list[tuple[int, float]], standby: float, floor: int = 0) -> list[list[float]]:
    """An estimated output curve [[brightness, relative output], ...] from power readings.

    Standby power is subtracted, the result normalised to 1 at full power and
    made non-decreasing; points below the floor are dropped (the floor point
    stays). Kept at the brightness values of CURVE_POINTS, interpolated.
    """
    if len(points) < 2:
        return []
    top = max(w for _, w in points) - standby
    if top <= 0:
        return []
    by_b = sorted(points)

    def at(b: float) -> float:
        if b <= by_b[0][0]:
            return by_b[0][1]
        for (b0, w0), (b1, w1) in zip(by_b, by_b[1:], strict=False):
            if b0 <= b <= b1:
                return w0 + (w1 - w0) * (b - b0) / (b1 - b0) if b1 != b0 else w1
        return by_b[-1][1]

    keep = sorted({b for b in CURVE_POINTS if b >= max(floor, by_b[0][0])} | ({floor} if floor else set()))
    curve: list[list[float]] = []
    best = 0.0
    for b in keep:
        rel = max(0.0, min(1.0, (at(b) - standby) / top))
        best = max(best, rel)
        curve.append([b, round(best, 4)])
    if curve and curve[-1][0] != 255:
        curve.append([255, 1.0])
    curve[-1][1] = 1.0
    return curve


# ---------------------------------------------------------------------------
# Readers for each source
# ---------------------------------------------------------------------------


def load_powercalc(src: Path) -> list[dict[str, Any]]:
    """Light models from the Powercalc library, with their LUT paths."""
    lib = json.loads((src / "powercalc/profile_library/library.json").read_text(encoding="utf-8"))
    out = []
    for manufacturer in lib["manufacturers"]:
        for model in manufacturer.get("models", []):
            if model.get("device_type") != "light":
                continue
            folder = src / "powercalc/profile_library" / manufacturer["dir_name"] / model["id"]
            lut = None
            for name in ("color_temp.csv.gz", "color_temp.csv", "brightness.csv.gz", "brightness.csv"):
                if (folder / name).exists():
                    lut = folder / name
                    break
            model = {k: v for k, v in model.items() if k != "authors"}  # no names or emails
            out.append({"manufacturer": manufacturer, "model": model, "lut": lut})
    return out


def load_z2m(src: Path) -> list[dict[str, Any]]:
    """Light definitions dumped by z2m.sh, one per z2m model."""
    path = src / "z2m/lights.json"
    if not path.exists():
        return []
    seen: dict[str, dict[str, Any]] = {}
    for light in json.loads(path.read_text(encoding="utf-8"))["lights"]:
        seen.setdefault(light["model"], light)
    return list(seen.values())


def load_deconz(src: Path) -> dict[str, str]:
    """Hue model id -> gamut letter, from the deCONZ file names."""
    gamuts: dict[str, str] = {}
    for path in sorted((src / "deconz/devices/philips").glob("light_*.json")):
        found = re.search(r"light_(?:zll|zb3)_([ABC])(?:_|\.)", path.name)
        if not found:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        ids = data.get("modelid") or []
        for mid in [ids] if isinstance(ids, str) else ids:
            gamuts[norm(mid)] = found.group(1)
    return gamuts


def load_diyhue(src: Path) -> dict[str, dict[str, Any]]:
    """Hue model id -> capabilities.control from diyHue's light types."""
    path = src / "diyhue/BridgeEmulator/lights/light_types.py"
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    pattern = re.compile(r'lightTypes\["([^"]+)"\]\["v1_static"\]\["capabilities"\]\s*=\s*(\{.*\})\s*$')
    for line in path.read_text(encoding="utf-8").splitlines():
        found = pattern.match(line)
        if not found:
            continue
        try:
            caps = ast.literal_eval(found.group(2))
        except (ValueError, SyntaxError):
            continue
        out[norm(found.group(1))] = caps.get("control") or {}
    return out


def load_pywizlight(src: Path) -> dict[str, dict[str, Any]]:
    """WiZ module name -> white kelvin range [min, max] from the captured configs."""
    path = src / "pywizlight/pywizlight/tests/fake_bulb.py"
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    text = path.read_text(encoding="utf-8")
    for found in re.finditer(r'\("(ESP[^"]+)", "[^"]+"\): \{(.*?)\n    \}', text, re.S):
        rng = re.search(r'"cctRange": \[([\d, ]+)\]', found.group(2))
        if rng:
            values = [int(v) for v in rng.group(1).split(",")]
            # [extended min, white min, white max, extended max]
            out.setdefault(found.group(1), {"kelvin": [min(values), max(values)]})
    return out


def load_yeelight(src: Path) -> dict[str, dict[str, Any]]:
    """Yeelight model -> kelvin range from python-yeelight's _MODEL_SPECS."""
    path = src / "yeelight/yeelight/main.py"
    if not path.exists():
        return {}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "_MODEL_SPECS" for t in node.targets):
            out: dict[str, dict[str, Any]] = {}
            for key, value in zip(node.value.keys, node.value.values, strict=False):
                if not isinstance(key, ast.Constant) or not isinstance(value, ast.Dict):
                    continue
                for k, v in zip(value.keys, value.values, strict=False):
                    if isinstance(k, ast.Constant) and k.value == "color_temp":
                        try:
                            ct = ast.literal_eval(v)
                        except ValueError:
                            break
                        out[key.value] = {"kelvin": [int(ct["min"]), int(ct["max"])]}
            return out
    return {}


# ---------------------------------------------------------------------------
# Joining and building
# ---------------------------------------------------------------------------


def z2m_index(lights: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Each normalised id (z2m model, Zigbee model, white-label model) -> definitions."""
    index: dict[str, list[dict[str, Any]]] = {}
    for light in lights:
        keys = {norm(light["model"]), *(norm(z) for z in light.get("zigbeeModel") or [])}
        keys |= {norm(w.get("model")) for w in light.get("whiteLabel") or []}
        for key in keys - {""}:
            index.setdefault(key, []).append(light)
    return index


def z2m_modes(light: dict[str, Any]) -> list[str]:
    """Home Assistant colour modes of a z2m light."""
    modes = []
    if light.get("color_temp"):
        modes.append("color_temp")
    if light.get("color_xy"):
        modes.append("xy")
    elif light.get("color_hs"):
        modes.append("hs")
    if not modes:
        modes.append("brightness" if light.get("brightness") else "onoff")
    return modes


def build_powercalc_profile(
    entry: dict[str, Any],
    z2m: dict[str, list[dict[str, Any]]],
    deconz: dict[str, str],
    diyhue: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """One imported profile for a Powercalc light; returns (profile, notes for the log)."""
    manu, model = entry["manufacturer"], entry["model"]
    log: list[str] = []
    ids = [model["id"], *(model.get("aliases") or []), *(model.get("legacy_ids") or [])]
    models: list[str] = list(dict.fromkeys(i for i in ids if i))
    imports: list[dict[str, Any]] = []
    caps: dict[str, Any] = {}
    pc_fields = ["models"]

    modes = [m for m in model.get("color_modes") or [] if m in COLOR_MODES]
    if modes:
        caps["color_modes"] = sorted(modes)
        pc_fields.append("color_modes")
    lumens = (model.get("device_specs") or {}).get("lumens")
    if isinstance(lumens, (int, float)) and lumens >= 1:
        caps["lumens"] = int(lumens)
        pc_fields.append("lumens")

    curve_import = None
    if entry["lut"] is not None:
        points, mireds = bri_to_watt(read_lut(entry["lut"]))
        if mireds and mireds[1] > mireds[0]:
            caps["min_kelvin"], caps["max_kelvin"] = kelvin(mireds[1]), kelvin(mireds[0])
            pc_fields += ["min_kelvin", "max_kelvin"]
        standby = float(model.get("standby_power") or 0.0)
        floor = infer_floor(points)
        curve = output_curve(points, standby, floor)
        if len(curve) >= 2:
            caps["output_curve"] = curve
            curve_import = source_entry("powercalc", ["output_curve"], "power-proxy")
        if floor:
            caps["brightness_floor"] = floor
            log.append(f"floor {floor} estimated from a flat power reading")
    imports.append(source_entry("powercalc", pc_fields, "measured-by-source"))
    if curve_import:
        imports.append(curve_import)
    if caps.get("brightness_floor"):
        imports.append(source_entry("powercalc", ["brightness_floor"], "estimated"))

    # zigbee-herdsman-converters
    hits = {id(d): d for i in models for d in z2m.get(norm(i), [])}
    if len(hits) == 1:
        light = next(iter(hits.values()))
        fields = []
        extra = [light["model"], *(light.get("zigbeeModel") or [])]
        new = [m for m in dict.fromkeys(extra) if norm(m) not in {norm(x) for x in models}]
        if new:
            models += new
            fields.append("models")
        if "color_modes" not in caps:
            caps["color_modes"] = sorted(z2m_modes(light))
            fields.append("color_modes")
        if light.get("color_temp") and "min_kelvin" not in caps:
            lo, hi = light["color_temp"]
            caps["min_kelvin"], caps["max_kelvin"] = kelvin(hi), kelvin(lo)
            fields += ["min_kelvin", "max_kelvin"]
        if "lumens" not in caps and (lm := lumens_from_text(light.get("description"))):
            caps["lumens"] = lm
            fields.append("lumens")
        if fields:
            imports.append(source_entry("z2m", fields, "spec"))
    elif len(hits) > 1:
        log.append(f"z2m: {len(hits)} definitions match, none used")

    # Hue gamut
    hue_ids = [norm(i) for i in models]
    letter = next((deconz[i] for i in hue_ids if i in deconz), None)
    diy = next((diyhue[i] for i in hue_ids if i in diyhue), None)
    if letter:
        caps["gamut"] = letter
        imports.append(source_entry("deconz", ["gamut"], "spec"))
    if diy:
        fields = []
        diy_letter = diy.get("colorgamuttype")
        if diy_letter and letter and diy_letter != letter:
            log.append(f"diyHue says gamut {diy_letter}, deCONZ {letter}: deCONZ kept")
        elif diy_letter in ("A", "B", "C") and not letter:
            caps["gamut"] = diy_letter
            fields.append("gamut")
        ct = diy.get("ct") or {}
        if ct.get("min") and ct.get("max") and "min_kelvin" not in caps:
            caps["min_kelvin"], caps["max_kelvin"] = kelvin(ct["max"]), kelvin(ct["min"])
            fields += ["min_kelvin", "max_kelvin"]
        if diy.get("maxlumen") and "lumens" not in caps:
            caps["lumens"] = int(diy["maxlumen"])
            fields.append("lumens")
        if fields:
            imports.append(source_entry("diyhue", fields, "spec"))

    shown = manu.get("full_name") or manu["name"]
    aliases = {norm(a): a for a in [manu["name"], *(manu.get("aliases") or [])] if a and norm(a) != norm(shown)}
    if manu["dir_name"] == "signify":
        aliases.setdefault(norm(HUE_MANUFACTURER), HUE_MANUFACTURER)
    profile = {
        "format": "light_match_profile",
        "version": 2,
        "revision": 1,
        "name": f"{manu.get('full_name') or manu['name']} {model.get('name') or model['id']}",
        "license": "CC-BY-4.0",
        "device": {
            "manufacturer": shown,
            "manufacturer_aliases": sorted(aliases.values(), key=str.casefold),
            "models": models,
        },
        "capabilities": caps,
        "provenance": {
            "method": "imported",
            "verified": False,
            "notes": "Partly calibrated: imported, unverified. Capabilities and an estimated brightness curve only; "
            "the curve comes from electrical power, not light.",
            "imports": imports,
        },
    }
    return profile, log


def build_kelvin_profile(
    manufacturer: str, model: str, kelvins: list[int], source: str, aliases: list[str] | None = None
) -> dict[str, Any]:
    """A capabilities-only profile with a white kelvin range."""
    return {
        "format": "light_match_profile",
        "version": 2,
        "revision": 1,
        "name": f"{manufacturer} {model}",
        "license": "CC-BY-4.0",
        "device": {"manufacturer": manufacturer, "manufacturer_aliases": aliases or [], "models": [model]},
        "capabilities": {"min_kelvin": kelvins[0], "max_kelvin": kelvins[1]},
        "provenance": {
            "method": "imported",
            "verified": False,
            "notes": "Imported, unverified: white range only.",
            "imports": [source_entry(source, ["min_kelvin", "max_kelvin"], "spec")],
        },
    }


def unique_name(names: set[str], base: str) -> str:
    """A file name not used yet."""
    name, n = base, 2
    while name in names:
        name = f"{base}-{n}"
        n += 1
    names.add(name)
    return name


def drop_shared_models(profiles: dict[str, dict[str, Any]], primary: dict[str, set[str]]) -> int:
    """Make every model id belong to one imported profile per manufacturer.

    ``primary`` holds each profile's own ids (from its main source); ids added
    by a join are secondary. A secondary id that is someone's primary id, or
    is added to several profiles, is dropped; a primary id shared by several
    profiles is dropped from all of them. Profiles left without ids go.
    """
    claims: dict[tuple[str, str], list[tuple[str, bool]]] = {}
    for name, prof in profiles.items():
        manu = norm(prof["device"]["manufacturer"])
        for model in prof["device"]["models"]:
            claims.setdefault((manu, norm(model)), []).append((name, norm(model) in primary.get(name, set())))
    drop: set[tuple[str, str]] = set()
    for (_manu, model), owners in claims.items():
        if len(owners) < 2:
            continue
        firsts = [n for n, is_primary in owners if is_primary]
        keep = firsts[0] if len(firsts) == 1 else None
        drop |= {(n, model) for n, _ in owners if n != keep}
    dropped = 0
    for name, model in drop:
        device = profiles[name]["device"]
        before = len(device["models"])
        device["models"] = [m for m in device["models"] if norm(m) != model]
        dropped += before - len(device["models"])
    for name in [n for n, p in profiles.items() if not p["device"]["models"]]:
        del profiles[name]
    return dropped


def read_copyrights(src: Path) -> None:
    """Take each source's copyright line from its licence file, when it has one."""
    for s in SOURCES.values():
        path = src / s["license_file"]
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip().lstrip("#* ").strip()
            if re.match(r"(?i)copyright\s+(\(c\)|©|\d{4})", line) and "[yyyy]" not in line:
                s["copyright"] = line
                break


def build_all(src: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Every imported profile by file stem, and a log."""
    read_copyrights(src)
    z2m = z2m_index(load_z2m(src))
    deconz, diyhue = load_deconz(src), load_diyhue(src)
    profiles: dict[str, dict[str, Any]] = {}
    primary: dict[str, set[str]] = {}
    names: set[str] = set()
    log: list[str] = []
    for entry in load_powercalc(src):
        prof, notes = build_powercalc_profile(entry, z2m, deconz, diyhue)
        stem = unique_name(names, f"{slug(entry['manufacturer']['dir_name'])}__{slug(entry['model']['id'])}")
        profiles[stem] = prof
        model = entry["model"]
        primary[stem] = {norm(i) for i in [model["id"], *(model.get("aliases") or []), *(model.get("legacy_ids") or [])]}
        log += [f"{stem}: {n}" for n in notes]
    covered = {(norm(p["device"]["manufacturer"]), norm(m)) for p in profiles.values() for m in p["device"]["models"]}
    for module, data in sorted(load_pywizlight(src).items()):
        if ("wiz", norm(module)) not in covered:
            stem = unique_name(names, f"wiz__{slug(module)}")
            profiles[stem] = build_kelvin_profile("WiZ", module, data["kelvin"], "pywizlight")
            primary[stem] = {norm(module)}
    for model, data in sorted(load_yeelight(src).items()):
        if ("yeelight", norm(model)) not in covered:
            stem = unique_name(names, f"yeelight__{slug(model)}")
            profiles[stem] = build_kelvin_profile(
                "Yeelight", model, data["kelvin"], "yeelight", ["Yeelight Technology", "Xiaomi"]
            )
            primary[stem] = {norm(model)}
    dropped = drop_shared_models(profiles, primary)
    if dropped:
        log.append(f"{dropped} model ids claimed by several profiles were left out")
    return profiles, log


def notice(src: Path) -> str:
    """NOTICE.md for the imported profiles."""
    versions = {}
    vfile = src / "versions.txt"
    if vfile.exists():
        for line in vfile.read_text().splitlines():
            parts = line.split()
            if len(parts) == 3:
                versions[parts[0]] = f"commit {parts[1][:12]} ({parts[2]})"
    z2m = src / "z2m/lights.json"
    if z2m.exists():
        versions["z2m"] = f"npm {json.loads(z2m.read_text())['version']}"
    lines = [
        "# Imported lamp profiles: sources and notices",
        "",
        "The profiles in this folder were generated by `tools/import/importer.py` from the",
        "public databases below. They hold capabilities and an estimated brightness curve",
        "only (\"partly calibrated / imported, unverified\"), never calibrated colour cells.",
        "",
        "The compilation is shared under CC BY 4.0 like the rest of the profile database;",
        "the data taken from each source keeps that source's licence and copyright notice,",
        "reproduced in `licenses/`. Every profile names the sources it used in",
        "`provenance.imports`.",
        "",
        "| Source | Licence | Copyright | Version used | Licence text |",
        "|---|---|---|---|---|",
    ]
    for key, s in SOURCES.items():
        lines.append(
            f"| [{s['name']}]({s['url']}) | {s['licence']} | {s['copyright']} | {versions.get(key, '-')} | `licenses/{key}.txt` |"
        )
    lines += [
        "",
        "Not used, because their licences do not allow it: python-kasa (GPL-3.0), ESPHome",
        "device definitions (GPL-3.0), Tasmota templates (EPL-2.0), the LIFX products list",
        "and the Philips Hue developer documentation (all rights reserved), Lumen",
        "Calibrator (no licence), diyHue's `Lights/` folder (CC BY-NC 4.0).",
        "",
    ]
    return "\n".join(lines)


def dumps(profile: dict[str, Any]) -> str:
    """A profile as written: indented, with short number lists kept on one line."""
    text = json.dumps(profile, indent=1, ensure_ascii=False)
    text = re.sub(r"\[\s*(-?[\d.]+),\s*(-?[\d.]+)\s*\]", r"[\1, \2]", text)
    return text + "\n"


def write(src: Path, out: Path) -> tuple[int, list[str]]:
    """Write the profiles, licences and notice; returns (count, log)."""
    profiles, log = build_all(src)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.json"):
        old.unlink()
    for stem, prof in sorted(profiles.items()):
        (out / f"{stem}.json").write_text(dumps(prof), encoding="utf-8")
    lic = out / "licenses"
    lic.mkdir(exist_ok=True)
    for key, s in SOURCES.items():
        path = src / s["license_file"]
        if path.exists():
            (lic / f"{key}.txt").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            log.append(f"licence file missing for {key}: {path}")
    (out / "NOTICE.md").write_text(notice(src), encoding="utf-8")
    return len(profiles), log


def main() -> int:
    """Run the importer."""
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    count, log = write(src, out)
    for line in log:
        print(line)
    print(f"{count} imported profiles written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
