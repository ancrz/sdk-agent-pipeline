#!/usr/bin/env bash
# Build the engine's images in order: base → model-gateway → worker.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
val() { [ -f .env ] && sed -n "s/^$1=//p" .env | tail -1; }   # no `source`: .env holds JSON values
P="$(val ENGINE_IMAGE_PREFIX)"; P="${P:-agent-engine}"; T="$(val ENGINE_IMAGE_TAG)"; T="${T:-latest}"
DOCKER_BUILDKIT=1 docker build -q -t "${P}/base:${T}" -f agents/base/Dockerfile .
docker compose build model-gateway worker-agents
echo "[build] ${P}/base, ${P}/model-gateway, ${P}/worker (${T})"
