"""The OpenBao client (orchestrator/secrets.py) against vault-transit-stack's default policies.

Ported from vanguard-control's api/tests/test_secret_backend.py (the parts without the API): the
policy matrix, fail-closed reads, stale-if-error, and the engine's mount names (VANGUARD_BAO_MOUNTS)."""

from __future__ import annotations

import json

import httpx
import pytest
from cryptography.fernet import Fernet

from orchestrator import secrets

KEY = "AIzaSyOPENBAO-test-key-0000000000000xyz9"
POLICIES = {  # the same rules vault-transit-stack's config/policies/agent-*.hcl write
    "writer": {"data": {"create", "update"}, "metadata": {"read", "delete", "list"}, "mounts": {"agent-ai"}},
    "gateway": {"data": {"read"}, "metadata": set(), "mounts": {"agent-ai"}},
}


class FakeOpenBao:
    """KV v2 + AppRole login, enforcing the per-consumer policies."""

    def __init__(self):
        self.kv: dict[str, list[dict]] = {}
        self.logins = 0
        self.reads: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1/")
        if path == "auth/approle/login":
            role = json.loads(request.content)["role_id"]
            self.logins += 1
            return httpx.Response(200, json={"auth": {"client_token": f"tok-{role}", "lease_duration": 3600}})
        if path == "sys/health":
            return httpx.Response(200, json={"initialized": True, "sealed": False, "version": "2.7.0"})
        role = request.headers.get("x-vault-token", "").removeprefix("tok-")
        mount, kind, key = path.split("/", 2)
        policy = POLICIES.get(role)
        op = {"POST": "create", "GET": "read", "DELETE": "delete"}[request.method]
        if policy is None or mount not in policy["mounts"] or op not in policy[kind]:
            return httpx.Response(403, json={"errors": ["permission denied"]})
        ref = f"{mount}/{key}"
        if kind == "data" and request.method == "POST":
            self.kv.setdefault(ref, []).append(json.loads(request.content)["data"])
            return httpx.Response(200, json={"data": {"version": len(self.kv[ref])}})
        if ref not in self.kv:
            return httpx.Response(404, json={"errors": []})
        if kind == "data":
            self.reads.append((role, ref))
            return httpx.Response(200, json={"data": {"data": self.kv[ref][-1]}})
        if request.method == "DELETE":
            self.kv.pop(ref)
            return httpx.Response(204)
        return httpx.Response(200, json={"data": {"current_version": len(self.kv[ref])}})


@pytest.fixture
def bao(monkeypatch, tmp_path):
    fake = FakeOpenBao()

    def consumer(name: str) -> secrets.OpenBaoClient:
        d = tmp_path / name
        d.mkdir(exist_ok=True)
        (d / "role_id").write_text(name)
        (d / "secret_id").write_text("s")
        return secrets.OpenBaoClient(addr="https://openbao:8200", consumer_dir=str(d),
                                     transport=httpx.MockTransport(fake), cache_s=0)

    fake.consumer = consumer
    monkeypatch.setenv("VANGUARD_CREDENTIALS_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("VANGUARD_SECRET_BACKEND", "openbao")
    secrets.set_client(consumer("writer"))
    yield fake
    secrets.set_client(None)


def test_policy_matrix_through_the_client(bao):
    writer, gateway = bao.consumer("writer"), bao.consumer("gateway")
    writer.write("agent-ai/credentials/g", {"api_key": KEY})
    assert writer.version("agent-ai/credentials/g") == 1
    with pytest.raises(secrets.SecretStoreUnavailable):
        writer.read("agent-ai/credentials/g")                       # the writer is write-only
    assert gateway.read("agent-ai/credentials/g") == {"api_key": KEY}
    with pytest.raises(secrets.SecretStoreUnavailable):
        gateway.write("agent-ai/credentials/g", {"api_key": "other"})  # the gateway only reads


def test_the_engine_names_its_mounts(monkeypatch):
    monkeypatch.delenv("VANGUARD_BAO_MOUNTS", raising=False)
    assert secrets.ref_for("model_provider", "g") == "vanguard-ai/credentials/g"     # vanguard-control's layout
    monkeypatch.setenv("VANGUARD_BAO_MOUNTS", '{"model_provider": "agent-ai"}')
    assert secrets.ref_for("model_provider", "g") == "agent-ai/credentials/g"
    monkeypatch.setenv("VANGUARD_BAO_MOUNTS", "not json")
    with pytest.raises(secrets.CredentialError):
        secrets.ref_for("model_provider", "g")


def test_put_goes_to_the_named_mount(bao, monkeypatch):
    monkeypatch.setenv("VANGUARD_BAO_MOUNTS", '{"model_provider": "agent-ai"}')
    secrets.set_client(bao.consumer("writer"))
    stored = secrets.put("model_provider", "g", {"provider": "gemini", "api_key": KEY})
    assert stored.backend == "openbao" and stored.ref == "agent-ai/credentials/g" and stored.ciphertext is None
    assert bao.consumer("gateway").read(stored.ref) == {"provider": "gemini", "api_key": KEY}


def test_unreachable_store_fails_closed(tmp_path):
    def down(request):
        raise httpx.ConnectError("refused")
    (tmp_path / "role_id").write_text("gateway")
    (tmp_path / "secret_id").write_text("s")
    c = secrets.OpenBaoClient(addr="https://openbao:8200", consumer_dir=str(tmp_path), transport=httpx.MockTransport(down))
    with pytest.raises(secrets.SecretStoreUnavailable):
        c.read("vanguard-ai/credentials/g")
    assert c.health() == {"reachable": False}


def test_a_secret_store_outage_serves_the_last_value_for_a_while(tmp_path, monkeypatch):
    """Resilience: OpenBao sealed or unreachable → the last value read, up to VANGUARD_SECRET_STALE_S."""
    (tmp_path / "role_id").write_text("gateway")
    (tmp_path / "secret_id").write_text("s")
    state = {"up": True}

    def bao(request):
        if not state["up"]:
            raise httpx.ConnectError("sealed", request=request)
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"auth": {"client_token": "t", "lease_duration": 3600}})
        return httpx.Response(200, json={"data": {"data": {"api_key": "k1"}}})

    c = secrets.OpenBaoClient(addr="https://openbao:8200", consumer_dir=str(tmp_path),
                              transport=httpx.MockTransport(bao), cache_s=0)
    assert c.read("vanguard-ai/model_provider/x") == {"api_key": "k1"}
    state["up"] = False
    assert c.read("vanguard-ai/model_provider/x") == {"api_key": "k1"}          # stale, still answering
    monkeypatch.setenv("VANGUARD_SECRET_STALE_S", "0")
    with pytest.raises(secrets.SecretStoreUnavailable):
        c.read("vanguard-ai/model_provider/x")
