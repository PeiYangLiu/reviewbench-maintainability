#!/usr/bin/env python3
"""Run the original fixed Scala CLI; a missing marker or failed process fails."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

SOURCE_SHA256 = "71c746039b60dd4645b6ee45e1a71db1629cbe56bb78d927025c41f4901906e3"
TEST = "reviewbench.oracle.ScalaJSLinkerBehavior#emptyInputIsRejectedWithoutOutput"
MARKER = b"empty public linker input is rejected without output"
SETTING = 'set LocalProject("linker2_13") / Compile / unmanagedSourceDirectories += file("{private}/session-tests")'
RUN_MAIN = "linker2_13/Compile/runMain reviewbench.oracle.ScalaJSLinkerBehavior"


def run_command(command, output):
    """Stream every original byte; only a bounded partial line is retained."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    tail, matches = b"", 0
    assert process.stdout is not None
    while True:
        chunk = process.stdout.read(8192)
        if not chunk:
            break
        output.write(chunk)
        output.flush()
        parts = (tail + chunk).split(b"\n")
        for line in parts[:-1]:
            if line.strip(b"\r") == MARKER:
                matches += 1
        tail = parts[-1][-4096:]
    if tail.strip(b"\r") == MARKER:
        matches += 1
    return process.wait(), matches


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())
    cli = spec["original_sbt_cli"]
    private = args.spec.resolve().parent
    source = private / "session-tests/ScalaJSLinkerBehavior.scala"
    if (source.is_symlink() or not source.is_file()
            or hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE_SHA256
            or cli["source_sha256"] != SOURCE_SHA256
            or spec["expected_tests"] != [TEST] or cli["expected_tests"] != [TEST]
            or cli["marker"].encode() != MARKER):
        raise ValueError("original Scala.js CLI source/assertions differ")
    words = shlex.split(spec["command"])
    if (not words or words[0] != "/opt/reviewbench-assets/jvm-build-tools/sbt"
            or words[-3:] != ["-batch", SETTING.format(private=private), RUN_MAIN]):
        raise ValueError("original Scala.js CLI command/targets differ")
    allowed = [
        "-sbt-launch-dir", "/opt/reviewbench-cache/sbt/.sbt/launchers",
        "-Dsbt.boot.directory=/opt/reviewbench-cache/sbt/.sbt/boot",
        "-Dsbt.global.base=/opt/reviewbench-cache/sbt/global",
        "-Dsbt.ivy.home=/opt/reviewbench-cache/sbt/ivy", "-Dsbt.task.cpus=2",
        "-Dsbt.log.noformat=true", "-Dsbt.supershell=false", "-Dsbt.server.autostart=false",
    ]
    if words[1:-3] != allowed:
        raise ValueError("fixed original sbt transport options differ")
    code, markers = run_command(words, sys.stdout.buffer)
    passed = code == 0 and markers == 1
    report = {"schema_version": "reviewbench-exec-test-results/v1",
              "tests": [{"id": TEST, "status": "passed" if passed else "failed"}],
              "original_exit_code": code, "original_success_marker_count": markers,
              "original_source_sha256": SOURCE_SHA256, "original_command_executions": 1}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print("Fixed original Scala CLI tests: " + ("1/1" if passed else "0/1"), flush=True)
    return 0 if passed else code or 1


if __name__ == "__main__":
    raise SystemExit(main())
