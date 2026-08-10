import importlib.util
from argparse import Namespace
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "generate_release_patches.py"
SPEC = importlib.util.spec_from_file_location("generate_release_patches", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_apply_search_replace_supports_multiple_blocks():
    before = "one\ntwo\nthree\n"
    response = """<<<<<<< SEARCH
one
=======
ONE
>>>>>>> REPLACE
<<<<<<< SEARCH
three
=======
THREE
>>>>>>> REPLACE"""

    after, note = MODULE.apply_search_replace(before, response)

    assert note == "ok"
    assert after == "ONE\ntwo\nTHREE\n"


def test_source_excerpt_contains_anchor_and_respects_budget():
    source = "".join(f"line-{index:04d}\n" for index in range(1000))

    excerpt, first, last = MODULE.source_excerpt(source, anchor_line=500, maximum_chars=1000)

    assert "line-0499\n" in excerpt
    assert first <= 500 <= last
    assert len(excerpt) <= 1020


def test_unified_patch_uses_git_paths():
    patch = MODULE.unified_patch("src/example.py", "value = 1\n", "value = 2\n")

    assert patch.startswith("--- a/src/example.py\n+++ b/src/example.py\n")
    assert "-value = 1" in patch
    assert "+value = 2" in patch


def test_source_at_start_trusts_exact_mounted_repo(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(command, 0, stdout="source", stderr="")

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)
    task = {
        "repo": "owner/repo",
        "start_sha": "abc123",
        "locality": {"path": "src/file.py"},
    }

    assert MODULE.source_at_start(task, tmp_path) == "source"
    repository = (tmp_path / "owner__repo").resolve()
    assert captured["cwd"] == repository
    assert f"safe.directory={repository}" in captured["command"]


def test_force_chat_routes_responses_model_and_checks_identity(monkeypatch):
    captured = {}

    def fake_post_json(endpoint, payload, headers, **kwargs):
        captured["endpoint"] = endpoint
        return {
            "model": "gpt-5.5",
            "choices": [{"message": {"content": "OK"}}],
            "usage": {},
        }

    monkeypatch.setattr(MODULE, "post_json", fake_post_json)
    args = Namespace(
        chat_endpoint="http://127.0.0.1:8787/v1/chat/completions",
        responses_endpoint="unused",
        token_endpoint="unused",
        max_output_tokens=128,
        request_timeout=30,
        request_retries=1,
        force_chat=True,
    )

    text, _ = MODULE.invoke_model("gpt-5.5", "system", "prompt", args)

    assert text == "OK"
    assert captured["endpoint"].endswith("/v1/chat/completions")


def test_force_chat_rejects_silent_solver_fallback(monkeypatch):
    monkeypatch.setattr(
        MODULE,
        "post_json",
        lambda *args, **kwargs: {
            "model": "gpt-5.6-sol",
            "choices": [{"message": {"content": "OK"}}],
            "usage": {},
        },
    )
    args = Namespace(
        chat_endpoint="http://127.0.0.1:8787/v1/chat/completions",
        responses_endpoint="unused",
        token_endpoint="unused",
        max_output_tokens=128,
        request_timeout=30,
        request_retries=1,
        force_chat=True,
    )

    with pytest.raises(RuntimeError, match="solver model mismatch"):
        MODULE.invoke_model("gpt-5.5", "system", "prompt", args)


@pytest.mark.parametrize(
    "model",
    ["gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra"],
)
def test_current_gpt_models_use_responses_api(monkeypatch, model):
    captured = {}

    def fake_post_json(endpoint, payload, headers, **kwargs):
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        return {"output_text": "OK", "usage": {}}

    monkeypatch.setattr(MODULE, "post_json", fake_post_json)
    monkeypatch.setattr(
        MODULE,
        "copilot_token",
        lambda endpoint, refresh=False: "token",
    )
    args = Namespace(
        chat_endpoint="unused",
        responses_endpoint="https://example.invalid/responses",
        token_endpoint="http://127.0.0.1:4141/token",
        max_output_tokens=128,
        request_timeout=30,
        request_retries=1,
        force_chat=False,
    )

    text, _ = MODULE.invoke_model(model, "system", "prompt", args)

    assert text == "OK"
    assert captured["endpoint"] == args.responses_endpoint
    assert captured["payload"]["model"] == model


def test_custom_model_defaults_to_openai_compatible_chat(monkeypatch):
    captured = {}

    def fake_post_json(endpoint, payload, headers, **kwargs):
        captured["payload"] = payload
        return {
            "model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            "choices": [{"message": {"content": "OK"}}],
            "usage": {},
        }

    monkeypatch.setattr(MODULE, "post_json", fake_post_json)
    args = Namespace(
        chat_endpoint="http://127.0.0.1:8000/v1/chat/completions",
        responses_endpoint="unused",
        token_endpoint="unused",
        max_output_tokens=128,
        request_timeout=30,
        request_retries=1,
        force_chat=True,
    )
    model = "Qwen/Qwen3-Coder-30B-A3B-Instruct"
    text, _ = MODULE.invoke_model(model, "system", "prompt", args)

    assert text == "OK"
    assert captured["payload"]["model"] == model
    assert "reasoning_effort" not in captured["payload"]
