"""G-AGENTS A3 in the engine: the gateway puts the run's agent team model first for a role.

Ported from vanguard-control's api/tests/test_agent_teams.py (test_the_gateway_puts_the_team_model_first);
the engine links a run to its team directly (activity_runs.agent_team_id) instead of through a plan."""

from __future__ import annotations

import uuid

import pytest

from model_gateway.app import team_model, with_model_first
from store.models.activity import ActivityRun
from store.models.automation import AgentTeam
from store.services.failover import Route
from store.services.models import ResolvedModel

from .test_gateway import env, sf  # noqa: F401  (fixtures)


@pytest.mark.asyncio
async def test_the_gateway_puts_the_team_model_first(sf):  # noqa: F811
    async with sf() as db:
        team = AgentTeam(name="programador", members={"executor": {"model": "gemini-3.5-flash", "addendum": "x"}},
                         created_by="test", updated_by="test")
        db.add(team)
        await db.flush()
        run = ActivityRun(run_name="team", sdd_yaml="", agent_team_id=team.id)
        plain = ActivityRun(run_name="basic", sdd_yaml="")
        db.add_all([run, plain])
        await db.commit()
        assert await team_model(db, f"run:{run.id}", "executor") == "gemini-3.5-flash"
        assert await team_model(db, f"run:{run.id}", "validator") is None       # the team leaves the validator alone
        assert await team_model(db, f"run:{plain.id}", "executor") is None      # a run without a team (basic)
        assert await team_model(db, f"run:{uuid.uuid4()}", "executor") is None  # an unknown run
        assert await team_model(db, f"plan:{uuid.uuid4()}", "executor") is None  # only a run names a team
        assert await team_model(db, None, "executor") is None

    def route(tier, provider, models):
        return Route(tier=tier, resolved=ResolvedModel(role="executor", model=models[0], provider=provider), models=models)
    chain = [route("primary", "gemini", ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]),
             route("backup", "gemini", ["gemini-3.5-flash-lite"]), route("local", "local", ["gemma4:e2b"])]
    out = with_model_first(chain, "gemini-3.5-flash")
    assert out[0].models == ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    assert out[1].models == ["gemini-3.5-flash-lite"] and out[2].models == ["gemma4:e2b"]
    assert chain[0].models[0] == "gemini-3.5-flash-lite"                     # the cached chain is not mutated
    offline = [route("offline", "offline", ["deterministic-executor"])]
    assert with_model_first(offline, "gemini-3.5-flash")[0].models == ["deterministic-executor"]
