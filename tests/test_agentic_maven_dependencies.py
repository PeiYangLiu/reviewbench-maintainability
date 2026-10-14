import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from prepare_agentic_maven_dependencies import latest_failure, missing_artifacts, resolve_command, image_classifier
from reviewbench_maintainability.agentic.contracts import ContractError


def test_missing_maven_artifacts_keep_exact_version_type_and_classifier():
    text = """
[ERROR] dependency: io.netty:netty-transport-native-epoll:jar:linux-aarch_64:4.1.85.Final (compile)
[ERROR] Cannot access central in offline mode and the artifact org.vafer:jdependency:jar:2.8.0 has not been downloaded from it before.
[ERROR] The following artifacts could not be resolved: org.apache.maven.surefire:surefire-junit4:jar:3.4.0 (absent)
[ERROR] dependency: org.vafer:jdependency:jar:2.8.0 (compile)
[INFO] Plugin org.example:other:1.0:compile
[WARNING] The POM for org.example:unrelated:jar:1.0 is missing
"""
    assert missing_artifacts(text) == [
        "io.netty:netty-transport-native-epoll:4.1.85.Final:jar:linux-aarch_64",
        "org.apache.maven.surefire:surefire-junit4:3.4.0:jar",
        "org.vafer:jdependency:2.8.0:jar",
    ]


def test_prefetch_does_not_download_first_party_reactor_outputs_or_dynamic_versions():
    assert missing_artifacts("[ERROR] dependency: org.example:production:jar:1.0-SNAPSHOT",
                             {("org.example", "production")}) == []
    for version in ("1.0-SNAPSHOT", "LATEST", "RELEASE"):
        with pytest.raises(ContractError, match="immutable release"):
            missing_artifacts("[ERROR] dependency: org.external:lib:jar:" + version)


def test_old_maven_missing_lists_preserve_protoc_executable_classifier_and_exclude_reactor():
    text = """
[ERROR] 1) com.google.protobuf:protoc:exe:linux-x86_64:3.19.2
[ERROR]   Path to dependency:
[ERROR]     1) org.apache.pinot:pinot-common:jar:0.12.0-SNAPSHOT
[ERROR]     2) com.google.protobuf:protoc:exe:linux-x86_64:3.19.2
"""
    assert missing_artifacts(text, {("org.apache.pinot", "pinot-common")}) == [
        "com.google.protobuf:protoc:3.19.2:exe:linux-x86_64"]


def test_maven_dependency_paths_do_not_become_download_requests_for_synthetic_parents():
    text = """
[ERROR] 1) org.apache.maven.surefire:surefire-testng:jar:2.22.2
[ERROR]   Path to dependency:
[ERROR]     1) dummy:dummy:jar:1.0
[ERROR]     2) org.apache.maven.surefire:surefire-testng:jar:2.22.2
[ERROR]
"""
    assert missing_artifacts(text) == ["org.apache.maven.surefire:surefire-testng:2.22.2:jar"]


def test_dependency_get_runs_outside_project_without_tests_or_install_goals():
    command = resolve_command(["io.netty:netty-handler:4.1.85.Final:jar"])
    assert command.startswith("set -e\n")
    words = shlex.split(command.splitlines()[1])
    assert "org.apache.maven.plugins:maven-dependency-plugin:3.7.1:get" in words
    assert "-Dartifact=io.netty:netty-handler:4.1.85.Final:jar" in words
    assert "-Dtransitive=true" in words
    assert "-Dos.detected.classifier=linux-x86_64" in words
    assert all(word not in words for word in ("test", "install", "deploy"))
    with pytest.raises(ContractError):
        resolve_command([])


def test_maven_native_classifier_comes_from_the_pinned_image_not_host_guessing():
    assert image_classifier({"Os": "linux", "Architecture": "amd64"}) == "linux-x86_64"
    assert image_classifier({"Os": "linux", "Architecture": "arm64"}) == "linux-aarch_64"
    with pytest.raises(ContractError):
        image_classifier({"Os": "unknown", "Architecture": "amd64"})


def _failure(root, name, text, timestamp):
    path = root / (name + ".json")
    (root / (name + ".stdout")).write_text(text)
    (root / (name + ".stderr")).write_text("")
    path.write_text(json.dumps({
        "command": "mvn -o test", "returncode": 1, "status": "completed",
        "stdout_file": name + ".stdout", "stderr_file": name + ".stderr",
        "stdout_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
    }))
    os.utime(path, (timestamp, timestamp))
    return path


def test_dependency_prefetch_uses_latest_bound_failure_not_earlier_missing_versions(tmp_path):
    _failure(tmp_path, "old", "[ERROR] dependency: org.example:lib:jar:1.0", 100)
    newest = _failure(tmp_path, "new", "[ERROR] dependency: org.example:lib:jar:2.0", 200)
    text, evidence = latest_failure(tmp_path)
    assert missing_artifacts(text) == ["org.example:lib:2.0:jar"]
    assert evidence["path"] == str(newest)
    assert evidence["sha256"] == hashlib.sha256(newest.read_bytes()).hexdigest()


def test_dependency_prefetch_checks_failure_stream_hashes_and_path_boundaries(tmp_path):
    path = _failure(tmp_path, "new", "[ERROR] dependency: org.example:lib:jar:2.0", 200)
    (tmp_path / "new.stdout").write_text("changed evidence")
    with pytest.raises(ContractError, match="differs"):
        latest_failure(tmp_path)
    record = json.loads(path.read_text())
    record["stdout_file"] = "../other"
    path.write_text(json.dumps(record))
    with pytest.raises(ContractError):
        latest_failure(tmp_path)


def test_dependency_prefetch_reports_absent_failures(tmp_path):
    (tmp_path / "verification.json").write_text('{"status":"blocked"}')
    with pytest.raises(ContractError, match="no completed"):
        latest_failure(tmp_path)
