#!/bin/sh
# Fetch the public sources the lamp profile importer reads into $1 (default ./sources).
# Runs inside a container: see tools/import/run.sh. Only permissively licensed sources.
set -eu
OUT="${1:-sources}"
mkdir -p "$OUT"
cd "$OUT"

sparse() {  # sparse <dir> <repo url> <pattern>...
  dir="$1"; url="$2"; shift 2
  if [ ! -d "$dir/.git" ]; then
    git clone -q --depth 1 --filter=blob:none --no-checkout "$url" "$dir"
  fi
  git -C "$dir" sparse-checkout set --no-cone "$@"
  git -C "$dir" checkout -q
  git -C "$dir" log -1 --format="$dir %H %cs" >> versions.txt
}

: > versions.txt
# Powercalc (MIT): light models and brightness->power tables (no hs tables: too big, not needed)
sparse powercalc https://github.com/bramstroker/homeassistant-powercalc.git \
  '/LICENSE*' '/profile_library/library.json' '/profile_library/**/model.json' \
  '/profile_library/**/color_temp.csv.gz' '/profile_library/**/brightness.csv.gz' \
  '/profile_library/**/color_temp.csv' '/profile_library/**/brightness.csv'
# deCONZ (BSD-3-Clause): Philips device description files, gamut type by file name
sparse deconz https://github.com/dresden-elektronik/deconz-rest-plugin.git '/LICENSE*' '/devices/philips/*.json'
# diyHue (Apache-2.0 for BridgeEmulator; its Lights/ directory is CC BY-NC and is NOT fetched)
sparse diyhue https://github.com/diyhue/diyHue.git '/LICENSE*' /BridgeEmulator/lights/light_types.py
# pywizlight (MIT): captured bulb configs
sparse pywizlight https://github.com/sbidy/pywizlight.git '/LICENSE*' /pywizlight/tests/fake_bulb.py
# python-yeelight (BSD-2-Clause): model specs
sparse yeelight https://gitlab.com/stavros/python-yeelight.git '/LICENSE*' /yeelight/main.py
echo "fetched into $OUT"
