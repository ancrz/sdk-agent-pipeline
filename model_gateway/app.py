"""Model gateway (Phase F3): the only holder of model provider credentials for agents.

Workers talk the Gemini REST protocol unchanged: google-genai honours
GOOGLE_GEMINI_BASE_URL (→ this service) and sends GOOGLE_API_KEY as
x-goog-api-key. Each worker's "API key" is its own gateway token
(VANGUARD_GATEWAY_TOKEN_<ROLE>), so the provider key is never in a worker.

Per request the gateway:
1. authenticates the token and derives the role from it (unknown → 401);
2. resolves the role's model and credential exactly like the API
   (store/services/models.resolve: profile → default profile → .env) and **uses
   that model**, whatever the worker asked for — a worker cannot pick a pricier
   model;
3. forwards to the provider with the real credential (Gemini: x-goog-api-key;
   Vertex AI: an IAM access token from ADC / Workload Identity);
4. walks the failover chain when a route fails (model_gateway/failover.py,
   store/services/failover.py): the fallback model, backup accounts, the local
   model, then the deterministic stand-in; broken routes are skipped for a
   cooldown (circuit breakers);
5. writes an audit row (model_calls: role, requested and used model, status,
   tokens, latency). Request and response bodies are never stored or logged.

Limits, checked before the provider is called (a refusal is a 429 in Gemini's
error shape with Retry-After, and is audited too):
- requests per minute per provider account and model (store/services/models.
  effective_rpm: the profile's rpm → VANGUARD_GATEWAY_RPM_<ROLE> →
  VANGUARD_GATEWAY_RPM → the account tier's default, free 10 / paid unlimited).
  Requests are spaced at 60/rpm through Valkey (shared by every gateway
  instance; in-process if Valkey fails); a request waits up to
  VANGUARD_GATEWAY_MAX_WAIT_S (default 30) for its slot, then gets the 429, so a
  free-tier burst queues instead of failing nodes.
- tokens per UTC day per role: VANGUARD_GATEWAY_DAILY_TOKENS_<ROLE>, else
  VANGUARD_GATEWAY_DAILY_TOKENS (0 or unset = unlimited), summed from model_calls.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from store.models.automation import Credential, ModelCall
from orchestrator.credentials import CredentialError

from store.services import prices
from store.services.models import ResolvedModel, effective_rpd, effective_rpm, resolve
from orchestrator import activity
from orchestrator.ratelimit import BoundedSlots, DailyCounter

from store.services.failover import chain as failover_chain

from . import failover, local_models, providers, signatures

logger = logging.getLogger("model_gateway")

ROLES = ("orchestrator", "executor", "validator", "documentalist", "author", "medic", "embeddings")
METHODS = {"generateContent", "streamGenerateContent", "countTokens", "embedContent", "batchEmbedContents"}
EMBEDDING_METHODS = {"embedContent", "batchEmbedContents"}
GEMINI_BASE = os.environ.get("VANGUARD_GEMINI_UPSTREAM") or "https://generativelanguage.googleapis.com"
# When Postgres is unreachable, calls keep using the last known routes for this long.
ROUTING_STALE_S = float(os.environ.get("VANGUARD_ROUTING_STALE_S", "3600"))
# CPU inference of a local model is slow; allow it time to answer.
LOCAL_TIMEOUT_S = float(os.environ.get("VANGUARD_LOCAL_MODEL_TIMEOUT_S", "600"))
# Thinking on a CPU-served model (docs/pipeline/audit_*): Qwen 3.5 reasons by default and spent the
# whole 600 s timeout on one planner call. Ollama maps reasoning_effort "none" to think=false;
# "" sends nothing (the model's default), or low / medium / high.
LOCAL_REASONING = os.environ.get("VANGUARD_LOCAL_REASONING", "none").strip().lower()
# Roles a CPU-served model does not do reliably (docs/pipeline/audit_*: 0 of 6 runs launched their
# templates): on the local tier they are answered by the deterministic procedure, which launches the
# template the approved plan names, on its host, with its parameters. The planner, the chat and
# Medic keep the local model. Empty: every role on the model.
LOCAL_DETERMINISTIC_ROLES = frozenset(r.strip() for r in os.environ.get(
    "VANGUARD_LOCAL_DETERMINISTIC_ROLES", "executor,validator").split(",") if r.strip())


def database_url() -> str:
    from urllib.parse import quote
    return (f"postgresql+asyncpg://{os.environ.get('POSTGRES_USER', 'vanguard')}:"
            f"{quote(os.environ.get('POSTGRES_PASSWORD', ''), safe='')}@{os.environ.get('POSTGRES_HOST', 'postgres')}:"
            f"{os.environ.get('POSTGRES_PORT', '5432')}/{os.environ.get('POSTGRES_DB', 'vanguard')}")


def role_for_token(token: str) -> Optional[str]:
    """The role whose VANGUARD_GATEWAY_TOKEN_<ROLE> matches (constant-time), else None."""
    if not token:
        return None
    for role in ROLES:
        expected = os.environ.get(f"VANGUARD_GATEWAY_TOKEN_{role.upper()}", "")
        if len(expected) >= 16 and hmac.compare_digest(token.encode(), expected.encode()):
            return role
    return None


def upstream(resolved: ResolvedModel, model: str, method: str) -> tuple[str, dict[str, str]]:
    """URL and auth headers for the provider."""
    if resolved.provider == "vertex_ai":
        kw = resolved.client_kwargs()
        project, location = kw.get("project"), kw.get("location")
        import google.auth
        import google.auth.transport.requests
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
        return (f"https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:{method}",
                {"Authorization": f"Bearer {creds.token}"})
    key = resolved.client_kwargs().get("api_key") or os.environ.get("GOOGLE_API_KEY", "")
    return f"{GEMINI_BASE}/v1beta/models/{model}:{method}", {"x-goog-api-key": key}


def _env_limit(prefix: str, role: str) -> int:
    for name in (f"{prefix}_{role.upper()}", prefix):
        raw = os.environ.get(name, "").strip()
        if raw:
            return max(0, int(raw))
    return 0


async def tokens_today(db: AsyncSession, role: str) -> int:
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if db.bind.dialect.name == "sqlite":
        midnight = midnight.replace(tzinfo=None)   # SQLite stores naive UTC
    total = (await db.execute(select(func.coalesce(func.sum(
        func.coalesce(ModelCall.prompt_tokens, 0) + func.coalesce(ModelCall.output_tokens, 0)), 0))
        .where(ModelCall.role == role, ModelCall.created_at >= midnight))).scalar_one()
    return int(total or 0)


def _limited(message: str, retry_after: float) -> JSONResponse:
    return JSONResponse(status_code=429, headers={"Retry-After": str(int(retry_after))},
                        content={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": message}})


def _usage(body: Any) -> tuple[Optional[int], Optional[int]]:
    usage = body.get("usageMetadata") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return None, None
    return usage.get("promptTokenCount"), usage.get("candidatesTokenCount")


def _usage_extra(body: Any) -> tuple[Optional[int], Optional[int]]:
    """(thinking, cached) tokens: billed apart (docs/pipeline/cost_*); thinking at the output price."""
    usage = body.get("usageMetadata") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return None, None
    return usage.get("thoughtsTokenCount"), usage.get("cachedContentTokenCount")


def _valkey():
    """Shared limiter state (several gateway instances); None → in-process slots."""
    host = os.environ.get("VALKEY_HOST")
    if not host:
        return None
    import valkey.asyncio as valkey
    return valkey.Valkey(host=host, port=int(os.environ.get("VALKEY_PORT", "6379")),
                         password=os.environ.get("VALKEY_PASSWORD") or None, socket_timeout=2)


async def wait_for_slot(request: Request, seconds: float, step: float = 0.5) -> bool:
    """Sleep until the slot, checking whether the client is still there. False = it left."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        left = deadline - loop.time()
        if left <= 0:
            return True
        if await request.is_disconnected():
            return False
        await asyncio.sleep(min(step, left))


