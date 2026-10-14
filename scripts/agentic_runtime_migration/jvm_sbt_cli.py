"""Original Scala.js public CLI Oracle compiled against the complete candidate."""
from pathlib import Path
import shlex
from reviewbench_maintainability.agentic.contracts import ContractError, file_hash

TASK = "github__scala-js__scala-js__pull_5003__comment_1976332925__api_design"
START = "cd04cf401c6bd7f731b0ab2bfa6af8e00d82ac7e"
SOURCE = "session-tests/ScalaJSLinkerBehavior.scala"
SOURCE_SHA256 = "71c746039b60dd4645b6ee45e1a71db1629cbe56bb78d927025c41f4901906e3"
REFERENCE_SHA256 = "acd5ae851983f21646a1ed80feeefa371e3ebb88b3c0e2bf3bc2f5e6bc4f5d71"
TESTS = ["reviewbench.oracle.ScalaJSLinkerBehavior#emptyInputIsRejectedWithoutOutput"]
MARKER = "empty public linker input is rejected without output"
ARGS = [
    "-batch",
    'set LocalProject("linker2_13") / Compile / unmanagedSourceDirectories += file("/task/session-tests")',
    "linker2_13/Compile/runMain reviewbench.oracle.ScalaJSLinkerBehavior",
]


def public_arguments(arguments):
    if arguments == ARGS:
        return ["-batch", "linker2_13/Compile/compile"]
    return None


def plan(task, oracle, directory):
    if task.get("id") != TASK:
        return None
    environment = oracle.get("environment", {})
    if (task.get("repo") != "scala-js/scala-js" or task.get("start_sha") != START
            or oracle.get("id") != TASK or oracle.get("start_sha") != START
            or shlex.split(oracle.get("command", "")) != ["/oracle-tools/sbt", *ARGS]
            or oracle.get("pass_to_pass_tests") != TESTS
            or oracle.get("source_only_patch", {}).get("sha256") != REFERENCE_SHA256
            or environment.get("build_system") != "sbt" or environment.get("jdk") != 21
            or environment.get("container") != "oracle-java-tools:21-node"):
        raise ContractError("original Scala.js CLI task/command/environment/reference binding differs")
    source = Path(directory) / SOURCE
    if source.is_symlink() or not source.is_file() or file_hash(source) != SOURCE_SHA256:
        raise ContractError("original Scala.js CLI assertions changed")
    return {"source": SOURCE, "source_sha256": SOURCE_SHA256,
            "marker": MARKER, "expected_tests": TESTS.copy(),
            "original_arguments": ARGS.copy(), "original_main": "reviewbench.oracle.ScalaJSLinkerBehavior"}
