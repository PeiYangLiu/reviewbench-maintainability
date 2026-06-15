"""End-to-end ReviewBench-Maintainability evaluation harness (PoC).

For a runnable Track A task, evaluate a candidate solution through the gate
sequence and emit Resolved@1:

  gate0 apply        - candidate writes valid file content
  gate1 correctness  - the task's stable PASS_TO_PASS tests still all pass
  gate2 scope        - only allowed source files changed, within a size limit
  gate3 maintainability - the calibrated judge says the concern is addressed

Resolved = correctness AND scope AND maintainability. Correctness is necessary
but not the maintainability signal (you cannot improve maintainability by
breaking tests); the judge is the actual signal.

Candidate solvers:
  - golden : the human reference (target version of the changed source files)  -> expect Resolved
  - empty  : no change                                                          -> expect NOT resolved
  - model  : an LLM produces the revised file from (concern + before code)      -> real verdict

Reuses trackA_filter (repo setup, env, PASS_TO_PASS) and judge_prototype (judge).
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import trackA_filter as ta
import trackA_go as tg
import judge_prototype as jp


SCOPE_MAX_CHANGED_LINES = 400
SOLVER_INSTRUCTIONS = """You are an expert software engineer addressing a code reviewer's MAINTAINABILITY request.
You are given the reviewer's concern and the FULL current content of one source file.
Edit the file to address the concern while PRESERVING ALL EXISTING BEHAVIOR.

Return ONE OR MORE edits, and NOTHING ELSE, each in this EXACT format:
<<<<<<< SEARCH
（the exact lines currently in the file）
=======
（the replacement lines）
>>>>>>> REPLACE

