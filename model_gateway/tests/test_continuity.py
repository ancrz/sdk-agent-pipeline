"""Who answers, told on every change (docs/pipeline/continuity_*): backup key, local model, no
internet, back on the primary; failback auto or manual; a restored key is tried at once."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from model_gateway import failover

from .test_failover import Keys, call, setup
from .test_gateway import client_for, env, sf  # noqa: F401  (fixtures)

ADMIN = {"Authorization": "Bearer " + "a" * 32}


@pytest.fixture(autouse=True)
def admin_token(monkeypatch):
    monkeypatch.setenv("VANGUARD_GATEWAY_ADMIN_TOKEN", "a" * 32)


async def _admin(sf):  # noqa: F811
    """The engine records notifications without recipients (store/services/notify.py): nothing
    to seed. Kept so the tests read as in vanguard-control, where an admin user receives them."""


async def _notices(sf):  # noqa: F811
    from store.models.notification import Notification
    async with sf() as db:
        rows = (await db.execute(select(Notification).order_by(Notification.created_at))).scalars().all()
        return [(n.event, n.count) for n in rows if n.event.startswith("gateway.")]


@pytest.mark.asyncio
async def test_backup_then_recovered_once_per_change(sf):  # noqa: F811
    await setup(sf)
    await _admin(sf)
    up = Keys({"main": httpx.Response(401, json={})})
    async with client_for(sf, up) as c:
        for _ in range(3):                                   # one change, three calls: one notice
            assert (await call(c)).status_code == 200
        assert (await _notices(sf)) == [("gateway.breaker_opened", 1), ("gateway.failover", 1)]
        up.answers.pop("main")                               # the key is fixed and stored again …
        r = await c.delete("/admin/breakers?prefix=account:gemini-main", headers=ADMIN)
        assert r.json()["reset"] == 1
        assert (await call(c)).status_code == 200            # … and used at once
        serving = (await c.get("/admin/serving", headers=ADMIN)).json()
    assert up.calls[-1] == "main" and serving["executor"]["state"] == "primary:gemini-main"
    assert (await _notices(sf))[-1] == ("gateway.recovered", 1)


@pytest.mark.asyncio
async def test_no_internet_goes_local_and_says_so(sf, monkeypatch):  # noqa: F811
    await setup(sf, local=True)
    await _admin(sf)

    async def internet_down(self, provider):                 # two providers unreachable within a minute
        return True
    monkeypatch.setattr(failover.Breakers, "network_failed", internet_down)
    down = httpx.ConnectError("no route to host")
    up = Keys({"main": down, "backup": down})
    async with client_for(sf, up) as c:
        assert (await call(c)).status_code == 200
    events = [e for e, _ in await _notices(sf)]
    assert "gateway.internet_down" in events and events[-1] == "gateway.local_fallback"
    assert up.calls[-1] == "ollama"


@pytest.mark.asyncio
async def test_failback_manual_stays_on_the_backup_until_told(sf):  # noqa: F811
    from store.services import failover as fo
    await setup(sf)
    await _admin(sf)
    async with sf() as db:
        await fo.set_config(db, [{"credential": "gemini-backup", "model": None}], True, "t", failback="manual")
        await db.commit()
    up = Keys({"main": httpx.Response(401, json={})})
    async with client_for(sf, up) as c:
        await call(c)                                        # main fails → backup
        up.answers.pop("main")
        await c.delete("/admin/breakers", headers=ADMIN)     # main healthy again …
        await call(c)
        assert up.calls[-1] == "backup"                      # … but manual: the backup keeps going first
        assert (await c.post("/admin/serving/reset", headers=ADMIN)).json()["reset"] >= 1
        await call(c)
    assert up.calls[-1] == "main"
    assert (await _notices(sf))[-1] == ("gateway.recovered", 1)


@pytest.mark.asyncio
async def test_failback_auto_returns_by_itself(sf):  # noqa: F811
    await setup(sf)
    up = Keys({"main": httpx.Response(401, json={})})
    async with client_for(sf, up) as c:
        await call(c)
        up.answers.pop("main")
        await c.delete("/admin/breakers", headers=ADMIN)
        await call(c)
    assert up.calls[-1] == "main"


@pytest.mark.asyncio
async def test_serving_state_is_one_change_across_replicas():
    class FakeValkey:
        def __init__(self):
            self.data = {}

        async def set(self, key, value, get=False, **_):
            old = self.data.get(key)
            self.data[key] = value
            return old if get else True

    shared = FakeValkey()
    a, b = failover.Serving(shared), failover.Serving(shared)
    assert (await a.changed("executor", "backup:k2")) == (True, None)
    assert (await b.changed("executor", "backup:k2"))[0] is False          # the other replica: no second notice
    assert (await b.changed("executor", "primary:k1")) == (True, "backup:k2")
