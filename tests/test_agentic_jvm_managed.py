from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.jvm_managed import (
    coursier_files, gradle_cache_files, sbt_cache_files, seed_once,
    sbt_compile_arguments, gradle_compile_arguments, scratch_directories, gradle_init_script, repository_mirrors,
    gradle_test_targets, gradle_test_dependency_resolver,
    bind_scala_classpath,
    sbt_jvm_options,
    shared_archive,
    pom_only_negative_cache,
    gradle_forced_test_execution,
)
from reviewbench_maintainability.agentic.contracts import ContractError
from agentic_runtime_migration.jvm_lane import (
    accepted_reference, frozen_non_p2p_statuses, frozen_non_p2p_exit_codes,
)
from agentic_runtime_migration.jvm_oracle_entry import accepted_process_result, failure_diagnostics
from prepare_agentic_sbt_dependencies import seed_thin_cache, dependency_overlay, refresh_prepared_overlay
from prepare_agentic_gradle_dependencies import dependency_input_names, successful_public_build
from prepare_agentic_managed_toolchains import managed_source


def put(root, name, contents="bytes"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    return path


def test_sbt_cache_contains_downloads_and_launchers_not_locally_published_candidate_outputs(tmp_path):
    put(tmp_path, ".cache/coursier/https/repo.maven.apache.org/lib/dep.jar")
    put(tmp_path, ".cache/coursier/https/repo.maven.apache.org/lib/dep.pom")
    put(tmp_path, ".cache/coursier/https/repo.maven.apache.org/lib/dep.jar.lock")
    put(tmp_path, ".cache/coursier/file/old-project/answer.jar")
    put(tmp_path, ".cache/coursier/https/user%40host/private.jar")
    put(tmp_path, ".ivy2/local/evaluated-project/answer.jar")
    put(tmp_path, ".sbt/1.0/staging/old-project/target/answer.jar")
    put(tmp_path, ".sbt/launchers/1.9.0/sbt-launch.jar")
    put(tmp_path, ".sbt/boot/scala-2.12/lib/scala-library.jar")
    assert sbt_cache_files(tmp_path) == [
        ".cache/coursier/https/repo.maven.apache.org/lib/dep.jar",
        ".cache/coursier/https/repo.maven.apache.org/lib/dep.pom",
        ".sbt/boot/scala-2.12/lib/scala-library.jar",
        ".sbt/launchers/1.9.0/sbt-launch.jar",
    ]


def test_coursier_cache_accepts_both_retained_layouts_and_ignores_links(tmp_path):
    target = put(tmp_path, "v1/https/repo.example/artifact.jar")
    put(tmp_path, "https/repo.example/other.jar")
    (target.parent / "link.jar").symlink_to(target)
    assert coursier_files(tmp_path) == ["https/repo.example/other.jar", "v1/https/repo.example/artifact.jar"]


def test_gradle_dependency_cache_excludes_workspace_build_cache_scripts_and_process_locks(tmp_path):
    put(tmp_path, "caches/modules-2/files-2.1/group/name/version/hash/name.jar")
    put(tmp_path, "caches/modules-2/metadata-2.106/descriptors/descriptor.bin")
    put(tmp_path, "caches/modules-2/modules-2.lock")
    put(tmp_path, "caches/build-cache-1/cached-project")
    put(tmp_path, "caches/8.1/scripts/generated-project.class")
    put(tmp_path, "wrapper/dists/gradle-8.1-bin/id/gradle-8.1/bin/gradle")
    put(tmp_path, "wrapper/dists/gradle-8.1-bin/id/distribution.zip.lck")
    assert gradle_cache_files(tmp_path) == [
        "caches/modules-2/files-2.1/group/name/version/hash/name.jar",
        "caches/modules-2/metadata-2.106/descriptors/descriptor.bin",
        "wrapper/dists/gradle-8.1-bin/id/gradle-8.1/bin/gradle",
    ]


def test_dependency_seed_is_complete_before_marking_and_is_reused_within_a_lease(tmp_path):
    source, target = tmp_path / "read-only seed", tmp_path / "cache"
    source.mkdir(); target.mkdir()
    (source / "library").write_text("seed")
    command = seed_once(str(source), str(target), "a" * 64)
    subprocess.run(["bash", "-e", "-c", command], check=True)
    (target / "library").write_text("per-lease cache metadata")
    subprocess.run(["bash", "-e", "-c", command], check=True)
    assert (target / "library").read_text() == "per-lease cache metadata"
    assert (source / "library").read_text() == "seed"


@pytest.mark.parametrize("command,expected", [
    ("/oracle-tools/sbt -batch 'testOnly *PrivateSuite'", ["-batch", "compile"]),
    ("/oracle-tools/sbt -batch 'rootJVM/Test/testOnly *.SecureRandomSpec'",
     ["-batch", "rootJVM/Compile/compile"]),
    ("/oracle-tools/sbt -batch '++3.3.1' 'cross/Test/testOnly *HoverSuite'",
     ["-batch", "++3.3.1", "cross/Compile/compile"]),
    ("mkdir out && /oracle-tools/sbt -batch aoc/compile && printf 'private input'",
     ["-batch", "aoc/compile"]),
])
def test_sbt_public_compile_preserves_project_and_version_without_running_private_tests(command, expected):
    assert sbt_compile_arguments(command) == expected


def test_sbt_arbitrary_runmain_is_not_silently_treated_as_a_complete_build():
    with pytest.raises(ContractError):
        sbt_compile_arguments("/oracle-tools/sbt -batch 'root/Compile/runMain PrivateHarness'")


def test_gradle_public_compile_keeps_build_flags_and_removes_only_private_test_selectors():
    command = ("rm -rf build/test-results && bash gradlew --no-daemon --init-script /tool/init.gradle "
               ":module:test --tests '*PrivateSuite*' -x generateGitProperties")
    assert gradle_compile_arguments(command, "gradlew") == [
        "--no-daemon", "--init-script", "/tool/init.gradle", "-x", "generateGitProperties", ":module:classes"]
    with pytest.raises(ContractError):
        gradle_compile_arguments("bash gradlew connectedDebugAndroidTest", "gradlew")


def test_build_scratch_does_not_hide_any_tracked_candidate_source():
    names = ["build.gradle", "app/build.gradle.kts", "app/src/Main.kt", "build/fixture.java",
             "scripts/build.gradle", "scripts/build/keep.txt"]
    assert scratch_directories(names, "gradle") == [".gradle", "app/build"]


def test_repository_relocation_is_explicit_and_does_not_change_test_selection_or_source():
    mirrors = repository_mirrors(["https://old.example/maven/=https://new.example/maven/"])
    assert mirrors == {"https://old.example/maven": "https://new.example/maven"}
    original = "allprojects { /* original build settings */ }\n"
    result = gradle_init_script(original, mirrors)
    assert result.startswith(original) and "repo.setUrl" in result
    assert "excludeTests" not in result and "ignoreFailures" not in result
    assert gradle_init_script(original, {}) == original
    with pytest.raises(ContractError):
        repository_mirrors(["https://user:secret@old.example/=https://new.example/"])
    with pytest.raises(ContractError):
        repository_mirrors(["http://old.example/=https://new.example/"])


def test_overwritten_jvm_reference_is_recovered_only_by_exact_frozen_hash(tmp_path):
    import hashlib
    from types import SimpleNamespace
    repository = tmp_path / "repo"
    repository.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args])
    git("init", "-q"); git("config", "user.name", "Fixture"); git("config", "user.email", "fixture@invalid")
    (repository / "Main.java").write_text("original\n")
    git("add", "."); git("commit", "-qm", "start")
    start = git("rev-parse", "HEAD").decode().strip()
    (repository / "Main.java").write_text("refactored\n")
    git("add", "."); git("commit", "-qm", "target")
    target = git("rev-parse", "HEAD").decode().strip()
    patch = git("diff", "--binary", start, target, "--", "Main.java")
    digest = hashlib.sha256(patch).hexdigest()
    overwritten = tmp_path / "old.patch"
    overwritten.write_text("later incompatible recovery attempt")
    ctx = SimpleNamespace(output=tmp_path / "output", repo=lambda task: repository,
                          golden=lambda *args: {"path": str(overwritten), "sha256": "wrong"},
                          copy_asset=lambda path, kind: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    oracle = {"source_only_patch": {"sha256": digest}, "source_only_production_files": ["Main.java"]}
    task = {"id": "task", "start_sha": start, "target_sha": target}
    recovered = accepted_reference(ctx, task, oracle, tmp_path)
    assert Path(recovered["path"]).read_bytes() == patch
    assert overwritten.read_text() == "later incompatible recovery attempt"
    oracle["source_only_patch"]["sha256"] = "a" * 64
    with pytest.raises(ContractError, match="frozen digest"):
        accepted_reference(ctx, task, oracle, tmp_path)


def test_thin_sbt_cache_uses_readonly_seed_links_and_exports_only_actual_additions(tmp_path):
    import os
    seed, cache, overlay = tmp_path / "seed", tmp_path / "cache", tmp_path / "overlay"
    jar = ".cache/coursier/https/repo.example/pkg/lib.jar"
    pom = ".cache/coursier/https/repo.example/pkg/lib.pom"
    put(seed, jar, "immutable jar bytes")
    put(seed, pom, "metadata")
    seed_thin_cache(seed, cache, [jar, pom], mount="/readonly-seed")
    assert os.readlink(cache / jar) == "/readonly-seed/" + jar
    assert (cache / pom).read_text() == "metadata"
    added = ".cache/coursier/https/repo.example/other/new.jar"
    put(cache, added, "downloaded new version")
    changed, removed = dependency_overlay(seed, cache, [jar, pom], overlay, mount="/readonly-seed")
    assert list(changed) == [added] and not removed
    assert not (overlay / jar).exists() and (seed / jar).read_text() == "immutable jar bytes"
    (cache / jar).unlink()
    put(cache, jar, "replacement")
    changed, removed = dependency_overlay(seed, cache, [jar, pom], tmp_path / "second", mount="/readonly-seed")
    assert jar in changed and (seed / jar).read_text() == "immutable jar bytes"


def test_thin_sbt_cache_rejects_links_to_unbound_host_content(tmp_path):
    seed, cache = tmp_path / "seed", tmp_path / "cache"
    name = ".cache/coursier/https/repo.example/lib.jar"
    put(seed, name)
    seed_thin_cache(seed, cache, [name])
    (cache / name).unlink()
    (cache / name).symlink_to("/other-host-content")
    with pytest.raises(ContractError, match="declared seed"):
        dependency_overlay(seed, cache, [name], tmp_path / "overlay")


def test_gradle_preparation_binds_original_dependency_descriptors_not_generated_outputs():
    assert dependency_input_names([
        "build.gradle.kts", "settings.gradle", "gradle/wrapper/gradle-wrapper.jar",
        "gradle/dependency-locks/compileClasspath.lockfile", "src/Main.java", "build/output.jar",
    ]) == [
        "build.gradle.kts", "gradle/dependency-locks/compileClasspath.lockfile",
        "gradle/wrapper/gradle-wrapper.jar", "settings.gradle",
    ]


def test_gradle_test_dependency_resolution_does_not_execute_or_replace_tests():
    command = "bash gradlew :server:test --tests '*FrozenSuite*' -x generatedThing :platform:test"
    assert gradle_test_targets(command, "gradlew") == [":server:test", ":platform:test"]
    code = gradle_test_dependency_resolver()
    assert "target.classpath.files" in code and "configuration.resolve()" in code
    assert '"CompileClasspath"' in code and '"RuntimeClasspath"' in code
    assert "ignoreFailures" not in code and "execute()" not in code and "excludeTests" not in code


def test_resolver_retry_reuses_only_hash_bound_successful_public_build_evidence(tmp_path):
    import hashlib
    import json
    history = tmp_path / "attempts/original"
    history.mkdir(parents=True)
    log = b"BUILD SUCCESSFUL\n"
    (history / "install.log").write_bytes(log)
    record = {"status": "prepared", "exit_status": 0, "oracle_fingerprint": "frozen",
              "repository_mirrors": {}, "log_sha256": hashlib.sha256(log).hexdigest()}
    (history / "preparation.json").write_text(json.dumps(record))
    (tmp_path / "preparation.json").write_text('{"status":"pending","resolve_only":true}')
    assert successful_public_build(tmp_path, "frozen", {}) == record
    (history / "install.log").write_text("changed")
    assert successful_public_build(tmp_path, "frozen", {}) is None


def test_scala_classpath_uses_compiled_version_and_pom_dependency_not_lexicographically_newest_jar(tmp_path):
    pom = """<project xmlns="http://maven.apache.org/POM/4.0.0"><dependencies><dependency>
<groupId>org.scala-lang</groupId><artifactId>scala-library</artifactId><version>2.13.8</version>
</dependency></dependencies></project>"""
    put(tmp_path, "https/repo.example/maven2/org/scala-lang/scala3-library_3/3.2.0/scala3-library_3-3.2.0.pom", pom)
    command = """CLASSPATH="aoc/target/scala-3.2.0/classes:$(find /cachehome/.cache/coursier -path '*/org/scala-lang/scala3-library_3/*/*.jar' | sort | tail -1):$(find /cachehome/.cache/coursier -path '*/org/scala-lang/scala-library/*/*.jar' | sort | tail -1)" """
    actual, binding = bind_scala_classpath(command, [tmp_path])
    assert "scala3-library_3/3.2.0/scala3-library_3-3.2.0.jar" in actual
    assert "scala-library/2.13.8/scala-library-2.13.8.jar" in actual
    assert binding["scala3"] == "3.2.0" and binding["scala_library"] == "2.13.8"
    with pytest.raises(ContractError, match="POM"):
        bind_scala_classpath(command, [tmp_path / "missing"])


def test_frozen_non_p2p_deviations_never_allow_a_required_assertion_or_new_failure_to_disappear():
    record = {"exit_code": 1, "statuses": {"required": "PASSED", "outside": "FAILED", "omitted": "SKIPPED"}}
    oracle = {"pass_to_pass_tests": ["required"], "start_run_records": [record] * 3,
              "golden_run_records": [record] * 3}
    allowed = frozen_non_p2p_statuses(oracle)
    assert allowed == {"omitted": ["SKIPPED"], "outside": ["FAILED"]}
    codes = frozen_non_p2p_exit_codes(oracle)
    assert codes == [1]
    assert accepted_process_result(1, record["statuses"], ["required"], allowed, codes)
    for observed in [
        {"required": "FAILED", "outside": "FAILED"},
        {"required": "PASSED", "new-failure": "FAILED"},
        {"outside": "FAILED"},
        {"required": "PASSED"},
        {"required": "PASSED", "outside": "FAILED", "omitted": "FAILED"},
    ]:
        assert not accepted_process_result(1, observed, ["required"], allowed, codes)
    assert not accepted_process_result(1, record["statuses"], ["required"], {}, codes)
    oracle["golden_run_records"] = [record, record, {"statuses": {"required": "FAILED"}}]
    assert frozen_non_p2p_statuses(oracle) == {}


def test_non_p2p_exception_does_not_hide_a_timeout_signal_or_unattested_exit_status():
    record = {"exit_code": 1, "statuses": {"required": "PASSED", "outside": "FAILED"}}
    oracle = {"pass_to_pass_tests": ["required"], "start_run_records": [record] * 3,
              "golden_run_records": [record] * 3}
    allowed = frozen_non_p2p_statuses(oracle)
    codes = frozen_non_p2p_exit_codes(oracle)
    for returncode in (-9, -15, 2, 124, 125, 126, 127, 137, 143):
        assert not accepted_process_result(returncode, record["statuses"], ["required"], allowed, codes)
    assert not accepted_process_result(1, record["statuses"], ["required"], allowed)
    for exit_code in (None, True, -9, 124, 137):
        oracle["golden_run_records"] = [record, record, {**record, "exit_code": exit_code}]
        assert frozen_non_p2p_exit_codes(oracle) == []


def test_sbt_flags_bound_initial_heap_and_compiler_stack_not_only_maximum_heap():
    import shlex
    options = shlex.split(sbt_jvm_options())
    assert options == ["-Xms256m", "-Xmx3g", "-Xss4m", "-XX:ActiveProcessorCount=2"]
    for prefix in ("-Xms", "-Xmx", "-Xss", "-XX:ActiveProcessorCount="):
        assert sum(option.startswith(prefix) for option in options) == 1


def test_shared_jvm_descriptor_reuse_skips_directory_rescan_and_rebundling(tmp_path):
    import hashlib
    from types import SimpleNamespace
    descriptor = put(tmp_path, "original-batch/descriptor.json", '{"immutable":"dependency tree"}')
    asset = {"path": str(descriptor), "sha256": hashlib.sha256(descriptor.read_bytes()).hexdigest()}
    calls = []
    def bundle(source, names):
        calls.append((source, names))
        return asset
    shared = tmp_path / "shared"
    source = tmp_path / "large-download-cache"
    ctx = SimpleNamespace(config={"migration": {"jvm_shared_assets": str(shared)}},
                          output=tmp_path / "batch-one", bundle=bundle)
    assert shared_archive(ctx, "sbt", source, lambda: ["remote.jar"]) == asset
    ctx.output = tmp_path / "another-batch"
    def unexpected_scan():
        raise AssertionError("a reusable descriptor must not rescan its download cache")
    assert shared_archive(ctx, "sbt", source, unexpected_scan) == asset
    assert calls == [(source, ["remote.jar"])]
    descriptor.write_text("changed descriptor")
    with pytest.raises(ContractError, match="descriptor changed"):
        shared_archive(ctx, "sbt", source, unexpected_scan)


def test_jvm_diagnostics_preserve_the_real_assertion_message_and_bound_only_log_volume(tmp_path):
    xml = put(tmp_path, "TEST-fixture.xml", """<testsuite>
<testcase classname="FrozenSuite" name="required"><failure type="java.lang.AssertionError"
message="expected 7 but got 8">at FrozenSuite.required(FrozenSuite.java:42)</failure></testcase>
<testcase classname="FrozenSuite" name="environment"><error type="MissingDependency"
message="missing SDK">original stack trace</error></testcase>
<testcase classname="FrozenSuite" name="skipped"><skipped/></testcase>
</testsuite>""")
    result = failure_diagnostics([xml])
    assert result["total_failures"] == 2 and not result["truncated"]
    assert result["failures"][0]["id"] == "FrozenSuite#required"
    assert result["failures"][0]["message"] == "expected 7 but got 8"
    assert result["failures"][1]["text"] == "original stack trace"
    limited = failure_diagnostics([xml], limit=1)
    assert limited["total_failures"] == 2 and limited["truncated"]
    assert len(limited["failures"]) == 1


def test_java_native_toolchain_is_explicit_and_checks_the_actual_c_toolchain(tmp_path):
    basic, _, basic_check = managed_source("oracle-java-tools:21", tmp_path)
    native, files, check = managed_source("oracle-java-tools:21", tmp_path, java_native_build_tools=True)
    assert "build-essential" not in basic and "cc --version" not in basic_check
    assert "build-essential pkg-config" in native and "cc --version" in check and "make --version" in check
    assert not files and "COPY" not in native and "ADD" not in native
    with pytest.raises(ValueError, match="Java toolchain"):
        managed_source("rb-csharp-sdk8-cov", tmp_path, java_native_build_tools=True)


def pom_only_fixture(root):
    relative = ".cache/coursier/https/repo.example/maven2/org/example/parent/1.0"
    pom = put(root, relative + "/parent-1.0.pom", """<project xmlns="http://maven.apache.org/POM/4.0.0">
<artifactId>parent</artifactId><version>1.0</version><packaging>pom</packaging></project>""")
    marker = put(root, relative + "/.parent-1.0.jar.error", "")
    return pom, marker


def test_pom_only_negative_marker_is_retained_but_required_jar_and_transport_errors_are_not(tmp_path):
    source, cache, output = tmp_path / "seed", tmp_path / "cache", tmp_path / "overlay"
    source.mkdir()
    pom, marker = pom_only_fixture(cache)
    relative = marker.relative_to(cache / ".cache/coursier")
    assert pom_only_negative_cache(marker, relative)
    changed, _ = dependency_overlay(source, cache, [], output)
    assert marker.relative_to(cache).as_posix() in changed
    marker.write_text("download failed")
    assert not pom_only_negative_cache(marker, relative)
    marker.write_text("")
    pom.write_text(pom.read_text().replace("<packaging>pom</packaging>", "<packaging>jar</packaging>"))
    assert not pom_only_negative_cache(marker, relative)


def test_overlay_refresh_keeps_public_build_evidence_and_only_adds_negative_metadata(tmp_path):
    import hashlib
    target, source = tmp_path / "target", tmp_path / "seed"
    source.mkdir()
    pom, marker = pom_only_fixture(target / "thin-home")
    # Simulate the preceding policy: it stored the positive POM but omitted
    # Coursier's empty marker for an optional, unpublished JAR.
    name = pom.relative_to(target / "thin-home").as_posix()
    put(target / "overlay", name, pom.read_text())
    log = put(target, "install.log", "[success] public compilation completed\n")
    previous = {"status": "prepared", "exit_status": 0, "oracle_fingerprint": "frozen",
                "base_asset": {"sha256": "base"}, "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                "overlay_files": {name: hashlib.sha256(pom.read_bytes()).hexdigest()}, "removed_files": []}
    result = refresh_prepared_overlay(target, source, [], previous, "frozen", previous["base_asset"])
    assert result["negative_cache_additions"] == [marker.relative_to(target / "thin-home").as_posix()]
    assert result["log_sha256"] == previous["log_sha256"] and result["refresh_overlay_only"]
    pom.write_text("altered positive dependency")
    with pytest.raises(ContractError, match="positive dependency"):
        refresh_prepared_overlay(target, source, [], previous, "frozen", previous["base_asset"])


def test_minixml_java_variant_uses_a_supplied_archive_and_real_header_link_check(tmp_path):
    source, files, check = managed_source(
        "oracle-java-tools:21", tmp_path, java_native_build_tools=True, java_mxml=True,
    )
    assert "COPY mxml-source.tar.gz" in source and "make -j2" in source
    assert "curl" not in source and "git clone" not in source and not files
    assert "#include <mxml.h>" in check and "mxmlNewXML" in check and " -lmxml " in check
    with pytest.raises(ValueError, match="native Java"):
        managed_source("oracle-java-tools:21", tmp_path, java_mxml=True)


def test_build_cache_forces_real_gradle_tests_without_changing_selectors_or_assertions():
    script = gradle_forced_test_execution()
    assert "testing.Test" in script and "outputs.upToDateWhen { false }" in script
    assert "outputs.cacheIf { false }" in script
    assert all(word not in script for word in ("excludeTests", "ignoreFailures", "testClassesDirs ="))


def test_cached_jvm_grader_reads_fresh_mirror_reports_and_observes_candidate_helper_edits(tmp_path):
    import json
    import shlex
    workspace, cache = tmp_path / "workspace", tmp_path / "cache"
    workspace.mkdir()
    (workspace / "helper.txt").write_text("7")
    scripts = Path(__file__).resolve().parents[1] / "scripts/agentic_runtime_migration"
    program = """from pathlib import Path
import sys
root=Path('build/test-results/test'); root.mkdir(parents=True, exist_ok=True)
counter=Path('build/executions')
counter.write_text(str(int(counter.read_text())+1 if counter.exists() else 1))
passed=Path('helper.txt').read_text()=='7'
failure='' if passed else '<failure message=\"expected 7\"/>'
(root/'TEST-Fixture.xml').write_text('<testsuite><testcase classname=\"Fixture\" name=\"check\">'+failure+'</testcase></testsuite>')
raise SystemExit(0 if passed else 1)
"""
    spec = {"command": shlex.join([sys.executable, "-c", program]), "expected_tests": ["Fixture#check"],
            "build_cache": {"helper": str(scripts / "jvm_build_entry.py"), "cache": str(cache)}}
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec))
    result_file = tmp_path / "result.json"
    command = [sys.executable, str(scripts / "jvm_oracle_entry.py"),
               "--spec", str(spec_file), "--output", str(result_file)]
    for iteration in (1, 2):
        result = subprocess.run(command, cwd=workspace, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert json.loads(result_file.read_text())["tests"] == [{"id": "Fixture#check", "status": "passed"}]
        assert (cache / "grader/mirror/build/executions").read_text() == str(iteration)
    (workspace / "helper.txt").write_text("8")
    result = subprocess.run(command, cwd=workspace, capture_output=True, text=True)
    assert result.returncode == 1
    observed = json.loads(result_file.read_text())
    assert observed["tests"] == [{"id": "Fixture#check", "status": "failed"}]
    assert observed["failure_diagnostics"]["failures"][0]["message"] == "expected 7"
    assert (cache / "grader/mirror/build/executions").read_text() == "3"
    assert (workspace / "helper.txt").read_text() == "8" and not (workspace / "build").exists()
