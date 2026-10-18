"""Thought signatures across a model switch (model_gateway/signatures.py)."""

import json

from model_gateway.signatures import DUMMY, backfill


def _fc(name, sig=None):
    part = {"functionCall": {"name": name, "args": {}}}
    if sig:
        part["thoughtSignature"] = sig
    return part


def _fr(name):
    return {"role": "user", "parts": [{"functionResponse": {"name": name, "response": {}}}]}


def test_an_unsigned_call_of_the_current_turn_gets_the_placeholder_and_signed_ones_stay():
    contents = [
        {"role": "user", "parts": [{"text": "old turn"}]},
        {"role": "model", "parts": [_fc("old")]},                       # earlier turn: not validated
        _fr("old"),
        {"role": "user", "parts": [{"text": "launch the template"}]},   # the current turn opens here
        {"role": "model", "parts": [_fc("a", "REAL"), _fc("b")]},       # parallel: only the first is signed
        _fr("a"), _fr("b"),
        {"role": "model", "parts": [{"text": "thinking"}, _fc("c")]},   # written by the local model
        _fr("c"),
    ]
    body, added = backfill(json.dumps({"contents": contents}).encode())
    out = json.loads(body)["contents"]
    assert added == 1
    assert out[4]["parts"][0]["thoughtSignature"] == "REAL" and "thoughtSignature" not in out[4]["parts"][1]
    assert out[7]["parts"][1]["thoughtSignature"] == DUMMY
    assert "thoughtSignature" not in out[1]["parts"][0]                 # the earlier turn is left alone


def test_a_request_with_nothing_missing_is_passed_byte_for_byte():
    raw = json.dumps({"contents": [{"role": "user", "parts": [{"text": "hi"}]},
                                   {"role": "model", "parts": [_fc("a", "S")]}]}).encode()
    assert backfill(raw) == (raw, 0)
    assert backfill(b"not json") == (b"not json", 0)
    assert backfill(b'{"contents": "x"}') == (b'{"contents": "x"}', 0)


def test_an_empty_stop_answer_is_recognised_and_a_real_one_is_not():
    from model_gateway.signatures import empty_answer
    assert empty_answer({"candidates": [{"finishReason": "STOP", "content": {"role": "model"}}]})
    assert empty_answer({"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]})
    assert not empty_answer({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "ok"}]}}]})
    assert not empty_answer({"candidates": [{"finishReason": "SAFETY", "content": {}}]})       # a refusal is an answer
    assert not empty_answer({"promptFeedback": {"blockReason": "OTHER"}, "candidates": [{"finishReason": "STOP"}]})
    assert not empty_answer({"candidates": []}) and not empty_answer(None)