@dataclass
class Outcome:
    """One attempt on one route: the response to send, or why the route failed."""
    status: int
    response: Optional[Response] = None
    failure: Optional[failover.Failure] = None
    prompt_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    error: Optional[str] = None
    thoughts_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None


def _native_error(status_code: int, raw: bytes, headers: httpx.Headers) -> Outcome:
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        body = None
    failure = failover.classify(status_code, body, headers)
    message = str(((body or {}).get("error") or {}).get("message", ""))[:500] if isinstance(body, dict) else None
    return Outcome(status_code, response=Response(content=raw, status_code=status_code,
                                                  media_type=headers.get("content-type", "application/json")),
                   failure=failure if failure.kind in failover.FAILOVER_KINDS else None, error=message or None)


async def _safe_rollback(db: AsyncSession) -> None:
    try:
        await db.rollback()
    except Exception:
        pass


def max_wait_s() -> float:
    return float(os.environ.get("VANGUARD_GATEWAY_MAX_WAIT_S", "30"))


async def _tell(db, event: str, dedupe_key: str, body: str = "", params: Optional[dict] = None,
                link: Optional[str] = None) -> None:
    """Notify the admins (store/services/notify.py); grouped by key, never fails the call."""
    try:
        from store.services import notify
        await notify.emit(db, event, params=params or {}, body=body, link=link, dedupe_key=dedupe_key)
        await db.commit()
    except Exception as exc:
        logger.warning("notification %s not recorded: %s", event, exc)
        await _safe_rollback(db)


