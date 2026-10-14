from pathlib import Path
from types import SimpleNamespace
import json
import shlex
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.native_lane import public_configure_command, configure_helper, production_prefix, limit_jobs
from reviewbench_maintainability.agentic.contracts import ContractError


def test_native_public_configure_preserves_source_flags_and_repository_tests():
    source = """#!/bin/bash
set -euo pipefail
# Original helper comment is not part of the public command.
cmake -S . -B build-oracle -GNinja \\
 -DCMAKE_CXX_FLAGS="--coverage -O0 -g" \\
 -DBUILD_TESTING=ON -DWITH_SSL=system
"""
    command = public_configure_command(source)
    assert shlex.split(command) == [
        "cmake", "-S", ".", "-B", "build-oracle", "-GNinja",
        "-DCMAKE_CXX_FLAGS=--coverage -O0 -g", "-DBUILD_TESTING=ON", "-DWITH_SSL=system",
    ]
    assert "Original helper comment" not in command and "/harnesses/" not in command


@pytest.mark.parametrize("body", [
    "cmake -S /reference -B build-oracle",
    "cmake -S . -B /tmp/external",
    "cmake -S . -B build-oracle -DCMAKE_PROJECT_INCLUDE=/private/assertions.cmake",
    "cmake -S . -B build-oracle -DORACLE_ROOT=/private",
    "cmake -S . -B build-oracle && cp reference.cpp src/target.cpp",
    'cmake -S "$oracle_root/harnesses/project" -B build-oracle',
    "python3 rewrite_production.py",
    "cmake -S . -B build-oracle\npython3 alter.py",
])
def test_native_public_configure_does_not_publish_private_harnesses_or_source_rewrites(body):
    with pytest.raises(ContractError):
        public_configure_command("#!/bin/bash\nset -euo pipefail\n" + body)


def test_native_configure_helper_records_original_hash_without_changing_it(tmp_path):
    source = tmp_path / "legacy/native/harnesses/configure_fixture.sh"
    source.parent.mkdir(parents=True)
    content = "#!/bin/bash\nset -euo pipefail\ncmake -S . -B build-oracle -DBUILD_TESTING=ON\n"
    source.write_text(content)
    ctx = SimpleNamespace(legacy=tmp_path / "legacy", output=tmp_path / "output")
    task = {"id": "fixture", "start_sha": "a" * 40}
    command = configure_helper(ctx, task, "bash '{root}/harnesses/configure_fixture.sh'")
    assert command == "cmake -S . -B build-oracle -DBUILD_TESTING=ON"
    assert source.read_text() == content
    record = json.loads(next((ctx.output / "native-public-configure").glob("*.json")).read_text())
    assert record["source_changes"] == [] and record["test_changes"] == []
    assert len(record["source_sha256"]) == 64


def test_directory_named_cmake_does_not_masquerade_as_a_public_production_build():
    command = "set -e; mkdir -p oracle-source-cmake; cp '{root}/harnesses/CMakeLists.txt' oracle-source-cmake/"
    with pytest.raises(ContractError, match="does not compile"):
        production_prefix(command)
    real = "cmake --build build-oracle --target library -j2; g++ /private/harnesses/test.cpp -o test"
    assert production_prefix(real) == "cmake --build build-oracle --target library -j2"


@pytest.mark.parametrize("jobs,expected", [("24", "4"), ("4", "4"), ("2", "2"), ("1", "1"), ("0", "4")])
def test_native_job_limit_caps_without_increasing_original_parallelism(jobs, expected):
    assert limit_jobs("cmake --build build-oracle -j" + jobs) == "cmake --build build-oracle -j" + expected


def test_declared_nmos_public_source_directory_is_bounded_and_explicit():
    command = "cmake -S Development -B build-native-public -DNMOS_CPP_BUILD_TESTS=ON"
    assert public_configure_command(command, source_directory="Development",
                                    build_directory="build-native-public") == command
    with pytest.raises(ContractError):
        public_configure_command(command)
    with pytest.raises(ContractError):
        public_configure_command("cmake -S /reference -B build-native-public",
                                 source_directory="/reference", build_directory="build-native-public")


@pytest.mark.parametrize("extra", ["-S /private", "-B /private"])
def test_native_public_configure_rejects_duplicate_directory_options(extra):
    with pytest.raises(ContractError):
        public_configure_command("cmake -S . -B build-oracle " + extra)
