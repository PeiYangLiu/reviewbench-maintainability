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
        captured["payload"] = payload
        return {
            "model": "gpt-5.5-2026-04-23",
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
    assert captured["payload"]["model"] == "gpt-5.5"


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
        return {"model": model, "output_text": "OK", "usage": {}}

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


def test_responses_model_requires_returned_identity(monkeypatch):
    monkeypatch.setattr(
        MODULE,
        "copilot_token",
        lambda endpoint, refresh=False: "token",
    )
    monkeypatch.setattr(
        MODULE,
        "post_json",
        lambda *args, **kwargs: {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            "usage": {},
        },
    )
    args = Namespace(
        force_chat=False,
        responses_endpoint="https://example.invalid/responses",
        token_endpoint="http://127.0.0.1:4141/token",
        max_output_tokens=100,
        request_timeout=30,
        request_retries=0,
    )

    with pytest.raises(RuntimeError, match="gateway returned <missing>"):
        MODULE.invoke_model("gpt-5.6-sol", "system", "prompt", args)


def test_gpt_55_requires_pinned_versioned_response_identity(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        MODULE,
        "copilot_token",
        lambda endpoint, refresh=False: "token",
    )
    def fake_post_json(endpoint, payload, headers, **kwargs):
        captured["payload"] = payload
        return {
            "model": "gpt-5.5-2026-04-23",
            "output_text": "OK",
            "usage": {},
        }

    monkeypatch.setattr(MODULE, "post_json", fake_post_json)
    args = Namespace(
        force_chat=False,
        responses_endpoint="https://example.invalid/responses",
        token_endpoint="http://127.0.0.1:4141/token",
        max_output_tokens=100,
        request_timeout=30,
        request_retries=0,
    )

    text, _, data = MODULE.invoke_model_envelope(
        "gpt-5.5",
        "system",
        "prompt",
        args,
    )

    assert text == "OK"
    assert data["model"] == "gpt-5.5-2026-04-23"
    assert captured["payload"]["model"] == "gpt-5.5"


def test_gpt_55_accepts_its_canonical_and_dated_identities(monkeypatch):
    """The pin refuses a different model, not a different snapshot label.

    `gpt-5.5-2026-04-23` is no longer served anywhere: a gateway answers with
    the canonical name and the direct chat route with the current snapshot.
    Rejecting those would refuse gpt-5.5 itself, so all three are accepted and
    the serving deployment is recorded per round instead.
    """
    for returned in ("gpt-5.5", "gpt-5.5-2026-04-23", "gpt-5.5-2026-04-24"):
        monkeypatch.setattr(
            MODULE,
            "post_json",
            lambda *args, _m=returned, **kwargs: {
                "model": _m,
                "output_text": "OK",
                "usage": {},
            },
        )
        args = Namespace(
            force_chat=False,
            responses_endpoint="http://127.0.0.1:4000/v1/responses",
            token_endpoint="",
            max_output_tokens=100,
            request_timeout=30,
            request_retries=0,
        )
        text, _ = MODULE.invoke_model("gpt-5.5", "system", "prompt", args)
        assert text == "OK", returned


def test_gpt_55_still_rejects_a_different_model(monkeypatch):
    monkeypatch.setattr(
        MODULE,
        "post_json",
        lambda *args, **kwargs: {
            "model": "gpt-5.4",
            "output_text": "OK",
            "usage": {},
        },
    )
    args = Namespace(
        force_chat=False,
        responses_endpoint="http://127.0.0.1:4000/v1/responses",
        token_endpoint="",
        max_output_tokens=100,
        request_timeout=30,
        request_retries=0,
    )

    with pytest.raises(RuntimeError, match="solver model mismatch"):
        MODULE.invoke_model("gpt-5.5", "system", "prompt", args)


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


# ---------------------------------------------------------------------------
# Context-window overflow is the model's limit, not an infrastructure fault
# ---------------------------------------------------------------------------


def test_context_overflow_is_not_retried_as_transient():
    # vLLM rejects a prompt longer than the model's context with HTTP 400.
    # Retrying cannot help, and treating it as transient would let a real
    # capability limit be re-labelled as flaky infrastructure.
    assert 400 not in MODULE.RETRYABLE_HTTP_CODES


def test_context_overflow_surfaces_as_a_hard_error(monkeypatch):
    import urllib.error
    import io

    calls = []

    def overflowing(request, timeout=None):
        calls.append(request)
        raise urllib.error.HTTPError(
            "http://x", 400, "Bad Request", {},
            io.BytesIO(
                b'{"error":{"message":"This model\'s maximum context length '
                b'is 40960 tokens, however you requested 41500 tokens"}}'
            ),
        )

    monkeypatch.setattr(MODULE.urllib.request, "urlopen", overflowing)
    with pytest.raises(RuntimeError, match="HTTP 400"):
        MODULE.post_json("http://x", {"model": "m"}, {}, timeout=5, retries=4)
    # surfaced on the first attempt rather than burning the retry budget
    assert len(calls) == 1


def test_local_gateway_needs_no_minted_token(monkeypatch):
    """An empty token endpoint must not make the run depend on a dead service."""
    captured = {}

    def refuse_token(endpoint, refresh=False):
        raise AssertionError("must not mint a token without a token endpoint")

    def fake_post_json(endpoint, payload, headers, **kwargs):
        captured["headers"] = headers
        return {"model": "gpt-5.6-sol", "output_text": "OK", "usage": {}}

    monkeypatch.setattr(MODULE, "copilot_token", refuse_token)
    monkeypatch.setattr(MODULE, "post_json", fake_post_json)
    args = Namespace(
        force_chat=False,
        chat_endpoint="unused",
        responses_endpoint="http://127.0.0.1:4000/v1/responses",
        token_endpoint="",
        max_output_tokens=128,
        request_timeout=30,
        request_retries=1,
    )

    text, _ = MODULE.invoke_model("gpt-5.6-sol", "system", "prompt", args)

    assert text == "OK"
    assert "Authorization" not in captured["headers"]


def test_conversation_replaces_the_single_user_message(monkeypatch):
    captured = {}

    def fake_post_json(endpoint, payload, headers, **kwargs):
        captured["payload"] = payload
        return {
            "model": "claude-opus-5",
            "choices": [{"message": {"content": "OK"}}],
            "usage": {},
        }

    monkeypatch.setattr(MODULE, "post_json", fake_post_json)
    args = Namespace(
        force_chat=True,
        chat_endpoint="http://127.0.0.1:4000/v1/chat/completions",
        responses_endpoint="unused",
        token_endpoint="",
        max_output_tokens=128,
        request_timeout=30,
        request_retries=1,
    )

    MODULE.invoke_model_envelope(
        "claude-opus-5",
        "system",
        "ignored",
        args,
        conversation=[
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": '{"action":"read"}'},
            {"role": "user", "content": '{"lines":"1: x"}'},
        ],
    )

    roles = [message["role"] for message in captured["payload"]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert captured["payload"]["messages"][0]["content"] == "system"
    assert all(
        message["content"] != "ignored"
        for message in captured["payload"]["messages"]
    )


def test_conversation_rides_the_responses_input_array(monkeypatch):
    captured = {}

    def fake_post_json(endpoint, payload, headers, **kwargs):
        captured["payload"] = payload
        return {"model": "gpt-5.6-sol", "output_text": "OK", "usage": {}}

    monkeypatch.setattr(MODULE, "post_json", fake_post_json)
    args = Namespace(
        force_chat=False,
        chat_endpoint="unused",
        responses_endpoint="http://127.0.0.1:4000/v1/responses",
        token_endpoint="",
        max_output_tokens=128,
        request_timeout=30,
        request_retries=1,
    )
    conversation = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": '{"action":"read"}'},
    ]

    MODULE.invoke_model_envelope(
        "gpt-5.6-sol", "system", "ignored", args, conversation=conversation
    )

    assert captured["payload"]["input"] == conversation
    assert captured["payload"]["instructions"] == "system"


def test_serving_headers_are_recorded_with_the_response():
    """The echoed model name cannot identify a deployment behind a gateway."""
    class FakeResponse:
        headers = {
            "x-llmgw-backend": "aoai",
            "x-llmgw-cluster": "ConversationhubSouthCentralUS",
            "x-llmgw-upstream-model": "gpt-5.6-sol",
            "x-llmgw-protocol": "responses",
        }

        def read(self):
            return b'{"model": "gpt-5.6-sol"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import urllib.request as urlreq

    original = urlreq.urlopen
    urlreq.urlopen = lambda *a, **k: FakeResponse()
    try:
        data = MODULE.post_json("http://127.0.0.1:4000/v1/responses", {}, {}, retries=1)
    finally:
        urlreq.urlopen = original

    assert data["served_by"]["x-llmgw-backend"] == "aoai"
    assert data["served_by"]["x-llmgw-upstream-model"] == "gpt-5.6-sol"
