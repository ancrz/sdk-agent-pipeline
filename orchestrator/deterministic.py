"""Deterministic stand-ins for the LLM, selected with VANGUARD_LLM_MODE=deterministic.

For running the whole core without a model quota (the free tier runs out) and
for reproducible smokes. Nothing else changes: the workers still run as ADK
agents behind A2A, still call the real aap-mcp tools with their worker token,
the Controller still enforces grants, and the planner's output still goes
through the same validation. Only the decision "what to call next" is a fixed
procedure instead of a model:

- WorkerLlm (ADK BaseLlm): reads the node's task text and the tool results so
  far and emits the next call: request_approval when the step is not covered
  by the plan approval, then job_templates_list → job_templates_launch_create →
  jobs_retrieve until the job ends. Honours error-edge steps ("Run ONLY IF x
  FAILED") and answers SUCCESS / FAILED / SKIPPED like the prompt asks.
- plan_completer: builds a Plan Graph from the planner prompt (document,
  inventory and supported actions blocks): one step per action/host mentioned,
  a gate before the first change, IP→DNS error-edge fallback, a check after
  changes, a documentalist report.
- author_playbook: a playbook for Phase 4 authoring from the goal and the
  closest approved template (Phase 4 runs lint/syntax/check on it like any draft).
- chat_llm (ADK BaseLlm): the chat orchestrator. A question (ends in "?" or
  starts with what/which/how/list/show/qué/cuál/cómo/lista/muestra…) calls the
  matching read tool by keyword (templates, nodes, run, plan, else knowledge
  search) and summarises the result; anything else is feedback → refine_plan.
- hash_embedding: 768-dim feature-hashing embedding (tokens + bigrams), L2
  normalised, so similar text lands close together without an API call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
from typing import Any, AsyncGenerator, Optional

EMBEDDING_DIMS = 768


def configured() -> bool:
    """VANGUARD_LLM_MODE=deterministic in the environment (also seeds the offline preset)."""
    return os.environ.get("VANGUARD_LLM_MODE", "gemini").strip().lower() == "deterministic"


def routed_by_gateway() -> bool:
    """VANGUARD_MODEL_ROUTING=gateway: every model call goes to the gateway, whose active
    preset decides between a provider and this stand-in (remediation R2)."""
    return os.environ.get("VANGUARD_MODEL_ROUTING", "").strip().lower() == "gateway"


def enabled() -> bool:
    """Use the stand-in in this process. Never when the gateway routes: it runs it there."""
    return configured() and not routed_by_gateway()


# ─────────────────────────────────────────────────────────────────────────────
# Embeddings
# ─────────────────────────────────────────────────────────────────────────────

def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.lower())


def hash_embedding(text: str, dims: int = EMBEDDING_DIMS) -> list[float]:
    vec = [0.0] * dims
    toks = _tokens(text)
    feats = toks + [f"{a} {b}" for a, b in zip(toks, toks[1:])]
    for f in feats:
        h = hashlib.sha256(f.encode()).digest()
        idx = int.from_bytes(h[:4], "big") % dims
        vec[idx] += 1.0 if h[4] & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# ─────────────────────────────────────────────────────────────────────────────
# Task text parsing (the format written by plangraph/compile.py and the executor)
# ─────────────────────────────────────────────────────────────────────────────

_TEMPLATE_RES = (
    re.compile(r"job template `([^`]+)`"),
    re.compile(r"request_approval\(action=\"([^\"]+)\""),
    re.compile(r"\b([a-z][a-z0-9_]*) job template\b"),
)
_TARGET_RES = (
    re.compile(r"Target host \(inventory name\): ([A-Za-z0-9_.\-]+?)\.?(?:\s|$)"),
    re.compile(r"job template `[^`]+` on `([^`]+)`"),
    re.compile(r"\bon ([a-z0-9][a-z0-9.\-]*-\d+)\b"),
)
_ONLY_IF_RE = re.compile(r"Run ONLY IF `([^`]+)` FAILED")
_UPSTREAM_RE = re.compile(r"^- ([a-z0-9_]+): (SUCCESS|FAILED|SKIPPED)\b", re.MULTILINE)
from .stepparams import param_pairs, render_pairs  # noqa: E402 — the one step-parameter parser
_TERMINAL = {"successful": "SUCCESS", "failed": "FAILED", "error": "FAILED", "canceled": "FAILED"}


def task_facts(text: str) -> dict[str, Any]:
    template = next((m.group(1) for rx in _TEMPLATE_RES for m in [rx.search(text)] if m), None)
    target = None
    for rx in _TARGET_RES:
        m = rx.search(text)
        if m:
            target = m.group(1).rstrip(".")
            break
    params: dict[str, Any] = {}
    for key, value in param_pairs(text):
        if key in {"action", "target", "address"}:
            continue
        params[key] = int(value) if value.isdigit() else value
    needs_approval = "request_approval(" in text and "no request_approval() call is needed" not in text
    return {
        "template": template, "target": target, "params": params, "needs_approval": needs_approval,
        "only_if_failed": _ONLY_IF_RE.findall(text), "upstream": dict(_UPSTREAM_RE.findall(text)),
    }


def _payload(response: Any) -> Any:
    """The JSON a tool returned: MCP results come as text content parts."""
    if isinstance(response, dict):
        if "structuredContent" in response and response["structuredContent"]:
            return response["structuredContent"]
        for key in ("content", "result"):
            if key in response:
                found = _payload(response[key])
                if found is not None:
                    return found
        if "text" in response and isinstance(response["text"], str):
            try:
                return json.loads(response["text"])
            except ValueError:
                return {"text": response["text"]}
        return response
    if isinstance(response, list):
        for item in response:
            found = _payload(item)
            if found is not None:
                return found
    if isinstance(response, str):
        try:
            return json.loads(response)
        except ValueError:
            return {"text": response}
    return None


def _is_error(response: Any) -> bool:
    return isinstance(response, dict) and bool(response.get("isError") or response.get("error"))


# ─────────────────────────────────────────────────────────────────────────────
# Worker model (ADK BaseLlm)
# ─────────────────────────────────────────────────────────────────────────────

def worker_llm(role: str):
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types as gt

    poll_s = float(os.environ.get("VANGUARD_DETERMINISTIC_POLL_S", "0.25"))   # jobs_retrieve itself waits (B2)
    max_polls = int(os.environ.get("VANGUARD_DETERMINISTIC_MAX_POLLS", "300"))

    def text(msg: str) -> "LlmResponse":
        return LlmResponse(content=gt.Content(role="model", parts=[gt.Part(text=msg)]))

    def call(name: str, args: dict) -> "LlmResponse":
        return LlmResponse(content=gt.Content(role="model", parts=[gt.Part(
            function_call=gt.FunctionCall(name=name, args=args))]))

    class DeterministicWorkerLlm(BaseLlm):
        async def generate_content_async(self, llm_request, stream: bool = False) -> AsyncGenerator[Any, None]:
            yield await self._next(llm_request)

        async def _next(self, llm_request):
            task = ""
            calls: list[tuple[str, dict]] = []
            results: list[tuple[str, Any]] = []
            for content in llm_request.contents or []:
                for part in content.parts or []:
                    if part.text and content.role == "user" and not task:
                        task = part.text
                    if part.function_call:
                        calls.append((part.function_call.name, dict(part.function_call.args or {})))
                    if part.function_response:
                        results.append((part.function_response.name, part.function_response.response or {}))
            if role == "medic":
                return await medic_step(task, calls, results, text, call, poll_s)
            if role == "author":
                name = next(iter(re.findall(r"Template name: ([a-z][a-z0-9_]*)", task)), "authored")
                goal = next(iter(re.findall(r"Goal: (.+)", task)), name)
                return text(f"SUCCESS drafted {name}\n```yaml\n{author_playbook(name, goal)}```")
            facts = task_facts(task)

            for src in facts["only_if_failed"]:
                if facts["upstream"].get(src) != "FAILED":
                    return text(f"SKIPPED `{src}` did not fail, so this fallback step is not needed.")

            template, target = facts["template"], facts["target"]
            granted: Optional[dict] = None
            if facts["needs_approval"]:
                approvals = [r for n, r in results if n == "request_approval"]
                if not approvals:
                    return call("request_approval", {"action": template or "(unspecified)", "target": target or ""})
                decision = approvals[-1]
                if decision.get("status") == "pending":
                    return text("Waiting for the human decision.")
                if not decision.get("approved"):
                    return text(f"SKIPPED not approved: {decision.get('reason') or 'no reason given'}")
                granted = decision.get("granted") or {}
                template = granted.get("template") or template
                target = granted.get("target") or target

            if role == "documentalist" or not template:
                lines = [f"{k}: {v}" for k, v in facts["upstream"].items()]
                return text("SUCCESS " + ("report — " + "; ".join(lines) if lines else "nothing to report"))

            listed = [r for n, r in results if n == "job_templates_list"]
            if not listed:
                return call("job_templates_list", {"search": template})
            if _is_error(listed[-1]):
                return text(f"FAILED job_templates_list: {str(_payload(listed[-1]))[:300]}")
            rows = (_payload(listed[-1]) or {}).get("results") or []
            match = next((t for t in rows if t.get("name") == template), None)
            if match is None:
                return text(f"FAILED job template `{template}` is not available.")

            launched = [r for n, r in results if n == "job_templates_launch_create"]
            if not launched:
                return call("job_templates_launch_create",
                            {"id": match["id"], "requestBody": {"extra_vars": facts["params"]}})
            launch = _payload(launched[-1]) or {}
            job_id = launch.get("job") or launch.get("id")
            if _is_error(launched[-1]) or not job_id:
                return text(f"FAILED launch refused: {str(launch)[:300]}")

            polls = [_payload(r) or {} for n, r in results if n == "jobs_retrieve"]
            if polls:
                status = polls[-1].get("status")
                if status in _TERMINAL:
                    outcome = _TERMINAL[status]
                    return text(f"{outcome} job {job_id} `{template}` on {target}: {status}")
                if len(polls) >= max_polls:
                    return text(f"FAILED job {job_id} still {status} after {len(polls)} polls")
                await asyncio.sleep(poll_s)
            return call("jobs_retrieve", {"id": job_id})

    return DeterministicWorkerLlm(model=f"deterministic-{role}")


# ─────────────────────────────────────────────────────────────────────────────
# Medic (docs/pipeline/remedy_*): a fixed diagnosis procedure for known failures
# ─────────────────────────────────────────────────────────────────────────────

_MISSING_DB_RE = re.compile(r'database \\?"([^"\\]+)\\?" does not exist')   # quotes may arrive escaped (JSON)
_PG_DOWN_RE = re.compile(r"Is the server running|could not connect to server|connection to server .* failed", re.I)
_CLUSTER_RE = re.compile(r"^(\d+)\s+(\S+)\s+\d+\s+(down|online)\b", re.MULTILINE)
_AD_HOC_DONE = {"successful", "failed", "error", "canceled"}
_NO_PYTHON_APT_RE = re.compile(r"No module named '?apt'?")
_APT_FIX = "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-apt"
_SERVICE_STOPPED_RE = re.compile(r"([A-Za-z0-9_.$-]+) is not running")   # FT8: mssql_check, a step's own check
_WIN_STATE_RE = re.compile(r'"state":\s*"(\w+)"')


def _text_of(response: Any) -> str:
    """A tool's text output (stdout endpoints answer plain text, wrapped by MCP)."""
    payload = _payload(response)
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("text", "content", "stdout"):
            if isinstance(payload.get(key), str):
                return payload[key]
        return json.dumps(payload)
    return str(payload or "")


