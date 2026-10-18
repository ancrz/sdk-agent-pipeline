# Agent engine — base image: Google ADK + A2A + MCP + FastAPI/uvicorn.
#
# Build: docker build -t agent-engine/base:latest -f agents/base/Dockerfile .
# Base of the gateway and worker images (scripts/build.sh builds it first).

ARG PYTHON_BASE_IMAGE=python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY agents/base/requirements.txt .
# BuildKit pip cache: a requirements change re-resolves without re-downloading every wheel.
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY orchestrator/ orchestrator/

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Not run: each image built FROM it (gateway, workers) installs as root and then sets
# USER 1000:1000 itself.
# nosemgrep: dockerfile.security.missing-user-entrypoint.missing-user-entrypoint
ENTRYPOINT ["python", "-c", "print('Base image: run the gateway or a worker image'); raise SystemExit(1)"]
