"""Which activity a model call belongs to: the header X-Engine-Activity.

Callers say `<kind>:<uuid>` (run, plan, chat, eda) or just `index`; the model gateway stores it on
the call's audit row so money can be summed per run, plan or chat. It is accounting, never
authorization: the gateway keeps it only when that run/plan/chat exists, and never refuses a call
without it.

- The API's own calls (planner, chat, indexer) set it with `bound(kind, id)` around the work; the
  genai config and the chat agent read `headers()`.
- Workers get it in the A2A request metadata (`engine_activity`, next to the worker token) and
  `adk_callback` puts it on each model request (proven: a per-request http_options header
  set in before_model_callback reaches the HTTP request).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

HEADER = "X-Engine-Activity"
METADATA_KEY = "engine_activity"
KINDS = frozenset({"run", "plan", "chat", "eda", "index"})
_current: ContextVar[Optional[str]] = ContextVar("engine_activity", default=None)


def tag(kind: str, ident: Any = None) -> str:
    return f"{kind}:{ident}" if ident is not None else kind


def parse(value: Optional[str]) -> Optional[tuple[str, Optional[uuid.UUID]]]:
    """'run:<uuid>' → ('run', UUID); 'index' → ('index', None); anything else → None."""
    if not value:
        return None
    kind, _, ident = value.strip().partition(":")
    if kind not in KINDS:
        return None
    if not ident:
        return (kind, None) if kind == "index" else None
    try:
        return kind, uuid.UUID(ident)
    except ValueError:
        return None


@contextmanager
def bound(kind: str, ident: Any = None) -> Iterator[None]:
    token = _current.set(tag(kind, ident))
    try:
        yield
    finally:
        _current.reset(token)


def mark(kind: str, ident: Any = None) -> None:
    """Tag the rest of this request's work (an HTTP request runs in its own context copy, so the
    tag ends with it): the planner's route and the chat's turn, whose model calls happen deep in
    the call."""
    _current.set(tag(kind, ident))


def current() -> Optional[str]:
    return _current.get()


def headers() -> dict[str, str]:
    value = _current.get()
    return {HEADER: value} if value else {}


def _from_invocation(callback_context: Any) -> Optional[str]:
    """The A2A request metadata a worker's invocation carries (RunConfig.custom_metadata)."""
    inv = getattr(callback_context, "_invocation_context", None)
    run_config = getattr(inv, "run_config", None)
    metadata = (getattr(run_config, "custom_metadata", None) or {}).get("a2a_metadata") or {}
    value = metadata.get(METADATA_KEY)
    return value if isinstance(value, str) else None


def adk_callback(callback_context: Any, llm_request: Any) -> None:
    """ADK before_model_callback: the activity as a header of this model request."""
    value = _current.get() or _from_invocation(callback_context)
    if not value or parse(value) is None or getattr(llm_request, "config", None) is None:
        return None
    from google.genai import types
    opts = llm_request.config.http_options or types.HttpOptions()
    llm_request.config.http_options = opts.model_copy(update={"headers": {**(opts.headers or {}), HEADER: value}})
    return None
