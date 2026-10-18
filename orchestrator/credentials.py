"""AWX-style credential inputs, encrypted at rest with Fernet (VANGUARD_CREDENTIALS_KEY).

The API encrypts when an admin stores a credential and never returns inputs
("$encrypted$", like AWX). Only the Controller decrypts, at launch, to hand the
secret to ansible-runner. Workers never hold the key.

Rotation: set VANGUARD_CREDENTIALS_KEY_OLD to the current key and
VANGUARD_CREDENTIALS_KEY to a new one, run scripts/rotate_credentials_key.py
(re-encrypts every row in one transaction), then drop the old key.
"""

from __future__ import annotations

import json
import os
from typing import Any

ENCRYPTED = "$encrypted$"

# Allowed input fields per credential type (AWX Machine / Vault subsets)
INPUT_FIELDS: dict[str, tuple[str, ...]] = {
    "machine": ("username", "password", "ssh_key_data", "become_password"),
    "vault": ("vault_password",),
    # Model provider (Phase F3): decrypted by the API's model resolver and the model gateway,
    # never by workers; machine/vault stay Controller-only.
    "model_provider": ("provider", "api_key", "project", "location", "credentials_ref", "tier", "base_url"),
    # An upstream AWX (docs/pipeline/aap_*, awx_*): read only by the Controller, which
    # runs approved jobs there when VANGUARD_EXECUTION_BACKEND=awx.
    "awx": ("url", "token", "api_prefix", "organization", "inventory", "credential", "project", "verify_ssl"),
    # Windows over PSRP + Kerberos (docs/pipeline/evolution_20261014_154541.md §3.6): a domain account
    # (UPN or name), its realm and KDCs; the Controller writes a krb5.conf and a ticket cache per job.
    # trusted_realms (optional): the realms this account reaches through AD trusts, with their KDCs
    # ("FILIAL.EXAMPLE=dc1.filial.example,dc2; OTHER.EXAMPLE=10.0.0.5"; docs/pipeline/evolution_20261018_150000.md).
    "windows": ("username", "password", "realm", "kdc", "trusted_realms"),
    # The SMTP module (docs/pipeline/evolution_20261016_230000.md): the relay account and the DKIM private key,
    # stored in vanguard-mail/ and read only by the mailer service (the API writes, never reads).
    "smtp": ("username", "password"),
    "dkim": ("private_key",),
}
# Gemini natively; the others through the gateway's OpenAI-compatible adapter (model_gateway/providers.py).
MODEL_PROVIDERS = ("gemini", "vertex_ai", "openai", "anthropic", "deepseek", "openai_compatible", "local")
TIERS = ("free", "paid")


class CredentialError(ValueError):
    pass


def _kdc_list(text: str) -> list[str]:
    kdcs = [k.strip() for k in str(text or "").split(",") if k.strip()]
    if not kdcs or any(" " in k or "/" in k or "\n" in k or "=" in k or ";" in k for k in kdcs):
        raise CredentialError("kdc lists the KDCs (DCs) by name or IP, separated by commas")
    return kdcs


def trusted_realms(text: Any) -> dict[str, list[str]]:
    """'REALM=kdc1,kdc2; OTHER=kdc3' → {"REALM": ["kdc1", "kdc2"], "OTHER": ["kdc3"]} (empty when unset).
    Realms in capitals, KDCs by name or IP; a malformed entry raises CredentialError."""
    out: dict[str, list[str]] = {}
    for entry in (e.strip() for e in str(text or "").split(";")):
        if not entry:
            continue
        realm, sep, kdcs = entry.partition("=")
        realm = realm.strip()
        if not sep or not realm or realm != realm.upper() or " " in realm or "." not in realm:
            raise CredentialError("trusted_realms is REALM=kdc1,kdc2 entries separated by ';', realms in capitals")
        if realm in out:
            raise CredentialError(f"trusted_realms names {realm} twice")
        out[realm] = _kdc_list(kdcs)
    return out


def _fernet(key_env: str = "VANGUARD_CREDENTIALS_KEY"):
    from cryptography.fernet import Fernet

    key = os.environ.get(key_env, "")
    if not key:
        raise CredentialError(f"{key_env} is not set")
    return Fernet(key.encode())


