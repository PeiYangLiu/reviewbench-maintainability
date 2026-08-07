"""Track A auto-filter: how many tasks have a REAL, runnable correctness oracle?

For each task this runs the funnel that decides whether a maintainability task
can be graded SWE-bench style:

  clone -> fetch start+target by sha -> build env
        -> run the PR-modified test files at start_sha           (baseline)
        -> apply full gold (target versions of changed files), re-run
        -> PASS_TO_PASS = tests green at start AND at gold        (stable set)
        -> coverage: do the stable tests execute the changed lines?
        -> SURVIVES iff PASS_TO_PASS non-empty AND changed lines covered

Most cases are expected to fail at "build env" (per-repo deps/builds) -- that is
the honest bottleneck we want to measure, not hide. Each stage failure is
recorded so the funnel is interpretable. Resumable via --resume.

Pitfalls handled (see docs/PITFALLS.md): P6 (stable PASS_TO_PASS, not all
start tests), P8 (coverage of the changed region), P10 (fetch start_sha by sha).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any


TEST_LINE_RE = re.compile(r"^(\S+::[^\s]+)\s+(PASSED|FAILED|ERROR|XFAIL|XPASS)", re.MULTILINE)
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
TEST_PATH_RE = re.compile(r"(^|/)(tests?|testing)(/|$)|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$")


def is_test_path(p: str) -> bool:
    return bool(TEST_PATH_RE.search(p))


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 300, env: dict | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, (p.stdout + "\n" + p.stderr)
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


def ensure_clone(repo: str, repos_dir: Path) -> tuple[Path | None, str]:
    dest = repos_dir / repo.replace("/", "__")
    if dest.exists():
        return dest, "cached"
    repos_dir.mkdir(parents=True, exist_ok=True)
    code, out = run(
        ["git", "clone", "--filter=blob:none", "--no-tags", f"https://github.com/{repo}.git", str(dest)],
        timeout=600,
    )
    if code != 0:
        return None, f"clone-fail: {out[-300:]}"
    return dest, "cloned"


def git(args: list[str], cwd: Path, timeout: int = 300) -> tuple[int, str]:
    return run(["git", *args], cwd=cwd, timeout=timeout)


def build_env(repo_dir: Path) -> tuple[str | None, str]:
    """Return (runner_prefix, note). runner_prefix runs python/pytest in the env."""
    uv = uv_binary()
    if not uv:
        return None, "uv-not-found"
    has_uv_lock = (repo_dir / "uv.lock").exists()
    if has_uv_lock:
        code, out = run([uv, "sync", "--extra", "test"], cwd=repo_dir, timeout=600)
        if code != 0:
            code, out = run([uv, "sync"], cwd=repo_dir, timeout=600)
        if code == 0:
            ensure_test_tools(uv, None, repo_dir)
            return f"{uv} run ", "uv sync"
        return None, f"uv-sync-fail: {out[-300:]}"

    # Generic: create a venv pinned to Python 3.12 via uv (the system python3 may be
    # too new -- 3.14 -- which many packages reject). See P22.
    venv = repo_dir / ".venv_bench"
    py = str(venv / "bin" / "python")
    if not venv.exists():
        code, out = run([uv, "venv", "--python", "3.12", str(venv)], timeout=300)
        if code != 0:
            return None, f"uv-venv-fail: {out[-200:]}"
    run(
        [uv, "pip", "install", "--python", py, "-q", "setuptools<81", "wheel"],
        timeout=300,
    )
    build_requirements = python_build_requirements(repo_dir)
    if build_requirements:
        run(
            [
                uv,
                "pip",
                "install",
                "--python",
                py,
                "-q",
                *build_requirements,
            ],
            cwd=repo_dir,
            timeout=600,
        )
    installed = False
    for spec in editable_install_specs(repo_dir):
        base = [uv, "pip", "install", "--python", py, "-q", "-e", spec]
        code, out = run(base, cwd=repo_dir, timeout=600)
        if code != 0:
            code, out = run(
                [*base, "--no-build-isolation"],
                cwd=repo_dir,
                timeout=600,
            )
        if code == 0:
            installed = True
            break
    if not installed:
        return None, f"pip-install-fail: {out[-300:]}"
    ensure_test_tools(uv, py, repo_dir)
    return f"{py} -m ", "uv venv py3.12"


def uv_binary() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    user_binary = Path.home() / ".local" / "bin" / "uv"
    if user_binary.is_file() and os.access(user_binary, os.X_OK):
        return str(user_binary)
    return None


def find_python_project_root(repo_dir: Path, source_path: str) -> Path:
    source = repo_dir / source_path
    current = source.parent
    legacy_candidates = []
    while current == repo_dir or repo_dir in current.parents:
        if (current / "pyproject.toml").is_file():
            return current
        setup_py = current / "setup.py"
        if setup_py.is_file():
            try:
                setup_text = setup_py.read_text(encoding="utf-8", errors="replace")
            except OSError:
                setup_text = ""
            if re.search(r"\bsetup\s*\(", setup_text):
                legacy_candidates.append(current)
        elif (current / "setup.cfg").is_file():
            legacy_candidates.append(current)
        if current == repo_dir:
            break
        current = current.parent
    return legacy_candidates[0] if legacy_candidates else repo_dir


def python_build_requirements(project_dir: Path) -> list[str]:
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        value = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    build_system = value.get("build-system") or {}
    requirements = build_system.get("requires") or []
    return [str(item) for item in requirements if str(item).strip()]


def editable_install_specs(project_dir: Path) -> list[str]:
    pyproject = project_dir / "pyproject.toml"
    if pyproject.is_file():
        try:
            value = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            value = {}
        optional = (value.get("project") or {}).get("optional-dependencies") or {}
        declared = {str(name) for name in optional}
        specs = [
            f".[{name}]"
            for name in ("test", "dev", "tests")
            if name in declared
        ]
        return [*specs, "."]
    return [".[test]", ".[dev]", ".[tests]", "."]


def ensure_test_tools(uv: str, python: str | None, project_dir: Path) -> None:
    if python:
        import_check = run(
            [python, "-c", "import pytest"],
            cwd=project_dir,
            timeout=60,
        )[0]
        prefix = [uv, "pip", "install", "--python", python, "-q"]
    else:
        import_check = run(
            [uv, "run", "python", "-c", "import pytest"],
            cwd=project_dir,
            timeout=60,
        )[0]
        prefix = [uv, "pip", "install", "-q"]
    if import_check != 0:
        run([*prefix, "pytest"], cwd=project_dir, timeout=300)
    run([*prefix, "--no-deps", "coverage"], cwd=project_dir, timeout=300)


def parse_tests(output: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in TEST_LINE_RE.finditer(output)}


def run_pytest(runner: str, repo_dir: Path, test_files: list[str], timeout: int = 300) -> dict[str, str]:
    # Run each file in isolation: unrelated files share DB/fixtures and contaminate
    # each other (a test passing alone errors when run alongside others). See P14.
    merged: dict[str, str] = {}
    for tf in test_files:
        cmd = runner.split() + ["pytest", "-v", "-p", "no:cacheprovider", tf]
        _, out = run(cmd, cwd=repo_dir, timeout=timeout)
        merged.update(parse_tests(out))
    return merged


def find_existing_tests(repo_dir: Path, source_path: str, limit: int = 6) -> list[str]:
    """Find existing test files that likely exercise the changed source file.

    PRs often only ADD tests, so the PR-touched test files may not exist at
    start_sha. We still want the repo's existing tests for the changed module as
    the PASS_TO_PASS baseline. See P21/P22.
    """
    import os

    stem = Path(source_path).stem
    if not stem or stem.startswith("test_") or stem.endswith("_test"):
        return []
    found: list[str] = []
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in (".git", ".venv", ".venv_bench", "node_modules")]
        for f in files:
            if not f.endswith(".py"):
                continue
            if f == f"test_{stem}.py" or f == f"{stem}_test.py" or (f.startswith("test_") and stem in f):
                found.append(os.path.relpath(os.path.join(root, f), repo_dir))
    # Prefer the closest path (same package dir) first.
    src_dir = str(Path(source_path).parent)
    found.sort(key=lambda p: (0 if str(Path(p).parent).startswith(src_dir) or src_dir in p else 1, len(p)))
    return found[:limit]


def changed_old_lines(repo_dir: Path, start: str, target: str, path: str) -> set[int]:
    code, patch = git(["diff", start, target, "--", path], repo_dir)
    if code != 0:
        return set()
    lines: set[int] = set()
    old = 0
    for raw in patch.splitlines():
        m = HUNK_RE.match(raw)
        if m:
            old = int(m.group(1))
            continue
        if raw.startswith("@@") or raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("-"):
            lines.add(old)
            old += 1
        elif raw.startswith("+"):
            continue
        else:
            old += 1
    return lines


def coverage_of_changed(runner: str, repo_dir: Path, stable_tests: list[str], path: str, changed: set[int]) -> dict[str, Any]:
    if not changed or not stable_tests:
        return {"checked": False, "covered": None, "note": "no changed old-lines or no stable tests"}
    cov_json = repo_dir / ".bench_cov.json"
    cov_json.unlink(missing_ok=True)
    # Run ONLY the stable tests (grouped per file to avoid cross-file contamination),
    # so coupled/excluded tests don't falsely mark the changed lines as covered.
    from collections import defaultdict
    by_file: dict[str, list[str]] = defaultdict(list)
    for t in stable_tests:
        by_file[t.split("::")[0]].append(t)
    run(runner.split() + ["coverage", "erase"], cwd=repo_dir, timeout=60)
    for nodeids in by_file.values():
        run(
            runner.split()
            + [
                "coverage",
                "run",
                "--parallel-mode",
                "--source",
                str(Path(path).parent),
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                *nodeids,
            ],
            cwd=repo_dir,
            timeout=300,
        )
    run(runner.split() + ["coverage", "combine"], cwd=repo_dir, timeout=60)
    run(runner.split() + ["coverage", "json", "-o", str(cov_json)], cwd=repo_dir, timeout=120)
    if not cov_json.exists():  # exit code is unreliable (projects set fail-under)
        return {"checked": True, "covered": None, "note": "coverage json not produced"}
    try:
        data = json.loads(cov_json.read_text())
    except Exception:
        return {"checked": True, "covered": None, "note": "coverage parse failed"}
    files = data.get("files", {})
    executed: set[int] = set()
    for fpath, info in files.items():
        if fpath.endswith(path) or Path(fpath).name == Path(path).name:
            executed = set(info.get("executed_lines", []))
            break
    hit = sorted(changed & executed)
    return {"checked": True, "covered": bool(hit), "changed_old_lines": len(changed), "executed_hit": len(hit)}


def filter_task(rec: dict[str, Any], repos_dir: Path) -> dict[str, Any]:
    repo = rec["repo"]
    path = rec["locality"]["path"]
    start = rec["start_sha"]
    target = rec["target_sha"]
    proxy = rec.get("correctness_oracle_proxy") or {}
    test_files = [
        str(path)
        for path in [
            *(proxy.get("test_change_paths") or []),
            *(rec.get("changed_files") or []),
        ]
        if str(path).endswith(".py") and is_test_path(str(path))
    ]
    result: dict[str, Any] = {"id": rec["id"], "repo": repo, "path": path, "stage": "start", "survives": False}

    repo_dir, note = ensure_clone(repo, repos_dir)
    result["clone"] = note
    if not repo_dir:
        result["stage"] = "clone"
        return result
    if git(["fetch", "--quiet", "origin", start], repo_dir, timeout=300)[0] != 0:
        result["stage"] = "fetch_start"; return result
    if git(["fetch", "--quiet", "origin", target], repo_dir, timeout=300)[0] != 0:
        result["stage"] = "fetch_target"; return result
    git(["fetch", "--quiet", "--tags", "origin"], repo_dir, timeout=600)
    git(["reset", "--hard", "--quiet", start], repo_dir)
    git(["clean", "-qfdx", "--exclude=.venv_bench", "--exclude=.venv"], repo_dir)

    # Test target = PR-touched test files present at start_sha UNION existing test
    # modules that match the changed source (PRs often only ADD tests). See P21/P22.
    present = [t for t in test_files if (repo_dir / t).exists()]
    existing = find_existing_tests(repo_dir, path)
    targets = sorted(set(present) | set(existing))
    result["test_files_total"] = len(test_files)
    result["test_files_present_at_start"] = len(present)
    result["existing_test_modules"] = len(existing)
    if not targets:
        result["stage"] = "no_test_file_at_start"; return result

    project_dir = find_python_project_root(repo_dir, path)
    runner, env_note = build_env(project_dir)
    result["env"] = env_note
    if runner is None:
        result["stage"] = "env"; return result

    baseline = run_pytest(runner, repo_dir, targets)
    result["baseline_collected"] = len(baseline)
    result["baseline_passed"] = sum(1 for v in baseline.values() if v == "PASSED")
    if result["baseline_passed"] == 0:
        result["stage"] = "baseline_no_pass"; return result

    # Apply the gold SOURCE change only (keep test files at start_sha): a correct
    # model fixes the source but does NOT get the human's test edits. Coupled tests
    # that assert the old implementation must therefore break (P6/P7).
    test_set = set(p for p in (proxy.get("test_change_paths") or []))
    source_changed = [
        str(path)
        for path in proxy.get("source_change_paths") or []
        if path and not is_test_path(str(path)) and str(path) not in test_set
    ]
    if not source_changed:
        _, names = git(["diff", "--name-only", start, target], repo_dir)
        changed_files = [n for n in names.splitlines() if n.strip()]
        source_changed = [
            f for f in changed_files if not is_test_path(f) and f not in test_set
        ]
    result["source_changed_count"] = len(source_changed)
    if not source_changed:
        result["stage"] = "test_only_task"; return result  # no source to fix -> not a behavior oracle
    git(["checkout", target, "--", *source_changed], repo_dir)
    gold = run_pytest(runner, repo_dir, targets)
    # PASS_TO_PASS = passed at start AND still passed after the source-only gold fix
    p2p = sorted(t for t, v in baseline.items() if v == "PASSED" and gold.get(t) == "PASSED")
    result["pass_to_pass"] = len(p2p)
    result["excluded_coupled"] = sorted(t for t, v in baseline.items() if v == "PASSED" and gold.get(t) != "PASSED")[:10]
    if not p2p:
        result["stage"] = "no_pass_to_pass"; return result

    # restore start tree for coverage of the to-be-changed lines
    git(["reset", "--hard", "--quiet", start], repo_dir)
    changed = changed_old_lines(repo_dir, start, target, path)
    cov = coverage_of_changed(runner, repo_dir, p2p, path, changed)
    result["coverage"] = cov
    result["changed_lines_covered"] = cov.get("covered")  # True / False / None(tooling failed)
    result["stage"] = "complete"
    # Runnable oracle = has a stable PASS_TO_PASS set. Coverage of the changed lines
    # is a quality bonus (reported separately), not a veto (P22).
    result["survives"] = bool(p2p)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", type=Path, required=True, help="Task JSONL.")
    parser.add_argument("--out", type=Path, required=True, help="Funnel results JSONL.")
    parser.add_argument("--repos-dir", type=Path, default=Path.home() / "trackA-poc" / "repos")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    recs = [json.loads(l) for l in args.input.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.limit:
        recs = recs[: args.limit]
    done: dict[str, dict[str, Any]] = {}
    if args.resume and args.out.exists():
        for l in args.out.read_text(encoding="utf-8").splitlines():
            if l.strip():
                r = json.loads(l); done[r["id"]] = r

    results: list[dict[str, Any]] = []
    for i, rec in enumerate(recs, 1):
        if rec["id"] in done:
            results.append(done[rec["id"]]); continue
        print(f"[{i}/{len(recs)}] {rec['repo']} ...", flush=True)
        try:
            res = filter_task(rec, args.repos_dir)
        except Exception as exc:  # noqa: BLE001
            res = {"id": rec["id"], "repo": rec["repo"], "stage": "exception", "error": str(exc)[:200], "survives": False}
        results.append(res)
        print(f"      stage={res['stage']} survives={res['survives']} "
              f"p2p={res.get('pass_to_pass')} cov={(res.get('coverage') or {}).get('covered')}", flush=True)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n", encoding="utf-8")

    print_funnel(results)
    return 0


def print_funnel(results: list[dict[str, Any]]) -> None:
    from collections import Counter
    stages = Counter(r["stage"] for r in results)
    print("\n=== Track A funnel ===")
    print(f"tasks: {len(results)}")
    for stage in ("clone", "fetch_start", "fetch_target", "no_test_file_at_start", "env", "baseline_no_pass", "test_only_task", "no_pass_to_pass", "complete", "exception"):
        if stages.get(stage):
            print(f"  stopped@{stage}: {stages[stage]}")
    runnable = [r for r in results if r.get("survives")]
    covered = [r for r in runnable if r.get("changed_lines_covered") is True]
    print(f"  RUNNABLE (has stable PASS_TO_PASS oracle): {len(runnable)}")
    print(f"    of which changed-lines-covered (stronger): {len(covered)}")


if __name__ == "__main__":
    raise SystemExit(main())
