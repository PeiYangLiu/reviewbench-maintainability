"""Standalone Kirby scripted lane. Parent adds only an early task-ID dispatch."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shlex
import tempfile

from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, canonical_hash, component, file_hash, repo_path,
)
from reviewbench_maintainability.agentic.snapshots import git
from .common import relocate
from . import composer_test_tools as tools
from . import composer_test_tools_runtime as runtime

IMAGE_ALIAS = "rb-scripted-php84-cov"
DEPENDENCIES = "/opt/reviewbench-assets/kirby-test-tools"
ENTRY = "/opt/reviewbench-assets/kirby-composer-runtime/entry.php"
TOOLS = "/opt/reviewbench-assets/scripted-tools"
RUNTIME = "/workspace/.reviewbench/scripted"
MIRROR = RUNTIME + "/kirby-current"
DEPENDENCY_ROOT_KEY = "composer_test_tools_dependency_root"
TEST_COMMAND = (
    'php -d auto_prepend_file=/oracle-tools/php_coverage_prepend.php '
    'vendor/phpunit/phpunit/phpunit -c phpunit.dist.xml --filter "::testFromArray$" tests/Cms/ResponderTest.php'
)


def container_payload(template: str, image=IMAGE_ALIAS):
    """Extract the frozen inner command, never run host Docker templates."""
    words = shlex.split(template)
    starts = [i for i in range(len(words) - 1) if words[i:i + 2] == ["docker", "run"]]
    if len(starts) != 1:
        raise ContractError("Kirby requires one explicit frozen container invocation")
    cursor, env, cwd = starts[0] + 2, {}, "/work"
    while cursor < len(words) and words[cursor] != image:
        token = words[cursor]
        if token == "--rm":
            cursor += 1
            continue
        if token not in {"--user", "-u", "-v", "--volume", "-w", "--workdir", "-e", "--env"} or cursor + 1 >= len(words):
            raise ContractError("unhandled Kirby container flag")
        value = words[cursor + 1]
        if token in {"-w", "--workdir"}:
            cwd = value
        elif token in {"-e", "--env"}:
            key, separator, text = value.partition("=")
            if not separator or not re.fullmatch(r"[A-Za-z_]\w*", key):
                raise ContractError("invalid frozen Kirby environment")
            env[key] = text
        cursor += 2
    command = words[cursor + 1:]
    if len(command) != 3 or command[:2] not in (["bash", "-lc"], ["bash", "-c"]):
        raise ContractError("Kirby frozen payload needs one bash command")
    return command[2], env, cwd


def validate_case(task, case):
    raw, _, cwd = container_payload(case.get("setup_host_command", ""))
    if (not tools.applies_to(task, raw) or cwd != "/work"
            or case.get("environment", {}).get("docker") != IMAGE_ALIAS):
        raise ContractError("not the exact original Kirby setup")
    test, environment, workdir = container_payload(case.get("test_host_command", ""))
    if (test != TEST_COMMAND or workdir != "/work"
            or case.get("test_selectors") != ["tests/Cms/ResponderTest.php::testFromArray"]
            or case.get("outcome_regex") != r"OK \((\d+) tests?, (\d+) assertions?\)"
            or environment.get("ORACLE_TARGET") != "/work/src/Cms/Responder.php"
            or environment.get("XDEBUG_MODE") != "coverage" or case.get("session_files")):
        raise ContractError("Kirby fixed test/coverage contract changed")
    return test, environment


def validate_prepared(repository, task, case, record, payload: Path, *, image: str) -> dict:
    validate_case(task, case)
    if (record.get("status") != "prepared" or record.get("id") != task["id"]
            or record.get("repo") != task["repo"] or record.get("start_sha") != task["start_sha"]
            or record.get("config_fingerprint") != canonical_hash(case)
            or record.get("image") != image or record.get("historical_lock") is not False
            or record.get("scratch_paths") != []):
        raise ContractError("prepared Kirby identity differs from frozen task/config/image")
    inputs = {}
    for field, filename in (("manifest", "composer.json"), ("lock", "composer.lock")):
        inputs[field] = git(repository, "show", task["start_sha"] + ":" + filename)
        if hashlib.sha256(inputs[field]).hexdigest() != record.get(field + "_sha256"):
            raise ContractError("prepared Kirby original " + field + " differs from start")
    if runtime.tree_fingerprint(payload) != record.get("payload_tree_sha256"):
        raise ContractError("prepared Kirby payload changed")
    reference = record["test_tools"]
    data = runtime.validate_payload(payload, expected_sha256=reference["record_sha256"], image=image)
    prepared = tools.load_prepared_tools(payload / runtime.BUILD_INPUTS,
                                        expected_sha256=reference["record_sha256"], image=image)
    if (prepared["plan"] != tools.plan_test_tools(task, inputs["manifest"], inputs["lock"])
            or prepared["derived"]["manifest_sha256"] != reference.get("derived_manifest_sha256")
            or prepared["derived"]["lock_sha256"] != reference.get("derived_lock_sha256")):
        raise ContractError("Kirby original/derived dependency plan changed")
    probe = record.get("build_probe", {})
    if (probe.get("exit_status") != 0 or probe.get("source_unchanged") is not True
            or probe.get("phpunit_version") != prepared["derived"]["phpunit_version"].lstrip("v")):
        raise ContractError("prepared Kirby lacks a successful real PHPUnit mirror probe")
    audit_ref = record["image_audit"]
    audit_path = Path(audit_ref["path"])
    if file_hash(audit_path) != audit_ref["sha256"]:
        raise ContractError("Kirby developer image audit changed")
    audit = json.loads(audit_path.read_text())
    if (audit.get("image") != image or audit.get("no_repository_history") is not True
            or audit.get("no_reference_assets") is not True):
        raise ContractError("invalid prepared Kirby image audit")
    return data


def completion_pattern(ctx, oracle, case):
    """Kirby's full original summary fixes both test and assertion counts."""
    summaries = []
    for variant in ("start", "golden"):
        runs = oracle.get("runs", {}).get(variant, [])
        if len(runs) != 3:
            raise ContractError("incomplete frozen Kirby triple-run evidence")
        for run in runs:
            if run.get("exit_code") != 0:
                raise ContractError("unsuccessful frozen Kirby run")
            text = (ctx.legacy / "scripted" / repo_path(run["log"])).read_text()
            match = re.search(case["outcome_regex"], text)
            if not match:
                raise ContractError("frozen Kirby completion missing")
            summaries.append(match.group())
    if len(set(summaries)) != 1:
        raise ContractError("frozen Kirby test/assertion counts differ")
    return "^" + re.escape(summaries[0]) + r"\s*$"


