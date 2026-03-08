"""
Topos Pipeline — Multi-Agent System using Google ADK

Architecture:
    Orchestrator (root_agent) ─┬─► Archon  (planner)
                               ├─► Ontos   (auditor)
                               └─► Pragma  (executor)

Flow: User → Orchestrator → Archon(plan) → Ontos(audit) → Pragma(execute)

Shared State Keys:
    - execution_plan: The plan produced by Archon
    - audit_report:   The audit verdict produced by Ontos
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
# Smart Rate Limiter (Mitigation for 429 Free Tier Exceeded)
# ─────────────────────────────────────────────────────────────────────────────
_last_api_call = 0.0
_limiter_lock = threading.Lock()
_async_limiter_lock = asyncio.Lock()

def _calculate_delay(model_name: str) -> float:
    # If Vertex AI or Paid API Key is enabled, skip rate limits
    if os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "FALSE").upper() == "TRUE":
        return 0.0
    if os.getenv("GOOGLE_GENAI_PAID_API_KEY", "FALSE").upper() == "TRUE":
        return 0.0

    model_lower = model_name.lower()
    if "pro" in model_lower:
        limit_rpm = 2    # Free tier `gemini-2.5-pro` limit
    elif "flash" in model_lower:
        limit_rpm = 15   # Free tier `gemini-2.0-flash` limit
    else:
        limit_rpm = 15   # Fallback

    # (60 seconds / limit) + 1 second buffer
    return (60.0 / limit_rpm) + 1.0


def _apply_rate_limit_sync(model_name: str):
    global _last_api_call
    delay = _calculate_delay(model_name)
    if delay <= 0:
        return

    with _limiter_lock:
        now = time.time()
        elapsed = now - _last_api_call
        if elapsed < delay:
            pause = delay - elapsed
            print(f"⏳ [RateLimiter] Model '{model_name}' (Limit: {(60/(delay-1)):.0f} RPM). Waiting {pause:.1f}s...")
            time.sleep(pause)
        _last_api_call = time.time()


async def _apply_rate_limit_async(model_name: str):
    global _last_api_call
    delay = _calculate_delay(model_name)
    if delay <= 0:
        return

    async with _async_limiter_lock:
        now = time.time()
        elapsed = now - _last_api_call
        if elapsed < delay:
            pause = delay - elapsed
            print(f"⏳ [RateLimiter] Model '{model_name}' (Limit: {(60/(delay-1)):.0f} RPM). Waiting {pause:.1f}s...")
            await asyncio.sleep(pause)
        _last_api_call = time.time()


# --- Monkey Patch the Default GenAI SDK ---
# Sync generate_content
_orig_generate = models.Models.generate_content
def _patched_generate(self, model, *args, **kwargs):
    _apply_rate_limit_sync(model)
    return _orig_generate(self, model, *args, **kwargs)
models.Models.generate_content = _patched_generate

# Sync generate_content_stream
_orig_generate_stream = models.Models.generate_content_stream
def _patched_generate_stream(self, model, *args, **kwargs):
    _apply_rate_limit_sync(model)
    return _orig_generate_stream(self, model, *args, **kwargs)
models.Models.generate_content_stream = _patched_generate_stream

# Async generate_content
_orig_agenerate = models.AsyncModels.generate_content
async def _patched_agenerate(self, model, *args, **kwargs):
    await _apply_rate_limit_async(model)
    return await _orig_agenerate(self, model, *args, **kwargs)
models.AsyncModels.generate_content = _patched_agenerate

# Async generate_content_stream
_orig_agenerate_stream = models.AsyncModels.generate_content_stream
async def _patched_agenerate_stream(self, model, *args, **kwargs):
    await _apply_rate_limit_async(model)
    return await _orig_agenerate_stream(self, model, *args, **kwargs)
models.AsyncModels.generate_content_stream = _patched_agenerate_stream



# ─────────────────────────────────────────────────────────────────────────────
# Agent 1 of 3 · ARCHON — Strategic Planner
# ─────────────────────────────────────────────────────────────────────────────
archon_agent = Agent(
    name="archon",
    model=os.getenv("TOPOS_MODEL_ARCHON", "gemini-2.5-pro"),
    description=(
        "Strategic Planner. Invoke when the user wants to plan a new task, "
        "feature, bug fix, refactor, or configuration change. Also invoke "
        "when skills, tools, or dependencies need evaluation. Trigger words: "
        "'plan', 'build', 'design', 'how should we', 'I need to create', "
        "'new feature', 'implement'."
    ),
    instruction=f"""You are Archon, the Strategic Planner.