The SEARCH block must match the current file content EXACTLY (whitespace included).
Make the smallest change that addresses the concern. Do not touch unrelated code."""

SR_BLOCK_RE = re.compile(r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE", re.DOTALL)
FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\n(.*)\n```\s*$", re.DOTALL)


def _lenient_find(content_lines: list[str], search_lines: list[str]) -> int:
    """Return the start index in content_lines where search_lines match ignoring
    trailing whitespace, or -1. First occurrence wins."""
    norm = [ln.rstrip() for ln in content_lines]
    want = [ln.rstrip() for ln in search_lines]
    if not want:
        return -1
    for i in range(len(norm) - len(want) + 1):
        if norm[i:i + len(want)] == want:
            return i
    return -1


def apply_search_replace(original: str, text: str) -> tuple[str | None, str]:
    blocks = SR_BLOCK_RE.findall(text)
    if not blocks:
        return None, "no SEARCH/REPLACE blocks"
    content = original
    for i, (search, replace) in enumerate(blocks):
        if search in content:
            content = content.replace(search, replace, 1)
            continue
        # Fallback: tolerate trailing-whitespace drift in the model's SEARCH block.
        c_lines, s_lines = content.split("\n"), search.split("\n")
        idx = _lenient_find(c_lines, s_lines)
        if idx < 0:
            return None, f"SEARCH block {i+1} not found in file"
        content = "\n".join(c_lines[:idx] + replace.split("\n") + c_lines[idx + len(s_lines):])
    if content == original:
        return None, "no-op (search==replace)"
    return content, "ok"


def git_show(repo_dir: Path, ref: str, path: str) -> str | None:
    import subprocess
    p = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=repo_dir, capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def run_nodes(runner: str, repo_dir: Path, nodeids: list[str]) -> dict[str, str]:
    by_file: dict[str, list[str]] = defaultdict(list)
    for n in nodeids:
        by_file[n.split("::")[0]].append(n)
    res: dict[str, str] = {}
    for nodes in by_file.values():
        cmd = runner.split() + ["pytest", "-v", "-p", "no:cacheprovider", *nodes]
        _, out = ta.run(cmd, cwd=repo_dir, timeout=300)
        res.update(ta.parse_tests(out))
    return res


def setup_and_p2p(rec: dict[str, Any], repos_dir: Path) -> dict[str, Any]:
    lang = rec.get("inferred_language", "")
    if lang == "Go":
        return setup_go(rec, repos_dir)
    return setup_py(rec, repos_dir)


def correctness(ctx: dict[str, Any], p2p: list[str]) -> dict[str, str]:
    """Run the PASS_TO_PASS tests against the current tree (language-aware)."""
    if ctx["lang"] == "go":
        res, _ = tg.go_test(ctx["repo_dir"], ctx["pkg"])
        return res
    return run_nodes(ctx["runner"], ctx["repo_dir"], p2p)


def stable_pass_go(repo_dir: Path, pkg: str, n: int = 3) -> tuple[set[str], set[str]]:
    """Run the package n times; return (tests passing EVERY run, flaky tests)."""
    runs = [tg.go_test(repo_dir, pkg)[0] for _ in range(n)]
    seen = set().union(*[set(r) for r in runs]) if runs else set()
    stable = {t for t in seen if all(r.get(t) == "PASS" for r in runs)}
    flaky = {t for t in seen if any(r.get(t) == "PASS" for r in runs) and t not in stable}
    return stable, flaky


def setup_go(rec: dict[str, Any], repos_dir: Path) -> dict[str, Any]:
    repo, path = rec["repo"], rec["locality"]["path"]
    start, target = rec["start_sha"], rec["target_sha"]
    if tg.is_go_test(path):
        return {"ok": False, "error": "test_anchor"}
    repo_dir, _ = ta.ensure_clone(repo, repos_dir)
    if not repo_dir:
        return {"ok": False, "error": "clone"}
    for sha in (start, target):
        if ta.git(["fetch", "--quiet", "origin", sha], repo_dir, timeout=600)[0] != 0:
            return {"ok": False, "error": f"fetch {sha[:8]}"}
    ta.git(["reset", "--hard", "--quiet", start], repo_dir)
    ta.git(["clean", "-qfd"], repo_dir)
    if not (repo_dir / path).exists():
        return {"ok": False, "error": "no_source_at_start"}
    pkg = tg.pkg_target(path)
    # Flaky-aware baseline: keep only tests passing in ALL of n runs (P30).
    stable, flaky = stable_pass_go(repo_dir, pkg, n=3)
    if not stable:
        return {"ok": False, "error": "baseline_no_pass"}
    _, names = ta.git(["diff", "--name-only", start, target], repo_dir)
    changed = [n for n in names.splitlines() if n.strip()]
    source_changed = [f for f in changed if f.endswith(".go") and not tg.is_go_test(f)]
    if not source_changed:
        return {"ok": False, "error": "test_only_task"}
    ta.git(["checkout", target, "--", *source_changed], repo_dir)
    stable_gold, flaky_g = stable_pass_go(repo_dir, pkg, n=3)
    p2p = sorted(stable & stable_gold)
    ta.git(["reset", "--hard", "--quiet", start], repo_dir)
    if not p2p:
        return {"ok": False, "error": "no_pass_to_pass"}
    return {"ok": True, "lang": "go", "repo_dir": repo_dir, "pkg": pkg, "p2p": p2p, "flaky_excluded": len(flaky | flaky_g),
            "source_changed": source_changed, "start": start, "target": target, "path": path}


def setup_py(rec: dict[str, Any], repos_dir: Path) -> dict[str, Any]:
    """Set up repo@start, build env, compute the PASS_TO_PASS test set. Leaves tree at start_sha."""
    repo, path = rec["repo"], rec["locality"]["path"]
    start, target = rec["start_sha"], rec["target_sha"]
    proxy = rec.get("correctness_oracle_proxy") or {}
    repo_dir, _ = ta.ensure_clone(repo, repos_dir)
    if not repo_dir:
        return {"ok": False, "error": "clone"}
    for sha in (start, target):
        if ta.git(["fetch", "--quiet", "origin", sha], repo_dir)[0] != 0:
            return {"ok": False, "error": f"fetch {sha[:8]}"}
    ta.git(["reset", "--hard", "--quiet", start], repo_dir)
    ta.git(["clean", "-qfdx", "--exclude=.venv_bench", "--exclude=.venv"], repo_dir)

    pr_tests = [p for p in (proxy.get("test_change_paths") or []) if p.endswith(".py")]
    targets = sorted(set(p for p in pr_tests if (repo_dir / p).exists()) | set(ta.find_existing_tests(repo_dir, path)))
    if not targets:
        return {"ok": False, "error": "no_tests"}
    runner, env_note = ta.build_env(repo_dir)
    if runner is None:
        return {"ok": False, "error": f"env: {env_note}"}

    # Flaky-aware baseline: keep only tests passing in ALL of 3 runs (P30).
    runs = [ta.run_pytest(runner, repo_dir, targets) for _ in range(3)]
    seen = set().union(*[set(r) for r in runs]) if runs else set()
    stable = {t for t in seen if all(r.get(t) == "PASSED" for r in runs)}
    flaky = {t for t in seen if any(r.get(t) == "PASSED" for r in runs) and t not in stable}
    if not stable:
        return {"ok": False, "error": "baseline_no_pass"}
    _, names = ta.git(["diff", "--name-only", start, target], repo_dir)
    changed = [n for n in names.splitlines() if n.strip()]
    test_set = set(proxy.get("test_change_paths") or [])
    source_changed = [f for f in changed if not ta.is_test_path(f) and f not in test_set]
    if not source_changed:
        return {"ok": False, "error": "test_only_task"}
    ta.git(["checkout", target, "--", *source_changed], repo_dir)
    gold_runs = [run_nodes(runner, repo_dir, sorted(stable)) for _ in range(3)]
    stable_gold = {t for t in stable if all(g.get(t) == "PASSED" for g in gold_runs)}
    p2p = sorted(stable & stable_gold)
    ta.git(["reset", "--hard", "--quiet", start], repo_dir)
    if not p2p:
        return {"ok": False, "error": "no_pass_to_pass"}
    return {"ok": True, "lang": "python", "repo_dir": repo_dir, "runner": runner, "p2p": p2p, "flaky_excluded": len(flaky),
            "source_changed": source_changed, "start": start, "target": target, "path": path}


def make_candidate(mode: str, ctx: dict[str, Any], rec: dict[str, Any], endpoint: str) -> tuple[dict[str, str], bool, str]:
    """Return (files_to_write, apply_ok, note)."""
    repo_dir, path = ctx["repo_dir"], ctx["path"]
    if mode == "empty":
        return {}, True, "empty"
    if mode == "golden":
        out = {}
        for f in ctx["source_changed"]:
            content = git_show(repo_dir, ctx["target"], f)
            if content is not None:
                out[f] = content
        return out, True, "golden"
    if mode.startswith("model:"):
        model = mode.split(":", 1)[1]
        before = git_show(repo_dir, ctx["start"], path) or (repo_dir / path).read_text(encoding="utf-8", errors="replace")
        concern = rec.get("task_instruction") or ""
        prompt = f"REVIEWER CONCERN:\n{concern}\n\nFILE: {path}\n\n{before}"
        text = jp.post_responses(endpoint, model, SOLVER_INSTRUCTIONS, prompt)
        new, note = apply_search_replace(before, text)
        if new is None:
            return {}, False, note
        return {path: new}, True, "model"
    raise ValueError(mode)


def evaluate(rec: dict[str, Any], ctx: dict[str, Any], mode: str, endpoint: str, judge_model: str) -> dict[str, Any]:
    repo_dir, p2p = ctx["repo_dir"], ctx["p2p"]
    path = ctx["path"]
    ta.git(["reset", "--hard", "--quiet", ctx["start"]], repo_dir)
    ta.git(["clean", "-qfd", "--exclude=.venv_bench", "--exclude=.venv"], repo_dir)

    res: dict[str, Any] = {"mode": mode}
    try:
        candidate, apply_ok, note = make_candidate(mode, ctx, rec, endpoint)
    except Exception as exc:  # noqa: BLE001
        return {**res, "gate0_apply": False, "error": f"solver: {exc}", "resolved": False}
    res["gate0_apply"] = apply_ok
    res["apply_note"] = note
    if not apply_ok:
        return {**res, "resolved": False}

    for f, content in candidate.items():
        (repo_dir / f).parent.mkdir(parents=True, exist_ok=True)
        (repo_dir / f).write_text(content, encoding="utf-8")

    _, names = ta.git(["diff", "--name-only"], repo_dir)
    changed_files = [n for n in names.splitlines() if n.strip()]
    _, numstat = ta.run(["git", "diff", "--numstat"], cwd=repo_dir)
    changed_lines = sum(int(p[0]) + int(p[1]) for ln in numstat.splitlines() if (p := ln.split()) and p[0].isdigit() and p[1].isdigit())
    allowed = set(ctx["source_changed"]) | {path}
    res["changed_files"] = changed_files
    # The golden reference is the full (possibly multi-file) PR source delta, so it
    # is exempt from the model scope limit; models must stay focused (anti-gaming).
    res["scope_ok"] = (set(changed_files) <= allowed) and (changed_lines <= SCOPE_MAX_CHANGED_LINES) and bool(changed_files)
    scope_gate = True if mode == "golden" else res["scope_ok"]

    pass_token = "PASS" if ctx["lang"] == "go" else "PASSED"
    failed = list(p2p)
    for _ in range(3):  # tolerate flaky-fails: keep retrying still-failing tests
        if not failed:
            break
        correct = correctness(ctx, p2p)
        failed = [t for t in failed if correct.get(t) != pass_token]
    res["correctness_ok"] = not failed
    res["correctness_failed"] = failed[:5]

    # Judge sees ONLY the anchored file's diff (the file the concern is about), so
    # golden's bundled multi-file PR changes don't drown out the relevant change.
    diff = ta.run(["git", "diff", "--", path], cwd=repo_dir)[1]
    before = (git_show(repo_dir, ctx["start"], path) or "")[:2500]
    prompt = jp.build_prompt(rec.get("task_instruction") or "", rec.get("difficulty", {}).get("category", "?"), before, diff or "(no changes)")
    verdict = jp.parse_verdict(jp.post_responses(endpoint, judge_model, jp.JUDGE_INSTRUCTIONS, prompt))
    res["maintainability"] = verdict["addressed"]
    res["judge_reason"] = verdict["reason"]

    res["resolved"] = bool(res["correctness_ok"] and scope_gate and verdict["addressed"] in {"yes", "partial"})
    ta.git(["reset", "--hard", "--quiet", ctx["start"]], repo_dir)
    return res


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", type=Path, required=True, help="Task JSONL (runnable tasks).")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--solvers", nargs="+", default=["golden", "empty", "model:claude-opus-4.7"],
                        help="Each is 'golden', 'empty', or 'model:<model_name>'.")
    parser.add_argument("--endpoint", default="http://127.0.0.1:4143/v1/responses")
    parser.add_argument("--judge-model", default="claude-opus-4.7")
    parser.add_argument("--repos-dir", type=Path, default=Path.home() / "trackA-poc" / "repos")
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    recs = [json.loads(l) for l in args.input.read_text(encoding="utf-8").splitlines() if l.strip()]
    results: list[dict[str, Any]] = []
    done: dict[str, dict[str, Any]] = {}
    if args.resume and args.out.exists():
        for t in json.loads(args.out.read_text()):
            done[t["id"]] = t

    for i, rec in enumerate(recs, 1):
        if rec["id"] in done and len(done[rec["id"]].get("evals", [])) >= len(args.solvers):
            results.append(done[rec["id"]]); continue
        print(f"[{i}/{len(recs)}] {rec['repo']} :: setup ...", flush=True)
        ctx = setup_and_p2p(rec, args.repos_dir)
        if not ctx.get("ok"):
            print(f"    setup failed: {ctx.get('error')}", flush=True)
            results.append({"id": rec["id"], "repo": rec["repo"], "setup_error": ctx.get("error")})
            args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            continue
        print(f"    PASS_TO_PASS={len(ctx['p2p'])}", flush=True)
        task_res = {"id": rec["id"], "repo": rec["repo"], "path": ctx["path"],
                    "tier": rec.get("difficulty", {}).get("tier"), "pass_to_pass": len(ctx["p2p"]), "evals": []}
        for mode in args.solvers:
            print(f"    eval {mode} ...", flush=True)
            try:
                r = evaluate(rec, ctx, mode, args.endpoint, args.judge_model)
            except Exception as exc:  # noqa: BLE001
                r = {"mode": mode, "error": str(exc)[:150], "resolved": False}
            print(f"      apply={r.get('gate0_apply')} correctness={r.get('correctness_ok')} scope={r.get('scope_ok')} maint={r.get('maintainability')} -> RESOLVED={r.get('resolved')}", flush=True)
            task_res["evals"].append(r)
        results.append(task_res)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    leaderboard(results, args.solvers)
    return 0


def leaderboard(results: list[dict[str, Any]], solvers: list[str]) -> None:
    scored = [t for t in results if t.get("evals")]

    def golden_correct(t: dict[str, Any]) -> bool:
        g = [e for e in t["evals"] if e.get("mode") == "golden"]
        return bool(g) and bool(g[0].get("correctness_ok"))

    # A task is VALID only if golden passes its own correctness gate; otherwise the
    # oracle is broken/flaky (e.g. grpc-go concurrency tests) and the task is dropped.
    valid = [t for t in scored if golden_correct(t)]
    print(f"\n=== Leaderboard ===")
    print(f"scored tasks: {len(scored)} | VALID (golden passes correctness): {len(valid)} | dropped(flaky/broken oracle): {len(scored) - len(valid)}")
    for label, pool in (("ALL VALID", valid),):
        if not pool:
            continue
        print(f"\n-- {label} ({len(pool)} tasks) --")
        for mode in solvers:
            resolved = sum(1 for t in pool for e in t["evals"] if e.get("mode") == mode and e.get("resolved"))
            hard_total = sum(1 for t in pool if t.get("tier") == "hard")
            hard_res = sum(1 for t in pool if t.get("tier") == "hard" for e in t["evals"] if e.get("mode") == mode and e.get("resolved"))
            rate = round(100 * resolved / len(pool), 1) if pool else 0
            print(f"  {mode:<28} {resolved:>2}/{len(pool)}  ({rate}%)   hard {hard_res}/{hard_total}")


if __name__ == "__main__":
    raise SystemExit(main())
