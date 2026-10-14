"""Budget accounting around mini's native Chat/Responses model adapters."""
from __future__ import annotations

import atexit
from dataclasses import dataclass
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Callable

from . import MINI_VERSION
from .contracts import ContractError, InfrastructureError


class BudgetLimit(RuntimeError):
    pass


@dataclass
class Budget:
    max_calls: int = 100
    max_output_tokens: int = 128000
    per_call_output_tokens: int = 8192
    wall_seconds: float = 3600
    model_calls: int = 0
    output_tokens: int = 0
    input_tokens: int = 0
    transport_retries: int = 0
    elapsed_before: float = 0
    started: float = 0

    def __post_init__(self):
        if min(self.max_calls, self.max_output_tokens, self.per_call_output_tokens, self.wall_seconds) <= 0:
            raise ContractError("episode budget limits must be positive")
        self.started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return self.elapsed_before + time.monotonic() - self.started

    @property
    def seconds_remaining(self) -> float:
        return max(0., self.wall_seconds - self.elapsed)

    def check(self) -> int:
        if self.model_calls >= self.max_calls:
            raise BudgetLimit("model_call_limit")
        if self.seconds_remaining <= 0:
            raise BudgetLimit("wall_time_limit")
        remaining = self.max_output_tokens - self.output_tokens
        if remaining <= 0:
            raise BudgetLimit("output_token_limit")
        return min(remaining, self.per_call_output_tokens)

    def charge(self, response: dict) -> None:
        usage = response.get("usage")
        if not isinstance(usage, dict):
            raise InfrastructureError("provider omitted usage; token budget is not measurable")
        output = usage.get("output_tokens", usage.get("completion_tokens"))
        inputs = usage.get("input_tokens", usage.get("prompt_tokens"))
        if any(type(v) is not int or v < 0 for v in (output, inputs)):
            raise InfrastructureError("provider returned invalid token usage")
        # OpenAI's nested reasoning detail is already included in output. Some
        # compatible gateways instead return a standalone reasoning_tokens
        # counter excluded from completion_tokens (e.g. a translated Gemini
        # response). Require total_tokens to disambiguate that shape; neither
        # silently undercount hidden output nor count inclusive reasoning twice.
        if "reasoning_tokens" in usage:
            reasoning, total = usage["reasoning_tokens"], usage.get("total_tokens")
            if any(type(v) is not int or v < 0 for v in (reasoning, total)):
                raise InfrastructureError("standalone reasoning usage needs valid total_tokens")
            if total == inputs + output + reasoning:
                output += reasoning
            elif total != inputs + output:
                raise InfrastructureError("ambiguous standalone reasoning token accounting")
        self.output_tokens += output
        self.input_tokens += inputs

    def serialize(self) -> dict:
        return {"model_calls": self.model_calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "transport_retries": self.transport_retries,
                "elapsed_seconds": round(self.elapsed, 6)}

    def restore(self, saved: dict) -> None:
        for field in ("model_calls", "output_tokens", "input_tokens", "transport_retries"):
            value = saved[field]
            if type(value) is not int or value < 0:
                raise ContractError(f"invalid saved usage {field}")
            setattr(self, field, value)
        self.elapsed_before = float(saved["elapsed_seconds"])
        self.started = time.monotonic()


def load_mini():
    # Importing upstream reads a personal .env by default. Benchmarks instead
    # get an empty isolated global-config directory and explicit run config.
    if "minisweagent" not in __import__("sys").modules:
        directory = tempfile.mkdtemp(prefix="reviewbench-mini-config-")
        atexit.register(shutil.rmtree, directory, ignore_errors=True)
        os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = directory
    os.environ["MSWEA_SILENT_STARTUP"] = "1"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    try:
        mini = importlib.import_module("minisweagent")
    except ImportError as exc:
        raise InfrastructureError("install reviewbench-maintainability[agentic] using the pinned lock") from exc
    if mini.__version__ != MINI_VERSION:
        raise InfrastructureError(f"expected mini-swe-agent {MINI_VERSION}, got {mini.__version__}")
    return mini


