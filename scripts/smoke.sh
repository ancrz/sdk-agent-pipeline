#!/usr/bin/env bash
# Run scripts/smoke.py inside the agents network (the worker image has a2a-sdk and httpx).
#   ./scripts/smoke.sh [--role documentalist] [--run <id>]
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
val() { sed -n "s/^$1=//p" .env | tail -1; }
P="$(val ENGINE_IMAGE_PREFIX)"; T="$(val ENGINE_IMAGE_TAG)"; N="$(val ENGINE_AGENTS_NETWORK_NAME)"
exec docker run --rm --network "${N:-engine-agents-net}" -v "${PWD}/scripts/smoke.py:/app/smoke.py:ro" \
  -e ENGINE_GATEWAY_TOKEN_ORCHESTRATOR="$(val ENGINE_GATEWAY_TOKEN_ORCHESTRATOR)" \
  -e ENGINE_DEFAULT_MODEL="$(val ENGINE_DEFAULT_MODEL)" \
  --entrypoint python "${P:-agent-engine}/worker:${T:-latest}" /app/smoke.py "$@"
