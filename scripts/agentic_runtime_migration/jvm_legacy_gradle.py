"""Whole-candidate Gradle execution for two byte-bound original legacy fixtures."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile

from reviewbench_maintainability.agentic.assets import asset_descriptor
from reviewbench_maintainability.agentic.contracts import (
    ContractError, RuntimeRecipe, atomic_json, canonical_hash, file_hash,
)

MIRROR = "/opt/reviewbench-assets/legacy-gradle-mirror"
DOWNLOADS = "/opt/reviewbench-assets/legacy-gradle-downloads"
PUBLIC = "/opt/reviewbench-assets/legacy-gradle-public"
PRIVATE = "/opt/reviewbench-assets/legacy-gradle-grader"
CACHE = "/opt/reviewbench-cache/gradle"
HOME = "/workspace/.reviewbench/legacy-gradle-home"
RESULT = ".oracle/reviewbench-results.json"
INIT_SHA256 = "d1e1ffee7439e1144bcf571918be80ba5d6798ff9b530466056089229e501f65"
JACOCO_SHA256 = "47e700ccb0fdb9e27c5241353f8161938f4e53c3561dd35e063c5fe88dc3349b"
CASES = {
    "github__SkriptLang__Skript__pull_7136__comment_1835154932__complexity": {
        "index": "17", "gradle": "8.7",
        "junit_id": "oracle.ExprShooterBehaviorOracleTest#settingShooterUpdatesEveryPublicProjectile",
        "fixture_sha256": "f1f72596d85b795e591ab10115d524354ea5173199f4d50ba15c2eb314f171be",
        "golden_sha256": "b4f5176799e53c78cf8090b59190f33e4138549df897203068e39117c4e7de85",
        "command": "rm -rf build/test-results build/reports/tests build/tmp/test; bash ./gradlew --no-daemon --console=plain cleanTest testClasses oracleSessionTest -I /oracle-cache/oracle.init.gradle",
        "fresh_private_compile": False,
    },
    "github__reposense__RepoSense__pull_2078__comment_1472205280__duplication": {
        "index": "41", "gradle": "7.6.1",
        "junit_id": "oracle.RepoConfigurationBehaviorOracleTest#csvParsingPreservesPublicConfigurationValues()",
        "fixture_sha256": "a32290dd2f3b1806dbdeb2aab1afd8cf9361fd0ade714cbe5c2ad4f6057ca1ef",
        "golden_sha256": "d8d514ec33803041d588bb29aab98d4494ec0ea23921619e6eb6b7e0354a08d5",
        "command": "bash ./gradlew --no-daemon --console=plain test --tests 'oracle.RepoConfigurationBehaviorOracleTest' -I /oracle-cache/oracle.init.gradle --rerun-tasks -x compileTestJava",
        "fresh_private_compile": True,
    },
}


def checked(reference):
    path = Path(reference["path"])
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ContractError("original legacy Gradle input must be a regular absolute file")
    if file_hash(path) != reference["sha256"]:
        raise ContractError("original legacy Gradle input bytes changed")
    return path


def load_profile(task, oracle, reference):
    profile = json.loads(checked(reference).read_text())
    fixed = CASES.get(task["id"])
    if fixed is None or profile.get("schema_version") != "reviewbench-original-legacy-gradle-profile/v1":
        raise ContractError("unknown original legacy Gradle profile")
    case = profile["case"]
    if (profile.get("full_scope") != 402 or case["id"] != task["id"]
            or case["task_sha256"] != canonical_hash(task)
            or case["oracle_sha256"] != canonical_hash(oracle)
            or oracle.get("command") != fixed["command"]
            or case["original_command"] != fixed["command"]
            or oracle.get("pass_to_pass") != 1 or oracle.get("pass_to_pass_tests") != [1] * 6
            or case["junit_id"] != fixed["junit_id"]
            or case["index"] != fixed["index"] or case["gradle"] != fixed["gradle"]):
        raise ContractError("original legacy Gradle task/command/test binding changed")
    fixture, golden = checked(case["fixture"]), checked(case["golden_patch"])
    if (file_hash(fixture) != fixed["fixture_sha256"] or file_hash(golden) != fixed["golden_sha256"]
            or oracle["source_only_patch"]["sha256"] != fixed["golden_sha256"]
            or file_hash(checked(profile["original_init"])) != INIT_SHA256
            or file_hash(checked(profile["jacoco_agent"])) != JACOCO_SHA256):
        raise ContractError("original legacy Gradle fixture/reference/instrumentation changed")
    if fixture.read_text().count("@Test") != 1:
        raise ContractError("original legacy Gradle fixture test count changed")
    expected_runs = [(variant, n) for variant in ("start", "golden") for n in (1, 2, 3)]
    if [(r["variant"], r["repetition"]) for r in case["original_logs"]] != expected_runs:
        raise ContractError("original legacy Gradle requires all six original logs")
    for row in case["original_logs"]:
        text = checked(row["retained"]).read_text()
        if (row["original_exit_code"] != 0 or row["original_test_count"] != 1
                or "BUILD SUCCESSFUL" not in text or "BUILD FAILED" in text):
            raise ContractError("original legacy Gradle retained execution was not successful")
    image = profile["image"]
    if (image["original_environment"] != oracle["environment"]["container"]
            or image["image"] != "sha256:afbdc05084062f82f48ba381961879206c52d6974b43fd4de378524b31857de1"):
        raise ContractError("original legacy Gradle JDK17 image changed")
    checked(image["image_audit"])
    mirror, downloads = profile["mirror"], profile["downloads"]
    if (mirror["full_tree_sha256"] != "1dd6ee41cc10766c83c9cebb93813abc1b7ed1c27a0f959545648c9e4f1845d2"
            or mirror["candidate_implementation_classes_present"] is not False
            or downloads["id"] != task["id"] or downloads["original_gradle_version"] != fixed["gradle"]
            or downloads["project_outputs_included"] is not False
            or downloads["prior_test_results_included"] is not False):
        raise ContractError("original legacy Gradle public dependency binding changed")
    for asset in (mirror["asset"], downloads["asset"]):
        checked(asset)
    return profile


def public_init():
    """The same first-position pinned mirror policy, with no private fixture."""
    return """def addOriginalMirror = { handler ->
    def local = handler.maven { repository ->
        repository.name = 'oracleLocalMirror'
        repository.url = new File('%s').toURI().toString()
    }
    handler.remove(local)
    handler.addFirst(local)
}
settingsEvaluated { settings ->
    if (settings.pluginManagement.repositories.isEmpty()) {
        settings.pluginManagement.repositories.gradlePluginPortal()
    }
    addOriginalMirror(settings.pluginManagement.repositories)
    addOriginalMirror(settings.buildscript.repositories)
}
allprojects { project ->
    addOriginalMirror(project.buildscript.repositories)
    addOriginalMirror(project.repositories)
}
""" % MIRROR


def migrate(ctx, task, oracle, reference):
    profile = load_profile(task, oracle, reference)
    case, fixed = profile["case"], CASES[task["id"]]
    mirror, downloads = profile["mirror"]["asset"], profile["downloads"]["asset"]
    for asset in (mirror, downloads):
        asset_descriptor(asset, ctx.output)
    with tempfile.TemporaryDirectory(prefix="legacy-gradle-public-", dir=ctx.scratch) as temporary:
        root = Path(temporary)
        (root / "mirror.init.gradle").write_text(public_init())
        public_asset = ctx.bundle(root, role="toolchain")
    with tempfile.TemporaryDirectory(prefix="legacy-gradle-private-", dir=ctx.scratch) as temporary:
        root = Path(temporary)
        for name, source in (
            ("runner.py", Path(__file__).with_name("jvm_legacy_gradle_entry.py")),
            ("reports.py", Path(__file__).with_name("jvm_oracle_entry.py")),
            ("oracle.init.gradle", checked(profile["original_init"])),
            ("jacocoagent.jar", checked(profile["jacoco_agent"])),
            ("original.java", checked(case["fixture"])),
        ):
            shutil.copy2(source, root / name)
        atomic_json(root / "spec.json", {
            "schema_version": "reviewbench-original-legacy-gradle-execution/v1",
            "id": task["id"], "original_command": fixed["command"],
            "original_fixture": "original.java", "fixture_sha256": fixed["fixture_sha256"],
            "source_relative_path": case["source_relative_path"],
            "expected_tests": [fixed["junit_id"]], "original_expected_count": 1,
            "original_init_sha256": INIT_SHA256, "jacoco_agent_sha256": JACOCO_SHA256,
            "mirror": MIRROR, "fresh_private_compile": fixed["fresh_private_compile"],
        })
        private_asset = ctx.bundle(root, role="grader")
    source_names = [path for path in ctx.immutable_tests(task)]
    if case["source_relative_path"] not in source_names:
        source_names.append(case["source_relative_path"])
    marker = CACHE + "/.reviewbench-seed-" + downloads["sha256"]
    recipe = RuntimeRecipe(
        task_id=task["id"], repo=task["repo"], start_sha=task["start_sha"], lane="jvm",
        image=profile["image"]["image"], image_audit=profile["image"]["image_audit"],
        cpus=4, memory_mb=6144, build_scratch_mb=8192, command_timeout_seconds=1800,
        submodules=ctx.modules(task), cache_mounts={"gradle": CACHE},
        assets=[
            {**mirror, "kind": "archive", "visibility": "developer", "target": MIRROR},
            {**downloads, "kind": "archive", "visibility": "developer", "target": DOWNLOADS},
            {**public_asset, "kind": "archive", "visibility": "developer", "target": PUBLIC},
            {**private_asset, "kind": "archive", "visibility": "grader", "target": PRIVATE},
        ],
        env={"HOME": HOME, "TMPDIR": "/workspace/.reviewbench/tmp",
             "GRADLE_USER_HOME": CACHE, "ORACLE_INDEX": fixed["index"],
             "JAVA_TOOL_OPTIONS": "-Duser.home=" + HOME + " -XX:ActiveProcessorCount=4",
             "GRADLE_OPTS": "-Dorg.gradle.workers.max=4 -Dorg.gradle.java.installations.auto-download=false"},
        bootstrap=["mkdir -p " + HOME + " /workspace/.reviewbench/tmp",
                   "if [ ! -f " + marker + " ]; then cp -a " + DOWNLOADS + "/. " + CACHE
                   + " && touch " + marker + "; fi"],
        dev_checks=["env -u CI -u ORACLE_JCENTER_MIRROR bash ./gradlew --no-daemon "
                    "--console=plain --offline classes -I " + PUBLIC + "/mirror.init.gradle"],
        scratch_paths=[".oracle", ".gradle", "build"],
        grading={"multifile_build": True, "immutable_paths": sorted(source_names),
                 "golden_patch": ctx.copy_asset(checked(case["golden_patch"]), "golden"),
                 "original_legacy_profile": reference, "bootstrap": [],
                 "commands": [{"command": "python3 " + PRIVATE + "/runner.py --spec " + PRIVATE
                               + "/spec.json --output /workspace/" + RESULT,
                               "parser": "json", "report_path": RESULT,
                               "expected_tests": [fixed["junit_id"]]}]},
    )
    recipe.validate()
    return recipe
