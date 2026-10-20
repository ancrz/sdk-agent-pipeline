"""Local models (profile local-llm): what is installed, install on demand, remove.

Two Ollama processes share one model volume:
- `ollama` serves inference on engine-llm-net, which has no internet;
- `ollama-pull` only downloads and deletes. It has its own egress network and only
  the gateway can reach it.

So installing a model never gives the serving process internet. After a pull, the
model is checked through the serving process (`/api/show`): that proves it is
served and reads its capabilities. Workers need "tools" (function calling); a model
without it can still answer plain chat or JSON plans.

Names: Ollama library names (`qwen3:4b`, `library/x:tag`) or a Hugging Face GGUF
(`hf.co/<user>/<repo>[:<quant>]`).

Pull jobs are shared through Valkey (vg:pull:<model>, 1 h), so every gateway replica
shows the same progress and a second pull of a running model is not started twice;
process memory is the fallback. A restart loses only the progress view: Ollama
resumes a partial download on the next pull.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, Optional

import httpx

NAME = re.compile(r"^(hf\.co/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+"          # a Hugging Face GGUF
                  r"|[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9._-]*"           # namespace/model (no dot in the namespace)
                  r"|[a-z0-9][a-z0-9._-]*)"                              # model
                  r"(:[A-Za-z0-9._-]+)?$")
TOOL_ROLES = ("orchestrator", "executor", "validator", "documentalist", "author", "medic")


def serve_url() -> str:
    return os.environ.get("ENGINE_OLLAMA_URL", "http://ollama:11434").rstrip("/")


def pull_url() -> str:
    return os.environ.get("ENGINE_OLLAMA_PULL_URL", "http://ollama-pull:11434").rstrip("/")


def valid_name(name: str) -> bool:
    return 2 <= len(name) <= 160 and NAME.fullmatch(name) is not None


async def _version(client: httpx.AsyncClient, base: str) -> Optional[str]:
    try:
        r = await client.get(f"{base}/api/version", timeout=5)
        return r.json().get("version") if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def show(client: httpx.AsyncClient, name: str) -> Optional[dict[str, Any]]:
    """The serving process's view of a model: details and capabilities; None when it does not serve it."""
    try:
        r = await client.post(f"{serve_url()}/api/show", json={"model": name}, timeout=30)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    details = data.get("details") or {}
    return {"capabilities": sorted(data.get("capabilities") or []), "family": details.get("family"),
            "parameter_size": details.get("parameter_size"), "quantization": details.get("quantization_level"),
            "context_length": next((v for k, v in (data.get("model_info") or {}).items() if k.endswith(".context_length")), None)}


async def installed(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    r = await client.get(f"{serve_url()}/api/tags", timeout=15)
    r.raise_for_status()
    out = []
    for m in r.json().get("models") or []:
        info = await show(client, m["name"]) or {"capabilities": []}
        out.append({"name": m["name"], "size": m.get("size"), "modified_at": m.get("modified_at"), **info,
                    "tools": "tools" in info["capabilities"]})
    return sorted(out, key=lambda m: m["name"])


PULL_PREFIX = "vg:pull:"
STALE_S = 120.0            # a running job not updated for this long belongs to a replica that died


class Pulls:
    """Download jobs, one per model name, run in the background against ollama-pull."""

    def __init__(self, valkey: Any = None) -> None:
        self.valkey = valkey
        self.jobs: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._saved: dict[str, float] = {}

    async def _save(self, job: dict[str, Any], force: bool = False) -> None:
        job["updated_at"] = time.time()
        if self.valkey is None or (not force and time.time() - self._saved.get(job["model"], 0) < 1.0):
            return
        self._saved[job["model"]] = time.time()
        try:
            await self.valkey.set(PULL_PREFIX + job["model"], json.dumps(job), ex=3600)
        except Exception:
            pass                                  # Valkey down: this replica still shows its own jobs

    async def _shared(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        if self.valkey is not None:
            try:
                async for key in self.valkey.scan_iter(match=PULL_PREFIX + "*"):
                    raw = await self.valkey.get(key)
                    if raw:
                        job = json.loads(raw)
                        found[job["model"]] = job
            except Exception:
                pass
        for name, job in self.jobs.items():         # this replica's own view is the freshest
            found[name] = job
        return found

    async def start(self, client: httpx.AsyncClient, name: str) -> dict[str, Any]:
        job = (await self._shared()).get(name)
        if job and job["state"] == "running" and time.time() - job.get("updated_at", 0) < STALE_S:
            return job                              # already downloading here or on another replica
        self.jobs[name] = {"model": name, "state": "running", "status": "starting", "completed": 0, "total": 0,
                           "error": None, "started_at": time.time(), "finished_at": None, "capabilities": []}
        await self._save(self.jobs[name], force=True)
        self._tasks[name] = asyncio.create_task(self._run(client, name), name=f"pull-{name}")
        return self.jobs[name]

    async def _run(self, client: httpx.AsyncClient, name: str) -> None:
        job = self.jobs[name]
        try:
            async with client.stream("POST", f"{pull_url()}/api/pull", json={"model": name, "stream": True},
                                     timeout=httpx.Timeout(None, connect=10)) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"ollama-pull answered {r.status_code}: {(await r.aread()).decode()[:300]}")
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if event.get("error"):
                        raise RuntimeError(str(event["error"])[:300])
                    job["status"] = event.get("status", job["status"])
                    if event.get("total"):
                        job["total"], job["completed"] = event["total"], event.get("completed", 0)
                    await self._save(job)
            info = await show(client, name)
            if info is None:
                raise RuntimeError("downloaded, but the model server does not list it")
            job.update(state="done", status="installed", capabilities=info["capabilities"])
        except Exception as exc:  # recorded for the UI; the next pull resumes the download
            job.update(state="failed", error=f"{type(exc).__name__}: {exc}"[:400])
        finally:
            job["finished_at"] = time.time()
            await self._save(job, force=True)
            self._tasks.pop(name, None)

    async def snapshot(self) -> list[dict[str, Any]]:
        cutoff, now = time.time() - 3600, time.time()     # finished jobs are shown for an hour
        out = []
        for job in (await self._shared()).values():
            if job["state"] == "running" and now - job.get("updated_at", now) >= STALE_S:
                job = {**job, "state": "failed", "finished_at": job.get("updated_at"),
                       "error": "interrupted (its gateway replica stopped); pull again to resume"}
            if job["state"] == "running" or (job.get("finished_at") or 0) > cutoff:
                out.append(job)
        return sorted(out, key=lambda j: j["model"])


async def status(client: httpx.AsyncClient, pulls: Pulls) -> dict[str, Any]:
    serve, puller = await asyncio.gather(_version(client, serve_url()), _version(client, pull_url()))
    models: list[dict[str, Any]] = []
    error = None
    if serve:
        try:
            models = await installed(client)
        except httpx.HTTPError as exc:
            error = f"listing failed: {type(exc).__name__}"
    return {"server": {"reachable": serve is not None, "version": serve},
            "puller": {"reachable": puller is not None, "version": puller},
            "models": models, "pulls": await pulls.snapshot(), "error": error}


async def delete(client: httpx.AsyncClient, name: str) -> int:
    r = await client.request("DELETE", f"{pull_url()}/api/delete", json={"model": name}, timeout=60)
    return r.status_code
