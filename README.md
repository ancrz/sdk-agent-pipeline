<a id="readme-top"></a>

# SDK Agent Pipeline — Agent Engine

A modular **agent engine** for other projects: Google ADK agents served over **A2A**, and a
**highly available model gateway** that is the only holder of model provider keys. Provider
keys can live in [vault-transit-stack](https://github.com/ancrz/vault-transit-stack) (OpenBao),
the sibling module.

The planning is done by the consuming application's **chat** — the superior model, reached
through the gateway as the `orchestrator` role — and only the **smaller agent roles that do
useful work** run here, each with its own small model, token, limits and audit.

## Purpose: an independent blueprint

The engine is **self-contained**: it builds, runs and is tested on its own, with no other
project needed at run time. It is a **blueprint to transplant** into any project that needs an
agentic core:

| A project that needs… | Takes |
|---|---|
| one gateway in front of every model call (keys, limits, failover, audit, cost) | `model_gateway/` + `store/` + Postgres + Valkey |
| small agents doing bounded work over A2A | `workers/` (+ the gateway) |
| its own tools for those agents | an MCP server declared in `ENGINE_MCP_SERVERS` ([docs/mcp.md](docs/mcp.md)) |
| provider keys out of the database | [vault-transit-stack](https://github.com/ancrz/vault-transit-stack) + `docker-compose.openbao.yml` |

<p align="right">(<a href="#readme-top">back to top</a>)</p>

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
        AAP["Ansible Controller MCP<br/>(AAP_MCP_URL, reference)"]
        MCP["any MCP server<br/>(ENGINE_MCP_SERVERS)"]
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
    W -. "allowlist + tokens per request" .-> MCP
```

| Piece | What it does |
|---|---|
| **model-gateway** ×2 | Speaks the Gemini REST protocol, so ADK/google-genai clients work unchanged with `GOOGLE_GEMINI_BASE_URL`. Each caller's "API key" is its **gateway token**; the gateway derives the role from it, resolves the role's model and credential (**the role cannot pick a pricier model**), injects the real provider key, enforces limits, walks the failover chain and writes an audit row. Two replicas answer the same DNS name; Docker's DNS returns only running containers, and all shared state is in Valkey. |
| **worker-agents** | One process serving one ADK agent per role, each built from an `AgentSpec` (instruction, skills, how it asks a human, which Controller tools it may call). One role is served at the root; several each under `/<role>/`. Tasks and ADK sessions persist in Postgres, so an `INPUT_REQUIRED` task survives a restart. |
| **store/** | The data contract: SQLAlchemy models, model resolution, presets, failover chains, prices, notifications, schema bootstrap (`python -m store.schema`) and the admin CLI (`python -m store.admin`). |
| **MCP tools** | Each role can use tools from MCP servers: two wired in code (the Ansible Controller's aap-mcp front, the knowledge server) and **any other declared by configuration** with an allowlist and per-request tokens — see [docs/mcp.md](docs/mcp.md). |
| **orchestrator/** | Runtime shared by gateway and workers: per-role models, Valkey RPM limiter, Fernet credentials, the OpenBao client, activity attribution, base instructions + addenda, and the deterministic stand-in. |

### Roles

| Role | Default model | Tools | Use |
|---|---|---|---|
| `orchestrator` | `ENGINE_MODEL_ORCHESTRATOR` | — (no worker: the consumer's chat calls the gateway) | planning, chat; the superior model |
| `documentalist` | default (flash-lite) | `request_approval` | reports and traces from what it is given — **enabled by default** |
| `author` | default | `request_approval`, knowledge MCP | drafts Ansible job templates; never runs them |
| `executor` | default | `request_approval`, Controller MCP | runs approved job templates |
| `validator` | default | `request_approval`, Controller MCP (check mode) | validates state |
| `medic` | default | `raise_hand`, Controller MCP (ad hoc, bounded) | diagnoses and fixes a failed step |
| `embeddings` | `ENGINE_EMBEDDING_MODEL` | — | vector embeddings through the gateway |

`executor`, `validator` and `medic` need an Ansible Controller's MCP front (an
`aap-mcp-server`); add them to `ENGINE_AGENT_ROLES` with `AAP_MCP_URL` set.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

---

## How a call is routed

```mermaid
flowchart TD
    REQ(["POST /v1beta/models/*:generateContent<br/>x-goog-api-key: gateway token"]) --> AUTH{"token → role<br/>(constant-time)"}
    AUTH -- unknown --> R401["401"]
    AUTH --> RES["resolve the role in the active preset<br/>profile → preset default → .env"]
    RES --> TEAM{"X-Engine-Activity run:&lt;id&gt;<br/>with an agent team?"}
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

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## How a task reaches an agent

```mermaid
sequenceDiagram
    participant C as Consumer (dispatcher)
    participant W as worker-agents /role/
    participant G as model-gateway-ha
    participant P as Postgres
    C->>W: SendMessage(text, metadata: engine_activity=run:id,<br/>engine_instructions={version,text,team,team_text})
    W->>P: a2a_tasks + adk session
    W-->>C: task id (return_immediately)
    loop model calls
        W->>W: instruction = base + operator addendum (A2) + team addendum (A3)
        W->>G: generateContent · role token · X-Engine-Activity
        G-->>W: answer (the role's model, or the team's)
    end
    C->>W: GetTask(id) until COMPLETED / FAILED / INPUT_REQUIRED
    W-->>C: artifact text: SUCCESS | FAILED | SKIPPED …
```

The metadata contract (`orchestrator/instructions.py`, `orchestrator/activity.py`):

| Key | Value | Effect |
|---|---|---|
| `engine_activity` | `run:<uuid>` | the gateway attributes the calls (and their cost) to that run, and applies the run's agent team |
| `engine_instructions` | JSON `{"version": n, "text": "...", "team": "name", "team_text": "..."}` | added under the base instruction of tunable roles (executor, validator, medic); cannot remove the base |
| `engine_worker_token` | the Controller's per-node token | the Controller MCP's bearer; never in model-visible text |

<p align="right">(<a href="#readme-top">back to top</a>)</p>

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
[ok] worker-agents serves one role at the root: engine_documentalist
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

<p align="right">(<a href="#readme-top">back to top</a>)</p>

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
#    ENGINE_SECRET_BACKEND=openbao
#    VAULT_STACK_DIR=../vault-transit-stack
docker compose up -d --wait
echo -n "$GEMINI_KEY" | docker compose --profile admin run --rm -T admin credential put gemini-main --provider gemini
```

Postgres then holds only `secret_ref = agent-ai/credentials/gemini-main`. The gateway logs in
with its own AppRole (bound to the store's secrets subnet), caches a read for
`ENGINE_SECRET_CACHE_S` and serves the last value for `ENGINE_SECRET_STALE_S` if the store
is sealed or unreachable; with nothing cached, reads **fail closed**.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

---

## MCP tools for the agents

The blueprint is [docs/mcp.md](docs/mcp.md). In short:

```jsonc
// ENGINE_MCP_SERVERS (or a file: ENGINE_MCP_SERVERS_FILE=/app/config/mcp/servers.json)
[{ "name": "tickets", "url": "http://tickets-mcp:8000/mcp", "roles": ["documentalist"],
   "tools": ["search_tickets", "get_ticket"],
   "auth": { "type": "bearer_env", "env": "TICKETS_MCP_TOKEN", "forward_worker_token": "X-Engine-Worker-Token" } }]
```

- **Allowlist only**: the model sees the listed tools and nothing the server adds later.
- **No secret in the declaration**: tokens live in `config/mcp/tokens.env` (gitignored), the only
  environment file the worker loads; the task's own token is forwarded per request.
- **Fail closed**: a bad declaration stops the worker at start.
- **Prove it**: `docker compose --profile mcp-example up -d` starts a reference server
  (`echo`, `utc_now`, `whoami`, and a `delete_everything` the allowlist hides), then
  `docker compose exec worker-agents python -m workers.mcp_servers check --call whoami`.

The Ansible Controller connection (`aap_mcp_toolset`, the official aap-mcp-server in front of an
ansible-core Controller) is written in code and is the reference implementation of the pattern.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

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

<p align="right">(<a href="#readme-top">back to top</a>)</p>

---

## Operations

| Task | Command |
|---|---|
| Status: preset, profiles, credentials, teams, call counts | `docker compose --profile admin run --rm admin status` |
| Switch preset (no restart) | `… admin preset activate offline \| demo-free \| paid \| custom \| local` |
| More roles in the worker | `ENGINE_AGENT_ROLES=documentalist,author` → `docker compose up -d worker-agents` |
| What each MCP server offers each role | `docker compose exec worker-agents python -m workers.mcp_servers check [--call TOOL --args '{}']` |
| Local model fallback | `docker compose --profile local-llm up -d` (Ollama; only the gateway reaches it) |
| Calls and cost per role | `docker compose exec postgres psql -U engine -c "select role, count(*), sum(cost_usd) from model_calls group by 1"` |
| Notifications | `… psql -U engine -c "select event, severity, title, count, last_at from notifications order by last_at desc"` |
| Gateway admin API (agents network) | `GET /admin/serving`, `DELETE /admin/breakers?prefix=…` with `Authorization: Bearer $ENGINE_GATEWAY_ADMIN_TOKEN` |
| Tests (120) | `pip install -r agents/base/requirements.txt pytest pytest-asyncio aiosqlite && pytest` |

<p align="right">(<a href="#readme-top">back to top</a>)</p>

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `worker-agents` unhealthy, logs `ENGINE_MCP_SERVERS: …` | a bad MCP declaration (empty allowlist, unknown role, inline secret, missing token variable): fix it, `docker compose up -d worker-agents` |
| `/healthz` 404 on the worker | one role is served at the root (its card at `/.well-known/agent-card.json`); `/healthz` and `/<role>/` exist with two or more roles |
| `401` from the gateway | the caller's token is not one of `ENGINE_GATEWAY_TOKEN_<ROLE>` (a worker serving several roles sends each role's own token per request) |
| `429` with `Retry-After` | the RPM slot did not free within `ENGINE_GATEWAY_MAX_WAIT_S`; raise the limit or the profile's `rpm`, or use another account |
| every answer is "Deterministic …" | the active preset is `offline`, or every route failed and the stand-in answered (`notifications`: `gateway.last_resort`) |
| `gateway.breaker_opened` | an account answered an auth error: fix the key (`credential put` again), then `DELETE /admin/breakers?prefix=account:<name>` |
| `secrets.unavailable` / OpenBao refused | the AppRole directory is not mounted or not readable (`HOST_GID`), the gateway is not on `bao-secrets-net`, or `BAO_ADDR` is not `https://openbao-secrets:8200` |
| `check` reports `401` for an MCP server | the token in `config/mcp/tokens.env` differs from the server's |

<p align="right">(<a href="#readme-top">back to top</a>)</p>

---

## Configuration

`.env.example` documents every variable. The essentials:

| Variable | Default | Description |
|---|---|---|
| `ENGINE_GATEWAY_TOKEN_<ROLE>` | generated | one token per role: the gateway derives role, model and limits from it |
| `ENGINE_GATEWAY_ADMIN_TOKEN` | generated | the gateway's admin API |
| `ENGINE_DEFAULT_MODEL` / `ENGINE_MODEL_<ROLE>` | `gemini-3.5-flash-lite` / empty | the `.env` step of model resolution |
| `ENGINE_AGENT_ROLES` | `documentalist` | roles served by `worker-agents` |
| `ENGINE_MCP_SERVERS` / `ENGINE_MCP_SERVERS_FILE` | empty | MCP servers for the agents ([docs/mcp.md](docs/mcp.md)) |
| `ENGINE_SECRET_BACKEND` | `local` | `local` (Fernet, `ENGINE_CREDENTIALS_KEY`) or `openbao` |
| `ENGINE_BAO_MOUNTS` | `{"model_provider":"agent-ai"}` | the store's mount name per credential type |
| `ENGINE_GATEWAY_RPM_FREE` / `_PAID` / `ENGINE_GATEWAY_RPM` | `10` / `0` / empty | requests per minute per account and model (0 = unlimited) |
| `ENGINE_GATEWAY_MAX_WAIT_S` | `30` | how long a request waits for its RPM slot before a 429 |
| `ENGINE_LLM_MODE` | `gemini` | `deterministic`: the first schema run seeds the offline preset active |

<p align="right">(<a href="#readme-top">back to top</a>)</p>

---

## Project structure

```text
sdk-agent-pipeline/
├── docker-compose.yml           # postgres, valkey, schema, model-gateway ×2, worker-agents (+ admin, local-llm, mcp-example)
├── docker-compose.openbao.yml   # overlay: AppRoles and the secrets network of vault-transit-stack
├── .env.example
├── agents/base/                 # base image: ADK, A2A, MCP, FastAPI, SQLAlchemy, Valkey
├── model_gateway/               # the gateway (app, providers, failover, local models, signatures) + tests
├── workers/                     # AgentSpec registry, ADK agent factory, A2A entrypoint, mcp_servers.py
├── config/mcp/                  # servers.example.json, tokens.env.example (tokens.env is gitignored)
├── examples/mcp-example/        # a reference MCP server to prove a connection
├── docs/mcp.md                  # the MCP blueprint
├── orchestrator/                # runtime: models, ratelimit, credentials, secrets, activity, instructions, deterministic
├── store/                       # models, services, prices, schema bootstrap, admin CLI
├── tests/                       # activity header, OpenBao client, workers, MCP servers (against the example)
└── scripts/                     # bootstrap_env.py, build.sh, smoke.sh / smoke.py
```

<p align="right">(<a href="#readme-top">back to top</a>)</p>

---

## License

[Apache License 2.0](LICENSE): use, modify and fork it, keep the notices ([NOTICE](NOTICE)).
Its dependencies (Google ADK, a2a-sdk, the MCP SDK, FastAPI, SQLAlchemy, httpx, Valkey's
client, cryptography) are Apache-2.0, MIT or BSD licensed, all compatible with it.

<p align="right">(<a href="#readme-top">back to top</a>)</p>