def sanitize_config(value: Any) -> Any:
    """Only configuration is scrubbed; model conversations are preserved."""
    if isinstance(value, dict):
        return {k: ("<environment credential>" if k.lower() in {
            "api_key", "authorization", "api-key", "x-api-key", "password", "access_token"
        } else sanitize_config(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_config(v) for v in value]
    return value


def engine_identity() -> dict:
    from .contracts import canonical_hash, file_hash
    import sys
    packages = {}
    for name in ("mini-swe-agent", "litellm", "openai", "pydantic", "tenacity", "jinja2"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    identity = {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "packages": packages,
        "code": {p.name: file_hash(p) for p in sorted(Path(__file__).parent.glob("*.py"))},
    }
    return {"fingerprint": canonical_hash(identity), **identity}


OBSERVATION_TEMPLATE = """{{ {
    "exit_code": output.returncode,
    "stdout": output.get("stdout", output.output),
    "stderr": output.get("stderr", ""),
    "error": output.exception_info,
    "timed_out": output.get("extra", {}).get("timed_out", false)
} | tojson }}"""

FORMAT_ERROR_TEMPLATE = (
    "Tool call error: {{ error }}\n"
    "Use the native bash tool with a nonempty string command. "
    "Do not put a tool call in prose or in a reasoning block."
)


class BudgetedModel:
    def __init__(self, inner, budget: Budget, *, expected_models: list[str],
                 protocol: str = "chat", request_timeout: float = 120, retries: int = 2):
        self.inner, self.budget = inner, budget
        self.config = inner.config
        self.expected_models = expected_models
        self.protocol = protocol
        self.request_timeout = request_timeout
        self.retries = retries
        self.last_response: dict | None = None
        self.before_request: Callable[[], None] | None = None
        # Disable upstream's unbounded-for-our-budget retry schedule. This
        # wrapper owns every transport attempt and accounts it explicitly.
        if hasattr(inner, "abort_exceptions"):
            inner.abort_exceptions = [Exception]

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def query(self, messages: list[dict]) -> dict:
        load_mini()
        from minisweagent.exceptions import FormatError
        for attempt in range(self.retries + 1):
            limit = self.budget.check()
            kwargs = {"timeout": min(self.request_timeout, self.budget.seconds_remaining)}
            kwargs["max_output_tokens" if self.protocol == "responses" else "max_tokens"] = limit
            self.budget.model_calls += 1
            if self.before_request is not None:
                self.before_request()
            try:
                message = self.inner.query(messages, **kwargs)
            except FormatError as exc:
                raw = exc.messages[0].get("extra", {}).get("response")
                if isinstance(raw, dict):
                    self.last_response = raw
                    self._validate_and_charge(raw)
                else:
                    raise InfrastructureError("upstream omitted response/usage on a format error") from exc
                raise
            except Exception as exc:
                if type(exc).__name__ == "ContextWindowExceededError":
                    raise BudgetLimit("context_limit") from exc
                code = getattr(exc, "status_code", None)
                retryable = code in {408, 429, 500, 502, 503, 504} or isinstance(exc, (TimeoutError, ConnectionError))
                if not retryable or attempt >= self.retries:
                    raise InfrastructureError(f"model transport: {type(exc).__name__}: {exc}") from exc
                self.budget.transport_retries += 1
                time.sleep(min(2 ** attempt, self.budget.seconds_remaining))
                continue
            raw = message if message.get("object") == "response" else message.get("extra", {}).get("response")
            if not isinstance(raw, dict):
                raise InfrastructureError("native model adapter omitted raw response")
            self.last_response = raw
            self._validate_and_charge(raw)
            actions = message.get("extra", {}).get("actions", [])
            if any(not isinstance(a.get("command"), str) or not a["command"].strip() or "\0" in a["command"]
                   or not isinstance(a.get("tool_call_id"), str) or not a["tool_call_id"]
                   for a in actions):
                raise FormatError({"role": "user", "content": "Malformed native bash tool call.",
                                   "extra": {"response": raw, "cost": message.get("extra", {}).get("cost", 0)}})
            ids = [a["tool_call_id"] for a in actions]
            if len(ids) != len(set(ids)):
                raise FormatError({"role": "user", "content": "Duplicate tool_call_id in one response.",
                                   "extra": {"response": raw, "cost": message.get("extra", {}).get("cost", 0)}})
            return message
        raise AssertionError("unreachable")

    def _validate_and_charge(self, raw: dict) -> None:
        self.budget.charge(raw)
        if raw.get("model") not in self.expected_models:
            raise InfrastructureError(f"model identity mismatch: returned {raw.get('model')!r}")

    def serialize(self) -> dict:
        result = self.inner.serialize()
        result = sanitize_config(result)
        result["budget"] = self.budget.serialize()
        return result


def create_model(config: dict, budget: Budget) -> BudgetedModel:
    load_mini()
    protocol = config.get("protocol", "chat")
    if protocol not in {"chat", "responses"}:
        raise ContractError("only native chat and responses model protocols are supported")
    if protocol == "responses":
        from minisweagent.models.litellm_response_model import LitellmResponseModel as ModelClass
    else:
        from minisweagent.models.litellm_model import LitellmModel as ModelClass
    kwargs = dict(config.get("model_kwargs", {}))
    forbidden = {"tools", "messages", "input", "max_tokens", "max_output_tokens", "max_completion_tokens",
                 "timeout", "request_timeout", "stream",
                 "api_key", "headers", "extra_headers", "max_retries", "num_retries"}
    if forbidden & kwargs.keys():
        raise ContractError(f"model_kwargs override protocol/budget fields: {sorted(forbidden & kwargs.keys())}")
    kwargs["drop_params"] = False
    # Disable the SDK/LiteLLM layer's own retries too: otherwise one budgeted
    # query can secretly become three HTTP attempts, each with a fresh timeout.
    kwargs["max_retries"] = 0
    kwargs["num_retries"] = 0
    if config.get("api_base"):
        kwargs["api_base"] = config["api_base"]
    key_env = config.get("api_key_env")
    if key_env:
        if not os.environ.get(key_env):
            raise InfrastructureError(f"missing configured credential environment variable: {key_env}")
        kwargs["api_key"] = os.environ[key_env]
    elif config.get("api_base"):
        # Explicitly unauthenticated local gateways/vLLM; never borrow a user's
        # unrelated OPENAI_API_KEY for such an endpoint.
        kwargs["api_key"] = "reviewbench-local"
    name = config["model_name"]
    inner = ModelClass(
        model_name=name, model_kwargs=kwargs, cost_tracking="ignore_errors",
        observation_template=OBSERVATION_TEMPLATE, format_error_template=FORMAT_ERROR_TEMPLATE,
    )
    return BudgetedModel(
        inner, budget, expected_models=config.get("expected_response_models", [name.split("/", 1)[-1]]),
        protocol=protocol, request_timeout=config.get("request_timeout_seconds", 120),
        retries=config.get("transport_retries", 2),
    )
