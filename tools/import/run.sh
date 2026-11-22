#!/bin/sh
# Regenerate profiles/imported/ from the upstream databases, all in containers.
#   tools/import/run.sh [SOURCES_DIR]
# SOURCES_DIR (default: a temporary folder) receives the sparse clones and the
# zigbee-herdsman-converters dump; nothing is installed on the host.
set -eu
REPO=$(cd "$(dirname "$0")/../.." && pwd)
SRC=${1:-$(mktemp -d)}
mkdir -p "$SRC"
U="$(id -u):$(id -g)"
docker run --rm --entrypoint sh --user "$U" -e HOME=/tmp \
  -v "$REPO/tools/import:/tools:ro" -v "$SRC:/src" alpine/git:latest /tools/fetch.sh /src
docker run --rm --user "$U" -e HOME=/tmp -e npm_config_cache=/tmp/npm \
  -v "$REPO/tools/import:/tools:ro" -v "$SRC:/src" -w /tmp node:22-alpine sh /tools/z2m.sh /src
docker run --rm --user "$U" -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$REPO:/repo" -v "$SRC:/src:ro" -w /repo light-match-test:latest \
  sh -c "python -I tools/import/importer.py /src && python scripts/profiles.py index && python scripts/profiles.py check"
