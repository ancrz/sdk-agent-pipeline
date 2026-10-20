"""ADK agent factory for the engine's workers.

One Agent per role, built from that role's AgentSpec (SPECS): the definition is
data and the runtime is generic. One process may serve several specs (workers/entrypoint.py,
AGENT_ROLES). The per-node task text arrives in the A2A message body; workers do not
need to know node structure up front.

Design notes:
- Executor and validator reach the Controller only through the official
  aap-mcp-server (Streamable HTTP, /mcp/<role>). Each request carries the
  node's worker token as a bearer header, taken from the A2A request metadata
  by header_provider — the token never appears in LLM-visible text. The
  Controller decides what the token allows (template, target, job type).
- Every role has request_approval as an ADK LongRunningFunctionTool for
  unplanned actions (→ A2A INPUT_REQUIRED; decided through /api/approvals).
- The documentalist and the author
  have no automation tools.
- Medic has the `medic` aap-mcp toolset (ad hoc modules on its
  step's host, within a remedy grant) and raise_hand instead of request_approval.
- Every role may read the knowledge MCP server when KNOWLEDGE_MCP_URL and ENGINE_KNOWLEDGE_TOKEN are set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from google.adk.agents import Agent  # type: ignore[import]
from google.adk.tools.long_running_tool import LongRunningFunctionTool  # type: ignore[import]

from orchestrator.models import role_model
from orchestrator import activity, instructions
from orchestrator.ratelimit import rpm_before_model_callback

# ─────────────────────────────────────────────────────────────────────────────
# Long-running approval tool (for unplanned / ungated actions only)
# ─────────────────────────────────────────────────────────────────────────────


def request_approval(action: str, target: str) -> dict:
    """Ask a human to approve an unplanned action.

    This is an ADK LongRunningFunctionTool. Calling it puts the A2A task into
    INPUT_REQUIRED state. The executor waits for a FunctionResponse from the
    client: the operator's decision from /api/approvals, sent by the API executor.

    Gate-backed nodes from an approved plan do NOT call this function — plan
    approval wrote their grants, and the compiled node instruction says
    "Gate ... was approved with the plan; no request_approval() call is needed."
    """
    # ADK hands this return value to the model before the human decides; the model
    # must not act or give a final answer on it (the decision arrives as the real
    # function response when the executor resumes the task).
    return {"status": "pending", "action": action, "target": target,
            "note": "Waiting for a human decision. Do nothing else and do not answer yet."}


def raise_hand(diagnosis: str, proposal_kind: str, proposal: str) -> dict:
    """Ask a human for help with a step you could not fix (Medic only).

    Args:
        diagnosis: What you found, with the evidence (the failing task, your commands' output).
        proposal_kind: hint | allow_fix | subplan — what you think the human should do.
        proposal: The concrete fix you propose (the command, or the steps a sub-plan needs).

    This is an ADK LongRunningFunctionTool: the task waits in INPUT_REQUIRED until a human
    answers in the Dashboard (api/services/medic.py). The answer arrives as this call's
    function response: {"decision": "hint" | "allow_fix" | "subplan" | "give_up", "text", ...}.
    """
    return {"status": "pending", "note": "Waiting for a human. Do nothing else and do not answer yet."}


# ─────────────────────────────────────────────────────────────────────────────
# Per-role instructions
# ─────────────────────────────────────────────────────────────────────────────

_INSTRUCTIONS = instructions.INSTRUCTIONS


# aap-mcp tools each role may call (the toolset on the server enforces the same list)
AAP_TOOLS = (
    "job_templates_list", "job_templates_retrieve", "job_templates_launch_create",
    "jobs_retrieve", "jobs_stdout_retrieve", "jobs_job_events_list", "jobs_job_host_summaries_list",
    "hosts_list", "hosts_ansible_facts_retrieve",
)
MEDIC_TOOLS = (
    "ad_hoc_commands_create", "ad_hoc_commands_retrieve", "ad_hoc_commands_stdout_retrieve",
    "jobs_retrieve", "jobs_stdout_retrieve", "jobs_job_events_list", "hosts_list", "hosts_ansible_facts_retrieve",
)
TOKEN_METADATA_KEY = "engine_worker_token"


def worker_token_headers(ctx) -> dict[str, str]:
    """ADK header_provider: the node's worker token from the A2A request metadata."""
    run_config = getattr(ctx, "run_config", None)
    metadata = (getattr(run_config, "custom_metadata", None) or {}).get("a2a_metadata") or {}
    token = metadata.get(TOKEN_METADATA_KEY)
    return {"Authorization": f"Bearer {token}"} if token else {}


def knowledge_scope_headers(ctx) -> dict[str, str]:
    """ADK header_provider for the knowledge MCP: the worker token as a scope, not as auth."""
    token = worker_token_headers(ctx).get("Authorization", "").removeprefix("Bearer ")
    return {"X-Engine-Worker-Token": token} if token else {}


def aap_mcp_toolset(role: str):
    from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams  # type: ignore[import]
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset  # type: ignore[import]

    base = os.environ.get("AAP_MCP_URL", "http://aap-mcp:3000").rstrip("/")
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=f"{base}/mcp/{role}"),
        tool_filter=list(SPECS[role].controller_tools),
        header_provider=worker_token_headers,
    )

