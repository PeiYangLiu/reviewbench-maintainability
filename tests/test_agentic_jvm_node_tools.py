from pathlib import Path
import json
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from agentic_runtime_migration.jvm_lane import toolchain_environment
from agentic_runtime_migration.jvm_managed import sbt_compile_arguments
from reviewbench_maintainability.agentic.contracts import ContractError
import prepare_agentic_managed_toolchains as tools


@pytest.mark.parametrize("version", [17, 21])
def test_java_node_toolchain_preserves_node_and_package_versions(version, tmp_path):
    name = f"oracle-java-tools:{version}-node"
    source, files, check = tools.managed_source(name, tmp_path)
    assert f"FROM eclipse-temurin:{version}-jdk" in source
    assert "COPY --from=node:22.14.0-bookworm-slim /usr/local/ /usr/local/" in source
    assert "ENV NODE_PATH=/usr/local/lib/node_modules" in source
    assert files == {}
    assert "/workspace" not in source and "/task" not in source
    assert "v22.14.0" in check
    for package, pin in tools.NODE_PACKAGES.items():
        assert package + "@" + pin in source
        assert json.dumps(package) in check and json.dumps(pin) in check
    profile = tools.managed_build_profile(name)
    assert profile == {"kind": "java-node-tools", "cpus": 2, "memory_gib": 4,
                       "scratch_gib": 8, "heavy": False}
    assert toolchain_environment({"container": name, "build_system": "sbt"}) == name


def test_jdk_and_node_copy_parents_are_both_digest_pinned(monkeypatch, tmp_path):
    calls = []
    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "pull":
            return ""
        assert args[:2] == ("image", "inspect")
        reference = args[2]
        prefix = "a" if reference.startswith("eclipse") else "b"
        return json.dumps([{"Id": "sha256:" + prefix * 64,
                            "RepoDigests": [reference.split(":")[0] + "@sha256:" + prefix * 64]}])
    monkeypatch.setattr(tools, "docker", docker)
    source, _, _ = tools.managed_source("oracle-java-tools:17-node", tmp_path)
    pinned, parents = tools.pin_parents(source)
    assert "FROM eclipse-temurin@sha256:" + "a" * 64 in pinned
    assert "COPY --from=node@sha256:" + "b" * 64 in pinned
    assert [row["reference"] for row in parents] == ["eclipse-temurin:17-jdk", tools.NODE_IMAGE]
    assert len([call for call in calls if call[0] == "pull"]) == 2


@pytest.mark.parametrize("name,system", [
    ("oracle-java-tools:17-node", "maven"), ("oracle-java-tools:21-node", "gradle"),
    ("oracle-java-tools:17-emulator", "sbt"), ("oracle-java-tools:17-python", "sbt"),
    ("oracle-java-tools:11-node", "sbt"), ("arbitrary-node:latest", "sbt"),
])
def test_node_extension_does_not_enable_unhandled_platform_contracts(name, system):
    with pytest.raises(ContractError, match="specialized"):
        toolchain_environment({"container": name, "build_system": system})


def test_public_scalajs_build_keeps_frozen_wasm_setting_without_private_test_selectors():
    command = ("rm -rf test-suite/js/.2.13/target/scala-2.13/scalajs-test-suite-test-fastopt* && "
               "/oracle-tools/sbt -batch 'set Global / enableWasmEverywhere := true' "
               "'testSuite2_13/Test/testOnly *ExportsTest *StackTraceTest'")
    assert sbt_compile_arguments(command) == [
        "-batch", "set Global / enableWasmEverywhere := true", "testSuite2_13/Compile/compile"]


@pytest.mark.parametrize("commands", [
    "'set Global / enableWasmEverywhere := false' compile",
    "'set Global / enableWasmEverywhere := true; println(\"private\")' compile",
    "'set every Test / skip := true' compile",
    "'set Global / enableWasmEverywhere := true' 'set Global / enableWasmEverywhere := true' compile",
    "compile 'set Global / enableWasmEverywhere := true'",
    "'set LocalProject(\"linker2_13\") / Compile / unmanagedSourceDirectories += file(\"/task/session-tests\")' "
    "'linker2_13/Compile/runMain PrivateMain'",
])
def test_only_literal_original_wasm_setting_is_a_supported_public_build_operation(commands):
    with pytest.raises(ContractError):
        sbt_compile_arguments("/oracle-tools/sbt -batch " + commands)