def _medic_facts(task: str) -> dict[str, Any]:
    target = next(iter(re.findall(r"failed on `([^`]+)`", task)), "")
    job = next(iter(re.findall(r"Failing job: (\d+)", task)), None)
    mode = next(iter(re.findall(r"Mode: (fix|diagnose)", task)), "diagnose")
    names_line = next(iter(re.findall(r"Names this run's steps use: (.*)", task)), "")
    names = [n.strip() for n in names_line.split(",") if n.strip() and n.strip() != "(none)"]
    instruction = task.split("The step's instruction:", 1)[-1].split("What the step answered:", 1)[0]
    params = dict(param_pairs(instruction))
    platform = next(iter(re.findall(r"Platform: (windows|linux)", task)), "linux")
    return {"target": target, "job": int(job) if job else None, "mode": mode, "names": names, "params": params,
            "platform": platform}


async def medic_step(task: str, calls: list, results: list, text, call, poll_s: float):
    """One turn of the deterministic Medic: read the failing output, run diagnose (and, in fix
    mode, fix) commands through the Controller, answer a verdict, or raise its hand."""
    import difflib

    facts = _medic_facts(task)
    by_name: dict[str, list[Any]] = {}
    for name, response in results:
        by_name.setdefault(name, []).append(response)
    args_of: dict[str, list[dict]] = {}
    for name, args in calls:
        args_of.setdefault(name, []).append(args)

    hand = next((r for r in reversed(by_name.get("raise_hand", [])) if isinstance(r, dict) and r.get("decision")),
                None)
    mode = "fix" if hand and hand.get("decision") == "allow_fix" else facts["mode"]
    if hand and hand.get("decision") == "give_up":
        return text(f"FAILED a human stopped the remediation\nVERDICT: GAVE_UP {hand.get('text') or ''}".strip())
    if hand and hand.get("decision") == "subplan":
        return text("SUCCESS a human approved a sub-plan for this step\nVERDICT: SUBPLAN")
    if hand and hand.get("decision") == "hint":
        pairs = dict(param_pairs(hand.get("text") or ""))
        if pairs:
            return text(f"SUCCESS the hint names the variables to use\nVERDICT: RETRY_WITH {json.dumps(pairs)}")
        return text(f"FAILED the hint had no variables to use\nVERDICT: GAVE_UP {hand.get('text') or ''}".strip())
    waiting = [r for r in by_name.get("raise_hand", []) if isinstance(r, dict) and r.get("status") == "pending"]
    if waiting and not hand:
        return text("Waiting for the human answer.")

    stdout = ""
    if facts["job"] is not None:
        if not by_name.get("jobs_stdout_retrieve"):
            return call("jobs_stdout_retrieve", {"id": facts["job"]})
        stdout = _text_of(by_name["jobs_stdout_retrieve"][-1])

    # Each command: create, poll until it ends, read its stdout.
    created = [_payload(r) or {} for r in by_name.get("ad_hoc_commands_create", [])]
    polled = [_payload(r) or {} for r in by_name.get("ad_hoc_commands_retrieve", [])]
    outputs = [_text_of(r) for r in by_name.get("ad_hoc_commands_stdout_retrieve", [])]

    async def command(index: int, module: str, args: str):
        """None when command `index` has finished (its output is outputs[index]), else the next call."""
        if index >= len(created):
            return call("ad_hoc_commands_create", {"requestBody": {"module_name": module, "module_args": args,
                                                                    "limit": facts["target"]}})
        cmd = created[index]
        if not cmd.get("id"):
            return text(f"FAILED the Controller refused `{module} {args}`: {str(cmd)[:300]}\nVERDICT: GAVE_UP refused")
        mine = [p for p in polled if p.get("id") == cmd["id"]]
        if not mine or mine[-1].get("status") not in _AD_HOC_DONE:
            if mine:
                await asyncio.sleep(poll_s)
            return call("ad_hoc_commands_retrieve", {"id": cmd["id"]})
        if index >= len(outputs):
            return call("ad_hoc_commands_stdout_retrieve", {"id": cmd["id"]})
        return None

    missing = _MISSING_DB_RE.search(stdout)
    if missing:
        wrong = missing.group(1)
        own = set(facts["params"].values())         # the step's own values are not what it meant
        close = difflib.get_close_matches(wrong, [n for n in facts["names"] if n not in own], n=1, cutoff=0.6)
        key = next((k for k, v in facts["params"].items() if v == wrong), "db_name")
        if close:
            return text(f"SUCCESS `{wrong}` looks like a typo of `{close[0]}`, which this run uses\n"
                        f"VERDICT: RETRY_WITH {json.dumps({key: close[0]})}")
        owner = facts["params"].get("db_owner") or f"{wrong}_owner"
        return call("raise_hand", {
            "diagnosis": f"The database `{wrong}` does not exist on {facts['target']}, and no step of this run "
                         "creates it or uses a similar name.",
            "proposal_kind": "subplan",
            "proposal": f"postgres_create_db on {facts['target']} db_name={wrong} db_owner={owner}"})

    stopped = _SERVICE_STOPPED_RE.search(stdout) if facts["platform"] == "windows" else None
    if stopped:                                 # FT8: a Windows service is stopped — read it, then start it
        svc = stopped.group(1)
        step = await command(0, "win_service_info", f"name={svc}")
        if step is not None:
            return step
        state = _WIN_STATE_RE.search(outputs[0])
        if '"exists": true' not in outputs[0] or state is None:
            return call("raise_hand", {"diagnosis": f"The service {svc} does not exist on {facts['target']} "
                                                    "(win_service_info).",
                                       "proposal_kind": "hint", "proposal": "Install it, or fix the step's service name."})
        if state.group(1) != "stopped":
            return call("raise_hand", {"diagnosis": f"The service {svc} on {facts['target']} is {state.group(1)}, "
                                                    "not stopped: the step's failure has another cause.",
                                       "proposal_kind": "hint", "proposal": "Tell me what to look at."})
        if mode != "fix":
            return call("raise_hand", {"diagnosis": f"The service {svc} is stopped on {facts['target']} "
                                                    "(win_service_info).",
                                       "proposal_kind": "allow_fix", "proposal": f"win_service name={svc} state=started"})
        step = await command(1, "win_service", f"name={svc} state=started")
        if step is not None:
            return step
        result = next((p.get("status") for p in reversed(polled) if p.get("id") == created[1].get("id")), "")
        if result != "successful":
            return call("raise_hand", {"diagnosis": f"Starting {svc} failed: {outputs[1][:300]}",
                                       "proposal_kind": "hint", "proposal": "Look at the service's event log."})
        return text(f"SUCCESS the service {svc} was stopped; started it\nVERDICT: FIXED win_service {svc} started")

    if _NO_PYTHON_APT_RE.search(stdout):        # version drift: the apt module's bindings are missing
        step = await command(0, "command", "dpkg -s python3-apt")
        if step is not None:
            return step
        if "Status: install ok installed" in outputs[0]:
            return call("raise_hand", {"diagnosis": f"The apt module cannot import python3-apt on {facts['target']}, "
                                                    "but the package is installed: its Python may differ.",
                                       "proposal_kind": "hint", "proposal": "Check the host's Python interpreter."})
        if mode != "fix":
            return call("raise_hand", {"diagnosis": f"python3-apt is not installed on {facts['target']} "
                                                    "(dpkg -s); the apt module needs it for the host's Python.",
                                       "proposal_kind": "allow_fix", "proposal": _APT_FIX})
        step = await command(1, "shell", _APT_FIX)
        if step is not None:
            return step
        state = next((p.get("status") for p in reversed(polled) if p.get("id") == created[1].get("id")), "")
        if state != "successful":
            return call("raise_hand", {"diagnosis": f"Installing python3-apt failed: {outputs[1][:300]}",
                                       "proposal_kind": "hint", "proposal": "Check the host's package sources."})
        return text("SUCCESS the host's Python had no python3-apt (a new release); installed it\n"
                    "VERDICT: FIXED installed python3-apt")

    if _PG_DOWN_RE.search(stdout):
        step = await command(0, "command", "pg_lsclusters")
        if step is not None:
            return step
        cluster = _CLUSTER_RE.search(outputs[0])
        if cluster is None or cluster.group(3) != "down":
            return call("raise_hand", {"diagnosis": f"PostgreSQL does not answer on {facts['target']}, but "
                                                    f"pg_lsclusters shows: {outputs[0][:300]}",
                                       "proposal_kind": "hint", "proposal": "Check the server's configuration."})
        version, name = cluster.group(1), cluster.group(2)
        if mode != "fix":
            return call("raise_hand", {"diagnosis": f"The PostgreSQL cluster {version}/{name} is down on "
                                                    f"{facts['target']} (pg_lsclusters).",
                                       "proposal_kind": "allow_fix", "proposal": f"pg_ctlcluster {version} {name} start"})
        step = await command(1, "command", f"pg_ctlcluster {version} {name} start")
        if step is not None:
            return step
        state = next((p.get("status") for p in reversed(polled) if p.get("id") == created[1].get("id")), "")
        if state != "successful":
            return call("raise_hand", {"diagnosis": f"Starting cluster {version}/{name} failed: {outputs[1][:300]}",
                                       "proposal_kind": "hint", "proposal": "Look at the cluster's log."})
        return text(f"SUCCESS the cluster was down; started it\nVERDICT: FIXED pg_ctlcluster {version} {name} start")

    return call("raise_hand", {"diagnosis": f"Unrecognised failure on {facts['target']}: {stdout[-600:]}",
                               "proposal_kind": "hint", "proposal": "Tell me which variable or command to change."})


