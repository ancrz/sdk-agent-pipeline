"""Local models: install on demand through ollama-pull, served by ollama, capabilities checked."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from model_gateway import local_models

from .test_gateway import client_for, sf  # noqa: F401  (fixture)

ADMIN = {"Authorization": "Bearer " + "a" * 32}


class FakeOllama:
    """Two Ollama processes sharing a volume: `ollama` (serve) and `ollama-pull`."""

    def __init__(self, fail: str | None = None):
        self.volume = {"gemma4:e4b": ["completion", "tools", "vision"]}
        self.fail = fail
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        self.calls.append(f"{request.method} {host}{path}")
        if path == "/api/version":
            return httpx.Response(200, json={"version": "0.35.0"})
        if host == "ollama" and path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": n, "size": 1} for n in self.volume]})
        if host == "ollama" and path == "/api/show":
            name = json.loads(request.content)["model"]
            if name not in self.volume:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"capabilities": self.volume[name],
                                             "details": {"family": "x", "parameter_size": "4B", "quantization_level": "Q4_K_M"},
                                             "model_info": {"x.context_length": 32768}})
        if host == "ollama-pull" and path == "/api/pull":
            name = json.loads(request.content)["model"]
            if self.fail:
                lines = [{"status": "pulling manifest"}, {"error": self.fail}]
            else:
                self.volume[name] = ["completion"] if "tiny" in name else ["completion", "tools"]
                lines = [{"status": "pulling manifest"}, {"status": "pulling abc", "total": 100, "completed": 40},
                         {"status": "pulling abc", "total": 100, "completed": 100}, {"status": "success"}]
            return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())
        if host == "ollama-pull" and path == "/api/delete":
            name = json.loads(request.content)["model"]
            return httpx.Response(200 if self.volume.pop(name, None) else 404)
        return httpx.Response(500, json={"error": f"unexpected {host}{path}"})


@pytest.fixture(autouse=True)
def admin_token(monkeypatch):
    monkeypatch.setenv("ENGINE_GATEWAY_ADMIN_TOKEN", "a" * 32)


async def _wait(c, name):
    for _ in range(50):
        jobs = {j["model"]: j for j in (await c.get("/admin/local", headers=ADMIN)).json()["pulls"]}
        if jobs.get(name, {}).get("state") in ("done", "failed"):
            return jobs[name]
        await asyncio.sleep(0.02)
    raise AssertionError("the pull did not finish")


def test_model_names():
    for ok in ("qwen3:4b", "gemma4:e4b", "library/llama3.1:8b", "hf.co/bartowski/Qwen3-4B-GGUF:Q4_K_M", "mistral-nemo"):
        assert local_models.valid_name(ok), ok
    for bad in ("", "x", "../etc", "qwen3:4b; rm -rf /", "http://evil/x", "hf.co/a", "UPPER:case"):
        assert not local_models.valid_name(bad), bad


@pytest.mark.asyncio
async def test_status_lists_models_with_capabilities(sf):  # noqa: F811
    async with client_for(sf, FakeOllama()) as c:
        assert (await c.get("/admin/local")).status_code == 401
        data = (await c.get("/admin/local", headers=ADMIN)).json()
    assert data["server"] == {"reachable": True, "version": "0.35.0"} and data["puller"]["reachable"]
    (m,) = data["models"]
    assert (m["name"], m["tools"], m["parameter_size"], m["context_length"]) == ("gemma4:e4b", True, "4B", 32768)


@pytest.mark.asyncio
async def test_install_on_demand_then_remove(sf):  # noqa: F811
    fake = FakeOllama()
    async with client_for(sf, fake) as c:
        bad = await c.post("/admin/local/pull", headers=ADMIN, json={"model": "x; curl evil"})
        assert bad.status_code == 422
        r = await c.post("/admin/local/pull", headers=ADMIN, json={"model": "qwen3:4b"})
        assert r.status_code == 202 and r.json()["state"] == "running"
        job = await _wait(c, "qwen3:4b")
        assert job["state"] == "done" and job["completed"] == job["total"] == 100 and "tools" in job["capabilities"]
        names = [m["name"] for m in (await c.get("/admin/local", headers=ADMIN)).json()["models"]]
        assert names == ["gemma4:e4b", "qwen3:4b"]
        assert (await c.delete("/admin/local/models/qwen3:4b", headers=ADMIN)).status_code == 204
        assert (await c.delete("/admin/local/models/qwen3:4b", headers=ADMIN)).status_code == 404
    # downloads go to ollama-pull only; the server is only asked what it serves
    assert all("ollama-pull" in call for call in fake.calls if "/api/pull" in call or "/api/delete" in call)


@pytest.mark.asyncio
async def test_a_failed_download_is_reported(sf):  # noqa: F811
    async with client_for(sf, FakeOllama(fail="pull model manifest: file does not exist")) as c:
        await c.post("/admin/local/pull", headers=ADMIN, json={"model": "nope:1b"})
        job = await _wait(c, "nope:1b")
    assert job["state"] == "failed" and "does not exist" in job["error"]


@pytest.mark.asyncio
async def test_unreachable_profile_is_reported_not_raised(sf):  # noqa: F811
    def down(request):
        raise httpx.ConnectError("no route", request=request)
    async with client_for(sf, down) as c:
        data = (await c.get("/admin/local", headers=ADMIN)).json()
    assert data["server"]["reachable"] is False and data["models"] == []


class SharedValkey:
    """The subset of valkey.asyncio the pull jobs use."""

    def __init__(self):
        self.data = {}

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def get(self, key):
        return self.data.get(key)

    async def scan_iter(self, match="*"):
        prefix = match.rstrip("*")
        for key in list(self.data):
            if key.startswith(prefix):
                yield key


@pytest.mark.asyncio
async def test_replicas_share_downloads_through_valkey(monkeypatch):
    shared, fake = SharedValkey(), FakeOllama()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
        a, b = local_models.Pulls(shared), local_models.Pulls(shared)
        job = await a.start(client, "qwen3:4b")
        assert job["state"] == "running"
        again = await b.start(client, "qwen3:4b")               # the other replica does not pull twice
        assert again["state"] == "running" and "qwen3:4b" not in b._tasks
        await a._tasks["qwen3:4b"]
        (seen,) = await b.snapshot()                             # and shows the same progress
        assert seen["state"] == "done" and "tools" in seen["capabilities"]
    pulls_to_ollama = [c for c in fake.calls if "/api/pull" in c]
    assert len(pulls_to_ollama) == 1


@pytest.mark.asyncio
async def test_a_job_of_a_dead_replica_is_reported_interrupted():
    import json as _json
    import time as _time
    shared = SharedValkey()
    shared.data["vg:pull:x:1b"] = _json.dumps({"model": "x:1b", "state": "running", "status": "pulling", "completed": 1,
                                                "total": 9, "error": None, "finished_at": None, "capabilities": [],
                                                "updated_at": _time.time() - 600})
    (job,) = await local_models.Pulls(shared).snapshot()
    assert job["state"] == "failed" and "interrupted" in job["error"]
