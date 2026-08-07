"""Track A filter for Go repos (scale the runnable hard pool).

Go is the largest, cleanest ecosystem in the pool (~31 source-anchor hard tasks)
and `go test ./pkg/` is far more reproducible than Python's compile-heavy stack.

Per task: clone/fetch by sha (reuse trackA_filter), checkout start, run the test
for the PACKAGE containing the changed file (baseline), apply the source-only gold
(target versions of changed non-_test.go files), re-run, compute PASS_TO_PASS =
tests passing at start AND after gold (excludes PR-rewritten/coupled tests, P6/P7),
then check whether the stable tests cover the changed lines via `go test -coverprofile`.

Test-file-anchored tasks (`*_test.go`) are skipped (P26: the test is both the task
artifact and the oracle). Reuses git/clone helpers from trackA_filter.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import trackA_filter as ta


GO_TEST_RE = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)", re.MULTILINE)
DEFAULT_GO_IMAGE = "golang:1.25-bookworm"


def is_go_test(path: str) -> bool:
    return path.endswith("_test.go")


def pkg_target(path: str | Path) -> str:
    d = str(Path(path).parent)
    return "./" if d in ("", ".") else "./" + d.strip("/") + "/"


def module_context(repo_dir: Path, source_path: str) -> tuple[Path, str]:
    source = repo_dir / source_path
    current = source.parent
    while current == repo_dir or repo_dir in current.parents:
        if (current / "go.mod").is_file():
            relative_source = source.relative_to(current)
            return current, pkg_target(relative_source)
        if current == repo_dir:
            break
        current = current.parent
    return repo_dir, pkg_target(source_path)


def go_test(repo_dir: Path, target: str, run_names: list[str] | None = None,
            cover_out: str | None = None, timeout: int = 900) -> tuple[dict[str, str], str]:
    args = ["test", "-v", "-count=1"]
    if run_names:
        tops = sorted({n.split("/")[0] for n in run_names})
        args += ["-run", "^(" + "|".join(tops) + ")$"]
    if cover_out:
        cover_path = Path(cover_out)
        if shutil.which("go") is None and cover_path.is_absolute():
            cover_path = Path("/repo") / cover_path.relative_to(repo_dir.resolve())
        args += ["-coverprofile", str(cover_path)]
    args += [target]
    cmd = build_go_command(repo_dir, args)
    _, out = ta.run(cmd, cwd=repo_dir, timeout=timeout)
    res = {m.group(2): m.group(1) for m in GO_TEST_RE.finditer(out)}
    return res, out


def build_go_command(
    repo_dir: Path,
    args: list[str],
    use_docker: bool | None = None,
) -> list[str]:
    if use_docker is None:
        use_docker = shutil.which("go") is None
    if not use_docker:
        return ["go", *args]
    image = os.environ.get("REVIEWBENCH_GO_IMAGE", DEFAULT_GO_IMAGE)
    return [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "-e",
        "GOTOOLCHAIN=auto",
        "-v",
        f"{repo_dir.resolve()}:/repo",
        "-v",
        "reviewbench-go-mod:/go/pkg/mod",
        "-v",
        "reviewbench-go-build:/root/.cache/go-build",
        "-w",
        "/repo",
        image,
        "go",
        *args,
    ]


def go_covered_lines(repo_dir: Path, cover_out: Path, path: str) -> set[int]:
    if not cover_out.exists():
        return set()
    covered: set[int] = set()
    name = Path(path).name
    for line in cover_out.read_text(errors="replace").splitlines():
        # format: import/path/file.go:startLine.col,endLine.col numStmt count
        m = re.match(r"^(.*\.go):(\d+)\.\d+,(\d+)\.\d+\s+\d+\s+(\d+)$", line)
        if not m:
            continue
        if not (m.group(1).endswith(path) or m.group(1).endswith(name)):
            continue
        if int(m.group(4)) > 0:
            covered.update(range(int(m.group(2)), int(m.group(3)) + 1))
    return covered


def filter_task(rec: dict[str, Any], repos_dir: Path) -> dict[str, Any]:
    repo = rec["repo"]
    path = rec["locality"]["path"]
    start, target = rec["start_sha"], rec["target_sha"]
    result: dict[str, Any] = {"id": rec["id"], "repo": repo, "path": path, "stage": "start", "survives": False}
    if is_go_test(path):
        result["stage"] = "test_anchor_skip"; return result

    repo_dir, note = ta.ensure_clone(repo, repos_dir)
    result["clone"] = note
    if not repo_dir:
        result["stage"] = "clone"; return result
    for sha in (start, target):
        if ta.git(["fetch", "--quiet", "origin", sha], repo_dir, timeout=600)[0] != 0:
            result["stage"] = "fetch"; return result
    ta.git(["reset", "--hard", "--quiet", start], repo_dir)
    ta.git(["clean", "-qfd"], repo_dir)

    if not (repo_dir / path).exists():
        result["stage"] = "no_source_at_start"; return result
    go_dir, target_pkg = module_context(repo_dir, path)
    result["pkg"] = target_pkg
    result["module_dir"] = str(go_dir.relative_to(repo_dir))

    baseline, out = go_test(go_dir, target_pkg)
    result["baseline_collected"] = len(baseline)
    result["baseline_passed"] = sum(1 for v in baseline.values() if v == "PASS")
    if result["baseline_passed"] == 0:
        result["stage"] = "baseline_no_pass"
        result["hint"] = ("build failed" if "[build failed]" in out or "cannot find" in out else out[-200:])
        return result

    # source-only gold (keep _test.go at start)
    proxy = rec.get("correctness_oracle_proxy") or {}
    source_changed = [
        str(path)
        for path in proxy.get("source_change_paths") or []
        if str(path).endswith(".go") and not is_go_test(str(path))
    ]
    if not source_changed:
        _, names = ta.git(["diff", "--name-only", start, target], repo_dir)
        changed = [n for n in names.splitlines() if n.strip()]
        source_changed = [
            f for f in changed if f.endswith(".go") and not is_go_test(f)
        ]
    result["source_changed_count"] = len(source_changed)
    if not source_changed:
        result["stage"] = "test_only_task"; return result
    ta.git(["checkout", target, "--", *source_changed], repo_dir)
    gold, _ = go_test(go_dir, target_pkg)
    p2p = sorted(t for t, v in baseline.items() if v == "PASS" and gold.get(t) == "PASS")
    result["pass_to_pass"] = len(p2p)
    result["excluded_coupled"] = sorted(t for t, v in baseline.items() if v == "PASS" and gold.get(t) != "PASS")[:8]
    if not p2p:
        result["stage"] = "no_pass_to_pass"; return result

    # coverage: do the stable tests execute the changed lines?
    ta.git(["reset", "--hard", "--quiet", start], repo_dir)
    changed_lines = ta.changed_old_lines(repo_dir, start, target, path)
    cover_out = go_dir / ".bench_go_cover.out"
    cover_out.unlink(missing_ok=True)
    go_test(go_dir, target_pkg, run_names=p2p, cover_out=str(cover_out))
    executed = go_covered_lines(go_dir, cover_out, path)
    hit = sorted(changed_lines & executed)
    result["coverage"] = {"changed_old_lines": len(changed_lines), "executed_hit": len(hit), "covered": bool(hit)}
    result["changed_lines_covered"] = bool(hit)
    result["stage"] = "complete"
    result["survives"] = bool(p2p)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repos-dir", type=Path, default=Path.home() / "trackA-poc" / "go-repos")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    recs = [json.loads(l) for l in args.input.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.limit:
        recs = recs[: args.limit]
    done = {}
    if args.resume and args.out.exists():
        for l in args.out.read_text().splitlines():
            if l.strip():
                r = json.loads(l); done[r["id"]] = r
    results = []
    for i, rec in enumerate(recs, 1):
        if rec["id"] in done:
            results.append(done[rec["id"]]); continue
        print(f"[{i}/{len(recs)}] {rec['repo']} ...", flush=True)
        try:
            res = filter_task(rec, args.repos_dir)
        except Exception as exc:  # noqa: BLE001
            res = {"id": rec["id"], "repo": rec["repo"], "stage": "exception", "error": str(exc)[:200], "survives": False}
        results.append(res)
        print(f"      stage={res['stage']} survives={res['survives']} p2p={res.get('pass_to_pass')} cov={res.get('changed_lines_covered')}", flush=True)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n", encoding="utf-8")
    stages = Counter(r["stage"] for r in results)
    print("\n=== Go Track A funnel ===")
    for s, c in stages.most_common():
        print(f"  {s}: {c}")
    print(f"  RUNNABLE: {sum(1 for r in results if r.get('survives'))} (covered: {sum(1 for r in results if r.get('changed_lines_covered') is True)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
