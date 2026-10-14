"""ReviewBench hooks on mini's actual DefaultAgent loop (not a one-shot runner)."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import time
from typing import Callable

from . import PROTOCOL
from .contracts import (
    ContractError, EpisodeResult, InfrastructureError, PublicTaskSpec, RuntimeRecipe,
    atomic_json, canonical_hash, file_hash,
)
from .environment import ReviewBenchDockerEnvironment
from .models import Budget, BudgetLimit, create_model, engine_identity, load_mini
from .snapshots import collect_patch, copy_source, extract_start, initialize_git, tree_hash

load_mini()
from minisweagent.agents.default import DefaultAgent  # noqa: E402
from minisweagent.exceptions import Submitted  # noqa: E402


SYSTEM_TEMPLATE = """You are a software engineer improving repository maintainability.
Resolve the reviewer's specific concern with a complete, focused implementation.
Use the native bash tool to inspect, edit, build, and test actual code.
You may change related source files, tests, and necessary build configuration.
The review location is a starting point, not the only editable file.
Preserve existing behavior unless the concern explicitly requires a change.
Use actual command output to decide what to do next. Failed tests are feedback:
inspect the error, revise the implementation, and test again as often as needed.
Each command uses a fresh shell; filesystem changes persist. Use explicit cd
when needed. Do not commit credentials or temporary build/debug artifacts.
Finish by calling bash with exactly: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
The host will collect the complete filesystem patch, including new files.
Final scoring is independent and its private tests and scores are not available."""

INSTANCE_TEMPLATE = "{{task}}"


class ReviewBenchAgent(DefaultAgent):
    """Override persistence/action hooks; run()/step()/query() remain upstream."""

    def __init__(self, model, env, *, bindings: dict, restore: dict | None = None, **kwargs):
        super().__init__(model, env, **kwargs)
        self.bindings = bindings
        self.restored = restore
        self.inflight = False
        self.pending_model_request = False
        # Preserve upstream run/step/query, but durably charge each HTTP attempt
        # before it is sent. A controller crash must not silently resample that
        # query or reset the already-issued request's budget on --resume.
        model.before_request = self._checkpoint_model_request
        if restore:
            if (restore.get("bindings") != bindings or restore.get("inflight")
                    or restore.get("pending_model_request")):
                raise InfrastructureError("checkpoint is stale or has an uncertain request/action")
            if restore.get("workspace_sha256") != tree_hash(env.workspace):
                raise InfrastructureError("workspace differs from the completed checkpoint")
            model.budget.restore(restore["budget"])
            self.n_calls = restore["info"]["model_stats"]["api_calls"]
            self.cost = restore["info"]["model_stats"]["instance_cost"]
            self.n_consecutive_format_errors = restore.get("consecutive_format_errors", 0)
            self._start_time = time.time() - model.budget.elapsed
            saved_env = restore["info"]["environment"]
            env.records = saved_env.get("commands", [])
            env.network_id = saved_env.get("network_id")
            env.sidecar_ids = saved_env.get("sidecar_ids", [])

    def add_messages(self, *messages):
        if self.restored is not None:
            # DefaultAgent.run initializes exactly these two messages. Substitute
            # a verified completed history without copying its run loop.
            initial = self.restored["messages"][:2]
            if list(messages) != initial:
                raise InfrastructureError("initial prompt differs from saved episode")
            self.messages.extend(self.restored["messages"])
            self.restored = None
            return self.messages
        added = super().add_messages(*messages)
        # DefaultAgent records either the response or a format/transport error
        # here. The next durable save captures both consumption and history;
        # a crash before that save still leaves the on-disk pending flag set.
        if messages:
            self.pending_model_request = False
        return added

    def _checkpoint_model_request(self) -> None:
        self.pending_model_request = True
        self.save(self.config.output_path)

    def execute_actions(self, message: dict) -> list[dict]:
        self.inflight = True
        self.save(self.config.output_path)
        outputs = []
        submitted = False
        for action in message.get("extra", {}).get("actions", []):
            self.env.deadline = time.monotonic() + self.model.budget.seconds_remaining
            output = self.env.execute(action)
            outputs.append(output)
            if output.get("submitted"):
                submitted = True
                break
        observations = self.model.format_observation_messages(message, outputs, self.get_template_vars())
        result = self.add_messages(*observations)
        self.inflight = False
        if submitted:
            # Submission stdout is not a trusted patch. run_episode extracts the
            # actual complete workspace after all solver processes have stopped.
            raise Submitted({"role": "exit", "content": "Submitted",
                             "extra": {"exit_status": "Submitted", "submission": ""}})
        return result

    def save(self, path: Path | None, *extra_dicts) -> dict:
        data = self.serialize(*extra_dicts)
        data["bindings"] = self.bindings
        data["budget"] = self.model.budget.serialize()
        data["inflight"] = self.inflight
        data["pending_model_request"] = self.pending_model_request
        data["consecutive_format_errors"] = self.n_consecutive_format_errors
        # An uncertain request/action is rejected before the workspace hash is
        # inspected on either resume route. Keep its journal durable without
        # walking a potentially multi-GB tree that cannot authorize a resume.
        # Clean checkpoints still read and validate every current source byte.
        data["workspace_sha256"] = (
            None if self.inflight or self.pending_model_request
            else tree_hash(self.env.workspace)
        )
        if self.model.last_response is not None:
            data["last_raw_response"] = self.model.last_response
        if path:
            atomic_json(path, data)
        return data


def _existing_result(path: Path, bindings: dict) -> dict | None:
    if not path.is_file():
        return None
    result = json.loads(path.read_text())
    for key, value in bindings.items():
        if result.get(key) != value:
            raise ContractError(f"existing episode has different {key}; use a new run directory")
    patch = result.get("patch_file")
    if result.get("apply_ok") and (not patch or not Path(patch).is_file()
                                  or file_hash(Path(patch)) != result.get("patch_sha256")):
        raise ContractError("existing candidate patch is missing or changed")
    return result


def run_episode(task: dict, recipe: RuntimeRecipe, config: dict, repository: Path,
                output: Path, asset_root: Path, *, resume: bool = False,
                retry_infra: bool = False, model_factory: Callable | None = None,
                environment_factory: Callable = ReviewBenchDockerEnvironment) -> dict:
    public = PublicTaskSpec.from_task(task)
    recipe.bind(public)
    model_name = config["model"]["id"]
    bindings = {
        "id": public.id, "model": model_name, "protocol": PROTOCOL,
        "task_fingerprint": canonical_hash(task), "runtime_fingerprint": recipe.fingerprint,
        "config_fingerprint": canonical_hash(config), "start_sha": public.start_sha,
        "engine_fingerprint": engine_identity()["fingerprint"],
    }
    output = output.resolve()
    result_path = output / "result.json"
    existing = _existing_result(result_path, bindings)
    if existing:
        latest = output / "latest-attempt.json"
        if latest.is_file():
            pointer = json.loads(latest.read_text())
            target = Path(pointer["result"])
            if not target.resolve().is_relative_to(output) or file_hash(target) != pointer["sha256"]:
                raise ContractError("invalid latest infrastructure attempt pointer")
            most_recent = _existing_result(target, bindings)
            if most_recent is not None:
                existing = most_recent
        if retry_infra and existing["status"] == "infra_error":
            # Preserve every attempt; never overwrite a completed model failure
            # or re-sample until a successful candidate appears.
            attempt = 1
            while (output / f"retry-{attempt:03d}").exists():
                attempt += 1
            result = run_episode(task, recipe, config, repository, output / f"retry-{attempt:03d}",
                                 asset_root, model_factory=model_factory,
                                 environment_factory=environment_factory)
            atomic_json(output / "latest-attempt.json", {
                "result": str(output / f"retry-{attempt:03d}" / "result.json"),
                "sha256": file_hash(output / f"retry-{attempt:03d}" / "result.json"),
            })
            return result
        if resume:
            return existing
        raise ContractError("episode already exists; use --resume or a new run directory")
    output.mkdir(parents=True, exist_ok=True)
    base, workspace = output / "start", output / "workspace"
    trace_path = output / "trajectory.json"
    restore = json.loads(trace_path.read_text()) if resume and trace_path.is_file() else None
    result = EpisodeResult(**bindings)
    result.trace_file = str(trace_path)
    env = None
    budget = None
    try:
        if restore:
            if (restore.get("bindings") != bindings or restore.get("inflight")
                    or restore.get("pending_model_request")):
                raise InfrastructureError("in-progress state is stale or request/action completion is uncertain")
            if not base.is_dir() or not workspace.is_dir():
                raise InfrastructureError("saved source snapshot/workspace is missing")
            if restore.get("workspace_sha256") != tree_hash(workspace):
                raise InfrastructureError("workspace changed since checkpoint")
            snapshot = json.loads((output / "snapshot.json").read_text())
            if tree_hash(base) != snapshot["tree_sha256"]:
                raise InfrastructureError("trusted start snapshot changed")
            cid = restore["info"]["environment"]["container_id"]
            env = environment_factory(recipe, workspace, output / "commands", asset_root, container_id=cid)
        else:
            if base.exists() or workspace.exists():
                raise InfrastructureError("unfinished preparation needs an explicit infrastructure retry")
            original_hash = extract_start(repository, public.start_sha, base)
            atomic_json(output / "snapshot.json", {"start_sha": public.start_sha, "tree_sha256": original_hash})
            copy_source(base, workspace)
            initialize_git(workspace)
            env = environment_factory(recipe, workspace, output / "commands", asset_root,
                                      max_output_chars=config.get("observation", {}).get("max_chars", 10000),
                                      lifetime_seconds=int(config.get("agent", {}).get("wall_time_limit_seconds", 3600))
                                      + recipe.command_timeout_seconds * len(recipe.bootstrap) + 300)
            for command in recipe.bootstrap:
                observation = env.execute({"command": command})
                if observation["returncode"] != 0:
                    raise InfrastructureError(f"developer bootstrap failed: {observation['output'][-1500:]}")
            # Toolchain bootstrap may create ignored build files, but cannot
            # rewrite original production/test files behind the agent's back.
            from .snapshots import tree_files
            before_files, after_files = tree_files(base), tree_files(workspace)
            for name, original in before_files.items():
                current = after_files.get(name)
                if current is None or original.is_symlink() != current.is_symlink():
                    raise InfrastructureError(f"bootstrap changed original source: {name}")
                if original.is_symlink():
                    import os
                    same = os.readlink(original) == os.readlink(current)
                else:
                    same = file_hash(original) == file_hash(current)
                if not same:
                    raise InfrastructureError(f"bootstrap changed original source: {name}")
        limits = config.get("budget", {})
        settings = config.get("agent", {})
        budget = Budget(
            max_calls=settings.get("step_limit", 100),
            max_output_tokens=limits.get("total_output_tokens", 128000),
            per_call_output_tokens=limits.get("per_call_output_tokens", 8192),
            wall_seconds=settings.get("wall_time_limit_seconds", 3600),
        )
        model = model_factory(budget) if model_factory else create_model(config["model"], budget)
        agent = ReviewBenchAgent(
            model, env, bindings=bindings, restore=restore,
            system_template=SYSTEM_TEMPLATE, instance_template=INSTANCE_TEMPLATE,
            step_limit=budget.max_calls, cost_limit=0,
            wall_time_limit_seconds=int(budget.wall_seconds),
            max_consecutive_format_errors=settings.get("max_consecutive_format_errors", 3),
            output_path=trace_path,
        )
        try:
            if restore and restore["messages"][-1].get("role") == "exit":
                ending = restore["messages"][-1].get("extra", {})
            else:
                prompt = public.prompt(recipe.dev_checks, workdir=recipe.workdir)
                if recipe.submodules:
                    prompt += "\nPinned submodule checkout layout:\n" + "\n".join(
                        f"- {m['path']} @ {m['commit']}: "
                        + ("optional checkout component, not materialized" if m.get("omit")
                           else "source materialized at the pinned commit")
                        for m in recipe.submodules
                    )
                ending = agent.run(prompt)
            result.stop_reason = {
                "LimitsExceeded": "model_call_limit", "TimeExceeded": "wall_time_limit",
                "RepeatedFormatError": "format_error_limit",
            }.get(ending.get("exit_status"), ending.get("exit_status", "unknown"))
        except BudgetLimit as exc:
            result.stop_reason = str(exc)
        # Stop all background processes before reading the final working tree.
        env.close()
        collected = collect_patch(base, workspace, output / "candidate.patch")
        for field, value in collected.items():
            setattr(result, field, value)
        result.status = "generated" if result.apply_ok else "generation_failed"
    except Exception as exc:
        result.status = "infra_error"
        result.stop_reason = "infrastructure_error"
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        if env:
            env.close()
        if budget:
            result.usage = budget.serialize()
    record = asdict(result)
    if trace_path.is_file():
        record["trace_sha256"] = file_hash(trace_path)
    atomic_json(result_path, record)
    return record
