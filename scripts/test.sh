#!/usr/bin/env bash
# Run lint, the Python tests and the panel syntax check in containers.
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE=light-match-test:latest
# Build the test image only when it is missing, or when REBUILD=1 is set.
if [[ "${REBUILD:-}" == 1 ]] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker build -q -t "$IMAGE" -f scripts/Dockerfile.test . >/dev/null
fi
RUN=(docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -v "$PWD:/repo" -w /repo)
"${RUN[@]}" "$IMAGE" ruff check --no-cache custom_components tests scripts tools
"${RUN[@]}" "$IMAGE" python -m pytest -q -p no:cacheprovider "$@"
"${RUN[@]}" node:22-alpine node --check custom_components/light_match/frontend/light-match-panel.js
echo "panel: syntax OK"
