import importlib.util
from argparse import Namespace
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "judge_quality_pool.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("judge_quality_pool", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_load_completed_retries_errors(tmp_path):
    path = tmp_path / "results.jsonl"
    path.write_text(
        '{"id":"good","verdict":{"decision":"reject"},"error":null}\n'
        '{"id":"retry","verdict":null,"error":"HTTP 500"}\n',
        encoding="utf-8",
    )

    completed = MODULE.load_completed(path)

    assert set(completed) == {"good"}


def test_force_chat_routes_a_responses_model_through_chat(monkeypatch):
    captured = {}

    def fake_post_json(endpoint, payload, headers):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        return {"choices": [{"message": {"content": "{}"}}], "usage": {}}

    monkeypatch.setattr(MODULE.api, "post_json", fake_post_json)
    monkeypatch.setattr(MODULE.api, "chat_text", lambda data: "{}")
    args = Namespace(
        chat_endpoint="http://127.0.0.1:8787/v1/chat/completions",
        responses_endpoint="https://example.invalid/responses",
        token_endpoint="http://127.0.0.1:8787/token",
        max_output_tokens=256,
        force_chat=True,
    )

    MODULE.invoke("gpt-5.6-sol", "prompt", args)

    assert captured["endpoint"] == "http://127.0.0.1:8787/v1/chat/completions"
    assert captured["payload"]["messages"][1]["content"] == "prompt"


def test_without_force_chat_a_responses_model_uses_the_responses_api(monkeypatch):
    captured = {}

    def fake_post_json(endpoint, payload, headers):
        captured["endpoint"] = endpoint
        return {"output": [], "usage": {}}

    monkeypatch.setattr(MODULE.api, "post_json", fake_post_json)
    monkeypatch.setattr(MODULE.api, "response_text", lambda data: "{}")
    monkeypatch.setattr(MODULE.api, "copilot_token", lambda endpoint, refresh=False: "t")
    args = Namespace(
        chat_endpoint="http://127.0.0.1:4399/v1/chat/completions",
        responses_endpoint="https://example.invalid/responses",
        token_endpoint="http://127.0.0.1:4399/token",
        max_output_tokens=256,
        force_chat=False,
    )

    MODULE.invoke("gpt-5.6-sol", "prompt", args)

    assert captured["endpoint"] == "https://example.invalid/responses"


def test_force_chat_rejects_a_silent_model_fallback(monkeypatch):
    def fake_post_json(endpoint, payload, headers):
        return {
            "model": "gpt-5.6-sol",
            "choices": [{"message": {"content": "{}"}}],
            "usage": {},
        }

    monkeypatch.setattr(MODULE.api, "post_json", fake_post_json)
    args = Namespace(
        chat_endpoint="http://127.0.0.1:8787/v1/chat/completions",
        responses_endpoint="https://example.invalid/responses",
        token_endpoint="http://127.0.0.1:8787/token",
        max_output_tokens=256,
        force_chat=True,
    )

    with pytest.raises(RuntimeError, match="judge model mismatch"):
        MODULE.invoke("claude-opus-4.8", "prompt", args)
