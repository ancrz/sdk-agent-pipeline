"""X-Engine-Activity: which activity a model call belongs to."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from google.genai import types

from orchestrator import activity


def test_parse_accepts_only_known_kinds_and_uuids():
    rid = uuid.uuid4()
    assert activity.parse(f"run:{rid}") == ("run", rid)
    assert activity.parse("index") == ("index", None)
    assert activity.parse("run") is None and activity.parse("run:not-a-uuid") is None
    assert activity.parse(f"admin:{rid}") is None and activity.parse(None) is None


def test_bound_restores_the_outer_activity():
    with activity.bound("chat", "11111111-1111-1111-1111-111111111111"):
        with activity.bound("plan", "22222222-2222-2222-2222-222222222222"):
            assert activity.current().startswith("plan:")
        assert activity.current().startswith("chat:")
    assert activity.headers() == {}


def _request():
    return SimpleNamespace(config=types.GenerateContentConfig(http_options=types.HttpOptions(headers={"keep": "1"})))


def test_a_worker_tags_its_model_requests_from_the_a2a_metadata():
    rid = uuid.uuid4()
    ctx = SimpleNamespace(_invocation_context=SimpleNamespace(run_config=SimpleNamespace(
        custom_metadata={"a2a_metadata": {"engine_activity": f"run:{rid}", "engine_worker_token": "t"}})))
    req = _request()
    assert activity.adk_callback(ctx, req) is None                 # the model call proceeds
    assert req.config.http_options.headers == {"keep": "1", "X-Engine-Activity": f"run:{rid}"}


def test_no_activity_leaves_the_request_alone():
    req = _request()
    activity.adk_callback(SimpleNamespace(), req)
    assert req.config.http_options.headers == {"keep": "1"}
