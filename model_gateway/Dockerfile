# Model gateway: holds the model provider credentials; workers only get a gateway token.
ARG BASE_IMAGE=agent-engine/base:latest
FROM ${BASE_IMAGE}

ARG MODEL_GATEWAY_PORT=8095
ENV MODEL_GATEWAY_PORT=${MODEL_GATEWAY_PORT} PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# The data contract (store/: model resolution, presets, failover, prices, agent teams,
# notifications, the schema bootstrap and the admin CLI) and the runtime shared with the
# workers (orchestrator/).
COPY store/ store/
COPY orchestrator/ orchestrator/
COPY model_gateway/ model_gateway/

USER 1000:1000
EXPOSE ${MODEL_GATEWAY_PORT}
HEALTHCHECK --interval=15s --timeout=5s --retries=5 --start-period=10s \
    CMD curl -sf "http://127.0.0.1:${MODEL_GATEWAY_PORT}/healthz" || exit 1
ENTRYPOINT []
CMD ["sh", "-c", "exec uvicorn model_gateway.app:app --host 0.0.0.0 --port ${MODEL_GATEWAY_PORT} --workers ${MODEL_GATEWAY_WORKERS:-1} --no-server-header"]
