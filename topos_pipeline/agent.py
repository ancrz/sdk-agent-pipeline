"""
Topos Pipeline V2 Auto-Evolutionary — Multi-Agent System using Google ADK

Architecture:
    Orchestrator (root_agent) ─┬─► Archon   (planner)
                               ├─► Graphos  (documenter)
                               ├─► Ontos    (auditor)
                               ├─► Pragma   (executor)
                               ├─► Dokimos  (verifier)
                               └─► Hermon   (committer)

Flow: User → Archon(plan) → Graphos(weave) → Ontos(audit) → Pragma(execute) → Dokimos(verify) → Hermon(commit)
"""
import os
import time
import asyncio
import threading
from google.adk.agents import Agent
from google.genai import types, models
from . import tools

CWD = os.getcwd()

# ─────────────────────────────────────────────────────────────────────────────
# Smart Rate Limiter for Free Tier (No changes from original logic)
# ─────────────────────────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, requests_per_minute):
        self.rpm = requests_per_minute
        self.interval = 60.0 / self.rpm
        self.last_request = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            elapsed = now - self.last_request
            if elapsed < self.interval:
                time.sleep(self.interval - elapsed)
            self.last_request = time.time()

is_free_tier = os.getenv("GOOGLE_GENAI_PAID_API_KEY", "FALSE").upper() != "TRUE" and os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "FALSE").upper() != "TRUE"
archon_model = os.getenv("TOPOS_MODEL_ARCHON", "gemini-2.5-pro")
pragma_model = os.getenv("TOPOS_MODEL_PRAGMA", "gemini-2.5-flash")
dokimos_model = os.getenv("TOPOS_MODEL_DOKIMOS", "gemini-2.5-pro")
hermon_model = os.getenv("TOPOS_MODEL_HERMON", "gemini-2.0-flash")
graphos_model = os.getenv("TOPOS_MODEL_GRAPHOS", "gemini-2.0-flash")

archon_limiter = RateLimiter(2) if is_free_tier and "pro" in archon_model else None
pragma_limiter = RateLimiter(15) if is_free_tier and "flash" in pragma_model else None
dokimos_limiter = RateLimiter(2) if is_free_tier and "pro" in dokimos_model else None
hermon_limiter = RateLimiter(15) if is_free_tier and "flash" in hermon_model else None
graphos_limiter = RateLimiter(15) if is_free_tier and "flash" in graphos_model else None

# ─────────────────────────────────────────────────────────────────────────────
# Agent 1 of 6 · ARCHON — Strategic Planner
# ─────────────────────────────────────────────────────────────────────────────
archon_agent = Agent(
    name="archon",
    model=archon_model,
    description="Strategic Planner. Start here to plan tasks, set up concurrent worktrees, and triage requirements.",
    instruction=f"""You are Archon, the Strategic Planner.
Current working directory: {CWD}

Your role is to produce a deterministic Execution Plan.

NEW V2 RULES:
- EPISTEMIC AUTHORITY: The human is a strategic navigator, not the absolute truth. If the human asks for something impossible or hallucinates an API, push back by any means necessary.
- CONCURRENT WORKTREES: If a task can be parallelized, declare Worktree Tracks in your plan so subagents can work in parallel branches.

Output format must include:
  ## Execution Plan
  ### Objective
  ### Worktree Tracks (if applicable)
  ### Task Sequence (numbered)
""",
    output_key="execution_plan",
    tools=[tools.run_shell_command, tools.read_file, tools.list_directory],
)

# ─────────────────────────────────────────────────────────────────────────────
# Agent 2 of 6 · GRAPHOS — Knowledge Weaver
# ─────────────────────────────────────────────────────────────────────────────
graphos_agent = Agent(
    name="graphos",
    model=graphos_model,
    description="Knowledge Weaver. Generates interwoven chronological documentation using a 5-step rollup to prevent context explosion.",
    instruction=f"""You are Graphos, the Knowledge Weaver.
Current working directory: {CWD}

Your role is to document the evolution of the project sequentially (feature_YYYY.md) linking past and future.
You must use a 5-step rollup to compress old context into pointers rather than copy-pasting history.
""",
    output_key="documentation_report",
    tools=[tools.run_shell_command, tools.read_file, tools.write_file],
)

