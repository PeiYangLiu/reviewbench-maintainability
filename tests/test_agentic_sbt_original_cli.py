from copy import deepcopy
import io
from pathlib import Path
import shlex
import shutil
import sys

import pytest

from agentic_runtime_migration import jvm_sbt_cli as cli
from agentic_runtime_migration import jvm_sbt_cli_entry as entry
from agentic_runtime_migration.jvm_managed import sbt_compile_arguments
from reviewbench_maintainability.agentic.contracts import ContractError


def inputs(tmp_path):
    task = {"id": cli.TASK, "repo": "scala-js/scala-js", "start_sha": cli.START}
    oracle = {
        **task, "command": shlex.join(["/oracle-tools/sbt", *cli.ARGS]),
        "pass_to_pass_tests": cli.TESTS.copy(),
        "source_only_patch": {"sha256": cli.REFERENCE_SHA256},
        "environment": {"build_system": "sbt", "jdk": 21, "container": "oracle-java-tools:21-node"},
    }
    path = tmp_path / cli.SOURCE
    path.parent.mkdir()
    shutil.copy2(Path(__file__).parent / "fixtures/scalajs140/ScalaJSLinkerBehavior.scala", path)
    return task, oracle, tmp_path


def test_original_cli_has_a_complete_public_build_without_grader_sources(tmp_path):
    task, oracle, directory = inputs(tmp_path)
    result = cli.plan(task, oracle, directory)
    assert result["source_sha256"] == cli.SOURCE_SHA256
    assert result["expected_tests"] == cli.TESTS
    assert result["original_arguments"] == cli.ARGS
    assert sbt_compile_arguments(oracle["command"]) == ["-batch", "linker2_13/Compile/compile"]
    assert "session-tests" not in " ".join(sbt_compile_arguments(oracle["command"]))
    assert "runMain" not in " ".join(sbt_compile_arguments(oracle["command"]))


@pytest.mark.parametrize("field,value", [
    ("start_sha", "1" * 40), ("repo", "another/repo"),
])
def test_original_cli_task_binding_is_not_inferred(field, value, tmp_path):
    task, oracle, directory = inputs(tmp_path)
    task[field] = value
    with pytest.raises(ContractError):
        cli.plan(task, oracle, directory)


@pytest.mark.parametrize("change", ["command", "tests", "reference", "jdk", "image", "source"])
def test_changed_original_cli_evidence_is_rejected(change, tmp_path):
    task, oracle, directory = inputs(tmp_path)
    if change == "command":
        oracle["command"] += " another-target"
    elif change == "tests":
        oracle["pass_to_pass_tests"] = ["invented"]
    elif change == "reference":
        oracle["source_only_patch"]["sha256"] = "2" * 64
    elif change == "jdk":
        oracle["environment"]["jdk"] = 17
    elif change == "image":
        oracle["environment"]["container"] = "oracle-java-tools:21"
    else:
        (directory / cli.SOURCE).write_text("println(\"fake\")")
    with pytest.raises(ContractError):
        cli.plan(task, oracle, directory)


@pytest.mark.parametrize("arguments", [
    ["-batch", "linker2_13/Compile/runMain another.Main"],
    ["-batch", cli.ARGS[1], "linker2_13/Compile/runMain another.Main"],
    ["-batch", cli.ARGS[1] + " ", cli.ARGS[2]],
])
def test_other_runmain_shapes_do_not_gain_a_public_build(arguments):
    with pytest.raises(ContractError):
        sbt_compile_arguments(shlex.join(["/oracle-tools/sbt", *arguments]))


@pytest.mark.parametrize("code,body,expected", [
    (0, cli.MARKER + "\n", (0, 1)),
    (3, cli.MARKER + "\n", (3, 1)),
    (0, "no original assertion output\n", (0, 0)),
    (0, cli.MARKER + "\n" + cli.MARKER + "\n", (0, 2)),
    (0, "prefix" + cli.MARKER + "\n", (0, 0)),
    (0, "x" * 20000 + "\n" + cli.MARKER + "\n", (0, 1)),
])
def test_cli_streams_original_output_and_preserves_process_failure(code, body, expected):
    target = io.BytesIO()
    result = entry.run_command([sys.executable, "-c",
        "import sys;sys.stdout.write(" + repr(body) + ");sys.exit(" + str(code) + ")"], target)
    assert result == expected
    assert target.getvalue() == body.encode()
    assert (result[0] == 0 and result[1] == 1) == (expected == (0, 1))


def test_unrelated_task_does_not_select_the_cli_profile(tmp_path):
    task, oracle, directory = inputs(tmp_path)
    task["id"] = "another"
    assert cli.plan(task, oracle, directory) is None
