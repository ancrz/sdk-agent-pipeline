"""Gemini answers the gateway repairs on the way (docs/pipeline/demo_*): thought signatures across a
model switch, and an empty answer asked again.

Thought signatures.

Gemini 3 validates, in the *current turn* of a request's history, that the first `functionCall`
part of every model step carries the `thoughtSignature` Gemini returned with it; a missing one is
a 400 ("Function call is missing a thought_signature"). The gateway is the one component that
switches models mid-conversation: a failover to the local model (Gemma), or the deterministic
stand-in, writes function calls without signatures, and the next request that reaches Gemini with
that history is refused. Found rehearsing the demo: gemini-main exhausted its free quota, the backup
answered 503, the executor's step went on with Gemma, and Medic's fixed step could not be re-run.

Google documents the remedy for a trace that comes from a model without signatures: put a dummy
value in the field to skip validation (`skip_thought_signature_validator`;
ai.google.dev/gemini-api/docs/generate-content/thought-signatures). Real signatures are never
touched; only the first function call of each step of the current turn is filled, as the
validation reads it (parallel calls carry one signature, on the first part).
"""

from __future__ import annotations

import json
from typing import Any

DUMMY = "skip_thought_signature_validator"


def _turn_start(contents: list[dict[str, Any]]) -> int:
    """Index of the current turn's opening user message: the latest user content with text
    (a functionResponse is part of the turn, not a new one)."""
    for i in range(len(contents) - 1, -1, -1):
        content = contents[i]
        if content.get("role", "user") == "user" and any(isinstance(p, dict) and "text" in p
                                                         for p in content.get("parts") or []):
            return i
    return 0


def backfill(body: bytes) -> tuple[bytes, int]:
    """(request body, signatures added). The body is returned unchanged when nothing is missing."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return body, 0
    contents = data.get("contents") if isinstance(data, dict) else None
    if not isinstance(contents, list):
        return body, 0
    added = 0
    for content in contents[_turn_start(contents):]:
        if not isinstance(content, dict) or content.get("role") != "model":
            continue
        for part in content.get("parts") or []:
            if isinstance(part, dict) and ("functionCall" in part or "function_call" in part):
                if not (part.get("thoughtSignature") or part.get("thought_signature")):
                    part["thoughtSignature"] = DUMMY
                    added += 1
                break                         # only the step's first function call is validated
    return (json.dumps(data).encode(), added) if added else (body, 0)


# ── An empty answer ──────────────────────────────────────────────────────────
# Gemini sometimes answers 200 with finishReason STOP and no parts at all. ADK turns that into
# MODEL_RETURNED_NO_CONTENT and ends the agent's turn, which cost Medic an attempt in the demo
# (case 4: no diagnosis, no hand, the step failed). It is usually transient: the same request
# asked again answers. The gateway retries it (EMPTY_RETRIES) before passing it on.

EMPTY_RETRIES = 2


def empty_answer(payload: Any) -> bool:
    """A 200 whose every candidate stopped with no content parts (and that nothing blocked)."""
    if not isinstance(payload, dict) or (payload.get("promptFeedback") or {}).get("blockReason"):
        return False
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return False
    for c in candidates:
        if not isinstance(c, dict) or c.get("finishReason") not in (None, "STOP"):
            return False
        if ((c.get("content") or {}).get("parts")):
            return False
    return True
