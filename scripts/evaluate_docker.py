"""Docker-backed end-to-end evaluator for compile-heavy repos (scikit-learn, scipy...).

Mirrors evaluate_e2e.py's 4-gate flow (apply -> correctness -> scope -> judge ->
Resolved@1) but runs the repo + tests INSIDE a per-repo Docker image, because the
direct host harness cannot build the scientific-Python stack.

Per task we start ONE persistent container (`docker run -d ... sleep infinity`),
rebuild once at start_sha, then reuse it for every solver: a candidate that only
edits a pure-Python file is served live by the meson-editable install, so no
recompile is needed between solvers (fast). LLM calls (solver + judge) stay on the
host; the container only executes tests.

PASS_TO_PASS = tests stable across 3 runs at start AND passing after the localized
source-gold (the PR's source_change_paths, NOT the far-apart two-dot diff). A task
is VALID only if golden passes its own correctness gate (drops broken/rebased oracles).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

import judge_prototype as jp
import evaluate_e2e as ee
import docker_harness as dh

NEEDS_COMPILE = (".pyx", ".pxd", ".pyx.tp", ".c", ".cpp", ".pxi")


def is_test_path(p: str) -> bool:
    return dh.is_test_path(p)


def docker(args: list[str], timeout: int = 1800, inp: str | None = None) -> tuple[int, str]:
    p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, input=inp)
    return p.returncode, p.stdout + p.stderr


def exec_in(container: str, script: str, timeout: int = 1800) -> tuple[int, str]:
    code, out = docker(["exec", "-w", "/repo", container, "bash", "-lc", script], timeout=timeout)
    return code, dh.ANSI_RE.sub("", out)


def parse_pytest(text: str) -> dict[str, str]:
    return dict(dh.PYTEST_RE.findall(dh.ANSI_RE.sub("", text)))


def needs_compile(files: list[str]) -> bool:
    return any(f.endswith(NEEDS_COMPILE) for f in files)


class Container:
    """A persistent container for one task; rebuild once, reuse across solvers."""

    def __init__(self, image: str):
        self.image = image
        self.name = "rbm_" + uuid.uuid4().hex[:10]

    def __enter__(self) -> "Container":
        docker(["run", "-d", "--network=host", "--name", self.name, self.image, "sleep", "infinity"], timeout=120)
        return self

    def __exit__(self, *exc) -> None:
        docker(["rm", "-f", self.name], timeout=120)

    def sh(self, script: str, timeout: int = 1800) -> tuple[int, str]:
        return exec_in(self.name, script, timeout)

    def write_file(self, path: str, content: str) -> None:
        docker(["exec", "-i", "-w", "/repo", self.name, "bash", "-lc", f"mkdir -p $(dirname {path}) && cat > {path}"],
               timeout=120, inp=content)

    def show(self, ref: str, path: str) -> str | None:
        code, out = self.sh(f"git show {ref}:{path}", timeout=120)
        return out if code == 0 else None


PYTEST = "python -m pytest {tf} -v -p no:cacheprovider --color=no -o console_output_style=classic"


def rebuild(c: Container) -> None:
    # Some sci-Python repos vendor build inputs as git submodules (e.g. scipy's
    # array_api_compat/xsf/unuran); the blob-filtered clone omits them, so init
    # them before the meson-editable build. No-op for repos without submodules.
    # Try the fast path first (reuse the image's build deps via --no-build-isolation);
    # fall back to an isolated build, which installs the exact [build-system] deps the
    # task's commit pins (older commits need a specific numpy/cython, like SWE-bench).
    c.sh("git submodule update --init --recursive >/dev/null 2>&1 || true; "
         "pip install -e . --no-build-isolation -q >/dev/null 2>&1 "
         "|| pip install -e . -q >/dev/null 2>&1 || true", timeout=2400)


def setup_task(c: Container, rec: dict[str, Any], test_file: str) -> dict[str, Any]:
    """Checkout start, rebuild, compute flaky-aware PASS_TO_PASS via localized source-gold."""
    start, target, path = rec["start_sha"], rec["target_sha"], rec["locality"]["path"]
    proxy = rec.get("correctness_oracle_proxy", {})
    src_paths = proxy.get("source_change_paths") or [
        f for f in rec.get("changed_files", []) if f.endswith(".py") and not is_test_path(f)
    ]
    if not src_paths:
        return {"ok": False, "error": "test_only_task"}
    code, _ = c.sh(f"git fetch -q origin {start} && git fetch -q origin {target}", timeout=900)
    if code != 0:
        return {"ok": False, "error": "fetch"}
    c.sh(f"git checkout -q -f {start} && git clean -qfdx -e build >/dev/null 2>&1", timeout=300)
    rebuild(c)
    before = c.show(start, path)
    if before is None:
        return {"ok": False, "error": "no_source_at_start"}

    tf = test_file
    runs = []
    for _ in range(3):
        _, out = c.sh("export COLUMNS=300; " + PYTEST.format(tf=tf) + " 2>/dev/null || true", timeout=1200)
        runs.append(parse_pytest(out))
    seen = set().union(*[set(r) for r in runs]) if runs else set()
    stable = {t for t in seen if all(r.get(t) == "PASSED" for r in runs)}
    if not stable:
        return {"ok": False, "error": "baseline_no_pass"}

    # localized source-gold -> gold PASS_TO_PASS
    c.sh(f"git checkout -q {target} -- {' '.join(src_paths)} >/dev/null 2>&1 || true", timeout=300)
    if needs_compile(src_paths):
        rebuild(c)
    gold_runs = []
    for _ in range(3):
        _, out = c.sh("export COLUMNS=300; " + PYTEST.format(tf=tf) + " 2>/dev/null || true", timeout=1200)
        gold_runs.append(parse_pytest(out))
    stable_gold = {t for t in stable if all(g.get(t) == "PASSED" for g in gold_runs)}
    p2p = sorted(stable_gold)
    # restore start
    c.sh(f"git checkout -q -f {start} >/dev/null 2>&1", timeout=120)
    if needs_compile(src_paths):
        rebuild(c)
    if not p2p:
        return {"ok": False, "error": "no_pass_to_pass"}
    return {"ok": True, "p2p": p2p, "before": before, "src_paths": src_paths,
            "path": path, "start": start, "target": target, "test_file": tf,
            "flaky_excluded": len(seen - stable)}


def make_candidate(c: Container, mode: str, ctx: dict[str, Any], rec: dict[str, Any], endpoint: str) -> tuple[dict[str, str], bool, str]:
    path = ctx["path"]
    if mode == "empty":
        return {}, True, "empty"
    if mode == "golden":
        out = {}
        for f in ctx["src_paths"]:
            content = c.show(ctx["target"], f)
            if content is not None:
                out[f] = content
        return out, True, "golden"
    if mode.startswith("model:"):
        model = mode.split(":", 1)[1]
        before = ctx["before"]
        concern = rec.get("task_instruction") or ""
        prompt = f"REVIEWER CONCERN:\n{concern}\n\nFILE: {path}\n\n{before}"
        # Resolve@1 measures the maintainability fix, not edit-protocol luck. The
        # SEARCH/REPLACE apply is non-deterministic (some samples reflow long lines
        # and fail exact match), so re-sample up to 3x until the edit applies.
        note = "no SEARCH/REPLACE blocks"
        for attempt in range(3):
            text = jp.post_responses(endpoint, model, ee.SOLVER_INSTRUCTIONS, prompt)
            new, note = ee.apply_search_replace(before, text)
            if new is not None:
                return {path: new}, True, f"model(try{attempt+1})"
        return {}, False, note
    raise ValueError(mode)


def evaluate(c: Container, rec: dict[str, Any], ctx: dict[str, Any], mode: str, endpoint: str, judge_model: str) -> dict[str, Any]:
    path, start, target = ctx["path"], ctx["start"], ctx["target"]
    # clean slate at start
    c.sh(f"git checkout -q -f {start} >/dev/null 2>&1 && git clean -qfd >/dev/null 2>&1", timeout=120)

    res: dict[str, Any] = {"mode": mode}
    try:
        candidate, apply_ok, note = make_candidate(c, mode, ctx, rec, endpoint)
    except Exception as exc:  # noqa: BLE001
        return {**res, "gate0_apply": False, "error": f"solver: {exc}"[:150], "resolved": False}
    res["gate0_apply"] = apply_ok
    res["apply_note"] = note
    if not apply_ok:
        return {**res, "resolved": False}

    for f, content in candidate.items():
        c.write_file(f, content)
    if needs_compile(list(candidate)):
        rebuild(c)

    _, names = c.sh("git diff --name-only", timeout=120)
    changed_files = [n for n in names.splitlines() if n.strip()]
    _, numstat = c.sh("git diff --numstat", timeout=120)
    changed_lines = sum(int(p[0]) + int(p[1]) for ln in numstat.splitlines()
                        if (p := ln.split()) and len(p) >= 2 and p[0].isdigit() and p[1].isdigit())
    allowed = set(ctx["src_paths"]) | {path}
    res["scope_ok"] = bool(changed_files) and set(changed_files) <= allowed and changed_lines <= ee.SCOPE_MAX_CHANGED_LINES
    scope_gate = True if mode == "golden" else res["scope_ok"]

    # correctness: run the test file, all PASS_TO_PASS must PASS (retry to tolerate flaky)
    failed = list(ctx["p2p"])
    for _ in range(3):
        if not failed:
            break
        _, out = c.sh("export COLUMNS=300; " + PYTEST.format(tf=ctx["test_file"]) + " 2>/dev/null || true", timeout=1200)
        got = parse_pytest(out)
        failed = [t for t in failed if got.get(t) != "PASSED"]
    res["correctness_ok"] = not failed
    res["correctness_failed"] = failed[:5]

    # judge sees ONLY the anchored file's diff
    _, diff = c.sh(f"git diff -- {path}", timeout=120)
    before = (ctx["before"] or "")[:2500]
    prompt = jp.build_prompt(rec.get("task_instruction") or "", rec.get("difficulty", {}).get("category", "?"),
                             before, diff or "(no changes)")
    verdict = jp.parse_verdict(jp.post_responses(endpoint, judge_model, jp.JUDGE_INSTRUCTIONS, prompt))
    res["maintainability"] = verdict["addressed"]
    res["judge_reason"] = verdict["reason"]
    res["resolved"] = bool(res["correctness_ok"] and scope_gate and verdict["addressed"] in {"yes", "partial"})
    c.sh(f"git checkout -q -f {start} >/dev/null 2>&1 && git clean -qfd >/dev/null 2>&1", timeout=120)
    return res


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", type=Path, required=True, help="Task JSONL.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--solvers", nargs="+",
                        default=["golden", "empty", "model:gpt-5.5", "model:claude-opus-4.8"])
    parser.add_argument("--endpoint", default="http://127.0.0.1:4143/v1/responses")
    parser.add_argument("--judge-model", default="claude-opus-4.7")
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
        image = dh.slug(rec["repo"])
        test_file = dh.derive_test_file(rec)
        print(f"[{i}/{len(recs)}] {rec['repo']} :: {rec['locality']['path']} (tf={test_file})", flush=True)
        with Container(image) as c:
            ctx = setup_task(c, rec, test_file)
            if not ctx.get("ok"):
                print(f"    setup failed: {ctx.get('error')}", flush=True)
                results.append({"id": rec["id"], "repo": rec["repo"], "path": rec["locality"]["path"],
                                "tier": rec.get("difficulty", {}).get("tier"), "setup_error": ctx.get("error")})
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
                continue
            print(f"    PASS_TO_PASS={len(ctx['p2p'])} flaky_excluded={ctx['flaky_excluded']}", flush=True)
            task_res = {"id": rec["id"], "repo": rec["repo"], "path": ctx["path"],
                        "tier": rec.get("difficulty", {}).get("tier"), "pass_to_pass": len(ctx["p2p"]), "evals": []}
            for mode in args.solvers:
                try:
                    r = evaluate(c, rec, ctx, mode, args.endpoint, args.judge_model)
                except Exception as exc:  # noqa: BLE001
                    r = {"mode": mode, "error": str(exc)[:150], "resolved": False}
                print(f"    {mode:<22} apply={r.get('gate0_apply')} correct={r.get('correctness_ok')} "
                      f"scope={r.get('scope_ok')} maint={r.get('maintainability')} -> RESOLVED={r.get('resolved')}", flush=True)
                task_res["evals"].append(r)
            results.append(task_res)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    ee.leaderboard(results, args.solvers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
