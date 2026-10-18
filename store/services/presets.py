"""AI presets (remediation R2): which set of model profiles is active.

The active preset (app_settings "ai.active_preset") is read on every model
resolution, so switching it (PUT /api/admin/ai/preset) takes effect on the next
call for the API, the workers and the knowledge server alike, without restarts:
all of them reach models through the gateway, and the gateway resolves here.

kind "offline": the gateway answers with the deterministic stand-in (no provider,
no quota). The embeddings role never follows presets: the knowledge index keeps
one vector space (VANGUARD_EMBEDDING_MODE), so a preset switch cannot mix spaces.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.automation import ACTIVE_PRESET_KEY, AiPreset, AppSetting

BUILTIN = (
    ("offline", "offline", "Deterministic stand-in answered by the gateway: no provider call, no quota (smokes)."),
    ("demo-free", "routed", "Free-tier accounts (for example Google AI Studio): low RPM, for demos and limit tests."),
    ("paid", "routed", "By-use accounts: provider limits, costs per token."),
    ("custom", "routed", "Profiles made before presets existed, or any mix."),
    ("local", "routed", "Everything on a local model server (for example Gemma 4 on Ollama): works without internet."),
)


@dataclass(frozen=True)
class Preset:
    name: str
    kind: str

    @property
    def offline(self) -> bool:
        return self.kind == "offline"


DEFAULT = Preset("custom", "routed")


def initial_preset() -> str:
    """The preset seeded on first start: offline when the environment asks for deterministic mode."""
    return "offline" if os.environ.get("VANGUARD_LLM_MODE", "").strip().lower() == "deterministic" else "custom"


async def seed_presets(db: AsyncSession) -> None:
    for name, kind, description in BUILTIN:
        if await db.get(AiPreset, name) is None:
            db.add(AiPreset(name=name, kind=kind, description=description, builtin=True))
    if await db.get(AppSetting, ACTIVE_PRESET_KEY) is None:
        db.add(AppSetting(key=ACTIVE_PRESET_KEY, value={"name": initial_preset()}, updated_by="seed"))
    await db.commit()


async def active(db: AsyncSession) -> Preset:
    setting = await db.get(AppSetting, ACTIVE_PRESET_KEY)
    name = (setting.value or {}).get("name") if setting else None
    row = await db.get(AiPreset, name) if name else None
    return Preset(row.name, row.kind) if row else DEFAULT


async def get(db: AsyncSession, name: str) -> Optional[AiPreset]:
    return await db.get(AiPreset, name)


async def activate(db: AsyncSession, name: str, actor: str) -> Preset:
    row = await db.get(AiPreset, name)
    if row is None:
        raise KeyError(name)
    setting = await db.get(AppSetting, ACTIVE_PRESET_KEY)
    if setting is None:
        db.add(AppSetting(key=ACTIVE_PRESET_KEY, value={"name": name}, updated_by=actor))
    else:
        setting.value, setting.updated_by = {"name": name}, actor
    return Preset(row.name, row.kind)


LOCAL_FALLBACK_KEY = "ai.local_fallback"


async def local_fallback(db: AsyncSession) -> Optional[dict]:
    """{"credential", "model", "enabled"}: where the gateway turns when a cloud provider is
    unreachable (no internet, DNS, 502/503/504). None or disabled = no fallback."""
    setting = await db.get(AppSetting, LOCAL_FALLBACK_KEY)
    value = setting.value if setting else None
    return value if value and value.get("enabled") and value.get("credential") and value.get("model") else None


async def set_local_fallback(db: AsyncSession, credential: Optional[str], model: Optional[str], enabled: bool,
                             actor: str) -> dict:
    value = {"credential": credential, "model": model, "enabled": bool(enabled and credential and model)}
    setting = await db.get(AppSetting, LOCAL_FALLBACK_KEY)
    if setting is None:
        db.add(AppSetting(key=LOCAL_FALLBACK_KEY, value=value, updated_by=actor))
    else:
        setting.value, setting.updated_by = value, actor
    return value


async def all_presets(db: AsyncSession) -> list[AiPreset]:
    return list((await db.execute(select(AiPreset).order_by(AiPreset.builtin.desc(), AiPreset.name))).scalars())
