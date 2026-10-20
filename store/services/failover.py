"""Model failover: the core never stays without an active model.

For each call the gateway walks a chain of routes, in order:

1. primary   the role's route in the active preset (profile → default → .env), its
             model then its fallback model on the same account;
2. backups   other accounts (ai.failover.backups, ordered by the admin): a second key
             of the same provider (its own quota) or another provider;
3. local     the local model (ai.local_fallback), no internet needed;
4. offline   the deterministic stand-in (ai.failover.last_resort): tools, grants, the
             Controller and SSH stay real; only the next-step decision is fixed.

What moves a call down the chain depends on why the route failed (see for the full case list):
- the provider or the network failed (outage, timeout, no internet, invalid or
  revoked key, daily quota, billing, retired model, secret store down) → every tier;
- the gateway's own per-minute limit → other cloud accounts only (they have their
  own quota); never a silent downgrade to the stand-in in normal operation;
- the role's daily token budget (a cost decision) → the local model only;
- the request itself is wrong (400) or the answer is blocked → no failover.

Embeddings fail over only to the same embedding model on another Google account:
the vector space is pinned, a different model would corrupt the index.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.automation import AppSetting, Credential
from .models import ResolvedModel, resolve

FAILOVER_KEY = "ai.failover"
DEFAULTS: dict[str, Any] = {"backups": [], "last_resort": True, "failback": "auto"}
FAILBACK = ("auto", "manual")   # keepalived's preempt / nopreempt
EMBEDDING_PROVIDERS = ("gemini", "vertex_ai")
MAX_BACKUPS = 8


@dataclass
class Route:
    """One link of the chain."""
    tier: str                         # primary | backup | local | offline
    resolved: ResolvedModel
    models: list[str] = field(default_factory=list)

    @property
    def cloud(self) -> bool:
        return self.resolved.provider not in ("local", "offline")

    def label(self) -> str:
        return f"{self.tier}:{self.resolved.account}/{self.models[0] if self.models else self.resolved.model}"


async def config(db: AsyncSession) -> dict[str, Any]:
    setting = await db.get(AppSetting, FAILOVER_KEY)
    value = dict(setting.value) if setting and isinstance(setting.value, dict) else {}
    failback = value.get("failback")
    return {"backups": list(value.get("backups") or [])[:MAX_BACKUPS],
            "last_resort": bool(value.get("last_resort", DEFAULTS["last_resort"])),
            "failback": failback if failback in FAILBACK else DEFAULTS["failback"]}


async def set_config(db: AsyncSession, backups: list[dict[str, Optional[str]]], last_resort: bool, actor: str,
                     failback: str = "auto") -> dict:
    value = {"backups": [{"credential": b["credential"], "model": b.get("model") or None} for b in backups][:MAX_BACKUPS],
             "last_resort": bool(last_resort), "failback": failback if failback in FAILBACK else "auto"}
    setting = await db.get(AppSetting, FAILOVER_KEY)
    if setting is None:
        db.add(AppSetting(key=FAILOVER_KEY, value=value, updated_by=actor))
    else:
        setting.value, setting.updated_by = value, actor
    return value


async def _credential(db: AsyncSession, name: str) -> Optional[Credential]:
    cred = (await db.execute(select(Credential).where(Credential.name == name))).scalar_one_or_none()
    return cred if cred is not None and cred.credential_type == "model_provider" else None


def _resolved_for(role: str, cred: Credential, model: str) -> ResolvedModel:
    summary = cred.summary or {}
    return ResolvedModel(role=role, model=model, provider=summary.get("provider", "gemini"), source="failover",
                         tier=summary.get("tier"), account=cred.name,
                         _stored=(cred.backend or "local", cred.secret_ref, cred.inputs_encrypted))


async def chain(db: AsyncSession, role: str, embeddings: bool = False, streaming: bool = False,
                serving: Optional[str] = None) -> list[Route]:
    """The ordered routes for one call of `role`. `serving` is who answers the role now
    (`tier:account`, the gateway's state): with failback `manual`, a backup that took over keeps
    going first until an admin returns to the primary."""
    routes = await _chain(db, role, embeddings, streaming)
    if serving and serving.startswith("backup:") and (await config(db))["failback"] == "manual":
        account = serving.split(":", 1)[1]
        first = next((r for r in routes if r.tier == "backup" and r.resolved.account == account), None)
        if first is not None:
            routes = [first] + [r for r in routes if r is not first]
    return routes


async def _chain(db: AsyncSession, role: str, embeddings: bool, streaming: bool) -> list[Route]:
    primary = await resolve(db, "embeddings" if embeddings else role)
    models = [primary.model]
    if primary.fallback_model and primary.fallback_model != primary.model and not streaming:
        models.append(primary.fallback_model)
    routes = [Route("primary", primary, models)]
    if primary.provider == "offline":
        return routes                                       # the offline preset never fails
    seen = {(primary.account, m) for m in models}
    cfg = await config(db)
    for b in cfg["backups"]:
        cred = await _credential(db, b.get("credential") or "")
        if cred is None:
            continue
        provider = (cred.summary or {}).get("provider", "gemini")
        if embeddings:
            if provider not in EMBEDDING_PROVIDERS:
                continue
            model = primary.model                           # same vector space or nothing
        else:
            model = b.get("model") or primary.model
        if (cred.name, model) in seen:
            continue
        seen.add((cred.name, model))
        routes.append(Route("backup", _resolved_for(primary.role, cred, model), [model]))
    if embeddings:
        return routes
    from .presets import local_fallback
    local = await local_fallback(db)
    if local:
        cred = await _credential(db, local["credential"])
        if cred is not None and (cred.name, local["model"]) not in seen:
            routes.append(Route("local", _resolved_for(primary.role, cred, local["model"]), [local["model"]]))
    if cfg["last_resort"]:
        stand_in = ResolvedModel(role=primary.role, model=f"deterministic-{primary.role}", provider="offline",
                                 source="failover", account="offline")
        routes.append(Route("offline", stand_in, [stand_in.model]))
    return routes


async def preview(db: AsyncSession, roles: tuple[str, ...]) -> dict[str, list[dict[str, Any]]]:
    """Each role's chain as plain data (the UI)."""
    out = {}
    for role in roles:
        routes = await chain(db, role, embeddings=role == "embeddings")
        out[role] = [{"tier": r.tier, "account": r.resolved.account, "provider": r.resolved.provider,
                      "models": r.models} for r in routes]
    return out
