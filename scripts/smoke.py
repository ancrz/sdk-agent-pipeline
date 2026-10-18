"""End-to-end smoke of the engine, run inside the agents network (scripts/smoke.sh starts it):

1. both gateway replicas answer /healthz, and model-gateway-ha resolves to the live ones;
2. the orchestrator role (the consuming application's chat: the superior model) calls a model
   through model-gateway-ha with its own gateway token;
3. an A2A task to a worker role (default documentalist) at http://worker-agents:<port>/<role>
   (or the root when the worker serves one role),
   with the same client and request shape vanguard-control's API uses; it prints the answer.

With the offline preset (`python -m store.admin preset activate offline`) every answer comes
from the gateway's deterministic stand-in: no provider, no key, reproducible.

    python scripts/smoke.py [--role documentalist] [--run <run id>] [--text "..."]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid

import httpx

GATEWAY = os.environ.get("MODEL_GATEWAY_URL", f"http://model-gateway-ha:{os.environ.get('MODEL_GATEWAY_PORT', '8095')}")
WORKERS = os.environ.get("ENGINE_WORKERS_URL", f"http://worker-agents:{os.environ.get('WORKER_PORT', '8080')}")
TERMINAL = ("COMPLETED", "FAILED", "CANCELED", "REJECTED", "INPUT_REQUIRED", "AUTH_REQUIRED")


def step(ok: bool, what: str) -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {what}")
    if not ok:
        raise SystemExit(1)


def gateways() -> None:
    port = os.environ.get("MODEL_GATEWAY_PORT", "8095")
    for name in ("model-gateway", "model-gateway-b"):
        try:
            r = httpx.get(f"http://{name}:{port}/healthz", timeout=5)
            print(f"[{'ok' if r.status_code == 200 else '--'}] {name} /healthz {r.status_code}")
        except httpx.HTTPError as exc:
            print(f"[--] {name} unreachable ({type(exc).__name__})")
    r = httpx.get(f"{GATEWAY}/healthz", timeout=5)
    step(r.status_code == 200, f"model-gateway-ha /healthz {r.status_code}")


def orchestrator_call(model: str) -> None:
    token = os.environ.get("VANGUARD_GATEWAY_TOKEN_ORCHESTRATOR", "")
    step(bool(token), "VANGUARD_GATEWAY_TOKEN_ORCHESTRATOR is set")
    t0 = time.monotonic()
    r = httpx.post(f"{GATEWAY}/v1beta/models/{model}:generateContent", headers={"x-goog-api-key": token},
                   json={"contents": [{"role": "user", "parts": [{"text": "Answer OK."}]}]}, timeout=120)
    text = ""
    if r.status_code == 200:
        parts = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        text = " ".join(p.get("text", "") for p in parts).strip()
    step(r.status_code == 200, f"orchestrator → gateway {r.status_code} in {time.monotonic() - t0:.1f}s: {text[:80]!r}")


async def a2a_task(role: str, text: str, run: str | None) -> None:
    from a2a.client import ClientConfig, create_client  # type: ignore[import]
    from a2a.types import GetTaskRequest, Message, Part, Role, SendMessageConfiguration, SendMessageRequest, TaskState
    from google.protobuf.struct_pb2 import Struct

    # several roles: each under /<role>/ and /healthz lists them; one role: served at the root
    r = httpx.get(f"{WORKERS}/healthz", timeout=5)
    if r.status_code == 200:
        step(role in r.json().get("roles", []), f"worker-agents /healthz {r.text.strip()}")
        url = f"{WORKERS}/{role}"
    else:
        card = httpx.get(f"{WORKERS}/.well-known/agent-card.json", timeout=5)
        step(card.status_code == 200 and card.json().get("name") == f"vanguard_{role}",
             f"worker-agents serves one role at the root: {card.json().get('name') if card.status_code == 200 else card.status_code}")
        url = WORKERS
    client = await create_client(url, client_config=ClientConfig(streaming=False))
    try:
        request = SendMessageRequest(
            message=Message(message_id=str(uuid.uuid4()), role=Role.ROLE_USER, context_id=str(uuid.uuid4()),
                            parts=[Part(text=text)]),
            configuration=SendMessageConfiguration(return_immediately=True))
        if run:
            metadata = Struct()
            metadata.update({"vanguard_activity": f"run:{run}"})      # orchestrator/activity.py METADATA_KEY
            request.metadata.CopyFrom(metadata)
        task_id = None
        async for item in client.send_message(request):
            task_id = item.task.id if item.HasField("task") else (
                item.status_update.task_id if item.HasField("status_update") else None)
            if task_id:
                break
        step(bool(task_id), f"A2A {role}: task {task_id}")
        t0, state, task = time.monotonic(), "", None
        while time.monotonic() - t0 < 180:
            task = await client.get_task(GetTaskRequest(id=task_id))
            state = TaskState.Name(task.status.state)
            if any(s in state for s in TERMINAL):
                break
            await asyncio.sleep(1)
        answer = next((p.text for a in task.artifacts for p in a.parts if p.HasField("text") and p.text), "") if task else ""
        if not answer and task is not None:
            answer = next((p.text for p in task.status.message.parts if p.HasField("text")), "")
        step("COMPLETED" in state, f"A2A {role}: {state} in {time.monotonic() - t0:.1f}s")
        print("--- answer ---\n" + answer.strip()[:1200])
    finally:
        await client.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default="documentalist")
    ap.add_argument("--run", help="a run id (python -m store.admin run new …) to attribute the calls to")
    ap.add_argument("--text", default="Write a three-line closure report for: smoke test of the agent engine, "
                                       "gateway HA and A2A worker, all checks passed.")
    ap.add_argument("--model", default=os.environ.get("VANGUARD_DEFAULT_MODEL", "gemini-3.5-flash-lite"))
    args = ap.parse_args()
    gateways()
    orchestrator_call(args.model)
    asyncio.run(a2a_task(args.role, args.text, args.run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
