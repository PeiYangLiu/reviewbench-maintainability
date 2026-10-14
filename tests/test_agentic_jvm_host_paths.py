from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agentic_runtime_migration.jvm_lane import historical_host_assumptions


@pytest.mark.parametrize("command", [
    "cp /data/cache/archive /workspace",
    "bash /home/old/task.sh",
    "/oracle-tools/maven/bin/mvn test",
    "java -Ddependency.root=/data/cache -jar app.jar",
    "ROOT='/home/old/work'; echo value",
    "python -c \"open('/data/cache/file')\"",
    "git diff --cached -- source.java",
])
def test_historical_host_paths_remain_rejected(command):
    assert historical_host_assumptions(command)


@pytest.mark.parametrize("command", [
    "mkdir -p data/src/test/java/com/microsoft/azure/kusto/data/",
    "cp /opt/reviewbench-assets/jvm-tests/Test.java data/src/test/java/com/microsoft/azure/kusto/data/",
    "mkdir -p ./data/cache",
    "mkdir -p /workspace/data/cache",
    "mkdir -p src/main/java/example/home/",
    "echo https://example.invalid/data/file",
    "cp /database/input ./data/output",
    "java -Ddata.root=/workspace/home/data -jar app.jar",
])
def test_package_components_and_workspace_paths_are_not_host_paths(command):
    assert not historical_host_assumptions(command)
