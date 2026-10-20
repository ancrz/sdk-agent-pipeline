"""Provider adapters for the model gateway.

Every client of the gateway (API, workers, knowledge server) speaks the Gemini
REST protocol. The gateway answers it with the provider the active preset
resolves for the role:

- gemini / vertex_ai: forwarded as is (app.py).
- offline: the deterministic stand-in (orchestrator/deterministic.py) run here, on
  the same request the model would get. A preset switch therefore needs no
  restart anywhere, and smokes run without quota.
- local: a model server on an internal network (Ollama with Gemma 4 in the compose
  profile local-llm, or llama.cpp / vLLM / Lemonade on the host), OpenAI-compatible,
  no key. It is also the automatic fallback when a cloud provider is unreachable.
- openai, deepseek, anthropic, openai_compatible: the Gemini request is translated
  to OpenAI chat completions (messages, tools, tool calls and results, JSON mode)
  and the answer back to a Gemini response. DeepSeek is OpenAI-compatible
  natively.
- anthropic: the native Messages API (system, alternating turns, tool_use and
  tool_result blocks, max_tokens), not Anthropic's OpenAI layer, which Anthropic
  describes as for evaluation. JSON mode becomes a system instruction.

Model catalogues (list_models) come from each provider's model listing, called
here because only the gateway holds the keys.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Optional

# Provider endpoints: defaults here, overridable without a code change
# (ENGINE_PROVIDER_URL_OPENAI, _DEEPSEEK, _ANTHROPIC), e.g. for a proxy or a regional endpoint.
_DEFAULT_BASES = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
    "anthropic": "https://api.anthropic.com/v1",
}
OPENAI_BASES = {name: (os.environ.get(f"ENGINE_PROVIDER_URL_{name.upper()}") or url).rstrip("/")
                for name, url in _DEFAULT_BASES.items()}
TRANSLATED = frozenset({"openai", "deepseek", "anthropic", "openai_compatible", "local"})
GEMINI_NATIVE = frozenset({"gemini", "vertex_ai"})


# ── offline: the deterministic stand-in ──────────────────────────────────────

def _contents(body: dict) -> list:
    from google.genai import types as gt
    return [gt.Content.model_validate(c) for c in body.get("contents") or []]


def _prompt_text(body: dict) -> str:
    texts = [p.get("text", "") for c in body.get("contents") or [] for p in c.get("parts") or [] if p.get("text")]
    return "\n".join(texts)


async def offline_generate(role: str, body: dict) -> dict:
    """A Gemini generateContent response computed by the deterministic stand-in."""
    from google.adk.models.llm_request import LlmRequest

    from orchestrator import deterministic

    if role == "orchestrator" and not body.get("tools"):
        text = await deterministic.plan_completer()(_prompt_text(body))     # the planner (JSON plan)
        content = {"role": "model", "parts": [{"text": text}]}
    else:
        llm = deterministic.chat_llm() if role == "orchestrator" else deterministic.worker_llm(role)
        response = None
        async for response in llm.generate_content_async(LlmRequest(contents=_contents(body))):
            pass
        content = response.content.model_dump(mode="json", by_alias=True, exclude_none=True)
    return {"candidates": [{"content": content, "finishReason": "STOP", "index": 0}],
            "usageMetadata": {"promptTokenCount": 0, "candidatesTokenCount": 0, "totalTokenCount": 0},
            "modelVersion": f"deterministic-{role}"}


# ── Gemini ⇄ OpenAI chat completions ─────────────────────────────────────────

_SCHEMA_KEYS = {"type", "description", "properties", "items", "required", "enum", "format", "nullable",
                "anyOf", "minimum", "maximum", "minItems", "maxItems", "default", "title"}


def to_json_schema(schema: Any) -> Any:
    """Gemini Schema (types in upper case, extra keys) → plain JSON Schema."""
    if isinstance(schema, list):
        return [to_json_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _SCHEMA_KEYS:
            continue
        if key == "type" and isinstance(value, str):
            out["type"] = value.lower()
        elif key == "properties":
            out["properties"] = {k: to_json_schema(v) for k, v in (value or {}).items()}
        elif key in ("items", "anyOf"):
            out[key] = to_json_schema(value)
        elif key == "nullable":
            continue                     # folded below
        else:
            out[key] = value
    if schema.get("nullable") and isinstance(out.get("type"), str):
        out["type"] = [out["type"], "null"]
    return out


def gemini_to_openai(body: dict, model: str) -> dict:
    messages: list[dict] = []
    system = " ".join(p.get("text", "") for p in (body.get("systemInstruction") or {}).get("parts") or [])
    if system.strip():
        messages.append({"role": "system", "content": system})
    pending: dict[str, list[str]] = {}          # function name → ids of calls not answered yet
    for content in body.get("contents") or []:
        role = content.get("role", "user")
        texts, calls, results = [], [], []
        for part in content.get("parts") or []:
            if "text" in part and not part.get("thought"):
                texts.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                call_id = fc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
                pending.setdefault(fc["name"], []).append(call_id)
                calls.append({"id": call_id, "type": "function",
                              "function": {"name": fc["name"], "arguments": json.dumps(fc.get("args") or {})}})
            elif "functionResponse" in part:
                fr = part["functionResponse"]
                queue = pending.get(fr["name"]) or []
                call_id = fr.get("id") or (queue.pop(0) if queue else f"call_{uuid.uuid4().hex[:12]}")
                if fr.get("id") and fr["id"] in queue:
                    queue.remove(fr["id"])
                results.append({"role": "tool", "tool_call_id": call_id,
                                "content": json.dumps(fr.get("response") or {})})
        if role == "model":
            msg: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or None}
            if calls:
                msg["tool_calls"] = calls
            messages.append(msg)
        else:
            messages.extend(results)
            if texts:
                messages.append({"role": "user", "content": "\n".join(texts)})
    request: dict[str, Any] = {"model": model, "messages": messages}
    tools = [{"type": "function", "function": {
        "name": fd["name"], "description": fd.get("description", ""),
        "parameters": to_json_schema(fd.get("parametersJsonSchema") or fd.get("parameters")
                                     or {"type": "object", "properties": {}})}}
             for tool in body.get("tools") or [] for fd in tool.get("functionDeclarations") or []]
    if tools:
        request["tools"] = tools
    config = body.get("generationConfig") or {}
    if "temperature" in config:
        request["temperature"] = config["temperature"]
    if "maxOutputTokens" in config:
        request["max_tokens"] = config["maxOutputTokens"]
    if config.get("responseMimeType") == "application/json":
        schema = response_schema(config)
        # The schema travels: dropping it left a local model to guess the
        # Plan Graph's shape, and Gemma answered JSON the planner refused (E_SCHEMA). With
        # json_schema, Ollama (llama.cpp grammars) and OpenAI-compatible servers constrain the
        # output to it.
        request["response_format"] = ({"type": "json_schema", "json_schema": {"name": "response", "schema": schema}}
                                      if schema else {"type": "json_object"})
    return request


def response_schema(config: dict) -> Optional[dict]:
    """The JSON Schema a Gemini request asks the answer to follow: responseJsonSchema as given
    (plain JSON Schema, $defs included), or responseSchema (Gemini's dialect) converted."""
    if isinstance(config.get("responseJsonSchema"), dict):
        return config["responseJsonSchema"]
    if isinstance(config.get("responseSchema"), dict):
        return to_json_schema(config["responseSchema"])
    return None


_FINISH = {"stop": "STOP", "length": "MAX_TOKENS", "tool_calls": "STOP", "content_filter": "SAFETY"}


def openai_to_gemini(response: dict) -> dict:
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    parts: list[dict] = []
    if message.get("content"):
        parts.append({"text": message["content"]})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {"_raw": fn.get("arguments")}
        parts.append({"functionCall": {"id": call.get("id"), "name": fn.get("name"), "args": args}})
    usage = response.get("usage") or {}
    return {"candidates": [{"content": {"role": "model", "parts": parts or [{"text": ""}]},
                            "finishReason": _FINISH.get(choice.get("finish_reason") or "stop", "STOP"), "index": 0}],
            "usageMetadata": {"promptTokenCount": usage.get("prompt_tokens"),
                              # OpenAI counts reasoning inside completion_tokens; Gemini apart
                              "candidatesTokenCount": (usage.get("completion_tokens") or 0)
                              - ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
                              if usage.get("completion_tokens") is not None else None,
                              "thoughtsTokenCount": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                              "cachedContentTokenCount": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                              "totalTokenCount": usage.get("total_tokens")},
            "modelVersion": response.get("model")}


def openai_error_to_gemini(status: int, body: Any) -> dict:
    message = ((body or {}).get("error") or {}).get("message") if isinstance(body, dict) else None
    return {"error": {"code": status, "message": message or f"Provider error {status}",
                      "status": "RESOURCE_EXHAUSTED" if status == 429 else "UNAVAILABLE" if status >= 500 else "INVALID_ARGUMENT"}}


def openai_endpoint(provider: str, secret: dict) -> tuple[str, dict[str, str]]:
    base = (secret.get("base_url") if provider in ("openai_compatible", "local") else OPENAI_BASES.get(provider)) or ""
    if not base:
        raise ValueError(f"No base URL for provider {provider}")
    key = secret.get("api_key") or ""
    return base.rstrip("/"), ({"Authorization": f"Bearer {key}"} if key else {})


# ── Model catalogues ──────────────────────────────────────────────────────────

async def list_models(client, provider: str, secret: dict, gemini_base: str) -> list[dict]:
    """[{name, generate, embed}] from the provider's own listing."""
    if provider == "gemini":
        models, token = [], None
        for _ in range(10):
            params = {"pageSize": 1000, **({"pageToken": token} if token else {})}
            r = await client.get(f"{gemini_base}/v1beta/models", params=params,
                                 headers={"x-goog-api-key": secret.get("api_key", "")})
            r.raise_for_status()
            body = r.json()
            for m in body.get("models") or []:
                methods = set(m.get("supportedGenerationMethods") or [])
                models.append({"name": m["name"].removeprefix("models/"), "generate": "generateContent" in methods,
                               "embed": bool(methods & {"embedContent", "batchEmbedContents"})})
            token = body.get("nextPageToken")
            if not token:
                break
        return models
    if provider == "anthropic":
        r = await client.get(f"{OPENAI_BASES['anthropic']}/models", params={"limit": 1000},
                             headers={"x-api-key": secret.get("api_key", ""), "anthropic-version": "2023-06-01"})
        r.raise_for_status()
        return [{"name": m["id"], "generate": True, "embed": False} for m in r.json().get("data") or []]
    if provider in TRANSLATED:
        base, headers = openai_endpoint(provider, secret)
        r = await client.get(f"{base}/models", headers=headers)
        r.raise_for_status()
        return [{"name": m["id"], "generate": "embedding" not in m["id"], "embed": "embedding" in m["id"]}
                for m in r.json().get("data") or []]
    raise ValueError(f"Model listing is not available for provider {provider}")


def catalog_error(exc: Exception) -> Optional[str]:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"provider answered {status}" if status else type(exc).__name__


# ── Gemini ⇄ Anthropic Messages API (native) ─────────────────────────────────

ANTHROPIC_URL = f"{OPENAI_BASES['anthropic']}/messages"
ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_STOP = {"end_turn": "STOP", "tool_use": "STOP", "stop_sequence": "STOP", "max_tokens": "MAX_TOKENS",
                   "refusal": "SAFETY"}


def _tool_id(raw: Optional[str]) -> str:
    """Anthropic ids must match ^[a-zA-Z0-9_-]+$; keep a valid one, else make one."""
    import re
    return raw if raw and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw) else f"toolu_{uuid.uuid4().hex[:24]}"


