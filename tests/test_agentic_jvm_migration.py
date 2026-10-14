from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.jvm_lane import (
    remote_maven_files, maven_compile_command, relocate_maven_links, dependency_repository, RESULT_PATH,
    dependency_delta, validate_dependency_manifest, maven_dependency_assets, MAVEN_CACHE,
)
from agentic_runtime_migration.jvm_oracle_entry import read_reports, report_files
from reviewbench_maintainability.agentic.contracts import ContractError, repo_path


def test_maven_bundle_excludes_local_task_builds_and_current_project_coordinates(tmp_path):
    root = tmp_path / "com/example/pkg/1"
    root.mkdir(parents=True)
    (root / "_remote.repositories").write_text(
        "pkg-1.jar>central=\npkg-1.jar>=\npkg-1.pom>central=\nexternal-1.jar>central=\n"
        "built-1.jar>=\nsession-1.jar>reviewbench-local=\n")
    for name in ["pkg-1.jar", "pkg-1.pom", "external-1.jar", "built-1.jar", "session-1.jar"]:
        (root / name).write_text("bytes")
    names = remote_maven_files(tmp_path)
    assert "com/example/pkg/1/external-1.jar" in names
    assert "com/example/pkg/1/pkg-1.pom" in names
    assert all(not name.endswith(("pkg-1.jar", "built-1.jar", "session-1.jar")) for name in names)
    with pytest.raises(ContractError, match="empty"):
        remote_maven_files(tmp_path, {("com.example", "pkg")})


def test_jvm_public_build_preserves_reactor_and_does_not_execute_private_tests():
    command = maven_compile_command("-B -pl core -am -Dtest=SecretBehaviorTest test")
    assert "-pl core -am" in command
    assert "SecretBehaviorTest" not in command
    assert command.endswith("compile")
    assert "-o " in command and "-Dmaven.repo.local=" in command
    with pytest.raises(ContractError):
        maven_compile_command("-B test && echo fake")


def test_jvm_report_parser_preserves_hash_style_ids_and_negative_duplicates(tmp_path):
    paths = []
    for number, child in enumerate(["", "<failure />", "<skipped />"]):
        path = tmp_path / f"{number}.xml"
        path.write_text('<testsuite><testcase classname="org.example.Suite" name="behavior">' +
                        child + "</testcase></testsuite>")
        paths.append(path)
    assert read_reports(paths[:1]) == {"org.example.Suite#behavior": True}
    assert read_reports(paths) == {"org.example.Suite#behavior": False}
    assert read_reports([]) == {}


def test_jvm_report_scan_ignores_cache_and_symlink_reports(tmp_path):
    real = tmp_path / "module/target/surefire-reports/result.xml"
    real.parent.mkdir(parents=True)
    real.write_text("<testsuite />")
    cached = tmp_path / ".reviewbench/cache/target/surefire-reports/stale.xml"
    cached.parent.mkdir(parents=True)
    cached.write_text("<testsuite />")
    (real.parent / "link.xml").symlink_to(cached)
    assert report_files(tmp_path) == [real]


def test_maven_configuration_links_remain_inside_the_sealed_bundle(tmp_path):
    (tmp_path / "maven/bin").mkdir(parents=True)
    config = tmp_path / "etc-maven"
    config.mkdir()
    (config / "m2.conf").write_text("retained classworlds configuration")
    (tmp_path / "maven/conf").symlink_to("/etc/maven")
    (tmp_path / "maven/bin/m2.conf").symlink_to("/etc/maven/m2.conf")
    relocate_maven_links(tmp_path)
    assert (tmp_path / "maven/conf").resolve() == config
    assert (tmp_path / "maven/bin/m2.conf").read_text() == "retained classworlds configuration"


def test_jvm_result_path_obeys_the_frozen_engine_report_contract():
    assert repo_path(RESULT_PATH) == RESULT_PATH


def test_offline_maven_preserves_remote_range_metadata_but_not_local_project_metadata(tmp_path):
    public = tmp_path / "io/grpc/grpc-core"
    public.mkdir(parents=True)
    metadata = public / "maven-metadata-central.xml"
    metadata.write_text("<metadata><versioning><versions><version>1.51.0</version></versions></versioning></metadata>")
    Path(str(metadata) + ".sha1").write_text("checksum")
    (public / "maven-metadata-local.xml").write_text("<metadata/>")
    own = tmp_path / "org/example/project"
    own.mkdir(parents=True)
    (own / "maven-metadata-central.xml").write_text("<metadata/>")
    result = remote_maven_files(tmp_path, {("org.example", "project")})
    assert result == ["io/grpc/grpc-core/maven-metadata-central.xml",
                      "io/grpc/grpc-core/maven-metadata-central.xml.sha1"]