async def _attribution(db: AsyncSession, value: Optional[str], role: Optional[str] = None) -> dict[str, Any]:
    """The X-Vanguard-Activity of a call (accounting, never authorization: docs/pipeline/cost_* R2).
    A run id comes from a worker and must name a run that exists. A plan or chat id is kept only
    from the API's own role (orchestrator): the API tags a plan it is still drafting, in a
    transaction this session cannot see yet."""
    parsed = activity.parse(value)
    if parsed is None:
        return {}
    kind, ident = parsed
    if kind in ("plan", "chat") and role != "orchestrator":
        return {}
    if kind == "run" and ident is not None:
        from store.models.activity import ActivityRun
        if await db.get(ActivityRun, ident) is None:
            return {}
    return {"activity_kind": kind, "activity_id": ident}


async def team_model(db: AsyncSession, value: Optional[str], role: str) -> Optional[str]:
    """The model the change's agent team names for this role (G-AGENTS A3, docs/pipeline/evolution_20261015_180000.md),
    read from the database through the run the call works for; None for basic, an unknown run or no model."""
    parsed = activity.parse(value)
    if parsed is None or parsed[0] != "run" or parsed[1] is None:
        return None
    from store.models.activity import ActivityRun
    from store.models.automation import AgentTeam
    # The engine links a run to its team directly (vanguard-control: run → plan → team).
    members = (await db.execute(select(AgentTeam.members).join(ActivityRun, ActivityRun.agent_team_id == AgentTeam.id)
                                .where(ActivityRun.id == parsed[1]))).scalar_one_or_none()
    member = (members or {}).get(role) if isinstance(members, dict) else None
    model = member.get("model") if isinstance(member, dict) else None
    return model if isinstance(model, str) and model else None


def with_model_first(routes: list, model: str) -> list:
    """The team's model first on the first cloud route, that route's own models after it as fallbacks.
    Offline and local chains have no cloud route and stay as they are (reproducible smokes)."""
    out, done = [], False
    for route in routes:
        if not done and route.cloud:
            route = type(route)(tier=route.tier, resolved=route.resolved,
                                models=[model] + [m for m in route.models if m != model])
            done = True
        out.append(route)
    return out


