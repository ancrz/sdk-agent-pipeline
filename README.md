# SDK Agent Pipeline — Agent Engine

A modular **agent engine** for other projects: Google ADK agents served over **A2A**, and a
**highly available model gateway** that is the only holder of model provider keys. Extracted
from [vanguard-control](https://github.com/ancrz/vanguard-control), where both were built and
hardened (gateway F3/F20/R2, G-AGENTS A2–A4); this repository is their project-agnostic form.
Provider keys can live in [vault-transit-stack](https://github.com/ancrz/vault-transit-stack)
(OpenBao), the sibling module.

> **What changed.** This repository used to be *Topos Pipeline*: a six-stage ADK pipeline
> (Archon → Ontos → Pragma → Graphos → Dokimos → Hermon) calling Gemini directly with a
> client-side rate limiter. That design is retired (see git history before `feat!: agent
> engine`). The planning is done by the consuming application's **chat** — the superior model,
> reached through the gateway as the `orchestrator` role — and only the **smaller agent roles
> that do useful work** run here, each with its own small model, token, limits and audit.

---

## Architecture

```mermaid
flowchart LR
    subgraph CONSUMER["Consuming project (joins engine-agents-net)"]
        CHAT["chat / planner<br/>role orchestrator<br/>(superior model)"]
        DISP["dispatcher<br/>A2A client"]
    end

    subgraph AGENTS["engine-agents-net · internal"]
        HA{{"model-gateway-ha<br/>(DNS alias of both replicas)"}}
        GA["model-gateway"]
        GB["model-gateway-b"]
        W["worker-agents<br/>one ADK agent per role<br/>/documentalist/ · /author/ · /executor/ …"]
    end

    subgraph DATA["engine-data-net · internal"]
        PG[("Postgres<br/>profiles · credentials refs · model_calls<br/>prices · agent_teams · a2a_tasks · adk")]
        VK[("Valkey<br/>RPM slots · breakers<br/>daily counters")]
    end

    subgraph EGRESS["engine-egress-net"]
        PROV["Gemini · Vertex AI · OpenAI<br/>Anthropic · DeepSeek · compatible"]
    end

    subgraph OPT["optional"]
        OL["ollama (profile local-llm)<br/>local fallback"]
        BAO["vault-transit-stack<br/>openbao-secrets:8200"]
        AAP["Ansible Controller MCP<br/>(AAP_MCP_URL)"]
    end

    CHAT -- "x-goog-api-key = orchestrator token" --> HA
    DISP -- "A2A message + metadata<br/>(run, addendum, team)" --> W
    W -- "per-role gateway token" --> HA
    HA --> GA & GB
    GA & GB --> PG
    GA & GB --> VK
    GA & GB --> PROV
    GA & GB -.-> OL
    GA & GB -. "AppRole gateway: read agent-ai/" .-> BAO
    W --> PG
    W -. "executor · validator · medic" .-> AAP
```

| Piece | What it does |
|---|---|
| **model-gateway** ×2 | Speaks the Gemini REST protocol, so ADK/google-genai clients work unchanged with `GOOGLE_GEMINI_BASE_URL`. Each caller's "API key" is its **gateway token**; the gateway derives the role from it, resolves the role's model and credential (**the role cannot pick a pricier model**), injects the real provider key, enforces limits, walks the failover chain and writes an audit row. Two replicas answer the same DNS name; Docker's DNS returns only running containers, and all shared state is in Valkey. |
| **worker-agents** | One process serving one ADK agent per role (G-AGENTS A4), each built from an `AgentSpec` (instruction, skills, how it asks a human, which Controller tools it may call). One role is served at the root; several each under `/<role>/`. Tasks and ADK sessions persist in Postgres, so an `INPUT_REQUIRED` task survives a restart. |
| **store/** | The data contract: SQLAlchemy models, model resolution, presets, failover chains, prices, notifications, schema bootstrap (`python -m store.schema`) and the admin CLI (`python -m store.admin`). |
| **orchestrator/** | Runtime shared by gateway and workers: per-role models, Valkey RPM limiter, Fernet credentials, the OpenBao client, activity attribution, base instructions + addenda, and the deterministic stand-in. |

### Roles

| Role | Default model | Tools | Use |
|---|---|---|---|
| `orchestrator` | `VANGUARD_MODEL_ORCHESTRATOR` | — (no worker: the consumer's chat calls the gateway) | planning, chat; the superior model |
| `documentalist` | default (flash-lite) | `request_approval` | reports and traces from what it is given — **enabled by default** |
| `author` | default | `request_approval`, knowledge MCP | drafts Ansible job templates; never runs them |
| `executor` | default | `request_approval`, Controller MCP | runs approved job templates |
| `validator` | default | `request_approval`, Controller MCP (check mode) | validates state |
| `medic` | default | `raise_hand`, Controller MCP (ad hoc, bounded) | diagnoses and fixes a failed step |
| `embeddings` | `VANGUARD_EMBEDDING_MODEL` | — | vector embeddings through the gateway |

`executor`, `validator` and `medic` need an Ansible Controller's MCP front (vanguard-control's
`aap-mcp`); add them to `ENGINE_AGENT_ROLES` with `AAP_MCP_URL` set.

---

## How a call is routed

```mermaid
flowchart TD
    REQ(["POST /v1beta/models/*:generateContent<br/>x-goog-api-key: gateway token"]) --> AUTH{"token → role<br/>(constant-time)"}
    AUTH -- unknown --> R401["401"]
    AUTH --> RES["resolve the role in the active preset<br/>profile → preset default → .env"]
    RES --> TEAM{"X-Vanguard-Activity run:&lt;id&gt;<br/>with an agent team?"}
    TEAM -- yes --> FIRST["team's model first<br/>on the first cloud route (A3)"]
    TEAM -- no --> CHAIN
    FIRST --> CHAIN["failover chain<br/>primary → fallback model → backup keys<br/>→ local model → deterministic stand-in"]
    CHAIN --> LIM{"RPM / RPD / daily tokens<br/>(Valkey, shared by replicas)"}
    LIM -- "no slot within MAX_WAIT" --> R429["429 + Retry-After (audited)"]
    LIM --> KEY["credential: OpenBao (AppRole) or Fernet<br/>stale-if-error cache"]
    KEY --> UP["provider call"]
    UP -- "auth error / 5xx / no network" --> BRK["open breaker (Valkey)<br/>notify · next route"]
    BRK --> CHAIN
    UP -- ok --> AUD["model_calls row: role, models, account,<br/>tokens, latency, cost, activity"]
    AUD --> RESP(["response in Gemini's shape"])
```

Bodies are never stored or logged. Notifications (`gateway.breaker_opened`, `failover`,
`local_fallback`, `internet_down`, `recovered`, `last_resort`, `secrets.unavailable`) are rows
in `notifications`, grouped by key, and log lines.

## How a task reaches an agent

```mermaid
sequenceDiagram
    participant C as Consumer (dispatcher)
    participant W as worker-agents /role/
    participant G as model-gateway-ha
    participant P as Postgres
    C->>W: SendMessage(text, metadata: vanguard_activity=run:id,<br/>vanguard_instructions={version,text,team,team_text})
    W->>P: a2a_tasks + adk session
    W-->>C: task id (return_immediately)
    loop model calls
        W->>W: instruction = base + operator addendum (A2) + team addendum (A3)
        W->>G: generateContent · role token · X-Vanguard-Activity
        G-->>W: answer (the role's model, or the team's)
    end
    C->>W: GetTask(id) until COMPLETED / FAILED / INPUT_REQUIRED
    W-->>C: artifact text: SUCCESS | FAILED | SKIPPED …
```

The metadata contract (`orchestrator/instructions.py`, `orchestrator/activity.py`):

| Key | Value | Effect |
|---|---|---|
| `vanguard_activity` | `run:<uuid>` | the gateway attributes the calls (and their cost) to that run, and applies the run's agent team |
| `vanguard_instructions` | JSON `{"version": n, "text": "...", "team": "name", "team_text": "..."}` | added under the base instruction of tunable roles (executor, validator, medic); cannot remove the base |
| `vanguard_worker_token` | the Controller's per-node token | the Controller MCP's bearer; never in model-visible text |

---

## Quick start

```bash
git clone https://github.com/ancrz/sdk-agent-pipeline.git
cd sdk-agent-pipeline
python3 scripts/bootstrap_env.py          # .env with generated passwords and tokens
./scripts/build.sh                        # base → model-gateway → worker
docker compose up -d --wait               # postgres, valkey, schema, gateway ×2, worker-agents

# Offline first: every answer from the deterministic stand-in, no key needed
docker compose --profile admin run --rm admin preset activate offline
./scripts/smoke.sh
```

Expected:

```text
[ok] model-gateway /healthz 200
[ok] model-gateway-b /healthz 200
[ok] model-gateway-ha /healthz 200
[ok] orchestrator → gateway 200 in 0.3s: '{"title": "Plan", …'
[ok] worker-agents serves one role at the root: vanguard_documentalist
[ok] A2A documentalist: TASK_STATE_COMPLETED in 1.0s
```

Then a real provider:

```bash
echo -n "$GEMINI_KEY" | docker compose --profile admin run --rm -T admin \
  credential put gemini-main --provider gemini --tier paid
docker compose --profile admin run --rm admin profile set default --model gemini-3.5-flash-lite --credential gemini-main
docker compose --profile admin run --rm admin profile set orchestrator --model gemini-3.5-pro --credential gemini-main
docker compose --profile admin run --rm admin preset activate custom
docker compose --profile admin run --rm admin status
```

(Or just set `GOOGLE_API_KEY` in `.env`: the gateway's last resolution step.)

---

## With OpenBao (vault-transit-stack)

```mermaid
flowchart LR
    subgraph VTS["vault-transit-stack"]
        BAO["openbao<br/>alias openbao-secrets"]
        KMS["openbao-kms"]
        KMS --- BAO
    end
    subgraph ENG["agent engine"]
        ADM["admin CLI<br/>AppRole writer"]
        GA["model-gateway"]
        GB["model-gateway-b"]
        PG[("Postgres<br/>credentials.secret_ref only")]
    end
    ADM -- "write agent-ai/credentials/&lt;name&gt;<br/>(cannot read back)" --> BAO
    GA -- "AppRole gateway: read" --> BAO
    GB -- "AppRole gateway: read" --> BAO
    ADM --> PG
    GA --> PG
```

```bash
# 1. the store (its default consumers.json declares agent-ai/, gateway and writer)
(cd ../vault-transit-stack && ./scripts/start.sh)

# 2. the engine: in .env
#    COMPOSE_FILE=docker-compose.yml:docker-compose.openbao.yml
#    VANGUARD_SECRET_BACKEND=openbao
#    VAULT_STACK_DIR=../vault-transit-stack
docker compose up -d --wait
echo -n "$GEMINI_KEY" | docker compose --profile admin run --rm -T admin credential put gemini-main --provider gemini
```

Postgres then holds only `secret_ref = agent-ai/credentials/gemini-main`. The gateway logs in
with its own AppRole (bound to the store's secrets subnet), caches a read for
`VANGUARD_SECRET_CACHE_S` and serves the last value for `VANGUARD_SECRET_STALE_S` if the store
is sealed or unreachable; with nothing cached, reads **fail closed**.

---

## Agent teams and addenda

```bash
docker compose --profile admin run --rm admin team set sql-scripts \
  --role executor --model gemini-3.5-flash --addendum "Prefer psql; quote every identifier."
docker compose --profile admin run --rm admin run new nightly-report --team sql-scripts
# → <run id> and the A2A metadata to send with the executor's tasks
```

A run with a team makes the gateway put the team's model first for the roles it names (cloud
routes only; offline and local chains stay reproducible). The addendum travels in the task's
metadata, after the operator's (A2) and under the base instruction.

---

## Operations

| Task | Command |
|---|---|
| Status: preset, profiles, credentials, teams, call counts | `docker compose --profile admin run --rm admin status` |
| Switch preset (no restart) | `… admin preset activate offline \| demo-free \| paid \| custom \| local` |
| More roles in the worker | `ENGINE_AGENT_ROLES=documentalist,author` → `docker compose up -d worker-agents` |
| Local model fallback | `docker compose --profile local-llm up -d` (Ollama; only the gateway reaches it) |
| Calls and cost per role | `docker compose exec postgres psql -U engine -c "select role, count(*), sum(cost_usd) from model_calls group by 1"` |
| Notifications | `… psql -U engine -c "select event, severity, title, count, last_at from notifications order by last_at desc"` |
| Gateway admin API (agents network) | `GET /admin/serving`, `DELETE /admin/breakers?prefix=…` with `Authorization: Bearer $VANGUARD_GATEWAY_ADMIN_TOKEN` |
| Tests | `pip install -r agents/base/requirements.txt pytest pytest-asyncio aiosqlite && pytest` |

---

## Configuration

`.env.example` documents every variable. The essentials:

| Variable | Default | Description |
|---|---|---|
| `VANGUARD_GATEWAY_TOKEN_<ROLE>` | generated | one token per role: the gateway derives role, model and limits from it |
| `VANGUARD_GATEWAY_ADMIN_TOKEN` | generated | the gateway's admin API |
| `VANGUARD_DEFAULT_MODEL` / `VANGUARD_MODEL_<ROLE>` | `gemini-3.5-flash-lite` / empty | the `.env` step of model resolution |
| `ENGINE_AGENT_ROLES` | `documentalist` | roles served by `worker-agents` |
| `VANGUARD_SECRET_BACKEND` | `local` | `local` (Fernet, `VANGUARD_CREDENTIALS_KEY`) or `openbao` |
| `VANGUARD_BAO_MOUNTS` | `{"model_provider":"agent-ai"}` | the store's mount names (vanguard-control uses `vanguard-ai`) |
| `VANGUARD_GATEWAY_RPM_FREE` / `_PAID` / `VANGUARD_GATEWAY_RPM` | `10` / `0` / empty | requests per minute per account and model (0 = unlimited) |
| `VANGUARD_GATEWAY_MAX_WAIT_S` | `30` | how long a request waits for its RPM slot before a 429 |
| `VANGUARD_LLM_MODE` | `gemini` | `deterministic`: the first schema run seeds the offline preset active |

Variables keep vanguard-control's `VANGUARD_*` names, so code and settings move between both
projects unchanged.

---

## Project structure

```text
sdk-agent-pipeline/
├── docker-compose.yml           # postgres, valkey, schema, model-gateway ×2, worker-agents (+ admin, local-llm)
├── docker-compose.openbao.yml   # overlay: AppRoles and the secrets network of vault-transit-stack
├── .env.example
├── agents/base/                 # base image: ADK, A2A, MCP, FastAPI, SQLAlchemy, Valkey
├── model_gateway/               # the gateway (app, providers, failover, local models, signatures) + tests
├── workers/                     # AgentSpec registry, ADK agent factory, A2A entrypoint (one or many roles)
├── orchestrator/                # runtime: models, ratelimit, credentials, secrets, activity, instructions, deterministic
├── store/                       # models, services, prices, schema bootstrap, admin CLI
├── tests/                       # activity header, OpenBao client, worker specs and instructions
└── scripts/                     # bootstrap_env.py, build.sh, smoke.sh / smoke.py
```

### Extraction map (from vanguard-control)

| Here | vanguard-control | Adaptation |
|---|---|---|
| `model_gateway/` | `model_gateway/` | imports `api.*` → `store.*`; the agent team is read through `activity_runs.agent_team_id` (there: run → plan → team) |
| `workers/` | `workers/` | none |
| `orchestrator/` | `activity, credentials, secrets, models, deterministic, stepparams, ratelimit, instructions` | `secrets.mounts()`: mount names from `VANGUARD_BAO_MOUNTS` |
| `store/models/` | `api/models/automation.py`, `activity.py`, `notification.py` | only the gateway's tables; runs and notifications without plans, users or inventories |
| `store/services/` | `api/services/models, presets, failover, prices, credentials, notify` | `notify`: no recipients (rows + logs); `credentials`: `store()` only |
| `store/schema.py` | Alembic history | `create_all` + `a2a_tasks` from a2a-sdk + schema `adk` + seeds |
| `store/admin.py` | the API's AI Gateway screens | CLI |

Not extracted (vanguard-control's domain): the plan graph compiler and SDD planner, the
Controller and its MCP front, the knowledge MCP, users/RBAC, the operator's versioned addenda
store (the engine receives addenda in the metadata). Comments that cite `docs/pipeline/*`
refer to vanguard-control's design records.
