#!/usr/bin/env bash
# Run scripts/smoke.py inside the agents network (the worker image has a2a-sdk and httpx).
#   ./scripts/smoke.sh [--role documentalist] [--run <id>]
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
val() { sed -n "s/^$1=//p" .env | tail -1; }
P="$(val ENGINE_IMAGE_PREFIX)"; T="$(val ENGINE_IMAGE_TAG)"; N="$(val ENGINE_AGENTS_NETWORK_NAME)"
exec docker run --rm --network "${N:-engine-agents-net}" -v "${PWD}/scripts/smoke.py:/app/smoke.py:ro" \
  -e VANGUARD_GATEWAY_TOKEN_ORCHESTRATOR="$(val VANGUARD_GATEWAY_TOKEN_ORCHESTRATOR)" \
  -e VANGUARD_DEFAULT_MODEL="$(val VANGUARD_DEFAULT_MODEL)" \
  --entrypoint python "${P:-agent-engine}/worker:${T:-latest}" /app/smoke.py "$@"
