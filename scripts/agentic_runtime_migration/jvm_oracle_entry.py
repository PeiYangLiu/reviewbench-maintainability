#!/usr/bin/env python3
"""Execute the fixed JVM command and preserve its original JUnit test IDs."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

REPORT_GLOBS = [
    "**/target/surefire-reports/*.xml", "**/target/failsafe-reports/*.xml",
    "**/target/test-reports/*.xml", "**/build/test-results/**/*.xml",
    ".oracle/test-reports/*.xml", ".oracle/test-results/*.xml",
]


def report_files(workspace):
    return sorted({path for pattern in REPORT_GLOBS for path in workspace.glob(pattern)
                   if ".reviewbench" not in path.relative_to(workspace).parts
                   and not path.is_symlink() and path.is_file()})


def read_statuses(paths):
    observed = {}
    severity = {"PASSED": 0, "SKIPPED": 1, "FAILED": 2}
    for path in paths:
        root = ET.parse(path).getroot()
        for case in root.iter("testcase"):
            ident = case.get("classname", "") + "#" + case.get("name", "")
            status = ("FAILED" if any(case.find(tag) is not None for tag in ("failure", "error"))
                      else "SKIPPED" if case.find("skipped") is not None else "PASSED")
            observed[ident] = max(observed.get(ident, "PASSED"), status, key=severity.get)
    return observed


def read_reports(paths):
    return {name: status == "PASSED" for name, status in read_statuses(paths).items()}


def failure_diagnostics(paths, limit=20):
    """Retain assertion messages before the disposable test workspace closes."""
    failures, total = [], 0
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            for tag in ("failure", "error"):
                for failure in case.findall(tag):
                    total += 1
                    if len(failures) < limit:
                        failures.append({
                            "id": case.get("classname", "") + "#" + case.get("name", ""),
                            "kind": tag, "type": failure.get("type", "")[:512],
                            "message": failure.get("message", "")[:4096],
                            "text": (failure.text or "")[:8192],
                        })
    return {"failures": failures, "total_failures": total, "truncated": total > len(failures)}


def accepted_process_result(returncode, observed, expected, allowed, allowed_exit_codes=()):
    if returncode == 0:
        return True
    if returncode not in allowed_exit_codes or not 0 < returncode < 124:
        return False
    deviations = {name: status for name, status in observed.items() if status != "PASSED"}
    return (any(status == "FAILED" for status in deviations.values())
            and set(expected).isdisjoint(allowed)
            and all(observed.get(name) == "PASSED" for name in expected)
            and all(status in allowed.get(name, []) for name, status in deviations.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())
    workspace = Path.cwd()
    build = spec.get("build_cache")
    module = None
    session = nullcontext(workspace)
    if build:
        loader = importlib.util.spec_from_file_location("reviewbench_jvm_build_entry", build["helper"])
        module = importlib.util.module_from_spec(loader)
        sys.modules[loader.name] = module
        loader.loader.exec_module(module)
        session = module.candidate_session(workspace, Path(build["cache"]), mode="grader")
    # XML removal, actual Test execution and report reading share the mirror
    # lock. No cached exit code/report is accepted instead of another run.
    with session as build_root:
        for path in report_files(build_root):
            path.unlink()
        environment = module.execution_environment(build_root, mode="grader") if module else None
        result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", spec["command"]],
                                cwd=build_root, env=environment)
        reports = report_files(build_root)
        observed = read_statuses(reports)
        diagnostics = failure_diagnostics(reports)
    accepted = accepted_process_result(result.returncode, observed, spec["expected_tests"],
                                       spec.get("frozen_non_p2p_statuses", {}),
                                       spec.get("frozen_non_p2p_exit_codes", []))
    tests = [{"id": name, "status": "passed" if accepted and observed.get(name) == "PASSED" else "failed"}
             for name in spec["expected_tests"]]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "schema_version": "reviewbench-exec-test-results/v1", "tests": tests,
        "original_exit_code": result.returncode,
        "observed_nonpassing": {name: status for name, status in observed.items() if status != "PASSED"},
        "frozen_non_p2p_statuses": spec.get("frozen_non_p2p_statuses", {}),
        "frozen_non_p2p_exit_codes": spec.get("frozen_non_p2p_exit_codes", []),
        "failure_diagnostics": diagnostics,
    }, indent=2))
    for failure in diagnostics["failures"]:
        print("JVM assertion failure: " + json.dumps(failure, ensure_ascii=False), flush=True)
    if diagnostics["truncated"]:
        print("JVM assertion diagnostics truncated; observed failure count: " +
              str(diagnostics["total_failures"]), flush=True)
    print("Fixed JVM tests: " + str(sum(item["status"] == "passed" for item in tests)) + "/" + str(len(tests)))
    return 0 if tests and all(item["status"] == "passed" for item in tests) else (result.returncode or 1)


if __name__ == "__main__":
    raise SystemExit(main())
