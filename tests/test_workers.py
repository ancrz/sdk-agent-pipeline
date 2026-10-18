"""The worker runtime: agents built from their specs (G-AGENTS A4), one process serving several roles, and
the instruction composed from the base, the caller's addendum (A2) and the agent team's (A3).

The composition test is ported from vanguard-control's api/tests/test_agent_instructions.py."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator import instructions


def _ctx(meta):
    return SimpleNamespace(_invocation_context=SimpleNamespace(
        run_config=SimpleNamespace(custom_metadata={"a2a_metadata": meta})))


def test_the_worker_composes_base_and_addendum_from_the_invocation():
    base = instructions.INSTRUCTIONS["validator"]
    add = instructions.addendum_from(_ctx({instructions.METADATA_KEY: json.dumps({"version": 4, "text": "Check twice."})}))
    assert add == (4, "Check twice.")
    text = instructions.compose(base, add)
    assert text.startswith(base.rstrip()) and "Operator addendum (version 4" in text and text.rstrip().endswith("Check twice.")
    assert instructions.compose(base, instructions.addendum_from(_ctx({}))) == base
    assert instructions.addendum_from(_ctx({instructions.METADATA_KEY: "not json"})) is None
    assert set(instructions.TUNABLE_ROLES) <= set(instructions.INSTRUCTIONS)


def test_the_team_addendum_comes_after_the_operators():
    meta = {instructions.METADATA_KEY: json.dumps({"version": 2, "text": "Operator note.",
                                                   "team": "sql-scripts", "team_text": "Prefer psql."})}
    text = instructions.compose("BASE", instructions.addendum_from(_ctx(meta)), instructions.team_from(_ctx(meta)))
    assert text.index("BASE") < text.index("Operator note.") < text.index('Agent team "sql-scripts"') < text.index("Prefer psql.")


def test_roles_are_parsed_in_order_without_repeats(monkeypatch):
    monkeypatch.setenv("AGENT_ROLE", "documentalist")      # the module validates nothing at import
    from workers.entrypoint import parse_roles
    assert parse_roles("executor, Validator,executor", "") == ["executor", "validator"]
    assert parse_roles("", "documentalist") == ["documentalist"]
    with pytest.raises(ValueError):
        parse_roles("", "")
    with pytest.raises(ValueError):
        parse_roles("archon", "")                          # the old Topos pipeline's roles are gone


@pytest.mark.parametrize("role", ["executor", "validator", "documentalist", "author", "medic"])
def test_every_spec_builds_its_agent(role, monkeypatch):
    monkeypatch.setenv(f"VANGUARD_GATEWAY_TOKEN_{role.upper()}", "gw-token")
    monkeypatch.delenv("KNOWLEDGE_MCP_URL", raising=False)
    from workers.agent import SPECS, make_worker_agent
    agent = make_worker_agent(role)
    assert agent.name == f"vanguard_{role}"
    names = {getattr(t, "name", type(t).__name__) for t in agent.tools}
    assert ("raise_hand" if SPECS[role].ask == "hand" else "request_approval") in names
    assert any(type(t).__name__ == "McpToolset" for t in agent.tools) == bool(SPECS[role].controller_tools)
