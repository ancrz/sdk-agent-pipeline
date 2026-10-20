"""Model resolution per role: profile → default profile → .env.

A role (orchestrator, executor, validator, documentalist, author, embeddings) maps to
a model and, optionally, a model_provider credential from the Vault. With no
profiles the result is exactly today's behaviour: GOOGLE_API_KEY and
ENGINE_MODEL_<ROLE> from the environment, so the current key and the smokes keep
working. This is the only API code that decrypts a credential, and only one of
type model_provider (machine/vault credentials stay Controller-only).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator import secrets
from orchestrator.models import fallback_model, role_model

from ..models.automation import Credential, ModelProfile

logger = logging.getLogger(__name__)

ENV_DEFAULT_MODELS = {"embeddings": "gemini-embedding-001"}


@dataclass(frozen=True)
class ResolvedModel:
    role: str
    model: str
    provider: str = "gemini"
    source: str = "env"                      # profile | default | env
    fallback_model: Optional[str] = None
    rpm: Optional[int] = None
    tier: Optional[str] = None                # free | paid, from the credential (non-secret)
    account: str = "env"                      # the credential's name; provider limits are per account and model
    _secret: dict[str, Any] = field(default_factory=dict, repr=False)
    # (backend, ref, ciphertext) of the profile's credential, read only when needed: the
    # API resolves models without reading keys (its OpenBao policy cannot); the gateway reads.
    _stored: Optional[tuple[str, Optional[str], Optional[str]]] = field(default=None, repr=False)

    def secret(self) -> dict[str, Any]:
        if not self._secret and self._stored:
            object.__setattr__(self, "_secret", secrets.load(*self._stored))
        return self._secret

    async def asecret(self) -> dict[str, Any]:
        if not self._secret and self._stored:
            object.__setattr__(self, "_secret", await secrets.aload(*self._stored))
        return self._secret

    def client_kwargs(self) -> dict[str, Any]:
        """Arguments for google.genai.Client. Empty = the SDK's environment defaults (.env)."""
        self.secret()
        if self.provider == "vertex_ai":
            return {"vertexai": True, "project": self._secret.get("project"), "location": self._secret.get("location")}
        if self._secret.get("api_key"):
            return {"api_key": self._secret["api_key"]}
        return {}


def gateway_kwargs(role: str) -> Optional[dict[str, Any]]:
    """google-genai Client arguments that send the API's own calls through the model gateway.

    Set when MODEL_GATEWAY_URL and ENGINE_GATEWAY_TOKEN_<ROLE> are configured (compose does
    it): the gateway then enforces the role's model, injects the provider credential and audits
    the call, and the API process holds no provider key. None = call the provider directly."""
    url = os.environ.get("MODEL_GATEWAY_URL", "").strip()
    token = os.environ.get(f"ENGINE_GATEWAY_TOKEN_{role.upper()}", "").strip()
    if not url or len(token) < 16:
        return None
    return {"api_key": token, "http_options": {"base_url": url}}


def api_client_kwargs(resolved: ResolvedModel) -> dict[str, Any]:
    """What the API passes to google.genai.Client for a resolved role: the gateway, else direct."""
    return gateway_kwargs(resolved.role) or resolved.client_kwargs()


def _env_int(name: str) -> Optional[int]:
    raw = os.environ.get(name, "").strip()
    return max(0, int(raw)) if raw else None


def effective_rpm(resolved: ResolvedModel) -> int:
    """Requests per minute the gateway allows the role (0 = unlimited): the profile's rpm →
    ENGINE_GATEWAY_RPM_<ROLE> → ENGINE_GATEWAY_RPM → the account tier's default
    (ENGINE_GATEWAY_RPM_FREE, default 10; ENGINE_GATEWAY_RPM_PAID, default 0)."""
    if resolved.provider == "offline":
        return 0
    for value in (resolved.rpm, _env_int(f"ENGINE_GATEWAY_RPM_{resolved.role.upper()}"),
                  _env_int("ENGINE_GATEWAY_RPM")):
        if value is not None:
            return value
    if resolved.tier == "free":
        return _env_int("ENGINE_GATEWAY_RPM_FREE") if _env_int("ENGINE_GATEWAY_RPM_FREE") is not None else 10
    if resolved.tier == "paid":
        return _env_int("ENGINE_GATEWAY_RPM_PAID") or 0
    return 0