KNOWLEDGE_TOOLS = ("search_playbooks", "search_job_history", "get_template", "get_host", "list_automatable_hosts")
_KNOWLEDGE_NOTE = (
    "\nRead-only knowledge tools are available: search_playbooks, search_job_history, get_template, "
    "get_host, list_automatable_hosts. Use them to check a template's variables, a host's OS and last "
    "outcome, or how similar jobs went; they cannot change anything.\n"
)


def knowledge_toolset():
    """Read-only knowledge MCP; None when not configured."""
    url, token = os.environ.get("KNOWLEDGE_MCP_URL"), os.environ.get("ENGINE_KNOWLEDGE_TOKEN")
    if not url or not token:
        return None
    from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams  # type: ignore[import]
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset  # type: ignore[import]

    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=f"{url.rstrip('/')}/mcp", headers={"Authorization": f"Bearer {token}"}),
        tool_filter=list(KNOWLEDGE_TOOLS),
        # The node's worker token scopes job history and hosts to its run's environment.
        header_provider=knowledge_scope_headers,
    )


@dataclass(frozen=True)
class AgentSpec:
    """What an agent is: its instructions, the human it asks, the Controller tools it may call."""
    role: str
    skills: tuple[str, ...]
    ask: str = "approval"                    # approval: request_approval · hand: raise_hand (Medic)
    controller_tools: tuple[str, ...] = ()   # aap-mcp toolset /mcp/<role>; empty = no automation tools

    @property
    def instruction(self) -> str:
        return _INSTRUCTIONS[self.role]

    @property
    def tunable(self) -> bool:               # the operator's addendum (A2) and the team's (A3) apply
        return self.role in instructions.TUNABLE_ROLES


SPECS: dict[str, AgentSpec] = {
    spec.role: spec for spec in (
        AgentSpec("executor", ("launch approved job templates", "apply remediation actions"),
                  controller_tools=AAP_TOOLS),
        AgentSpec("validator", ("validate system state", "run job templates in check mode"),
                  controller_tools=AAP_TOOLS),
        AgentSpec("documentalist", ("produce closure reports", "document execution traces")),
        AgentSpec("author", ("draft Ansible job templates from a goal", "reuse approved playbook patterns")),
        AgentSpec("medic", ("diagnose a failed step from its job output and host facts",
                            "fix small causes with bounded ad hoc Ansible modules", "raise a hand with a diagnosis"),
                  ask="hand", controller_tools=MEDIC_TOOLS),
    )
}
VALID_ROLES = frozenset(SPECS)
GATEWAY_KEY_HEADER = "x-goog-api-key"        # lower case: google-genai's own key, so a request value replaces it


def gateway_key_callback(role: str):
    """before_model_callback: this request's gateway token is the role's own (ENGINE_GATEWAY_TOKEN_<ROLE>).

    One process may serve several roles; the gateway tells roles apart by token (model, limits, accounting).
    Without the variable (a per-role container, GOOGLE_API_KEY = its token) it does nothing.
    """
    token = os.environ.get(f"ENGINE_GATEWAY_TOKEN_{role.upper()}", "")

    def callback(callback_context, llm_request):
        if not token or getattr(llm_request, "config", None) is None:
            return None
        from google.genai import types
        opts = llm_request.config.http_options or types.HttpOptions()
        llm_request.config.http_options = opts.model_copy(
            update={"headers": {**(opts.headers or {}), GATEWAY_KEY_HEADER: token}})
        return None
    return callback


def make_worker_agent(role: str) -> Agent:
    """Build the ADK Agent of a role from its AgentSpec.

    Raises:
        ValueError: If role has no spec.
    """
    spec = SPECS.get(role)
    if spec is None:
        raise ValueError(f"Unknown role {role!r}; must be one of {sorted(VALID_ROLES)}")

    from orchestrator import deterministic
    # ENGINE_LLM_MODE=deterministic: a fixed procedure stands in for the model;
    # tools, tokens, A2A and the Controller are the real ones.
    model = (deterministic.worker_llm(role) if deterministic.enabled()
             else role_model(role))
    # Medic asks for help with a raised hand, never for an approval
    tools: list = [LongRunningFunctionTool(func=raise_hand if spec.ask == "hand" else request_approval)]
    if spec.controller_tools:
        tools.append(aap_mcp_toolset(role))
    instruction = spec.instruction
    knowledge = knowledge_toolset()
    if knowledge is not None:
        tools.append(knowledge)
        instruction += _KNOWLEDGE_NOTE
    # Engine addition (docs/mcp.md): the MCP servers declared in ENGINE_MCP_SERVERS for this role, with
    # the same pattern as the two above (allowlist, token per request). A bad declaration stops the start.
    from workers import mcp_servers
    for toolset, note in mcp_servers.toolsets_for(role, VALID_ROLES):
        tools.append(toolset)
        instruction += note
    tunable = spec.tunable

    return Agent(
        name=f"engine_{role}",  # ADK requires a Python identifier
        description="; ".join(spec.skills),       # the A2A card's skills
        model=model,
        # A provider, not a string: ADK would treat "{{ target_host }}" in the text as
        # session-state placeholders; providers bypass state injection. It also adds the
        # operator's addendum in force that the API sends with each task.
        instruction=lambda ctx, _text=instruction: (
            instructions.compose(_text, instructions.addendum_from(ctx), instructions.team_from(ctx)) if tunable else _text),
        tools=tools,
        # the role's gateway token, the RPM slot, then the run this call works for (X-Engine-Activity,
        #)
        before_model_callback=[gateway_key_callback(role), rpm_before_model_callback, activity.adk_callback],
    )