# ─────────────────────────────────────────────────────────────────────────────
# Planner (Completer: prompt -> Plan Graph JSON)
# ─────────────────────────────────────────────────────────────────────────────

_HOST_RE = re.compile(r"^- (\S+) os=(\S+) ip=(\S+) dns=(\S+)(.*)$", re.MULTILINE)
_GROUP_RE = re.compile(r"\bgroup:([a-z][a-z0-9_]*)")
_ACTION_RE = re.compile(r"^- (\S+) \((read-only|changes infrastructure)\)(?: · runs on ([a-z, ]+))?$", re.MULTILINE)


def _block(prompt: str, header: str) -> str:
    start = prompt.find(header)
    if start < 0:
        return ""
    rest = prompt[start + len(header):]
    end = rest.find("\n\n")
    return rest if end < 0 else rest[:end]


def _document(prompt: str) -> str:
    for header in ("Operator document:\n", "Operator document (excerpt):\n"):
        i = prompt.find(header)
        if i >= 0:
            doc = prompt[i + len(header):]
            for stop in ("\n\nYour previous plan", "\n\nValidation issues", "\n\nOperator feedback", "\n\nReturn the full"):
                j = doc.find(stop)
                if j >= 0:
                    doc = doc[:j]
            return doc
    return ""


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (s or "step")[:40]


