"""Execute fixed public pytest nodes on the entire candidate, one file/process.

This file is a grader asset. It contains no replacement assertions or production
stubs. Pytest reports actual setup/call/teardown events; neither stdout markers,
summary counts, a missing report, nor an exit-zero/no-tests run can pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile

ENTRY_SCHEMA = "reviewbench-python-automatic-entry/v1"
REPORT_SCHEMA = "reviewbench-exec-test-results/v1"


def relative_path(value: str, *, allow_root: bool = False) -> str:
    if allow_root and value in {"", "."}:
        return "."
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or any(p in {"..", ".git", ".reviewbench"} for p in path.parts)
            or any(c in value for c in "\0\r\n\\")):
        raise ValueError("invalid fixed Python repository path")
    return path.as_posix()


def write_report(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + f".{os.getpid()}.tmp")
    pending.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    pending.replace(path)


def result(nodes: list[str], passed=(), **extra) -> dict:
    return {"schema_version": REPORT_SCHEMA,
            "tests": [{"id": n, "status": "passed" if n in passed else "failed"} for n in nodes],
            **extra}


def validate_contract(value: dict) -> dict:
    if value.get("schema_version") != ENTRY_SCHEMA:
        raise ValueError("invalid fixed Python entry contract")
    nodes = value["nodes"]
    if not nodes or len(nodes) != len(set(nodes)):
        raise ValueError("fixed Python selectors must be nonempty and unique")
    grouped = {}
    for node in nodes:
        if not isinstance(node, str) or "::" not in node or any(c in node for c in "\0\r\n"):
            raise ValueError("invalid fixed Python node ID")
        path, identity = node.split("::", 1)
        relative_path(path)
        if not path.endswith(".py") or not identity or path not in value["test_files"]:
            raise ValueError("fixed Python node has no pinned public test file")
        grouped.setdefault(path, []).append(node)
    if value["groups"] != list(grouped.values()):
        raise ValueError("fixed Python file/process grouping differs")
    for name, digest in value["test_files"].items():
        relative_path(name)
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("fixed Python test hash missing")
    for name in value["test_roots"]:
        relative_path(name)
    for name, target in value.get("test_symlinks", {}).items():
        relative_path(name)
        if Path(target).is_absolute():
            raise ValueError("fixed Python test symlink escapes workspace")
        relative_path(os.path.normpath(str(Path(name).parent / target)))
    relative_path(value["project_relative"], allow_root=True)
    relative_path(value["pytest_root_relative"], allow_root=True)
    if not 0 < value["timeout_seconds"] <= 1800:
        raise ValueError("fixed Python process timeout exceeds bound")
    return value


def check_inputs(contract: dict, workspace: Path) -> None:
    for name, digest in contract["test_files"].items():
        path = workspace / name
        if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(workspace)
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise ValueError("fixed public Python test/fixture changed or missing: " + name)
    for name, target in contract.get("test_symlinks", {}).items():
        path = workspace / name
        if (not path.is_symlink() or os.readlink(path) != target
                or not path.resolve().is_relative_to(workspace)):
            raise ValueError("fixed public Python fixture symlink changed or escaped: " + name)
    config = Path(contract["config_path"])
    if (not config.is_file() or config.is_symlink()
            or hashlib.sha256(config.read_bytes()).hexdigest() != contract["config_sha256"]):
        raise ValueError("fixed pytest configuration changed or missing")


class PytestEvents:
    def __init__(self, expected: list[str], workspace: Path):
        self.expected = expected
        self.workspace = workspace
        self.prefix = ""
        self.collected: list[str] = []
        self.events: dict[str, list] = {}

    def pytest_configure(self, config):
        root = Path(config.rootpath).resolve()
        if not root.is_relative_to(self.workspace):
            raise ValueError("pytest root escapes candidate workspace")
        relative = root.relative_to(self.workspace).as_posix()
        self.prefix = "" if relative == "." else relative + "/"

    def identity(self, node: str) -> str:
        # Pytest nodeids are rootdir-relative, while the retained selectors were
        # invoked relative to the repository cwd (notably airbyte-cdk/python).
        return node if node in self.expected else self.prefix + node

    def pytest_collection_finish(self, session):
        self.collected = [self.identity(item.nodeid) for item in session.items]

    def pytest_runtest_logreport(self, report):
        self.events.setdefault(self.identity(report.nodeid), []).append(
            (report.when, report.outcome, hasattr(report, "wasxfail")))

    def passed(self, code: int) -> set[str]:
        if (code != 0 or not self.collected or len(self.collected) != len(set(self.collected))
                or set(self.collected) != set(self.expected)):
            return set()
        complete = [("setup", "passed", False), ("call", "passed", False), ("teardown", "passed", False)]
        return {node for node in self.expected if self.events.get(node) == complete}


def worker(contract: dict, workspace: Path, report: Path, group: int) -> int:
    check_inputs(contract, workspace)
    if ".".join(map(str, sys.version_info[:3])) != contract["python_version"]:
        raise ValueError("prepared Python interpreter ABI/version differs")
    site = Path(contract["dependency_site"]).resolve()
    if not site.is_dir() or site.is_relative_to(workspace):
        raise ValueError("fixed pytest dependencies must be outside the candidate")
    # -I disables candidate PYTHONPATH, user-site and cwd at interpreter startup.
    # Do not run site.addsitedir(): retained .pth/editable hooks are not code inputs.
    sys.path.insert(0, str(site))
    import pytest
    if not Path(pytest.__file__).resolve().is_relative_to(site):
        raise ValueError("pytest did not load from the pinned dependency asset")
    for name in contract["source_packages"]:
        if name in sys.modules:
            raise ValueError("candidate production package was preloaded by a dependency")
    project = workspace / contract["project_relative"]
    roots = [workspace, workspace / "src", project, project / "src"]
    if any(not p.resolve().is_relative_to(workspace) for p in roots):
        raise ValueError("candidate Python import root escapes workspace")
    sys.path[:0] = list(dict.fromkeys(str(p) for p in roots))
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ.pop("PYTEST_ADDOPTS", None)
    os.environ.pop("PYTEST_PLUGINS", None)
    nodes = contract["groups"][group]
    events = PytestEvents(nodes, workspace)
    # Configuration bytes and rootdir match the retained invocation. These
    # transport options do not replace addopts, fixtures, assertions or selectors.
    arguments = ["-v", "-p", "no:cacheprovider", "-c", contract["config_path"],
                 "--rootdir", str(workspace / contract["pytest_root_relative"]), *nodes]
    os.chdir(workspace)
    code = int(pytest.main(arguments, plugins=[events]))
    check_inputs(contract, workspace)
    passed = events.passed(code)
    write_report(report, result(nodes, passed, pytest_exit_code=code,
                                collected=events.collected, events=events.events))
    return 0 if len(passed) == len(nodes) else (code or 1)


def execute(contract: dict, contract_path: Path, workspace: Path, report: Path) -> int:
    nodes = contract["nodes"]
    # Publish an explicit failing state before attempting any child; never reuse
    # a previous successful report when imports, collection or startup fail.
    write_report(report, result(nodes, error="fixed Python execution incomplete"))
    check_inputs(contract, workspace)
    run_root = Path(tempfile.mkdtemp(prefix="run-", dir=report.parent))
    passed: set[str] = set()
    runs = []
    for index, group in enumerate(contract["groups"]):
        child_report = run_root / f"group-{index}.json"
        command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                   "--contract", str(contract_path), "--workspace", str(workspace),
                   "--report", str(child_report), "--worker", str(index)]
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
               "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        for key in ("PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONOPTIMIZE"):
            env.pop(key, None)
        try:
            completed = subprocess.run(command, cwd=workspace, env=env, timeout=contract["timeout_seconds"])
            code = completed.returncode
        except subprocess.TimeoutExpired:
            code = 124
        good = set()
        error = None
        try:
            child = json.loads(child_report.read_text())
            tests = child["tests"]
            identities = [t["id"] for t in tests]
            if (child.get("schema_version") != REPORT_SCHEMA or identities != group
                    or len(identities) != len(set(identities))
                    or child.get("pytest_exit_code") != 0
                    or len(child.get("collected", [])) != len(group)
                    or set(child.get("collected", [])) != set(group)):
                raise ValueError("missing/mismatched fixed Python child completion")
            if code == 0:
                good = {t["id"] for t in tests if t["status"] == "passed"}
                complete = [["setup", "passed", False], ["call", "passed", False], ["teardown", "passed", False]]
                good = {n for n in good if child.get("events", {}).get(n) == complete}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            error = str(exc)
        passed.update(good)
        runs.append({"group": group, "command": command, "exit_code": code,
                     "report": str(child_report), "error": error})
    check_inputs(contract, workspace)
    write_report(report, result(nodes, passed, groups=runs))
    return 0 if passed == set(nodes) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--worker", type=int)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    report = args.report.absolute()
    if report.is_symlink() or not report.resolve().is_relative_to(workspace):
        raise ValueError("fixed Python report escapes workspace")
    report.unlink(missing_ok=True)
    contract = None
    try:
        contract = validate_contract(json.loads(args.contract.read_text()))
        if args.worker is not None:
            return worker(contract, workspace, report, args.worker)
        return execute(contract, args.contract.resolve(), workspace, report)
    except Exception as exc:
        if contract is not None:
            write_report(report, result(contract["nodes"] if args.worker is None
                                        else contract["groups"][args.worker],
                                        error=f"{type(exc).__name__}: {exc}"))
        print(f"FIXED_PYTHON_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
