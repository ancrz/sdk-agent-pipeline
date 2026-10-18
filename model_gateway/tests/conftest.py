"""Gateway tests exercise the local fallback with the executor's token; they keep the executor on
the local model unless a test asks for the deterministic routing (docs/pipeline/audit_*)."""

import pytest


@pytest.fixture(autouse=True)
def executor_on_the_local_model(monkeypatch):
    monkeypatch.setattr("model_gateway.app.LOCAL_DETERMINISTIC_ROLES", frozenset())