def test_prepared_maven_cache_is_explicit_and_task_scoped(tmp_path):
    from types import SimpleNamespace
    from reviewbench_maintainability.agentic.contracts import component
    task = {"id": "specific-task"}
    context = SimpleNamespace(config={}, legacy=tmp_path / "original")
    assert dependency_repository(context, task) == tmp_path / "original/jvm/cache/home/.m2/repository"
    context.config = {"migration": {"maven_cache_root": str(tmp_path / "prepared")}}
    with pytest.raises(ContractError, match="missing"):
        dependency_repository(context, task)
    path = tmp_path / "prepared" / component(task["id"]) / "repository"
    path.mkdir(parents=True)
    assert dependency_repository(context, task) == path


def test_maven_reactor_test_fixtures_are_built_from_current_candidate_without_running_tests():
    command = maven_compile_command("-B -pl ql -am -Dtest=FrozenBehavior test", build_test_fixtures=True)
    assert command.endswith(" package")
    assert "-DskipTests=true" in command
    assert "-Dmaven.test.skip=true" not in command
    assert "FrozenBehavior" not in command
    assert "-pl ql -am" in command


def test_maven_reactor_plugin_descriptor_is_generated_before_downstream_compilation():
    command = maven_compile_command(
        "-B -pl downstream -am -Dtest=FrozenBehavior test", build_plugin_descriptors=True)
    assert command.endswith(" process-classes")
    assert "-Dmaven.test.skip=true" in command
    assert "FrozenBehavior" not in command
    assert "-pl downstream -am" in command
    assert maven_compile_command("-B test", build_plugin_descriptors=True,
                                 build_test_fixtures=True).endswith(" package")


def test_maven_dependency_delta_omits_unchanged_bytes_but_tracks_deletions_and_modes(tmp_path):
    import hashlib
    (tmp_path / "unchanged.jar").write_bytes(b"base")
    (tmp_path / "changed.xml").write_bytes(b"new")
    (tmp_path / "added.pom").write_bytes(b"added")
    records = [("unchanged.jar", hashlib.sha256(b"base").hexdigest(), False),
               ("changed.xml", hashlib.sha256(b"old").hexdigest(), False),
               ("obsolete.jar", hashlib.sha256(b"obsolete").hexdigest(), False)]
    changed, removed = dependency_delta(tmp_path, ["unchanged.jar", "changed.xml", "added.pom"], records)
    assert changed == ["added.pom", "changed.xml"]
    assert removed == ["obsolete.jar"]
    (tmp_path / "unchanged.jar").chmod(0o755)
    assert "unchanged.jar" in dependency_delta(tmp_path, ["unchanged.jar"], records)[0]


def test_maven_base_manifest_is_cryptographically_bound_to_its_archive():
    from reviewbench_maintainability.agentic.contracts import canonical_hash
    records = [["library.jar", "a" * 64, False]]
    descriptor = {"files": 1, "tree_sha256": canonical_hash(records)}
    assert validate_dependency_manifest(records, descriptor) == records
    with pytest.raises(ContractError, match="differs"):
        validate_dependency_manifest([["library.jar", "b" * 64, False]], descriptor)
    bad = [["../escape", "a" * 64, False]]
    with pytest.raises(ContractError):
        validate_dependency_manifest(bad, {"files": 1, "tree_sha256": canonical_hash(bad)})


def test_maven_layering_reuses_public_base_and_limits_overlays_and_removals(tmp_path, monkeypatch):
    import hashlib
    import json
    from types import SimpleNamespace
    from agentic_runtime_migration import jvm_lane
    from reviewbench_maintainability.agentic import assets as asset_module
    (tmp_path / "same.jar").write_bytes(b"same")
    (tmp_path / "new.jar").write_bytes(b"new")
    records = [("same.jar", hashlib.sha256(b"same").hexdigest(), False),
               ("old.jar", hashlib.sha256(b"old").hexdigest(), False)]
    base = {"path": "/base/tree.json", "sha256": "a" * 64, "kind": "archive",
            "visibility": "developer", "target": jvm_lane.DEPENDENCIES}
    monkeypatch.setattr(jvm_lane, "base_dependency_asset", lambda *args: base)
    monkeypatch.setattr(jvm_lane, "dependency_manifest", lambda *args: records)
    monkeypatch.setattr(asset_module, "asset_descriptor", lambda asset, root: {
        "archive": {"input_bytes": 10000 if asset["sha256"] == "a" * 64 else 3}})
    copied = []
    def bundle(root, names):
        copied.extend(names)
        return {"path": "/overlay/tree.json", "sha256": "b" * 64}
    ctx = SimpleNamespace(output=tmp_path / "out", bundle=bundle)
    assets, commands = maven_dependency_assets(ctx, {"id": "task"}, tmp_path, ["same.jar", "new.jar"])
    assert copied == ["new.jar"] and assets[0] == base
    assert assets[1]["target"] == jvm_lane.MAVEN_OVERLAY
    assert commands[-1] == "rm -f -- " + MAVEN_CACHE + "/old.jar"
    report = json.loads(next((ctx.output / "maven-layering").glob("*.json")).read_text())
    assert report["base_reused"] is True and report["new_archive_input_bytes"] == 3