def create_app(session_factory: Optional[async_sessionmaker] = None,
               transport: Optional[httpx.AsyncBaseTransport] = None,
               limiter: Optional[BoundedSlots] = None, daily: Optional[DailyCounter] = None) -> FastAPI:
    app = FastAPI(title="Vanguard model gateway", docs_url=None, redoc_url=None, openapi_url=None)
    sf = session_factory or async_sessionmaker(create_async_engine(database_url(), pool_pre_ping=True),
                                               expire_on_commit=False)
    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0), transport=transport)
    slots = limiter or BoundedSlots(_valkey())
    app.state.slots = slots
    day_counter = daily or DailyCounter(_valkey())            # requests per day per account:model (cost_* T8)

    async def audit(db: AsyncSession, tier: Optional[str] = None, activity_value: Optional[str] = None,
                    **fields: Any) -> None:
        try:
            fields.update(await _attribution(db, activity_value, fields.get("role")))
            if fields.get("status_code", 500) < 400:
                stamp = await prices.stamp(db, fields["provider"], fields["model"], tier, fields.get("prompt_tokens"),
                                           fields.get("output_tokens"), fields.get("thoughts_tokens"),
                                           fields.get("cached_tokens"))
                fields.update(cost_usd=stamp.cost_usd, list_cost_usd=stamp.list_cost_usd, price_id=stamp.price_id)
            db.add(ModelCall(**fields))
            await db.commit()
        except Exception as exc:  # the audit never breaks a model call
            logger.warning("model_calls audit failed: %s", exc)
            await db.rollback()

    breakers = failover.Breakers(_valkey())
    app.state.breakers = breakers
    serving = failover.Serving(_valkey())
    app.state.serving = serving
    routing_cache: dict[tuple, tuple[float, list]] = {}

    async def transition(db: AsyncSession, role: str, route, model: str, reasons: list[str]) -> None:
        """Tell the admins when who answers `role` changes (docs/pipeline/continuity_*): one
        notice per change, grouped per state across roles (×N)."""
        res = route.resolved
        changed, previous = await serving.changed(role, f"{route.tier}:{res.account}")
        if not changed or (previous is None and route.tier == "primary"):
            return
        why = "; ".join(reasons)[:400] or "the previous route was skipped"
        link = "/ai-gateway?tab=resilience"
        if route.tier == "primary":
            await _tell(db, "gateway.recovered", f"gateway.recovered:{res.account}",
                        params={"account": res.account},
                        body=f"{role} is answered by the primary key {res.account} ({model}) again; it was on "
                             f"{(previous or '?').replace(':', ' ')}.", link=link)
        elif route.tier == "backup":
            await _tell(db, "gateway.failover", f"gateway.failover:{res.account}",
                        params={"account": res.account},
                        body=f"{role} moved to the backup key {res.account} ({model}) after: {why}.", link=link)
        elif route.tier == "local":
            await _tell(db, "gateway.local_fallback", f"gateway.local_fallback:{res.account}",
                        params={"model": model},
                        body=f"No cloud model answered {role}; the local model {model} does, until a key "
                             f"answers again. After: {why}.", link=link)

    async def routes_for(db: AsyncSession, role: str, embeddings: bool, streaming: bool) -> list:
        """The role's failover chain; the last known one (up to 1 h) when Postgres is unreachable."""
        key = (role, embeddings, streaming)
        try:
            routes = await failover_chain(db, role, embeddings=embeddings, streaming=streaming,
                                          serving=await serving.get(role))
            await db.commit()
            routing_cache[key] = (time.monotonic(), routes)
            return routes
        except Exception as exc:  # the database is down: keep answering on the last known routes
            await _safe_rollback(db)
            hit = routing_cache.get(key)
            if hit and time.monotonic() - hit[0] < ROUTING_STALE_S:
                logger.error("role=%s: routing unavailable (%s); using the last known routes", role, type(exc).__name__)
                return hit[1]
            raise

    async def secret_for(route) -> Optional[dict[str, Any]]:
        """The route's credential (stale-if-error inside the secret client). A local server needs
        no key: when its entry is unreadable, the configured Ollama address still works."""
        try:
            return await route.resolved.asecret()
        except CredentialError:
            if route.resolved.provider == "local":
                fallback = {"provider": "local", "base_url": local_models.serve_url() + "/v1"}
                object.__setattr__(route.resolved, "_secret", fallback)
                return fallback
            return None

    def sse_or_json(status_code: int, payload: Any, sse: bool) -> Response:
        if sse and status_code < 400:
            return Response(content=f"data: {json.dumps(payload)}\n\n", media_type="text/event-stream")
        return JSONResponse(status_code=status_code, content=payload)

    async def attempt_native(route, model: str, method: str, body: bytes, params: dict) -> Outcome:
        """Gemini API or Vertex AI, passed through as the provider answered. A history written partly by
        another model (a failover to the local one, the stand-in) gets the documented placeholder
        signature on its unsigned function calls, or Gemini 3 refuses it (signatures.py)."""
        body, added = signatures.backfill(body)
        if added:
            logger.info("role=%s %s/%s: %d unsigned function call(s) in the turn get the documented placeholder",
                        route.resolved.role, route.resolved.account, model, added)
        url, headers = upstream(route.resolved, model, method)
        headers = {**headers, "content-type": "application/json"}
        if method == "streamGenerateContent":
            try:
                r = await client.send(client.build_request("POST", url, params=params, content=body, headers=headers),
                                      stream=True)
            except httpx.TransportError as exc:
                return Outcome(503, failure=failover.classify(None, exc=exc))
            if r.status_code >= 400:                  # nothing relayed yet: the next route may still answer
                raw = await r.aread()
                await r.aclose()
                return _native_error(r.status_code, raw, r.headers)

            async def relay():
                try:
                    async for chunk in r.aiter_bytes():   # decoded: content-encoding is not forwarded
                        yield chunk
                finally:
                    await r.aclose()
            return Outcome(r.status_code, response=StreamingResponse(
                relay(), status_code=r.status_code, media_type=r.headers.get("content-type", "text/event-stream")))
        try:
            r = await client.post(url, params=params, content=body, headers=headers)
        except httpx.TransportError as exc:
            return Outcome(503, failure=failover.classify(None, exc=exc))
        if r.status_code >= 400:
            return _native_error(r.status_code, r.content, r.headers)
        try:
            payload = r.json()
        except ValueError:
            payload = None
        for retry in range(signatures.EMPTY_RETRIES):      # an empty 200 is transient (signatures.py)
            if not signatures.empty_answer(payload):
                break
            logger.info("role=%s %s/%s: empty answer (STOP, no parts), asking again (%d)",
                        route.resolved.role, route.resolved.account, model, retry + 1)
            try:
                again = await client.post(url, params=params, content=body, headers=headers)
                if again.status_code >= 400:
                    break
                r, payload = again, again.json()
            except (httpx.TransportError, ValueError):
                break
        prompt_tokens, output_tokens = _usage(payload)
        thoughts, cached = _usage_extra(payload)
        return Outcome(r.status_code, response=Response(content=r.content, status_code=r.status_code,
                                                         media_type=r.headers.get("content-type", "application/json")),
                       prompt_tokens=prompt_tokens, output_tokens=output_tokens,
                       thoughts_tokens=thoughts, cached_tokens=cached)

    async def attempt_translated(route, model: str, method: str, request_body: Any) -> Outcome:
        """offline (deterministic), OpenAI-compatible, Anthropic and local servers, in Gemini's shape."""
        sse = method == "streamGenerateContent"
        provider = route.resolved.provider
        if method not in ("generateContent", "streamGenerateContent"):
            if method == "countTokens":
                return Outcome(200, response=JSONResponse({"totalTokens": 0}))
            return Outcome(501, response=JSONResponse(status_code=501, content={"error": {
                "code": 501, "status": "UNIMPLEMENTED", "message": f"{method} is not available for {provider}."}}))
        if provider == "local" and route.resolved.role in LOCAL_DETERMINISTIC_ROLES:
            logger.info("role=%s on the local tier: answered by the deterministic procedure", route.resolved.role)
            provider = "offline"
        if provider == "offline":
            payload = await providers.offline_generate(route.resolved.role, request_body)
            prompt_tokens, output_tokens = _usage(payload)
            return Outcome(200, response=sse_or_json(200, payload, sse), prompt_tokens=prompt_tokens,
                           output_tokens=output_tokens)
        secret = route.resolved._secret
        native_anthropic = provider == "anthropic"
        if native_anthropic:
            url, headers = providers.ANTHROPIC_URL, providers.anthropic_headers(secret)
            translated_body = providers.gemini_to_anthropic(request_body, model)
        else:
            base, headers = providers.openai_endpoint(provider, secret)
            url = f"{base}/chat/completions"
            translated_body = providers.gemini_to_openai(request_body, model)
        extra = {"timeout": LOCAL_TIMEOUT_S} if provider == "local" else {}
        if provider == "local" and LOCAL_REASONING:
            translated_body["reasoning_effort"] = LOCAL_REASONING
        try:
            resp = await client.post(url, json=translated_body, headers={**headers, "content-type": "application/json"},
                                     **extra)
        except httpx.TransportError as exc:
            return Outcome(503, failure=failover.classify(None, exc=exc))
        try:
            raw = resp.json()
        except ValueError:
            raw = None
        if resp.status_code >= 400:
            payload = (providers.anthropic_error_to_gemini if native_anthropic
                       else providers.openai_error_to_gemini)(resp.status_code, raw)
            failure = failover.classify(resp.status_code, raw, resp.headers)
            return Outcome(resp.status_code, response=JSONResponse(status_code=resp.status_code, content=payload),
                           failure=failure if failure.kind in failover.FAILOVER_KINDS else None,
                           error=str(payload["error"]["message"])[:500])
        announced = providers.announces_without_acting(translated_body, raw) if provider == "local" else None
        if announced:
            logger.info("%s/%s announced without acting; asked once more", provider, model)
            again = {**translated_body, "messages": [*translated_body["messages"], {"role": "assistant", "content": announced},
                                                     {"role": "user", "content": providers.NUDGE}]}
            try:
                retry = await client.post(url, json=again, headers={**headers, "content-type": "application/json"}, **extra)
                if retry.status_code < 400:
                    raw = retry.json()
            except (httpx.TransportError, ValueError):
                pass                                  # keep the first answer
        payload = (providers.anthropic_to_gemini if native_anthropic else providers.openai_to_gemini)(raw or {})
        prompt_tokens, output_tokens = _usage(payload)
        thoughts, cached = _usage_extra(payload)
        return Outcome(resp.status_code, response=sse_or_json(resp.status_code, payload, sse),
                       prompt_tokens=prompt_tokens, output_tokens=output_tokens,
                       thoughts_tokens=thoughts, cached_tokens=cached)

    def admin_refusal(request: Request) -> Optional[Response]:
        expected = os.environ.get("VANGUARD_GATEWAY_ADMIN_TOKEN", "")
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if len(expected) < 16 or not hmac.compare_digest(given.encode(), expected.encode()):
            return JSONResponse(status_code=401, content={"detail": "Unknown admin token."})
        return None

    pulls = local_models.Pulls(_valkey())
    app.state.local_pulls = pulls

    @app.get("/admin/local")
    async def local_status(request: Request) -> Response:
        """Local model server: reachable, installed models with capabilities, downloads."""
        if (refusal := admin_refusal(request)) is not None:
            return refusal
        return JSONResponse(await local_models.status(client, pulls))

    @app.post("/admin/local/pull")
    async def local_pull(request: Request) -> Response:
        if (refusal := admin_refusal(request)) is not None:
            return refusal
        name = str((await request.json()).get("model", "")).strip()
        if not local_models.valid_name(name):
            return JSONResponse(status_code=422, content={"detail": "Not a model name (e.g. qwen3:4b or hf.co/user/repo:Q4_K_M)."})
        return JSONResponse(status_code=202, content=await pulls.start(client, name))

    @app.delete("/admin/local/models/{name:path}")
    async def local_delete(name: str, request: Request) -> Response:
        if (refusal := admin_refusal(request)) is not None:
            return refusal
        if not local_models.valid_name(name):
            return JSONResponse(status_code=422, content={"detail": "Not a model name."})
        try:
            code = await local_models.delete(client, name)
        except httpx.HTTPError as exc:
            return JSONResponse(status_code=502, content={"detail": f"ollama-pull unreachable: {type(exc).__name__}"})
        if code == 404:
            return JSONResponse(status_code=404, content={"detail": f"{name} is not installed."})
        return Response(status_code=204 if code == 200 else 502)

    @app.get("/admin/credentials/{name}/models")
    async def credential_models(name: str, request: Request) -> Response:
        """The provider's model list for a Vault credential (called by the API, which cannot read keys)."""
        if (refusal := admin_refusal(request)) is not None:
            return refusal
        async with sf() as db:
            from sqlalchemy import select as _select
            cred = (await db.execute(_select(Credential).where(Credential.name == name))).scalar_one_or_none()
        if cred is None or cred.credential_type != "model_provider":
            return JSONResponse(status_code=404, content={"detail": "No model_provider credential with that name."})
        from orchestrator import secrets as secret_store
        try:
            secret = await secret_store.aload(cred.backend, cred.secret_ref, cred.inputs_encrypted)
        except CredentialError as exc:
            return JSONResponse(status_code=503, content={"detail": str(exc)})
        provider = secret.get("provider", "gemini")
        try:
            models = await providers.list_models(client, provider, secret, GEMINI_BASE)
        except ValueError as exc:
            return JSONResponse(status_code=501, content={"detail": str(exc)})
        except httpx.HTTPError as exc:
            return JSONResponse(status_code=502, content={"detail": f"Listing failed: {providers.catalog_error(exc)}"})
        return JSONResponse({"provider": provider, "models": models})

    @app.post("/admin/providers/test")
    async def test_provider(request: Request) -> Response:
        """List a provider's models with inputs typed in the UI, before they are stored (nothing is
        kept or logged): a wrong key is caught at the form, not at the first failed call."""
        if (refusal := admin_refusal(request)) is not None:
            return refusal
        from orchestrator.credentials import check_inputs
        try:
            inputs = (await request.json()).get("inputs") or {}
            check_inputs("model_provider", inputs)
        except (CredentialError, ValueError, AttributeError) as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc)[:300]})
        provider = inputs.get("provider", "gemini")
        try:
            models = await providers.list_models(client, provider, inputs, GEMINI_BASE)
        except ValueError as exc:
            return JSONResponse(status_code=501, content={"detail": str(exc)})
        except httpx.HTTPStatusError as exc:
            failure = failover.classify(exc.response.status_code)
            return JSONResponse(status_code=400, content={"detail": f"The provider refused the key: {failure.detail}",
                                                          "kind": failure.kind})
        except httpx.HTTPError as exc:
            return JSONResponse(status_code=502, content={"detail": f"Provider unreachable: {providers.catalog_error(exc)}"})
        return JSONResponse({"provider": provider, "models": models})

    @app.get("/admin/breakers")
    async def list_breakers(request: Request) -> Response:
        """Open circuits: which routes are skipped, why and until when."""
        if (refusal := admin_refusal(request)) is not None:
            return refusal
        now = time.time()
        return JSONResponse([{**b, "seconds_left": max(0, int(b["until"] - now))} for b in await breakers.all()])

    @app.get("/admin/serving")
    async def list_serving(request: Request) -> Response:
        refusal = admin_refusal(request)
        if refusal:
            return refusal
        return JSONResponse(await serving.all())

    @app.post("/admin/serving/reset")
    async def reset_serving(request: Request) -> Response:
        """Return to the primary (failback manual), or forget who answered."""
        refusal = admin_refusal(request)
        if refusal:
            return refusal
        return JSONResponse({"reset": await serving.reset()})

    @app.delete("/admin/breakers")
    async def reset_breakers(request: Request, scope: Optional[str] = None, prefix: Optional[str] = None) -> Response:
        """Close one circuit (?scope=…) or all of them: the next call tries those routes again."""
        if (refusal := admin_refusal(request)) is not None:
            return refusal
        return JSONResponse({"reset": await breakers.reset(scope, prefix)})

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.api_route("/{version}/models/{model_and_method:path}", methods=["POST"])
    async def proxy(version: str, model_and_method: str, request: Request) -> Response:
        role = role_for_token(request.headers.get("x-goog-api-key", "")
                              or request.query_params.get("key", ""))
        if role is None:
            return JSONResponse(status_code=401, content={"error": {"code": 401, "message": "Unknown gateway token."}})
        requested, _, method = model_and_method.rpartition(":")
        if method not in METHODS:
            return JSONResponse(status_code=404, content={"error": {"code": 404, "message": f"Unsupported method {method}."}})
        body = await request.body()
        started = time.monotonic()
        embeddings = method in EMBEDDING_METHODS
        try:
            request_body = json.loads(body or b"{}")
        except ValueError:
            return JSONResponse(status_code=400, content={"error": {"code": 400, "message": "Invalid JSON body."}})
        params = dict(request.query_params)
        params.pop("key", None)
        activity_value = request.headers.get(activity.HEADER)       # never forwarded upstream
        async with sf() as db:
            try:
                routes = await routes_for(db, role, embeddings, method == "streamGenerateContent")
                if not embeddings:                       # the change's agent team (A3); never cached with the chain
                    try:
                        chosen = await team_model(db, activity_value, role)
                        await db.commit()
                    except Exception:
                        await _safe_rollback(db)
                        chosen = None
                    if chosen:
                        routes = with_model_first(routes, chosen)
            except Exception as exc:
                logger.error("role=%s: no routing (database unreachable, nothing cached): %s", role, type(exc).__name__)
                return JSONResponse(status_code=503, content={"error": {"code": 503, "status": "UNAVAILABLE",
                                                                        "message": "Model routing unavailable."}})
            # Which tiers may answer: every one, unless a deliberate limit narrows it.
            scope = "all"
            budget_refusal = None
            budget = _env_limit("VANGUARD_GATEWAY_DAILY_TOKENS", role)
            try:
                spent = budget and await tokens_today(db, role) >= budget
                await db.commit()
            except Exception:
                await _safe_rollback(db)
                spent = False                    # no database to count with: do not block the core
            if spent:
                now = datetime.now(timezone.utc)
                budget_refusal = (f"Gateway limit: daily token budget for role {role} is spent.",
                                  86400 - (now.hour * 3600 + now.minute * 60 + now.second))
                scope = "local"                  # a cost decision: free local answers only

            first = (routes[0].resolved.account, routes[0].models[0])
            reasons: list[str] = []
            last: Optional[Outcome] = None
            rate_refusal = None
            for route in routes:
                res = route.resolved
                if (scope == "local" and res.provider != "local") or (scope == "cloud" and not route.cloud):
                    continue
                for i, model in enumerate(route.models):
                    blocked = await breakers.blocking(failover.scopes_for(res.account, res.provider, model, route.cloud))
                    if blocked:
                        reasons.append(f"{res.account}/{model} skipped ({blocked['kind']})")
                        continue
                    if res.provider != "offline" and await secret_for(route) is None:
                        reasons.append(f"{res.account}: credential unavailable")
                        await _tell(db, "secrets.unavailable", f"secrets.unavailable:{res.account}",
                                    body=f"The gateway could not read the credential of {res.account} "
                                         "(secret store sealed or unreachable, and no recent copy).",
                                    link="/security?tab=vault")
                        break
                    if route.cloud:
                        rpd, day_key = effective_rpd(res), None
                        if rpd:              # skip the account before the provider's daily 429
                            day_key = f"{res.account}:{model}:{failover.quota_day(res.provider)}"
                            reset_s = failover.next_reset(res.provider)
                            if await day_counter.take(day_key, rpd, int(reset_s) + 3600) is None:
                                rate_refusal = (f"Gateway limit: {rpd} requests per day for {res.account}:{model} "
                                                "reached; the count resets with the provider's quota day.", reset_s)
                                reasons.append(f"{res.account}/{model} gateway rpd")
                                if scope == "all":
                                    scope = "cloud"
                                continue
                        rpm = effective_rpm(res)
                        reservation = await slots.reserve_slot(f"{res.account}:{model}", rpm, max_wait_s())
                        if reservation is None:
                            if day_key:
                                await day_counter.give_back(day_key)
                            rate_refusal = (f"Gateway limit: {rpm} requests per minute for {res.account}:{model} "
                                            f"reached; no slot within {max_wait_s():.0f}s.", 60 / max(rpm, 1))
                            reasons.append(f"{res.account}/{model} gateway rpm")
                            if scope == "all":
                                scope = "cloud"  # another account's quota, never a silent downgrade
                            continue
                        if reservation.wait_s > 0:
                            logger.info("role=%s waits %.1fs for a %s slot", role, reservation.wait_s, model)
                            if not await wait_for_slot(request, reservation.wait_s):
                                await slots.release(reservation)
                                if day_key:
                                    await day_counter.give_back(day_key)
                                return Response(status_code=499)
                    await db.commit()            # nothing held open while a model answers
                    if res.provider == "offline" or res.provider in providers.TRANSLATED:
                        outcome = await attempt_translated(route, model, method, request_body)
                    else:
                        outcome = await attempt_native(route, model, method, body, params)
                    failure = outcome.failure
                    if failure is None:      # an answer, or an error no other route would fix
                        used_fallback = (res.account, model) != first
                        if outcome.status < 400:
                            await transition(db, role, route, model, reasons)
                        note = ("fallback: " + "; ".join(reasons))[:500] if used_fallback and reasons else None
                        if used_fallback:
                            logger.warning("role=%s answered by %s/%s after: %s", role, res.account, model, "; ".join(reasons))
                            if res.provider == "offline":
                                await _tell(db, "gateway.last_resort", "gateway.last_resort", body=note or "",
                                            link="/ai-gateway?tab=resilience")
                        await audit(db, role=role, requested_model=requested[:128], model=model, provider=res.provider,
                                    account=res.account[:128],
                                    method=method, status_code=outcome.status, fallback_used=used_fallback,
                                    prompt_tokens=outcome.prompt_tokens, output_tokens=outcome.output_tokens,
                                    thoughts_tokens=outcome.thoughts_tokens, cached_tokens=outcome.cached_tokens,
                                    latency_ms=int((time.monotonic() - started) * 1000),
                                    error=outcome.error or note, tier=res.tier, activity_value=activity_value)
                        return outcome.response
                    last = outcome
                    reasons.append(f"{res.account}/{model} {res.provider} {failure.detail}")
                    scope_key, seconds = failover.scope_and_cooldown(failure.kind, failure, res.account, res.provider, model)
                    await breakers.open(scope_key, failure.kind, failure.detail, seconds)
                    if failure.kind in ("auth", "billing", "quota_daily"):    # a person has to act on these
                        await _tell(db, "gateway.breaker_opened", f"gateway.breaker:{scope_key}",
                                    params={"account": res.account},
                                    body=f"{failure.kind}: {failure.detail} — calls skip {scope_key} for "
                                         f"{int(seconds)} s and fail over.", link="/ai-gateway?tab=resilience")
                    if failure.kind == "network" and route.cloud and await breakers.network_failed(res.provider):
                        await _tell(db, "gateway.internet_down", "gateway.internet_down",
                                    body="Two providers unreachable within a minute: calls skip the cloud and go "
                                         "to the local model (or the stand-in) until the network is back. "
                                         f"Last: {res.provider} {failure.detail}.", link="/ai-gateway?tab=resilience")
                    if failure.kind in ("auth", "billing", "network"):
                        break                    # the whole account or host is out: next route

            latency = int((time.monotonic() - started) * 1000)
            if last is None and rate_refusal and not budget_refusal:
                await audit(db, role=role, requested_model=requested[:128], model=first[1], provider=routes[0].resolved.provider,
                            account=first[0][:128],
                            method=method, status_code=429, fallback_used=False, latency_ms=latency, error=rate_refusal[0],
                            activity_value=activity_value)
                logger.warning("role=%s refused: %s", role, rate_refusal[0])
                return _limited(*rate_refusal)
            if last is None and budget_refusal:
                await audit(db, role=role, requested_model=requested[:128], model=first[1], provider=routes[0].resolved.provider,
                            account=first[0][:128],
                            method=method, status_code=429, fallback_used=False, latency_ms=0, error=budget_refusal[0],
                            activity_value=activity_value)
                logger.warning("role=%s refused: %s", role, budget_refusal[0])
                return _limited(*budget_refusal)
            summary = "; ".join(reasons)[:500] or "no route available"
            logger.error("role=%s: every model route failed: %s", role, summary)
            status_code = last.status if last is not None else 503
            await audit(db, role=role, requested_model=requested[:128], model=first[1], provider=routes[0].resolved.provider,
                        account=first[0][:128],
                        method=method, status_code=status_code, fallback_used=False, latency_ms=latency, error=summary,
                        activity_value=activity_value)
            if last is not None and last.response is not None:
                return last.response
            return JSONResponse(status_code=503, content={"error": {"code": 503, "status": "UNAVAILABLE",
                                                                    "message": f"No model route answered: {summary}"}})

    return app


app = create_app() if os.environ.get("VANGUARD_GATEWAY_AUTOSTART", "1") == "1" else None