# The health check a host's platform takes after a change (M6: health_check is Linux-only).
_VERIFY = {"linux": "health_check", "windows": "win_ping"}


def build_plan(document: str, hosts: list[dict], actions: dict[str, bool],
               platforms: Optional[dict[str, list[str]]] = None) -> dict:
    """hosts: [{name, os, ip, dns, usable}], actions: {name: read_only}, platforms: {name: [os, ...]}."""
    platforms = platforms or {}
    usable = [h for h in hosts if h["usable"]]
    by_name = {h["name"]: h for h in usable}
    group_names = {h.get("group") for h in usable if h.get("group")}
    default_hosts = [h["name"] for h in usable if h["name"] in document] or [h["name"] for h in usable[:1]]

    steps: list[tuple[str, str, str]] = []  # (action, host, params text)
    for line in document.splitlines():
        found = sorted(((line.find(a), a) for a in actions if re.search(rf"\b{re.escape(a)}\b", line)))
        if not found:
            continue
        groups = [f"group:{g}" for g in _GROUP_RE.findall(line) if g in group_names]
        line_hosts = groups or [h for h in by_name if h in line] or default_hosts
        params = render_pairs(param_pairs(line))
        for _, action in found:
            for host in line_hosts:
                if (action, host, params) not in steps:
                    steps.append((action, host, params))
    if not steps and default_hosts:
        fallback = "health_check" if "health_check" in actions else next(iter(sorted(actions)), None)
        if fallback:
            steps = [(fallback, h, "") for h in default_hosts]

    title = next((l.lstrip("# ").strip() for l in document.splitlines() if l.strip()), "Plan")[:120]
    nodes: list[dict] = [{"id": "start", "lane": "executor", "type": "start", "label": "Start"}]
    edges: list[dict] = []
    prev = ["start"]
    ids: set[str] = {"start"}
    gated = False
    changed_hosts: list[str] = []

    def add(node: dict) -> str:
        base, n = node["id"], 2
        while node["id"] in ids:
            node["id"] = f"{base}_{n}"[:48]
            n += 1
        ids.add(node["id"])
        nodes.append(node)
        return node["id"]

    def link(srcs: list[str], dst: str) -> None:
        for s in srcs:
            edges.append({"src": s, "dst": dst, "role": "main"})

    for action, host, params in steps:
        read_only = actions[action]
        if not read_only and not gated:
            gid = add({"id": "approve_changes", "lane": "executor", "type": "gate",
                       "label": "Operator approves infrastructure changes"})
            link(prev, gid)
            prev, gated = [gid], True
        kind, lane = ("check", "validator") if read_only else ("task", "executor")
        detail = f"Parameters: {params}" if params else ""
        h = by_name.get(host, {"ip": "-", "dns": "-"})  # group targets: no address kind
        both = h["ip"] not in ("-", "") and h["dns"] not in ("-", "")
        primary = add({"id": _slug(f"{action}_{host}"), "lane": lane, "type": kind,
                       "label": f"{action} on {host}"[:80], "detail": detail, "target": host, "action": action,
                       **({"address": "ip"} if both else {})})
        link(prev, primary)
        prev = [primary]
        if both:
            fb = add({"id": _slug(f"{action}_{host}_dns"), "lane": lane, "type": kind,
                      "label": f"{action} on {host} by DNS"[:80], "detail": detail, "target": host,
                      "action": action, "address": "dns"})
            edges.append({"src": primary, "dst": fb, "role": "error", "label": "IP failed"})
            prev = [primary, fb]
        if not read_only:
            changed_hosts.append(host)

    changed_by: dict[str, set[str]] = {}
    for action, host, _ in steps:
        if not actions[action]:
            changed_by.setdefault(host, set()).add(action)

    def verifier(host: str) -> Optional[str]:
        os_family = (by_name.get(host) or {}).get("os") or "linux"
        action = _VERIFY.get(os_family, "health_check")
        # A host changed through AD templates is verified with the AD operations account's check:
        # the member servers' admin (win_ping's credential) has no access to a domain controller.
        if os_family == "windows" and any(a.startswith("ad_") for a in changed_by.get(host, ())) and "ad_health" in actions:
            action = "ad_health"
        fits = os_family in platforms.get(action, [os_family])
        return action if action in actions and actions[action] and fits else None

    last_action = steps[-1][0] if steps else None
    for host in dict.fromkeys(changed_hosts):
        action = verifier(host)
        if action is None or action == last_action:
            continue
        cid = add({"id": _slug(f"verify_{host}"), "lane": "validator", "type": "check",
                   "label": f"Verify {host}", "target": host, "action": action})
        link(prev, cid)
        prev = [cid]

    rid = add({"id": "report", "lane": "documentalist", "type": "task", "label": "Closure report"})
    link(prev, rid)
    eid = add({"id": "end", "lane": "documentalist", "type": "end", "label": "End"})
    link([rid], eid)
    lanes = [{"id": lane, "label": lane.capitalize()} for lane in ("executor", "validator", "documentalist")
             if any(n["lane"] == lane for n in nodes)]
    return {"title": title, "summary": f"Deterministic plan: {len(steps)} step(s).",
            "lanes": lanes, "nodes": nodes, "edges": edges}