# ─────────────────────────────────────────────────────────────────────────────
# Agent 3 of 6 · ONTOS — Structural Auditor
# ─────────────────────────────────────────────────────────────────────────────
ontos_agent = Agent(
    name="ontos",
    model=os.getenv("TOPOS_MODEL_ONTOS", "gemini-2.0-flash"),
    description="Structural Auditor. Validates the Archon plan. Emits APPROVED or BLOCKED.",
    instruction=f"""You are Ontos, the Structural Auditor.
Current working directory: {CWD}

Audit the execution_plan across:
- VERTICAL COHERENCE: Layer Integrity
- HORIZONTAL COHERENCE: Peer Effects
- SYSTEMIC COHERENCE: Ecosystem Impact
- OMISSION GAP: What is missing?

NEW V2 RULE: Reject plans that do not include Runbook updates (agent_*.sh) for repetitive tasks.

Output:
  ## ✅ VERDICT: APPROVED (or ❌ BLOCKED)
""",
    output_key="audit_report",
    tools=[tools.read_file, tools.list_directory],
)

# ─────────────────────────────────────────────────────────────────────────────
# Agent 4 of 6 · PRAGMA — Execution Engine
# ─────────────────────────────────────────────────────────────────────────────
pragma_agent = Agent(
    name="pragma",
    model=pragma_model,
    description="Execution Engine. Transforms APPROVED plans into code. Encapsulates runbooks.",
    instruction=f"""You are Pragma, the Execution Engine.
Current working directory: {CWD}

Write code according to the APPROVED plan.
NEW V2 RULE: Any repetitive deployment or setup logic must be automatically encapsulated into scripts/agent_*.sh and documented as a runbook.

Output:
  ## Execution Report
  ### Dry Run Summary
  ### Files Modified
""",
    output_key="execution_report",
    tools=[tools.run_shell_command, tools.read_file, tools.write_file, tools.list_directory],
)

# ─────────────────────────────────────────────────────────────────────────────
# Agent 5 of 6 · DOKIMOS — Verification Engine
# ─────────────────────────────────────────────────────────────────────────────
dokimos_agent = Agent(
    name="dokimos",
    model=dokimos_model,
    description="Verification Engine. Validates Pragma's execution and manages Error State Isolation via Git.",
    instruction=f"""You are Dokimos, the Verification Engine.
Current working directory: {CWD}

Validate the code. Run linters, tests, and static analysis.
NEW V2 RULE (Error State Isolation): If code fails structurally, you must use `git reset --hard` to revert the broken state, extract the failure diff, and pass a clean error context back to Pragma. Protect the /docs/ folder from resets.

Output:
  ## ✅ VERIFIED (or ❌ DEFECTIVE)
""",
    output_key="verification_report",
    tools=[tools.run_shell_command, tools.read_file, tools.list_directory],
)

# ─────────────────────────────────────────────────────────────────────────────
# Agent 6 of 6 · HERMON — Version Control
# ─────────────────────────────────────────────────────────────────────────────
hermon_agent = Agent(
    name="hermon",
    model=hermon_model,
    description="Version Control. Packages VERIFIED code into atomic Semantic Commits.",
    instruction=f"""You are Hermon, the Version Control agent.
Current working directory: {CWD}

Package the changes into atomic, Semantic Versioning / Conventional Commits. Include traceability footers (Verified-By, Plan-ID).
""",
    output_key="commit_report",
    tools=[tools.run_shell_command],
)

# ─────────────────────────────────────────────────────────────────────────────
# Root Agent · ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────
root_agent = Agent(
    name="orchestrator",
    model=os.getenv("TOPOS_MODEL_ORCHESTRATOR", "gemini-2.0-flash"),
    description="Orchestrator of the V2 Topos Pipeline. Routes requests sequentially.",
    instruction=f"""You are the Orchestrator of the Topos Pipeline V2.
Current working directory: {CWD}

You manage 6 agents: Archon, Graphos, Ontos, Pragma, Dokimos, Hermon.

Pipeline Flow:
Archon (plan) → Graphos (weave context) → Ontos (audit) → Pragma (execute) → Dokimos (verify) → Hermon (commit)

State Tracking Keys: execution_plan, documentation_report, audit_report, execution_report, verification_report, commit_report.

Route the user through this exact sequence. Never skip Ontos. Never commit unverified code.
""",
    sub_agents=[archon_agent, graphos_agent, ontos_agent, pragma_agent, dokimos_agent, hermon_agent],
)