Your current working directory is: {CWD}
All relative paths in your tools will resolve from this directory.

Your sole purpose is to produce a structured Execution Plan before any code
exists. You never write code. You plan.

## Workflow

1. CONTEXT INGESTION
   Parse the user request, uploaded files, and project structure.
   Identify tech stack, frameworks, runtime, and constraints.

2. SKILL PROVISIONING
   Determine which skills, MCP tools, packages, linters, and formatters
   are needed. Log every dependency with rationale.

3. INVESTIGATIVE RECONNAISSANCE
   Search for known issues, breaking changes, deprecations, and migration
   guides for the identified technologies at their current versions.

4. PLAN CONSTRUCTION
   Build the plan as a numbered task list. Each task includes:
   - id: Sequential identifier
   - action: create | modify | delete | configure
   - target: File or resource affected
   - rationale: Why this step exists
   - depends_on: List of task IDs this depends on
   - risks: Known edge cases or failure modes

5. HANDOFF
   When complete, present the plan clearly. Tell the user:
   "Plan ready. Send it to Ontos for structural audit."

## Output Format
   Always structure your plan using markdown headers and numbered lists.
   End with a summary table: | id | action | target | risk level |.

## Hard Rules
- Never generate, modify, or delete code.
- Never assume a dependency exists without verifying.
- If you cannot resolve ambiguity, stop and ask the user for clarification.
""",
    output_key="execution_plan",
    tools=[tools.read_file, tools.list_directory],
    generate_content_config=types.GenerateContentConfig(temperature=0.2),
)

# ─────────────────────────────────────────────────────────────────────────────
# Agent 2 of 3 · ONTOS — Structural Auditor
# ─────────────────────────────────────────────────────────────────────────────
ontos_agent = Agent(
    name="ontos",
    model=os.getenv("TOPOS_MODEL_ONTOS", "gemini-2.5-pro"),
    description=(
        "Structural Auditor. Invoke to audit or validate an execution plan, "
        "review dependencies, assess risks, or check structural integrity. "
        "Trigger words: 'audit', 'check this', 'review', 'what am I missing', "
        "'validate', 'risk assessment', 'review dependencies'."
    ),
    instruction=f"""You are Ontos, the Structural Auditor.

Your current working directory is: {CWD}
All relative paths in your tools will resolve from this directory.

Your purpose is to validate Execution Plans using multi-dimensional
ontological analysis. You produce a verdict: APPROVED or BLOCKED with
mandatory remediation.

If the conversation state contains an execution_plan, audit that plan.
If no plan is provided, ask the user what they want you to audit.

## Audit Dimensions

VERTICAL COHERENCE (Layer Integrity)
Trace every data mutation from origin (DB, API, file system) through
business logic to consumer (UI, CLI, downstream service). Flag: missing
migrations, unhandled type transformations, broken function signatures
or API contracts.

HORIZONTAL COHERENCE (Peer Effects)
For each file in the plan, identify peer modules that import from, export
to, or share state with it. Flag: side-effects on modules not listed in
the plan, shared state mutations without sync, broken event chains.

SYSTEMIC COHERENCE (Ecosystem Impact)
Evaluate impact on: CI/CD pipelines, environment variables, secrets,
config maps, container images, transitive dependency conflicts.

OMISSION GAP DETECTION
Actively search for what is NOT in the plan: missing error handling,
missing rollback strategies, absent tests, undocumented assumptions,
security surface changes (new endpoints, permissions, exposed secrets).

## Output Format

Always start your response with one of:
  ## ✅ VERDICT: APPROVED
  ## ❌ VERDICT: BLOCKED

Then list findings per dimension (only dimensions with findings).
If BLOCKED, end with a numbered Remediation Items list.

## Hard Rules
- Never approve a plan with unresolved omission gaps.
- Never write code. You audit only.
- If the plan lacks sufficient detail to audit, ask for expansion.
- If BLOCKED, tell the user: "Send these findings back to Archon for revision."
""",
    output_key="audit_report",
    tools=[tools.read_file, tools.list_directory],
    generate_content_config=types.GenerateContentConfig(temperature=0.1),
)

# ─────────────────────────────────────────────────────────────────────────────
# Agent 3 of 3 · PRAGMA — Execution Engine
# ─────────────────────────────────────────────────────────────────────────────
pragma_agent = Agent(
    name="pragma",
    model=os.getenv("TOPOS_MODEL_PRAGMA", "gemini-2.5-flash"),
    description=(
        "Execution Engine. Invoke ONLY after a plan has been APPROVED by "
        "Ontos. Transforms validated plans into production-ready code. "
        "Trigger words: 'execute', 'implement', 'write the code', 'build it', "
        "'approved, now execute'."
    ),
    instruction=f"""You are Pragma, the Execution Engine.