_BOX_RE = re.compile(r"^Box to change: id=(\S+) type=\S+ target=(\S+)", re.MULTILINE)
_BOX_EDGES_RE = re.compile(r"^Box neighbours: in=(\S+) out=(\S+)", re.MULTILINE)
_BEFORE_RE = re.compile(r"\b(before|antes|primero|first|previo)\b", re.IGNORECASE)


def node_patch(prompt: str, actions: dict[str, bool], hosts: list[str]) -> dict:
    """A node-scoped refine without a model: the feedback's steps become new boxes after the box
    (or before it when the feedback says so), on the box's target unless another host is named."""
    box = _BOX_RE.search(prompt)
    near = _BOX_EDGES_RE.search(prompt)
    feedback = _block(prompt, "Operator feedback for this box:\n")
    if not box or not near:
        return {"ops": []}
    box_id, box_target = box.group(1), box.group(2)
    def edges(text: str) -> list[tuple[str, str]]:     # "id:role,…" → [(id, role)]
        return [tuple(item.split(":", 1)) if ":" in item else (item, "main")  # type: ignore[misc]
                for item in ([] if text == "-" else text.split(","))]
    incoming, outgoing = edges(near.group(1)), edges(near.group(2))
    taken = set(re.findall(r"^n ([a-z0-9_]+)\|", prompt, re.MULTILINE))
    new_ids: list[str] = []
    ops: list[dict] = []
    for action in [a for a in actions if re.search(rf"\b{re.escape(a)}\b", feedback)]:
        target = next((h for h in hosts if h in feedback), None) or (box_target if box_target != "-" else None)
        if target is None:
            continue
        base = _slug(f"{action}_{target}")
        nid, n = base, 2
        while nid in taken:
            nid, n = f"{base}_{n}"[:48], n + 1
        taken.add(nid)
        params = render_pairs(param_pairs(feedback))
        read_only = actions[action]
        ops.append({"op": "add_node", "node": {
            "id": nid, "lane": "validator" if read_only else "executor", "type": "check" if read_only else "task",
            "label": f"{action} on {target}"[:80], "detail": f"Parameters: {params}" if params else "",
            "target": target, "action": action}})
        new_ids.append(nid)
    if not new_ids:
        return {"ops": []}
    chain = list(zip(new_ids, new_ids[1:]))
    if _BEFORE_RE.search(feedback):
        for src, role in incoming:                    # the new steps take the box's place, roles kept
            ops += [{"op": "remove_edge", "edge": {"src": src, "dst": box_id}},
                    {"op": "add_edge", "edge": {"src": src, "dst": new_ids[0], "role": role}}]
        last = new_ids[-1]
        ops.append({"op": "add_edge", "edge": {"src": last, "dst": box_id, "role": "main"}})
    else:
        for dst, role in outgoing:
            if role == "error":                       # the box's fallback stays the box's
                continue
            ops += [{"op": "remove_edge", "edge": {"src": box_id, "dst": dst}},
                    {"op": "add_edge", "edge": {"src": new_ids[-1], "dst": dst, "role": role}}]
        ops.append({"op": "add_edge", "edge": {"src": box_id, "dst": new_ids[0], "role": "main"}})
    ops += [{"op": "add_edge", "edge": {"src": a, "dst": b, "role": "main"}} for a, b in chain]
    return {"ops": ops}


