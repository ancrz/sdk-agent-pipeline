# Agent engine worker image — ADK agents built from their specs (workers/agent.py SPECS), served over A2A.
# The generic worker (compose service worker-agents, image agent-engine/worker) serves several roles in one
# process under /<role>/ (AGENT_ROLES, G-AGENTS A4); a per-role image serves one at the root.
#
# Build args:
#   BASE_IMAGE  Base image to extend (agent-engine/base:latest for every role)
#   ROLE        executor | validator | documentalist | author | medic (ENV AGENT_ROLE); empty for the
#               generic image (AGENT_ROLES at run time)
#
# Usage:
#   docker build --build-arg BASE_IMAGE=agent-engine/base:latest --build-arg ROLE= \
#                -f workers/Dockerfile -t agent-engine/worker:latest .
#   docker build --build-arg BASE_IMAGE=agent-engine/base:latest --build-arg ROLE=executor \
#                -f workers/Dockerfile -t agent-engine/worker-executor:latest .
#   docker build --build-arg BASE_IMAGE=agent-engine/base:latest --build-arg ROLE=validator \
#                -f workers/Dockerfile -t agent-engine/worker-validator:latest .

ARG BASE_IMAGE=agent-engine/base:latest
FROM ${BASE_IMAGE}

ARG ROLE=executor
ENV AGENT_ROLE=${ROLE}
ARG WORKER_PORT=8080
ENV WORKER_PORT=${WORKER_PORT}

WORKDIR /app

# Copy application packages in layer-cache order:
#   workers/ first (most volatile), then shared modules.
COPY workers/ /app/workers/
COPY orchestrator/ /app/orchestrator/

EXPOSE ${WORKER_PORT}

# Workers run unprivileged like the gateway (Semgrep dockerfile missing-user): they hold no
# files, only a writable HOME for library caches.
RUN mkdir -p /tmp/worker-home && chown 1000:1000 /tmp/worker-home
ENV HOME=/tmp/worker-home
USER 1000:1000

# Health: one role serves its AgentCard at the root; several answer /healthz (each card under /<role>/).
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fs "http://localhost:${WORKER_PORT}/.well-known/agent-card.json" > /dev/null \
        || curl -fs "http://localhost:${WORKER_PORT}/healthz" > /dev/null || exit 1

ENTRYPOINT ["python", "-m", "workers.entrypoint"]