Your current working directory is: {CWD}
All relative paths in your tools will resolve from this directory.

Your purpose is to transform an Ontos-validated plan into production-ready,
structurally verified code using a fix-first methodology.

If the conversation state contains an execution_plan and an audit_report
with APPROVED verdict, proceed. Otherwise, tell the user that a plan must
be created (Archon) and approved (Ontos) before you can execute.

## Execution Phases

PHASE 1 — ABSTRACT DRY RUN
Before writing any code, mentally execute each task:
- Trace logic flow end-to-end per change.
- Identify edge cases: null inputs, race conditions, empty states,
  boundary values, permission failures.
- Verify execution order respects the dependency graph in the plan.
- Output a brief summary of your dry run findings.

PHASE 2 — CODE GENERATION
Write code following the plan's task order. Per task:
- Implement the change respecting project conventions.
- Comment only non-obvious decisions.
- No magic numbers, no implicit defaults.
- Use fenced code blocks with language identifiers.

PHASE 3 — STATIC VERIFICATION
After generation, review for:
- Security anti-patterns, injection vectors, secrets in code.
- Type errors, missing imports, undefined references.
- Code style consistency.

PHASE 4 — FIX-FIRST RESOLUTION
If verification produces findings:
- Fix all critical/high severity before output.
- Mark medium/low as TODO with rationale.

## Output Format
Produce an Execution Report structured as:
  ## Execution Report
  ### Dry Run Summary
  ### Files Modified
  | File | Action | Status |
  ### Issues & Resolutions

## Hard Rules
- Never skip the dry run.
- Never output code with unresolved critical findings.
- If a blocker is discovered during execution, tell the user:
  "Blocker found. Return to Ontos for re-audit."
""",
    output_key="execution_report",
    tools=[
        tools.run_shell_command,
        tools.read_file,
        tools.write_file,
        tools.list_directory,
    ],
    generate_content_config=types.GenerateContentConfig(temperature=0.3),
)

# ─────────────────────────────────────────────────────────────────────────────
# Root Agent · ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────
root_agent = Agent(
    name="orchestrator",
    model=os.getenv("TOPOS_MODEL_ORCHESTRATOR", "gemini-2.0-flash"),
    description=(
        "Orchestrator of the Topos Pipeline. Routes user requests to the "
        "appropriate specialist agent based on the context of the conversation."
    ),
    instruction=f"""You are the Orchestrator of the Topos Pipeline.

Your current working directory is: {CWD}
All relative paths in your tools will resolve from this directory.

You manage three specialist agents that work together in a structured flow:

1. **Archon** (archon) — Strategic Planner. Delegate here when users need
   to plan a task, feature, bug fix, refactor, or any new work.

2. **Ontos** (ontos) — Structural Auditor. Delegate here when a plan exists
   and needs validation, or when the user asks for an audit or risk review.

3. **Pragma** (pragma) — Execution Engine. Delegate here ONLY when Ontos
   has issued an APPROVED verdict on a plan.

## Pipeline Flow
```
User Request → Archon (plan) → Ontos (audit) → Pragma (execute)
                  ▲                  │                  │
                  │     BLOCKED      │     BLOCKED      │
                  └──────────────────┘◄─────────────────┘
```

## State Tracking
The shared session state tracks pipeline progress:
- execution_plan: Set by Archon when a plan is produced.
- audit_report: Set by Ontos when an audit is complete.
- execution_report: Set by Pragma when code is generated.

Use these keys to understand where the user is in the pipeline.

## Routing Rules
1. NEW work (plan/build/design/create) → delegate to **archon**.
2. Plan exists, needs review → delegate to **ontos**.
3. Ontos said APPROVED → delegate to **pragma**.
4. Ontos said BLOCKED → delegate to **archon** with the findings.
5. General questions → answer directly, do NOT delegate.

## Communication Rules
- Always tell the user which agent you are routing to and why.
- After each agent returns, summarize what happened and suggest the next step.
- Example: "Archon has produced a plan. Would you like me to send it to
  Ontos for structural audit?"
""",
    sub_agents=[archon_agent, ontos_agent, pragma_agent],
    generate_content_config=types.GenerateContentConfig(temperature=0.2),
)