def plan_completer():
    async def complete(prompt: str) -> str:
        hosts = [{"name": m[0], "os": m[1], "ip": m[2], "dns": m[3], "usable": "DISABLED" not in m[4],
                  "group": next(iter(re.findall(r"group=(\S+)", m[4])), None)}
                 for m in _HOST_RE.findall(_block(prompt, "Inventory hosts:\n"))]
        found = _ACTION_RE.findall(_block(prompt, "Supported actions:\n"))
        actions = {m[0]: m[1] == "read-only" for m in found}
        platforms = {m[0]: [p.strip() for p in m[2].split(",")] for m in found if m[2]}
        if "Box to change: " in prompt:                       # a node-scoped refine answers with a patch
            return json.dumps(node_patch(prompt, actions, [h["name"] for h in hosts if h["usable"]]))
        # Refinement: the operator's feedback lines are extra steps, like the document's.
        document = _document(prompt) + "\n" + _block(prompt, "Operator feedback to apply:\n")
        return json.dumps(build_plan(document, hosts, actions, platforms))

    return complete


# ─────────────────────────────────────────────────────────────────────────────
# Template author (Phase 4)
# ─────────────────────────────────────────────────────────────────────────────

def author_playbook(name: str, goal: str) -> str:
    """A small, check-safe playbook for the goal: package/service/port/file facts
    are read, never changed, unless the goal asks to install or start something."""
    wants = goal.lower()
    package = next(iter(re.findall(r"package[s]?\s+([a-z0-9][a-z0-9.+\-]*)", wants)), None)
    service = next(iter(re.findall(r"service\s+([a-z0-9][a-z0-9@.\-]*)", wants)), None)
    port = next(iter(re.findall(r"port\s+(\d{1,5})", wants)), None)
    tasks = [
        "    - name: Gather minimal facts\n      ansible.builtin.setup:\n        gather_subset: [min]\n",
    ]
    if package:
        state = "present" if re.search(r"\b(install|ensure)\b", wants) else None
        if state:
            tasks.append(f"    - name: Ensure package {package} is installed\n      ansible.builtin.package:\n"
                         f"        name: {package}\n        state: present\n      become: true\n")
        else:
            tasks.append("    - name: Collect package facts\n      ansible.builtin.package_facts:\n")
            tasks.append(f"    - name: Report whether {package} is installed\n      ansible.builtin.debug:\n"
                         f"        msg: \"{package} installed: {{{{ '{package}' in ansible_facts.packages }}}}\"\n")
    if service:
        tasks.append("    - name: Collect service facts\n      ansible.builtin.service_facts:\n")
        tasks.append(f"    - name: Report service {service}\n      ansible.builtin.debug:\n"
                     f"        msg: \"{{{{ ansible_facts.services.get('{service}.service', {{}}).get('state', 'absent') }}}}\"\n")
    if port:
        tasks.append(f"    - name: Check TCP port {port}\n      ansible.builtin.wait_for:\n"
                     f"        port: {port}\n        timeout: 5\n      register: port_check\n"
                     f"      failed_when: false\n")
        tasks.append(f"    - name: Report TCP port {port}\n      ansible.builtin.debug:\n"
                     f"        msg: \"port {port} open: {{{{ 'Timeout' not in (port_check.msg | default('')) }}}}\"\n")
    tasks.append(f"    - name: Done\n      ansible.builtin.debug:\n        msg: \"{name} finished\"\n")
    return (f"---\n# Authored deterministically for: {goal.strip()[:200]}\n"
            f"- name: {name.replace('_', ' ').capitalize()}\n  hosts: \"{{{{ target_host }}}}\"\n  gather_facts: false\n  tasks:\n" + "".join(tasks))