def gemini_to_anthropic(body: dict, model: str) -> dict:
    messages: list[dict] = []
    pending: dict[str, list[str]] = {}
    ids: dict[str, str] = {}                        # Gemini call id → Anthropic tool_use id

    def push(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if messages and messages[-1]["role"] == role:     # the API needs alternating roles
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})

    for content in body.get("contents") or []:
        role = "assistant" if content.get("role") == "model" else "user"
        blocks: list[dict] = []
        for part in content.get("parts") or []:
            if "text" in part and not part.get("thought"):
                if part["text"]:
                    blocks.append({"type": "text", "text": part["text"]})
            elif "functionCall" in part:
                fc = part["functionCall"]
                tid = _tool_id(fc.get("id"))
                if fc.get("id"):
                    ids[fc["id"]] = tid
                pending.setdefault(fc["name"], []).append(tid)
                blocks.append({"type": "tool_use", "id": tid, "name": fc["name"], "input": fc.get("args") or {}})
            elif "functionResponse" in part:
                fr = part["functionResponse"]
                queue = pending.get(fr["name"]) or []
                tid = ids.get(fr.get("id") or "") or (queue.pop(0) if queue else _tool_id(None))
                if tid in queue:
                    queue.remove(tid)
                blocks.append({"type": "tool_result", "tool_use_id": tid, "content": json.dumps(fr.get("response") or {})})
        # tool results must come first in a user turn
        blocks.sort(key=lambda b: 0 if b["type"] == "tool_result" else 1)
        push(role, blocks)
    config = body.get("generationConfig") or {}
    request: dict[str, Any] = {"model": model, "messages": messages,
                               "max_tokens": int(config.get("maxOutputTokens") or 4096)}
    system = " ".join(p.get("text", "") for p in (body.get("systemInstruction") or {}).get("parts") or []).strip()
    if config.get("responseMimeType") == "application/json":
        schema = response_schema(config)
        system = (system + "\n\nAnswer with one JSON object only, no prose, no code fences."
                  + (f" It must validate against this JSON Schema: {json.dumps(schema)}" if schema else "")).strip()
    if system:
        request["system"] = system
    if "temperature" in config:
        request["temperature"] = min(1.0, float(config["temperature"]))
    tools = [{"name": fd["name"], "description": fd.get("description", ""),
              "input_schema": to_json_schema(fd.get("parametersJsonSchema") or fd.get("parameters")
                                             or {"type": "object", "properties": {}})}
             for tool in body.get("tools") or [] for fd in tool.get("functionDeclarations") or []]
    if tools:
        request["tools"] = tools
    return request


