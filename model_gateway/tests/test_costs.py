"""Money per call: the gateway stamps the cost and the activity on the audit."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from .test_failover import Keys, call, setup
from .test_gateway import client_for, env, sf  # noqa: F401  (fixtures)

USAGE = {"promptTokenCount": 1000, "candidatesTokenCount": 200, "thoughtsTokenCount": 300, "cachedContentTokenCount": 400}
ANSWER = {"candidates": [{"content": {"role": "model", "parts": [{"text": "OK"}]}}], "usageMetadata": USAGE}


async def _price(sf, model="m1", input_=0.3, output=2.5, cached=0.03, thinking=None):  # noqa: F811
    from store.models.automation import ModelPrice
    async with sf() as db:
        db.add(ModelPrice(provider="gemini", model=model, effective_from=datetime.now(timezone.utc) - timedelta(days=1),
                          input_per_mtok=input_, output_per_mtok=output, cached_input_per_mtok=cached,
                          thinking_per_mtok=thinking, source="manual", updated_by="t"))
        await db.commit()


async def _last(sf):  # noqa: F811
    from store.models.automation import ModelCall
    async with sf() as db:
        return (await db.execute(select(ModelCall).order_by(ModelCall.created_at.desc()))).scalars().first()


async def _run(sf):  # noqa: F811
    from store.models.activity import ActivityRun
    async with sf() as db:
        run = ActivityRun(run_name="cost", status="RUNNING", sdd_yaml="")
        db.add(run)
        await db.commit()
        return run.id


@pytest.mark.asyncio
async def test_a_paid_call_costs_its_tokens_and_belongs_to_its_run(sf):  # noqa: F811
    await setup(sf)
    await _price(sf)
    run_id = await _run(sf)
    up = Keys({"main": httpx.Response(200, json=ANSWER)})
    async with client_for(sf, up) as c:
        r = await c.post("/v1beta/models/x:generateContent", json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
                         headers={"x-goog-api-key": __import__("model_gateway.tests.test_gateway", fromlist=["EXEC_TOKEN"]).EXEC_TOKEN,
                                  "X-Engine-Activity": f"run:{run_id}"})
    assert r.status_code == 200
    row = await _last(sf)
    # (1000-400)*0.3 + 400*0.03 + 200*2.5 + 300*2.5 (thinking at the output price) per 1M
    expected = (600 * 0.3 + 400 * 0.03 + 200 * 2.5 + 300 * 2.5) / 1_000_000
    assert row.list_cost_usd == pytest.approx(expected) and row.cost_usd == pytest.approx(expected)
    assert (row.thoughts_tokens, row.cached_tokens) == (300, 400)
    assert (row.activity_kind, row.activity_id) == ("run", run_id) and row.price_id is not None


@pytest.mark.asyncio
async def test_a_free_account_bills_nothing_but_shows_the_list_price(sf):  # noqa: F811
    from store.models.automation import Credential
    from store.services import credentials as svc
    await setup(sf)
    async with sf() as db:                                      # the main account becomes free tier
        cred = (await db.execute(select(Credential).where(Credential.name == "gemini-main"))).scalar_one()
        await svc.store(db, "gemini-main", "model_provider",
                        {"provider": "gemini", "api_key": "AIzaMAIN-key-0000000000000000001111", "tier": "free"}, "t", cred)
        await db.commit()
    await _price(sf)
    up = Keys({"main": httpx.Response(200, json=ANSWER)})
    async with client_for(sf, up) as c:
        assert (await call(c)).status_code == 200
    row = await _last(sf)
    assert row.cost_usd == 0 and row.list_cost_usd > 0


@pytest.mark.asyncio
async def test_an_unknown_activity_or_an_unpriced_model_is_left_blank_not_zero(sf):  # noqa: F811
    await setup(sf)
    up = Keys({"main": httpx.Response(200, json=ANSWER)})
    async with client_for(sf, up) as c:
        from .test_gateway import EXEC_TOKEN
        r = await c.post("/v1beta/models/x:generateContent", json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
                         headers={"x-goog-api-key": EXEC_TOKEN, "X-Engine-Activity": f"run:{uuid.uuid4()}"})
    assert r.status_code == 200
    row = await _last(sf)
    assert row.activity_kind is None and row.activity_id is None      # a run that does not exist
    assert row.cost_usd is None and row.list_cost_usd is None          # no price for m1: unknown


@pytest.mark.asyncio
async def test_the_activity_header_never_reaches_the_provider(sf):  # noqa: F811
    await setup(sf)
    sent = []

    def answer(request):
        sent.append(dict(request.headers))
        return httpx.Response(200, json=ANSWER)
    async with client_for(sf, answer) as c:
        from .test_gateway import EXEC_TOKEN
        await c.post("/v1beta/models/x:generateContent", json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
                     headers={"x-goog-api-key": EXEC_TOKEN, "X-Engine-Activity": "index"})
    assert sent and all("x-engine-activity" not in {k.lower() for k in h} for h in sent)
    row = await _last(sf)
    assert row.activity_kind == "index"


@pytest.mark.asyncio
async def test_a_plan_is_kept_from_the_api_only_and_before_it_is_committed(sf, monkeypatch):  # noqa: F811
    """The API tags a plan it is still drafting (not visible to the gateway yet); a worker cannot
    claim a plan."""
    from model_gateway.app import _attribution
    pid = uuid.uuid4()
    async with sf() as db:
        assert await _attribution(db, f"plan:{pid}", "orchestrator") == {"activity_kind": "plan", "activity_id": pid}
        assert await _attribution(db, f"plan:{pid}", "executor") == {}
        assert await _attribution(db, f"run:{pid}", "executor") == {}          # runs must exist
