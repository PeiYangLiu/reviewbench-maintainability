"""Exact original-count views backed by six retained raw Gradle executions.

The release Oracle and frozen execution proof keep their own fingerprints.
No test, assertion, reference, model attempt or successful replay is replaced.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from functools import lru_cache
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from reviewbench_maintainability.agentic.contracts import ContractError, canonical_hash, file_hash
from reviewbench_maintainability.agentic.evaluation import validate_grading

ENGINE = "5142d1fec94eb6c5a6177a66ee7ce8533a26f0a2578280075a6947a399c01ca3"
PROFILE_KEY = "original_solr_hibernate_gradle_profile"
BRIDGE_KEY = "original_solr_hibernate_gradle_identity_bridge"
SCHEMA = "reviewbench-original-solr-hibernate-count-view/v1"
AUDIT_SCHEMA = "reviewbench-original-gradle-six-run-raw-audit/v1"
GRADER = "/opt/reviewbench-assets/original-gradle31-35-grader"
REPORT = ".oracle/original-gradle31-35/results.json"
COMMAND = "python3 " + GRADER + "/runner.py --spec " + GRADER + "/spec.json --output /workspace/" + REPORT
INIT = "d1e1ffee7439e1144bcf571918be80ba5d6798ff9b530466056089229e501f65"
AGENT = "47e700ccb0fdb9e27c5241353f8161938f4e53c3561dd35e063c5fe88dc3349b"
EXECUTOR = "255b7503a641b5dbeaaaad85d8901938ecac8570cddc009bc83ff3d67d40e1a5"
SLOTS = [(variant, repetition) for variant in ("start", "golden") for repetition in range(3)]
CASES = {'github__apache__solr__pull_3433__comment_2231064740__modularity': {'case': 'solr31',
                                                                     'count': 215,
                                                                     'declarations': '20bae53613d715e183f1bab98d791e0d9900fa1a763d2a60e39f45c5ef929967',
                                                                     'golden': '629d451f7e1677d6c22d92b7d0f13a06ba0e7529d08d80fff59dde2b6856019e',
                                                                     'grader_tree': '21ffb9957d0ee9ccf28738be61cc19291c1644811fb234e679f8e694d1638557',
                                                                     'gradle': '8.10',
                                                                     'immutable_paths': ['solr/modules/ltr/src/test',
                                                                                         'solr/modules/ltr/src/test-files',
                                                                                         'solr/test-framework/src/java/org/apache/solr/SolrTestCase.java',
                                                                                         'solr/test-framework/src/java/org/apache/solr/SolrTestCaseJ4.java'],
                                                                     'jdk': 21,
                                                                     'oracle': '39b8a12ea3dcc432dd11f9fdcfc4fa037d42d31931329eea8da513f9ac1d22ed',
                                                                     'public_contract': 'cac0e5bbdb3b246668174c4f7f0fd5ee7ddf8fefc4c3d0adaecffeecbbbde4ad',
                                                                     'repo': 'apache/solr',
                                                                     'report_roots': ['solr/modules/ltr/build/test-results',
                                                                                      'solr/modules/ltr/target/test-results'],
                                                                     'start': 'd016c35b96936e77d15f3f8a99ae543ca111e38e',
                                                                     'task': '7325d84a8ffbd8a53b18a068eedef29c8a45c946ddea419b844373697cc32c72',
                                                                     'test_ids': '45e74e42b61ba4fb4d8dc062ea2c4eac99b81ae5bc7031fb1ba411ed9279e5c8',
                                                                     'test_manifest': '07da882a27b4c786468039c61ec1d8beae56363318d1f9aff87cb93f012a7c04'},
 'github__hibernate__hibernate-orm__pull_4938__comment_844341285__duplication': {'case': 'hibernate35',
                                                                                 'count': 4,
                                                                                 'declarations': 'b5ddaa92b5fd505eb4be9988c262853183e84fa18138029af0221a302a6c6397',
                                                                                 'golden': '4c3079820f6e9608b5415cf7e119da503fd879cb8da8b45280af9c9af7546abe',
                                                                                 'grader_tree': '541c4da34327843d76a2c23ee77dd2fc3eec2449fa1d870a9dd73d1821a0ebcd',
                                                                                 'gradle': '7.3.3',
                                                                                 'immutable_paths': ['hibernate-spatial/src/test'],
                                                                                 'jdk': 11,
                                                                                 'oracle': '3c6e144a30012a7ce76ac6e3802c1ccb08af1a552662bdcec8bf75d3b9e17b38',
                                                                                 'public_contract': '50d058d4ef4f64b1030090c602aec6e124a6afbf9790b791e6d409b04b205103',
                                                                                 'repo': 'hibernate/hibernate-orm',
                                                                                 'report_roots': ['hibernate-spatial/build/test-results',
                                                                                                  'hibernate-spatial/target/test-results'],
                                                                                 'start': '61d515f57aab20aab06935816452d2b5df19ace1',
                                                                                 'task': '02f8d7d0664ca8a129c3bbcd5804a01c8b6d52f833c7f1361fb1baa57b9b9007',
                                                                                 'test_ids': '2bb9a191a64e51ddd3438475be03a7291aafa2d4b4685f97fb7d04dd637f70f4',
                                                                                 'test_manifest': 'd087a84d42d253bf4fa8c8e571340613cefa2359ed0585c9c82187a58291c19d'}}


def require(condition, message):
    if not condition:
        raise ContractError("original Solr/Hibernate view: " + message)


def stamp(path):
    stat = path.stat()
    return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@lru_cache(maxsize=4096)
def _digest(path, identity):
    return file_hash(Path(path))


def checked(pointer):
    require(isinstance(pointer, dict) and set(pointer) == {"path", "sha256"},
            "file needs an exact path/digest binding")
    require(isinstance(pointer["path"], str) and isinstance(pointer["sha256"], str)
            and re.fullmatch("[0-9a-f]{64}", pointer["sha256"]), "invalid file pointer")
    path = Path(pointer["path"])
    require(path.is_absolute() and path.is_file() and not path.is_symlink()
            and path.stat().st_size <= 64 * 1024**2, "bound input is missing or oversized")
    require(_digest(str(path), stamp(path)) == pointer["sha256"], "bound input bytes changed")
    return path


def read(pointer):
    return json.loads(checked(pointer).read_text())


def public_contract(recipe):
    data = asdict(recipe)
    fields = ("repo", "start_sha", "lane", "image", "env", "bootstrap", "dev_checks", "cache_mounts",
              "cpus", "memory_mb", "pids_limit", "build_scratch_mb", "command_timeout_seconds",
              "workdir", "scratch_paths", "sidecars", "submodules")
    result = {key: data[key] for key in fields}
    result["assets"] = [{key: asset[key] for key in ("kind", "sha256", "target", "visibility")}
                        for asset in recipe.assets if asset.get("visibility") == "developer"]
    return canonical_hash(result)


def execution_view(task, oracle, recipe):
    fixed = CASES.get(task.get("id"))
    require(fixed is not None and canonical_hash(task) == fixed["task"], "unsupported or changed task")
    require(task.get("repo") == fixed["repo"] and task.get("start_sha") == fixed["start"],
            "source revision changed")
    require(canonical_hash(oracle) == fixed["oracle"], "raw release Oracle changed")
    require(not ({"original_legacy_profile", "original_public_gradle_profile"} & set(recipe.grading)),
            "conflicting original-Oracle profiles")
    pointer = recipe.grading.get(PROFILE_KEY)
    profile = read(pointer)
    require(profile.get("schema_version") == "reviewbench-original-solr-hibernate-gradle-profile/v1"
            and type(profile.get("full_scope")) is int and profile["full_scope"] == 402
            and profile.get("id") == task["id"] and profile.get("case") == fixed["case"],
            "unknown original profile")
    require(profile.get("repo") == task["repo"] and profile.get("start_sha") == task["start_sha"]
            and profile.get("original_gradle_version") == fixed["gradle"]
            and type(profile.get("original_jdk_major")) is int
            and profile["original_jdk_major"] == fixed["jdk"], "original toolchain/source changed")
    count = fixed["count"]
    counts = oracle.get("pass_to_pass_tests")
    require(type(oracle.get("pass_to_pass")) is int and oracle["pass_to_pass"] == count
            and type(counts) is list and len(counts) == 6
            and all(type(value) is int and value == count for value in counts),
            "source field is not its original six-count vector")
    require(type(profile.get("original_count")) is int and profile["original_count"] == count
            and type(profile.get("original_count_vector")) is list
            and all(type(value) is int for value in profile["original_count_vector"])
            and profile["original_count_vector"] == counts, "profile count changed")
    tests = profile.get("expected_tests")
    require(type(tests) is list and len(tests) == count and len(set(tests)) == count
            and all(isinstance(name, str) for name in tests)
            and canonical_hash(tests) == fixed["test_ids"], "original test identities changed")
    require(profile.get("original_command") == oracle["command"]
            and profile.get("report_roots") == fixed["report_roots"]
            and profile.get("immutable_paths") == fixed["immutable_paths"],
            "original selector or assertion protection changed")
    for key, digest in [("test_source_manifest", fixed["test_manifest"]),
                        ("declarations", fixed["declarations"]), ("retained_legacy_init", INIT),
                        ("jacoco_agent", AGENT), ("original_executor", EXECUTOR)]:
        require(file_hash(checked(profile[key])) == digest, "original source/instrumentation changed: " + key)
    require(file_hash(checked(profile["original_golden_patch"])) == fixed["golden"]
            and oracle["source_only_patch"]["sha256"] == fixed["golden"]
            and file_hash(checked(recipe.grading["golden_patch"])) == fixed["golden"],
            "original source-only reference changed")
    require(canonical_hash(read(profile["original_task"])) == fixed["task"]
            and canonical_hash(read(profile["original_oracle"])) == fixed["oracle"],
            "profile task/Oracle bytes differ")
    binding = read(profile["original_input_bindings"])
    require(binding.get("full_scope") == 402 and binding.get("id") == task["id"]
            and binding.get("task_fingerprint") == fixed["task"]
            and binding.get("oracle_fingerprint") == fixed["oracle"]
            and canonical_hash(read(binding["task"])) == fixed["task"]
            and canonical_hash(read(binding["oracle"])) == fixed["oracle"]
            and binding.get("original_command") == oracle["command"],
            "original input binding changed")
    logs = binding.get("original_logs")
    require(type(logs) is list and len(logs) == 6, "six original historical logs are required")
    original_logs = []
    for row, (variant, repetition) in zip(logs, SLOTS):
        old = oracle[variant + "_runs"][repetition]
        require(row.get("variant") == variant and type(row.get("repetition")) is int
                and row["repetition"] == repetition + 1 and type(row.get("count")) is int
                and row["count"] == count and old["run"] == repetition + 1
                and type(old.get("tests")) is int and old["tests"] == count
                and type(old.get("exit_code")) is int and old["exit_code"] == 0 and not old.get("error"),
                "original historical slot/count/exit changed")
        source, copied = checked(row["original"]), checked(row["copy"])
        require(str(source).endswith("/" + old["log"]) and file_hash(source) == file_hash(copied),
                "historical log source differs")
        text = copied.read_text()
        require("BUILD SUCCESSFUL" in text and "BUILD FAILED" not in text, "historical run was unsuccessful")
        original_logs.append({"variant": variant, "repetition": repetition, "sha256": file_hash(copied), "count": count})
    validate_grading(recipe)
    require(recipe.task_id == task["id"] and public_contract(recipe) == fixed["public_contract"],
            "public execution/dependency contract changed")
    require(recipe.grading.get("immutable_paths") == fixed["immutable_paths"]
            and recipe.grading.get("commands") == [{"command": COMMAND, "expected_tests": tests,
                                                    "parser": "json", "report_path": REPORT}],
            "fixed executable test command changed")
    private = [asset for asset in recipe.assets if asset.get("visibility") == "grader"]
    require(len(private) == 1 and private[0].get("kind") == "archive"
            and private[0].get("target") == GRADER, "private execution asset changed")
    descriptor = read({key: private[0][key] for key in ("path", "sha256")})
    require(descriptor.get("schema_version") == "reviewbench-runtime-asset-tree/v1"
            and descriptor.get("source_kind") == "grader"
            and descriptor.get("tree_sha256") == fixed["grader_tree"], "private runner/spec bytes changed")
    view = deepcopy(oracle)
    view["pass_to_pass_tests"] = list(tests)
    certificate = {
        "schema_version": SCHEMA, "task_fingerprint": fixed["task"],
        "runtime_fingerprint": recipe.fingerprint, "original_oracle_fingerprint": fixed["oracle"],
        "execution_oracle_fingerprint": canonical_hash(view), "execution_engine_fingerprint": ENGINE,
        "source_profile": deepcopy(pointer), "original_reference_sha256": fixed["golden"],
        "original_logs": original_logs, "changed_fields": ["pass_to_pass_tests"],
        "source_field_encoding": "six_original_run_counts", "original_count_vector": list(counts),
        "execution_test_ids": list(tests), "original_assertions_changed": False, "original_reference_changed": False,
    }
    return view, certificate


@lru_cache(maxsize=2048)
def _xml_statuses(path, identity):
    result = []
    for case in ET.parse(path).getroot().iter("testcase"):
        original = case.get("classname", "") + "#" + case.get("name", "")
        ident = case.get("classname", "") + "#" + case.get("name", "").removesuffix("()")
        passed = all(case.find(tag) is None for tag in ("failure", "error", "skipped"))
        result.append((original, ident, passed))
    return tuple(result)


def audit_command_directory(directory, expected):
    records = []
    for path in directory.glob("*.json"):
        row = json.loads(path.read_text())
        if isinstance(row, dict) and "stdout_file" in row:
            records.append(row)
    require(Counter(row.get("command") for row in records) == Counter(expected),
            "bootstrap/public/fixed command inventory changed")
    for row in records:
        require(row.get("status") == "completed" and type(row.get("returncode")) is int
                and row["returncode"] == 0 and row.get("timed_out") is False
                and row.get("log_limit_exceeded") is False, "an original command did not succeed")
        for stream in ("stdout", "stderr"):
            name = row[stream + "_file"]
            require(Path(name).name == name, "command stream escaped its directory")
            checked({"path": str(directory / name), "sha256": row[stream + "_sha256"]})


def original_argv(command):
    require(command.count("bash ./gradlew") == 1, "original Gradle launcher changed")
    value = command.replace("bash ./gradlew", "bash ./gradlew --offline")
    init = "/workspace/.oracle/original-gradle31-35/oracle.init.gradle"
    if "/oracle-cache/oracle.init.gradle" in value:
        value = value.replace("/oracle-cache/oracle.init.gradle", init)
    else:
        value += " -I " + init
    value += (
        " -I /opt/reviewbench-assets/legacy-gradle-public/mirror.init.gradle"
        " -I /opt/reviewbench-assets/exact-gradle-dependencies/delta.init.gradle"
        " -I /opt/reviewbench-assets/resolved-original-gradle-graph/graph.init.gradle")
    return ["bash", "-e", "-o", "pipefail", "-c", value]


def verify_execution_audit(pointer, proof, task, view, recipe):
    audit = read(pointer)
    require(audit.get("schema_version") == AUDIT_SCHEMA and audit.get("status") == "verified"
            and type(audit.get("full_scope")) is int and audit["full_scope"] == 402,
            "complete raw execution audit is required")
    for key, value in [("task_fingerprint", canonical_hash(task)), ("runtime_fingerprint", recipe.fingerprint),
                       ("execution_oracle_fingerprint", canonical_hash(view)), ("engine_fingerprint", ENGINE)]:
        require(audit.get(key) == value, "raw execution audit identity differs: " + key)
    proof_path = checked(audit["execution_proof"])
    frozen = read(audit["execution_proof"])
    require(frozen == {key: value for key, value in proof.items() if key != BRIDGE_KEY},
            "original frozen proof was altered or relabeled")
    slots = audit.get("slots")
    require(type(slots) is list and len(slots) == 6, "raw audit needs all six executions")
    expected = view["pass_to_pass_tests"]
    profile = read(recipe.grading[PROFILE_KEY])
    audit_command_directory(proof_path.parent / "developer", [*recipe.bootstrap, *recipe.dev_checks])
    all_command_ids = []
    for row, (variant, repetition) in zip(slots, SLOTS):
        require(row.get("variant") == variant and type(row.get("repetition")) is int
                and row["repetition"] == repetition, "raw slot order/type differs")
        command_path = checked(row["command"])
        require(command_path.parent == proof_path.parent / (variant + "-" + str(repetition)),
                "calibration command belongs to another slot")
        audit_command_directory(command_path.parent,
                                [*recipe.bootstrap, *recipe.grading.get("bootstrap", []), COMMAND])
        command = read(row["command"])
        require(command.get("command") == COMMAND and command.get("status") == "completed"
                and type(command.get("returncode")) is int and command["returncode"] == 0
                and command.get("timed_out") is False and command.get("log_limit_exceeded") is False,
                "fixed grader command did not complete successfully")
        all_command_ids.append(command.get("id"))
        for stream in ("stdout", "stderr"):
            name = command[stream + "_file"]
            require(Path(name).name == name, "command output escaped its slot")
            checked({"path": str(command_path.parent / name), "sha256": command[stream + "_sha256"]})
        report_path = checked(row["raw_report"])
        require(report_path == command_path.parent / "original-artifacts/results.json",
                "raw report belongs to another execution")
        report = read(row["raw_report"])
        require(report.get("schema_version") == "reviewbench-exec-test-results/v1"
                and type(report.get("original_exit_code")) is int and report["original_exit_code"] == 0
                and type(report.get("expected_count")) is int and report["expected_count"] == len(expected)
                and type(report.get("observed_count")) is int and report["observed_count"] == len(expected)
                and report.get("original_command") == view["command"]
                and report.get("command") == original_argv(view["command"])
                and report.get("test_source_manifest_sha256") == profile["test_source_manifest"]["sha256"],
                "original subprocess, source or complete test count differs")
        require(report.get("tests") == [{"id": name, "status": "passed"} for name in expected]
                and report.get("observed_statuses") == {name: "PASSED" for name in expected}
                and report.get("failures") == [], "raw original tests did not all pass")
        xml_pointers = row.get("raw_xml")
        require(type(xml_pointers) is list and xml_pointers, "raw XML is absent")
        paths = [checked(p) for p in xml_pointers]
        declared = report.get("raw_xml")
        require(type(declared) is list and len(declared) == len(paths), "raw XML inventory was narrowed")
        supplied = {str(p.resolve()) for p in paths}
        require(len(supplied) == len(paths)
                and supplied == {str(p.resolve()) for p in (report_path.parent / "original-xml").rglob("*")
                                 if p.is_file() or p.is_symlink()},
                "complete copied XML inventory is required")
        for ref in declared:
            name = Path(ref["path"])
            require(not name.is_absolute() and ".." not in name.parts
                    and str((report_path.parent / name).resolve()) in supplied, "XML report escaped its inventory")
            checked({"path": str(report_path.parent / name), "sha256": ref["sha256"]})
        raw_ids = [item for path in paths for item in _xml_statuses(str(path), stamp(path))]
        require(len(raw_ids) == len(expected) and len({item[1] for item in raw_ids}) == len(expected)
                and {item[1] for item in raw_ids} == set(expected)
                and all(item[2] is True for item in raw_ids), "raw XML omitted, aliased, skipped or failed tests")
        require(report.get("raw_test_id_mapping") == [
            {"raw_id": original, "id": ident, "status": "PASSED"} for original, ident, passed in raw_ids],
            "reported test identities differ from original XML")
    require(len(all_command_ids) == len(set(all_command_ids)) == 6 and all(all_command_ids),
            "a calibration execution was reused in another slot")
    return audit


def annotate(proof, task, oracle, recipe, execution_audit):
    view, certificate = execution_view(task, oracle, recipe)
    require(BRIDGE_KEY not in proof and "original_public_gradle_identity_bridge" not in proof
            and "original_oracle_identity_bridge" not in proof, "conflicting or repeated annotation")
    require(proof.get("status") == "ready" and proof.get("oracle_fingerprint") == canonical_hash(view)
            and proof.get("task_fingerprint") == canonical_hash(task)
            and proof.get("runtime_fingerprint") == recipe.fingerprint
            and proof.get("engine_fingerprint") == ENGINE, "original execution proof identity differs")
    verify_execution_audit(execution_audit, proof, task, view, recipe)
    certificate["execution_audit"] = deepcopy(execution_audit)
    result = deepcopy(proof)
    result[BRIDGE_KEY] = certificate
    return result


def proof_identity_matches(proof, task, oracle, recipe):
    try:
        view, certificate = execution_view(task, oracle, recipe)
        received = proof.get(BRIDGE_KEY)
        require(isinstance(received, dict), "explicit count-view certificate is missing")
        reference = received.get("execution_audit")
        certificate["execution_audit"] = reference
        require(canonical_hash(certificate) == canonical_hash(received), "count-view certificate differs")
        require(proof.get("status") == "ready" and proof.get("engine_fingerprint") == ENGINE
                and proof.get("oracle_fingerprint") == canonical_hash(view)
                and proof.get("task_fingerprint") == canonical_hash(task)
                and proof.get("runtime_fingerprint") == recipe.fingerprint
                and "original_public_gradle_identity_bridge" not in proof
                and "original_oracle_identity_bridge" not in proof, "proof identity or status differs")
        runs = proof.get("runs")
        require(type(runs) is list and len(runs) == 6, "six successful original slots are required")
        expected = {"0::" + name: True for name in view["pass_to_pass_tests"]}
        for row, slot in zip(runs, SLOTS):
            require(type(row.get("repetition")) is int
                    and (row.get("variant"), row["repetition"]) == slot
                    and row.get("tests") == expected
                    and all(type(value) is bool for value in row["tests"].values()),
                    "original calibration slot or test outcomes changed")
        verify_execution_audit(reference, proof, task, view, recipe)
        return True
    except (ContractError, KeyError, TypeError, ValueError, OSError, ET.ParseError):
        return False
