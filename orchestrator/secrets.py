"""Secret backends (remediation R1): where credential inputs live.

- `local`: Fernet with VANGUARD_CREDENTIALS_KEY; the ciphertext sits in the
  credentials row (credentials.inputs_encrypted). The original store.
- `openbao`: KV v2 in OpenBao. The row keeps only a reference ("<mount>/<path>")
  plus non-secret metadata. Mounts by credential type: model_provider →
  vanguard-ai/, machine and vault → vanguard-automation/.

Each process authenticates with its own AppRole (VANGUARD_BAO_CONSUMER_DIR holds
role_id, secret_id and the CA) and can do only what its policy allows: the API
creates and updates but cannot read a value back; the model gateway reads
vanguard-ai/; the Controller reads vanguard-automation/. Other secret managers
(AWS, GCP, Azure) fit the same put/load/delete contract.

Reads fail closed: an unreachable or refusing store raises SecretStoreUnavailable,
never a fallback to another copy.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .credentials import CredentialError, decrypt_inputs, encrypt_inputs

BACKENDS = ("local", "openbao")
MOUNTS = {"model_provider": "vanguard-ai", "machine": "vanguard-automation", "vault": "vanguard-automation",
          "awx": "vanguard-automation", "windows": "vanguard-automation",
          # the SMTP module: read only by the mailer (docs/pipeline/evolution_20261016_230000.md)
          "smtp": "vanguard-mail", "dkim": "vanguard-mail"}


class SecretStoreUnavailable(CredentialError):
    """The configured store cannot be reached or refused the request."""


def configured_backend() -> str:
    backend = os.environ.get("VANGUARD_SECRET_BACKEND", "local").strip().lower() or "local"
    if backend not in BACKENDS:
        raise CredentialError(f"VANGUARD_SECRET_BACKEND must be one of {', '.join(BACKENDS)}")
    return backend


def mounts() -> dict[str, str]:
    """MOUNTS, with the store's own names when it uses others (vault-transit-stack's
    config/consumers.json): VANGUARD_BAO_MOUNTS='{"model_provider": "agent-ai"}'."""
    try:
        override = json.loads(os.environ.get("VANGUARD_BAO_MOUNTS") or "{}")
    except ValueError as exc:
        raise CredentialError("VANGUARD_BAO_MOUNTS must be a JSON object") from exc
    return {**MOUNTS, **override}


def ref_for(credential_type: str, name: str) -> str:
    return f"{mounts()[credential_type]}/credentials/{name}"


@dataclass(frozen=True)
class Stored:
    backend: str
    ref: Optional[str]          # openbao: "<mount>/<path>"
    ciphertext: Optional[str]   # local: the Fernet token


class OpenBaoClient:
    """Minimal KV v2 client with AppRole login, token reuse and a short read cache."""

    def __init__(self, addr: Optional[str] = None, consumer_dir: Optional[str] = None,
                 transport: Any = None, cache_s: Optional[float] = None) -> None:
        self.addr = (addr or os.environ.get("BAO_ADDR", "https://openbao-secrets:8200")).rstrip("/")
        self.dir = Path(consumer_dir or os.environ.get("VANGUARD_BAO_CONSUMER_DIR", "/run/vanguard/openbao"))
        self.transport = transport
        self.cache_s = float(os.environ.get("VANGUARD_SECRET_CACHE_S", "60") if cache_s is None else cache_s)
        self._token: Optional[str] = None
        self._token_expires = 0.0
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def _http(self):
        import httpx
        ca = self.dir / "ca.crt"
        return httpx.Client(timeout=10.0, transport=self.transport,
                            verify=str(ca) if ca.exists() else True)

    def _login(self, http) -> str:
        try:
            role_id = (self.dir / "role_id").read_text().strip()
            secret_id = (self.dir / "secret_id").read_text().strip()
        except OSError as exc:
            raise SecretStoreUnavailable(f"OpenBao AppRole credentials missing in {self.dir}") from exc
        r = http.post(f"{self.addr}/v1/auth/approle/login", json={"role_id": role_id, "secret_id": secret_id})
        if r.status_code != 200:
            raise SecretStoreUnavailable(f"OpenBao login refused ({r.status_code})")
        auth = r.json()["auth"]
        self._token = auth["client_token"]
        self._token_expires = time.monotonic() + 0.8 * float(auth.get("lease_duration") or 3600)
        return self._token

    def _request(self, method: str, path: str, **kwargs) -> Any:
        import httpx
        with self._lock:
            try:
                with self._http() as http:
                    for attempt in (1, 2):
                        token = self._token if self._token and time.monotonic() < self._token_expires else self._login(http)
                        r = http.request(method, f"{self.addr}/v1/{path}", headers={"X-Vault-Token": token}, **kwargs)
                        if r.status_code == 403 and attempt == 1:
                            self._token = None          # expired or revoked: log in again once
                            continue
                        return r
            except httpx.HTTPError as exc:
                raise SecretStoreUnavailable(f"OpenBao unreachable: {type(exc).__name__}") from exc
        raise SecretStoreUnavailable("OpenBao refused the request")

    def write(self, ref: str, data: dict[str, Any]) -> int:
        mount, path = ref.split("/", 1)
        r = self._request("POST", f"{mount}/data/{path}", json={"data": data})
        if r.status_code not in (200, 204):
            raise SecretStoreUnavailable(f"OpenBao write refused ({r.status_code})")
        self._cache.pop(ref, None)
        return int((r.json().get("data") or {}).get("version") or 0) if r.content else 0

    def read(self, ref: str) -> dict[str, Any]:
        hit = self._cache.get(ref)
        if hit and time.monotonic() - hit[0] < self.cache_s:
            return dict(hit[1])
        mount, path = ref.split("/", 1)
        try:
            r = self._request("GET", f"{mount}/data/{path}")
        except SecretStoreUnavailable:
            # stale-if-error: OpenBao sealed or unreachable → the last value read, for a while,
            # so a store outage does not stop model calls (VANGUARD_SECRET_STALE_S, 0 = off).
            stale_s = float(os.environ.get("VANGUARD_SECRET_STALE_S", "3600"))
            if hit and time.monotonic() - hit[0] < stale_s:
                return dict(hit[1])
            raise
        if r.status_code == 404:
            raise CredentialError("Credential not found in the secret store")
        if r.status_code != 200:
            raise SecretStoreUnavailable(f"OpenBao read refused ({r.status_code})")
        data = r.json()["data"]["data"]
        self._cache[ref] = (time.monotonic(), dict(data))     # also the stale-if-error copy
        return data

    def version(self, ref: str) -> int:
        """Current version from metadata (no secret value); 0 when absent."""
        mount, path = ref.split("/", 1)
        r = self._request("GET", f"{mount}/metadata/{path}")
        if r.status_code == 404:
            return 0
        if r.status_code != 200:
            raise SecretStoreUnavailable(f"OpenBao metadata read refused ({r.status_code})")
        return int(r.json()["data"].get("current_version") or 0)

    def delete(self, ref: str) -> None:
        mount, path = ref.split("/", 1)
        r = self._request("DELETE", f"{mount}/metadata/{path}")
        if r.status_code not in (200, 204, 404):
            raise SecretStoreUnavailable(f"OpenBao delete refused ({r.status_code})")
        self._cache.pop(ref, None)

    IDENTITY_MOUNT = "vanguard-identity/"

    def identity(self, method: str, path: str, json: Optional[dict[str, Any]] = None) -> tuple[int, dict[str, Any]]:
        """A call to the LDAP secrets engine (M2, docs/pipeline/evolution_20261014_164500.md §1), the one
        mount the API may read (static-cred) and configure. Any other path is refused here."""
        # One LDAP engine per directory (docs/pipeline/evolution_20261016_210000.md): vanguard-identity/ and the
        # slots vanguard-identity-2/ … created by scripts/agent_openbao_init.sh
        import re as _re
        if not _re.match(r"^vanguard-identity(-[2-9]|-[1-9][0-9])?/", path) or ".." in path:
            raise CredentialError("Only the identity engines (vanguard-identity/, vanguard-identity-N/) are reachable this way")
        r = self._request(method, path, **({"json": json} if json is not None else {}))
        try:
            body = r.json() if r.content else {}
        except ValueError:
            body = {}
        return r.status_code, body

    def health(self) -> dict[str, Any]:
        """Unauthenticated sys/health: initialised, sealed, version."""
        import httpx
        try:
            with self._http() as http:
                r = http.get(f"{self.addr}/v1/sys/health", params={"sealedcode": 200, "uninitcode": 200})
            body = r.json()
            return {"reachable": True, "initialized": body.get("initialized"), "sealed": body.get("sealed"),
                    "version": body.get("version")}
        except (httpx.HTTPError, ValueError):
            return {"reachable": False}


_client: Optional[OpenBaoClient] = None


def client() -> OpenBaoClient:
    global _client
    if _client is None:
        _client = OpenBaoClient()
    return _client


def set_client(value: Optional[OpenBaoClient]) -> None:
    """Tests: install a client with a fake transport (None resets)."""
    global _client
    _client = value


def put(credential_type: str, name: str, inputs: dict[str, Any], backend: Optional[str] = None) -> Stored:
    backend = backend or configured_backend()
    if backend == "local":
        return Stored("local", None, encrypt_inputs(inputs))
    ref = ref_for(credential_type, name)
    client().write(ref, inputs)
    return Stored("openbao", ref, None)


def load(backend: Optional[str], ref: Optional[str], ciphertext: Optional[str]) -> dict[str, Any]:
    if (backend or "local") == "local":
        if not ciphertext:
            raise CredentialError("Credential has no stored value")
        return decrypt_inputs(ciphertext)
    if not ref:
        raise CredentialError("Credential has no secret-store reference")
    return client().read(ref)


async def aput(credential_type: str, name: str, inputs: dict[str, Any], backend: Optional[str] = None) -> Stored:
    return await asyncio.to_thread(put, credential_type, name, inputs, backend)


async def aload(backend: Optional[str], ref: Optional[str], ciphertext: Optional[str]) -> dict[str, Any]:
    return await asyncio.to_thread(load, backend, ref, ciphertext)