def check_inputs(credential_type: str, inputs: dict[str, Any]) -> None:
    allowed = INPUT_FIELDS.get(credential_type)
    if allowed is None:
        raise CredentialError(f"Unknown credential type '{credential_type}'")
    unknown = set(inputs) - set(allowed)
    if unknown:
        raise CredentialError(f"Unknown inputs for {credential_type}: {', '.join(sorted(unknown))}")
    if credential_type == "windows":
        if not str(inputs.get("username") or "").strip() or not str(inputs.get("password") or ""):
            raise CredentialError("A windows credential needs username and password")
        realm = str(inputs.get("realm") or "")
        if not realm or realm != realm.upper() or " " in realm:
            raise CredentialError("realm is the Kerberos realm in capitals, e.g. CORP.EXAMPLE.COM")
        _kdc_list(str(inputs.get("kdc") or ""))
        if realm in trusted_realms(inputs.get("trusted_realms")):
            raise CredentialError("trusted_realms lists other realms, not the account's own")
    if credential_type == "awx":
        if not str(inputs.get("url") or "").startswith(("http://", "https://")) or not str(inputs.get("token") or "").strip():
            raise CredentialError("An awx credential needs url (http/https) and token")
    if credential_type == "model_provider":
        provider = inputs.get("provider")
        if provider not in MODEL_PROVIDERS:
            raise CredentialError(f"provider must be one of {', '.join(MODEL_PROVIDERS)}")
        if provider not in ("vertex_ai", "local") and not str(inputs.get("api_key") or "").strip():
            raise CredentialError(f"A {provider} provider needs api_key")
        if inputs.get("tier") not in (None, "", *TIERS):
            raise CredentialError(f"tier must be one of {', '.join(TIERS)}")
        if provider == "openai_compatible":
            url = str(inputs.get("base_url") or "")
            if not url.startswith("https://"):
                raise CredentialError("An openai_compatible provider needs an https:// base_url")
        if provider == "local":
            # A model server inside the stack or on the host (Ollama, llama.cpp, vLLM, Lemonade):
            # plain http is accepted because the traffic stays on an internal network.
            url = str(inputs.get("base_url") or "")
            if not url.startswith(("http://", "https://")):
                raise CredentialError("A local provider needs a base_url, e.g. http://ollama:11434/v1")
        if provider == "vertex_ai" and not (inputs.get("project") and inputs.get("location")):
            raise CredentialError("A vertex_ai provider needs project and location (auth: IAM / ADC, no key)")


def public_summary(credential_type: str, inputs: dict[str, Any]) -> dict[str, str]:
    """Non-secret facts the UI may show without decrypting (provider, key last 4, fingerprint)."""
    if credential_type == "windows":
        return {k: str(inputs[k]) for k in ("username", "realm", "kdc", "trusted_realms") if inputs.get(k)}
    if credential_type == "smtp":                    # the relay account (docs/pipeline/evolution_20261016_230000.md)
        return {"username": str(inputs["username"])} if inputs.get("username") else {}
    if credential_type == "machine":                 # the account a job runs as (G-CRED-ENV "runs as")
        return {"username": str(inputs["username"])} if inputs.get("username") else {}
    if credential_type != "model_provider":
        return {}
    import hashlib
    out = {k: str(inputs[k]) for k in ("provider", "project", "location", "tier", "base_url") if inputs.get(k)}
    key = str(inputs.get("api_key") or "")
    if key:
        out["key_last4"] = key[-4:]
        out["fingerprint"] = "sha256:" + hashlib.sha256(key.encode()).hexdigest()[:12]
    return out


def encrypt_inputs(inputs: dict[str, Any], key_env: str = "VANGUARD_CREDENTIALS_KEY") -> str:
    return _fernet(key_env).encrypt(json.dumps(inputs).encode()).decode()


def decrypt_inputs(token: str, key_env: str = "VANGUARD_CREDENTIALS_KEY") -> dict[str, Any]:
    from cryptography.fernet import InvalidToken

    try:
        return json.loads(_fernet(key_env).decrypt(token.encode()))
    except InvalidToken as exc:
        raise CredentialError("Credential cannot be decrypted with the configured key") from exc


def redacted(inputs_keys: list[str]) -> dict[str, str]:
    return {k: ENCRYPTED for k in inputs_keys}