def anthropic_to_gemini(response: dict) -> dict:
    parts: list[dict] = []
    for block in response.get("content") or []:
        if block.get("type") == "text" and block.get("text"):
            parts.append({"text": block["text"]})
        elif block.get("type") == "tool_use":
            parts.append({"functionCall": {"id": block.get("id"), "name": block.get("name"), "args": block.get("input") or {}}})
    usage = response.get("usage") or {}
    prompt, output = usage.get("input_tokens"), usage.get("output_tokens")
    cached = usage.get("cache_read_input_tokens")
    if prompt is not None and cached:
        prompt += cached                       # Anthropic's input_tokens excludes cache reads; Gemini's includes them
    return {"candidates": [{"content": {"role": "model", "parts": parts or [{"text": ""}]},
                            "finishReason": _ANTHROPIC_STOP.get(response.get("stop_reason") or "end_turn", "STOP"),
                            "index": 0}],
            "usageMetadata": {"promptTokenCount": prompt, "candidatesTokenCount": output,
                              "totalTokenCount": (prompt or 0) + (output or 0) if prompt is not None else None,
                              **({"cachedContentTokenCount": cached} if cached else {})},
            "modelVersion": response.get("model")}


def anthropic_error_to_gemini(status: int, body: Any) -> dict:
    message = ((body or {}).get("error") or {}).get("message") if isinstance(body, dict) else None
    return openai_error_to_gemini(status, {"error": {"message": message}})