# ─────────────────────────────────────────────────────────────────────────────
# Chat orchestrator (Phase F3, slice 5)
# ─────────────────────────────────────────────────────────────────────────────

_QUESTION_RE = re.compile(
    r"^\s*(what|which|how|why|when|where|who|list|show|tell|is|are|can|does|do|"
    r"qu[eé]|cu[aá]l(es)?|c[oó]mo|por\s*qu[eé]|cu[aá]ndo|d[oó]nde|lista|listar|muestra|mu[eé]strame|dime|hay|"
    r"estado|status)\b", re.IGNORECASE)
_TOOL_KEYWORDS = (
    ("search_flows", re.compile(r"\bflows?\b|\bflujos?\b|guardad|saved|repetitiv|recurrent", re.IGNORECASE)),
    ("list_templates", re.compile(r"templat|plantilla|playbook", re.IGNORECASE)),
    ("list_nodes", re.compile(r"\bnod[eo]s?\b|\bhosts?\b|servidor|inventor|target", re.IGNORECASE)),
    ("run_status", re.compile(r"\brun\b|ejecuci|estado|status|progres|avance|kanban|job", re.IGNORECASE)),
    ("current_plan", re.compile(r"\bplan\b|steps?|pasos?", re.IGNORECASE)),
)


def is_question(text: str) -> bool:
    text = text.strip()
    return text.endswith("?") or text.startswith("¿") or bool(_QUESTION_RE.match(text))