def normalize(value):
    value = relocate(value, {"mode": "candidate", "run": "1", "task_dir": RUNTIME, "work": "/workspace"})
    value = value.replace("/oracle-tools", TOOLS).replace("/oracle-task", RUNTIME)
    value = re.sub(r"/work(?=/|$)", "/workspace", value)
    if any(prefix in value for prefix in ("{root}", "{repo}", "/data/", "/home/")):
        raise ContractError("Kirby command still contains host-only paths")
    return value


def migrate(ctx, task, oracle):
    case = json.loads((ctx.legacy / "scripted/oracle_config.json").read_text())["tasks"][str(oracle["index"])]
    raw, private_env = validate_case(task, case)
    configured = ctx.config.get("migration", {}).get(DEPENDENCY_ROOT_KEY)
    registry_path = ctx.config.get("migration", {}).get("toolchain_registry")
    if not configured or not Path(configured).is_absolute() or not registry_path:
        raise ContractError("Kirby requires its standalone prepared dependency root and pinned registry")
    registry = json.loads(Path(registry_path).read_text())
    pinned = registry.get(IMAGE_ALIAS, {})
    if pinned.get("status") != "ready":
        raise ContractError("Kirby PHP84 registry entry is not ready")
    image = pinned["image"]
    root = Path(configured) / component(task["id"])
    record = json.loads((root / "preparation.json").read_text())
    payload = root / "payload"
    validate_prepared(ctx.repo(task), task, case, record, payload, image=image)
    # No ctx.image() call: reuse the gated, pinned, executed preparation audit.
    dependency = ctx.bundle(payload)
    with tempfile.TemporaryDirectory(prefix="kirby-runtime-", dir=ctx.scratch) as temporary:
        entry_root = Path(temporary)
        runtime.write_php_runtime(entry_root / "entry.php")
        entry_asset = ctx.bundle(entry_root)
    coverage = ctx.bundle(ctx.legacy / "scripted/tools", ["php_coverage_prepend.php"], role="grader")
    assets = [
        {**dependency, "kind": "archive", "visibility": "developer", "target": DEPENDENCIES},
        {**entry_asset, "kind": "archive", "visibility": "developer", "target": str(Path(ENTRY).parent)},
        {**coverage, "kind": "archive", "visibility": "grader", "target": TOOLS},
    ]
    args = (ENTRY, DEPENDENCIES, MIRROR, image, record["test_tools"]["record_sha256"])
    build = runtime.runtime_command(*args)
    command = runtime.runtime_command(*args, normalize(raw),
                                      env={key: normalize(value) for key, value in private_env.items()})
    expected = oracle.get("pass_to_pass_tests") or [
        task["id"] + "::" + selector for selector in case["test_selectors"]]
    directory = ctx.task_directory(task, oracle, "scripted")
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="scripted",
        image=image, image_audit=record["image_audit"], workdir="/workspace",
        cpus=2, memory_mb=4096, pids_limit=256, build_scratch_mb=4096,
        command_timeout_seconds=case.get("timeout", 900),
        submodules=ctx.modules(task), assets=assets, scratch_paths=[],
        env={"TMPDIR": "/workspace/.reviewbench/tmp", "CI": "true", "COMPOSER_DISABLE_NETWORK": "1"},
        bootstrap=["mkdir -p /workspace/.reviewbench/tmp", build],
        dev_checks=["php -l " + shlex.quote("/workspace/" + repo_path(task["locality"]["path"])), build],
        grading={
            "multifile_build": True,
            "immutable_paths": sorted(set(ctx.immutable_tests(task)) | {"phpunit.dist.xml"}),
            "golden_patch": ctx.golden(task, oracle, "scripted", directory),
            "bootstrap": ["mkdir -p " + RUNTIME + "/artifacts"],
            "commands": [{"command": command, "parser": "regex", "expected_tests": expected,
                          "success_pattern": completion_pattern(ctx, oracle, case)}],
        },
    )
    recipe.validate()
    return recipe
