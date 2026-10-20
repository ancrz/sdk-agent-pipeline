"""Model prices and the cost of a call.

- The seed is store/prices/litellm_prices.json (LiteLLM's table reduced to the supported
  providers). `python -m store.schema` inserts the models not priced yet, dated from the file's
  `as_of`; it never overwrites a price an admin set.
- The price in force for a call is the newest row with effective_from <= the call's time, so a
  provider's price change (Google lists dated prices) is a new row, and history keeps its cost.
- A call's cost is stamped when the gateway writes it: `list_cost_usd` is what the tokens cost at
  the paid price; `cost_usd` is what was billed (0 on a free tier, and for the local model and the
  stand-in). An unpriced model leaves both null: unknown, never zero.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.automation import ModelPrice

PINNED = Path(__file__).resolve().parent.parent / "prices" / "litellm_prices.json"
FREE_PROVIDERS = ("local", "offline")          # no money changes hands (electricity is out of scope)


def model_key(model: str) -> str:
    """'models/gemini-3.5-flash-lite' → 'gemini-3.5-flash-lite' (the gateway may see either)."""
    return (model or "").split("/", 1)[1] if (model or "").startswith("models/") else (model or "")


def pinned() -> dict:
    return json.loads(PINNED.read_text()) if PINNED.exists() else {"as_of": None, "prices": []}


async def seed_prices(db: AsyncSession) -> int:
    """Insert the pinned price of every model that has no price yet. Returns how many."""
    data = pinned()
    if not data["prices"]:
        return 0
    since = datetime.fromisoformat(data["as_of"]).replace(tzinfo=timezone.utc)
    have = {(p, m) for p, m in (await db.execute(select(ModelPrice.provider, ModelPrice.model))).all()}
    added = 0
    for row in data["prices"]:
        if (row["provider"], row["model"]) in have:
            continue
        db.add(ModelPrice(provider=row["provider"], model=row["model"], effective_from=since,
                          input_per_mtok=row["input"], output_per_mtok=row["output"],
                          cached_input_per_mtok=row["cached"], thinking_per_mtok=row["thinking"],
                          source=data.get("source", "litellm"), updated_by="seed"))
        added += 1
    return added


async def price_for(db: AsyncSession, provider: str, model: str, at: Optional[datetime] = None) -> Optional[ModelPrice]:
    at = at or datetime.now(timezone.utc)
    return (await db.execute(
        select(ModelPrice).where(ModelPrice.provider == provider, ModelPrice.model == model_key(model),
                                 ModelPrice.effective_from <= at)
        .order_by(ModelPrice.effective_from.desc()).limit(1))).scalar_one_or_none()


def tokens_cost(price: ModelPrice, prompt: int, output: int, thoughts: int = 0, cached: int = 0) -> float:
    """USD for these tokens at this price. `prompt` includes the cached part (Gemini's
    promptTokenCount does); thinking tokens are billed at the thinking (else output) price."""
    cached = min(max(cached, 0), max(prompt, 0))
    cached_rate = price.cached_input_per_mtok if price.cached_input_per_mtok is not None else price.input_per_mtok
    thinking_rate = price.thinking_per_mtok if price.thinking_per_mtok is not None else price.output_per_mtok
    usd = ((prompt - cached) * price.input_per_mtok + cached * cached_rate
           + output * price.output_per_mtok + thoughts * thinking_rate) / 1_000_000
    return round(usd, 10)


@dataclass
class Stamp:
    cost_usd: Optional[float]
    list_cost_usd: Optional[float]
    price_id: Optional[object]


async def stamp(db: AsyncSession, provider: str, model: str, tier: Optional[str], prompt: Optional[int],
                output: Optional[int], thoughts: Optional[int] = None, cached: Optional[int] = None) -> Stamp:
    """The cost to write on a model call."""
    if provider in FREE_PROVIDERS:
        return Stamp(0.0, 0.0, None)
    if prompt is None and output is None:
        return Stamp(None, None, None)          # no usage reported: unknown
    price = await price_for(db, provider, model)
    if price is None:
        return Stamp(None, None, None)
    listed = tokens_cost(price, prompt or 0, output or 0, thoughts or 0, cached or 0)
    return Stamp(0.0 if tier == "free" else listed, listed, price.id)