def chat_tool_for(text: str) -> tuple[str, dict]:
    """The tool the deterministic orchestrator calls for an operator message."""
    if not is_question(text):
        return "refine_plan", {"feedback": text}
    for name, pattern in _TOOL_KEYWORDS:
        if pattern.search(text):
            return name, {}
    return "search_knowledge", {"query": text}


def chat_summary(name: str, result: dict) -> str:
    """A plain-text answer from one tool result."""
    if "error" in result:
        return str(result["error"])
    if name == "list_templates":
        items = [f"`{t['name']}` v{t['version']} ({'changes the host' if t['mutating'] else 'read-only'})"
                 for t in result.get("templates", [])]
        return "Approved templates: " + (", ".join(items) if items else "none") + "."
    if name == "list_nodes":
        items = [f"`{n['name']}` ({'automatable' if n['automatable'] else n['state']})" for n in result.get("nodes", [])]
        return "Inventory nodes: " + (", ".join(items) if items else "none") + "."
    if name == "search_flows":
        items = [f"`{f['name']}` (id {f['id']}, {f['steps']} step(s){', owner ' + f['owner'] if f['owner'] else ''})"
                 for f in result.get("flows", [])]
        return ("Saved flows: " + ", ".join(items) + ". Pick one with “Start from a saved flow”."
                if items else "No saved flow matches.")
    if name == "run_status":
        nodes = result.get("nodes", [])
        detail = ", ".join(f"`{n['node']}` {n['outcome'] or n['state']}" for n in nodes)
        return f"The run is {result.get('status')}." + (f" Nodes: {detail}." if detail else "")
    if name in ("current_plan", "refine_plan"):
        steps = result.get("steps", [])
        errors = [i for i in result.get("issues", []) if i.get("severity") == "error"]
        head = "I refined the plan with your feedback." if name == "refine_plan" else f"Plan **{result.get('title')}**."
        return (f"{head} {len(steps)} step(s): "
                + ", ".join(f"`{st['action']}` on `{st['target']}`" for st in steps if st.get("action"))
                + (f". {len(errors)} error(s) to fix before approving." if errors else "."))
    hits = result.get("hits", [])
    if not hits:
        return "I found nothing about that in the approved playbooks or past jobs."
    return "Closest matches: " + "; ".join(f"[{h['source']}] {h['content'][:160]}" for h in hits[:3])


def chat_llm():
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types as gt

    class DeterministicChatLlm(BaseLlm):
        async def generate_content_async(self, llm_request, stream: bool = False) -> AsyncGenerator[Any, None]:
            prompt, response = "", None
            for content in llm_request.contents or []:
                for part in content.parts or []:
                    if part.text and content.role == "user" and not prompt:
                        prompt = part.text
                    if part.function_response:
                        response = (part.function_response.name, part.function_response.response or {})
            if response is None:
                message = prompt.rsplit("Operator message:\n", 1)[-1].strip()
                name, args = chat_tool_for(message)
                yield LlmResponse(content=gt.Content(role="model", parts=[gt.Part(
                    function_call=gt.FunctionCall(name=name, args=args))]))
                return
            name, result = response
            yield LlmResponse(content=gt.Content(role="model", parts=[gt.Part(text=chat_summary(name, result))]))

    return DeterministicChatLlm(model="deterministic-chat")