def anthropic_headers(secret: dict) -> dict[str, str]:
    return {"x-api-key": secret.get("api_key", ""), "anthropic-version": ANTHROPIC_VERSION}


# ── A turn that announces instead of acting ──────────────
# Measured on CPU-served models (Gemma 4, Qwen 3.5): with tools offered, a turn often ends with
# "Let me find the template…" and no call, which ends the agent's turn and fails the step. The
# gateway asks the local model once more, with the announcement kept and a plain instruction.

_ANNOUNCE = re.compile(r"\b(let me|i will|i'll|i am going to|i'm going to|i need to|next,? i|now i|i must|i should)\b",
                       re.IGNORECASE)
NUDGE = ("Do it now: call the tool you just described, with its arguments. Do not describe the call again. "
         "If no tool call is needed, give your final answer (a VERDICT line when your instructions ask for one).")


def announces_without_acting(request: dict, raw: Optional[dict]) -> Optional[str]:
    """The text of an OpenAI-shaped answer that offered tools, called none, and announced an action."""
    if not request.get("tools") or not isinstance(raw, dict):
        return None
    message = ((raw.get("choices") or [{}])[0] or {}).get("message") or {}
    text = (message.get("content") or "").strip()
    if message.get("tool_calls") or not text or "VERDICT" in text:
        return None
    return text if _ANNOUNCE.search(text[-400:]) else None