def effective_rpd(resolved: ResolvedModel) -> int:
    """Requests per day the gateway lets one account spend on one model (0 = unlimited T8): ENGINE_GATEWAY_RPD_FREE / ENGINE_GATEWAY_RPD_PAID by the
    account's tier, off by default. Set it to the provider's daily quota (for example Gemini's
    free tier) so the account is skipped before the provider's daily 429; after a 429 the
    breaker already skips it until the quota resets."""
    if resolved.provider in ("offline", "local"):
        return 0
    name = {"free": "ENGINE_GATEWAY_RPD_FREE", "paid": "ENGINE_GATEWAY_RPD_PAID"}.get(resolved.tier or "")
    return (_env_int(name) or 0) if name else 0


def _env_model(role: str) -> str:
    if role == "orchestrator":
        return os.environ.get("ENGINE_MODEL_PLANNER") or role_model("orchestrator")
    if role == "embeddings":
        return os.environ.get("ENGINE_EMBEDDING_MODEL") or ENV_DEFAULT_MODELS["embeddings"]
    return role_model(role)


TOUCH_EVERY_S = 30


async def _touch(db: AsyncSession, preset: str, role: str) -> None:
    """Stamp the profile's last_used_at (informative, best effort).

    On Postgres it is written in its own short transaction that skips a locked row and runs at most
    every TOUCH_EVERY_S: written in the caller's transaction, the row lock lasted until the caller
    committed, and callers that resolve and then wait for a model (the knowledge indexer, the
    planner, the chat) held it for up to a minute, blocking every other resolve and profile edit."""
    now = datetime.now(timezone.utc)
    if db.bind is None or db.bind.dialect.name != "postgresql":
        await db.execute(update(ModelProfile).where(ModelProfile.preset == preset, ModelProfile.role == role)
                         .values(last_used_at=now))
        return
    from sqlalchemy import or_, select as sa_select
    stale = or_(ModelProfile.last_used_at.is_(None), ModelProfile.last_used_at < now - timedelta(seconds=TOUCH_EVERY_S))
    free_row = (sa_select(ModelProfile.preset).where(ModelProfile.preset == preset, ModelProfile.role == role, stale)
                .with_for_update(skip_locked=True).scalar_subquery())
    try:
        async with AsyncSession(db.bind) as own:
            await own.execute(update(ModelProfile).where(ModelProfile.preset == preset, ModelProfile.role == role,
                                                         ModelProfile.preset == free_row)
                              .values(last_used_at=now))
            await own.commit()
    except Exception as exc:                 # a stamp is never worth failing a model call
        logger.debug("last_used_at not stamped for %s/%s: %s", preset, role, type(exc).__name__)


async def resolve(db: AsyncSession, role: str) -> ResolvedModel:
    """The role's model in the active preset: the role's profile → the
    preset's `default` profile → .env. An offline preset resolves every role but
    embeddings to the gateway's deterministic stand-in."""
    from . import presets

    preset = await presets.active(db)
    if preset.offline and role != "embeddings":
        return ResolvedModel(role=role, model=f"deterministic-{role}", provider="offline", source="preset")
    for key, source in ((role, "profile"), ("default", "default")):
        profile = await db.get(ModelProfile, (preset.name, key))
        if profile is None or not profile.active:
            continue
        stored = None
        provider = "gemini"
        tier = None
        account = "env"
        if profile.credential_id:
            cred = await db.get(Credential, profile.credential_id)
            if cred is None or cred.credential_type != "model_provider":
                continue  # a dangling or wrong-type reference never leaks another credential
            stored = (cred.backend or "local", cred.secret_ref, cred.inputs_encrypted)
            provider = (cred.summary or {}).get("provider", "gemini")
            tier = (cred.summary or {}).get("tier")
            account = cred.name
        await _touch(db, preset.name, key)
        model = profile.model if source == "profile" else (profile.model if role != "embeddings" else _env_model(role))
        return ResolvedModel(role=role, model=model, provider=provider, source=source,
                             fallback_model=profile.fallback_model or fallback_model(), rpm=profile.rpm,
                             tier=tier, account=account, _stored=stored)
    return ResolvedModel(role=role, model=_env_model(role), fallback_model=fallback_model())
