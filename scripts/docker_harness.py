"""SWE-bench-style Docker harness: per-repo images to unlock compile-heavy repos.

The direct harness (trackA_filter) cannot build scientific Python repos
(scikit-learn, scipy, ...) which hold many hard tasks. This builds a per-repo
Docker image that pins the toolchain + system build deps + the repo + its Python
deps, so the environment is reproducible and the compile happens ONCE per repo.

Per repo:
  - generate a Dockerfile (base by ecosystem + system build deps + clone + deps),
  - build image `reviewbench/<slug>`.

The correctness oracle then runs INSIDE the container (checkout start_sha,
flaky-aware PASS_TO_PASS, apply candidate, run the package tests). LLM calls
(solver/judge) stay on the host; the container only executes tests.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
from pathlib import Path


def slug(repo: str) -> str:
    return "reviewbench/" + repo.lower().replace("/", "__")


def is_test_path(p: str) -> bool:
    base = p.rsplit("/", 1)[-1]
    return (
        base.startswith("test_")
        or base.endswith("_test.py")
        or "/tests/" in p
        or "/test/" in p
        or p.startswith("tests/")
    )


# System build deps that cover the scientific-Python stack (numpy/scipy/sklearn).
PY_SCI_DOCKERFILE = """FROM python:{py}-slim
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends \\
    git build-essential gfortran libopenblas-dev liblapack-dev pkg-config \\
    python3-dev ninja-build libfreetype6-dev libpng-dev libqhull-dev && rm -rf /var/lib/apt/lists/*
RUN git clone --filter=blob:none https://github.com/{repo}.git /repo
WORKDIR /repo
# Warm common build deps as binary wheels (fast), then compile the project once.
RUN pip install -U pip setuptools wheel \\
 && pip install numpy scipy cython "meson-python>=0.16" ninja pytest pytest-xdist threadpoolctl joblib \\
    matplotlib pandas pillow
RUN pip install -e . --no-build-isolation || pip install -e . || true
"""

GO_DOCKERFILE = """FROM golang:{go}
ENV DEBIAN_FRONTEND=noninteractive
RUN git clone --filter=blob:none https://github.com/{repo}.git /repo
WORKDIR /repo
RUN go mod download || true
"""


def gen_dockerfile(repo: str, lang: str, py: str = "3.12", go: str = "1.22") -> str:
    if lang == "Go":
        return GO_DOCKERFILE.format(repo=repo, go=go)
    return PY_SCI_DOCKERFILE.format(repo=repo, py=py)


def build_image(repo: str, lang: str, py: str = "3.12", build_dir: Path | None = None, timeout: int = 2400) -> tuple[bool, str]:
    image = slug(repo)
    build_dir = build_dir or (Path.home() / "trackA-poc" / "docker" / repo.replace("/", "__"))
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "Dockerfile").write_text(gen_dockerfile(repo, lang, py), encoding="utf-8")
    print(f"building {image} ...", flush=True)
    p = subprocess.run(["docker", "build", "-t", image, str(build_dir)], capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        return False, (p.stdout + p.stderr)[-2000:]
    return True, image


def run_in_container(image: str, script: str, timeout: int = 1800) -> tuple[int, str]:
    """Run a bash script inside a fresh container of `image` (repo at /repo)."""
    p = subprocess.run(
        ["docker", "run", "--rm", "--network=host", "-w", "/repo", image, "bash", "-lc", script],
        capture_output=True, text=True, timeout=timeout,
    )
    return p.returncode, p.stdout + p.stderr


PYTEST_RE = __import__("re").compile(r"^(\S+::\S+?)\s+(PASSED|FAILED|ERROR)\b", __import__("re").M)
ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def container_oracle_py(image: str, rec: dict, test_file: str, timeout: int = 1800) -> dict:
    """Run the flaky-aware PASS_TO_PASS oracle for a Python task INSIDE the container.

    checkout start -> rebuild -> baseline (3x, keep all-pass) -> apply source-gold ->
    rebuild -> gold run -> PASS_TO_PASS = stable-at-start AND pass-at-gold.

    Gold = the PR's *localized* non-test source delta (from the task record), NOT the
    full two-dot ``git diff start target`` which can span hundreds of intervening files
    when start/target are far apart in history.
    """
    import re as _re

    path, start, target = rec["locality"]["path"], rec["start_sha"], rec["target_sha"]
    proxy = rec.get("correctness_oracle_proxy", {})
    src_paths = proxy.get("source_change_paths") or [
        f for f in rec.get("changed_files", []) if f.endswith(".py") and not is_test_path(f)
    ]
    src_arg = " ".join(shlex.quote(p) for p in src_paths)
    pytest_cmd = f"python -m pytest {test_file} -v -p no:cacheprovider --color=no -o console_output_style=classic"
    script = f"""set -e
export COLUMNS=300
cd /repo && git fetch -q origin {start} && git fetch -q origin {target}
git checkout -q -f {start} && git clean -qfdx -e build >/dev/null 2>&1
pip install -e . --no-build-isolation -q >/dev/null 2>&1 || true
for i in 1 2 3; do echo "===RUN$i==="; {pytest_cmd} 2>/dev/null || true; done
echo "===GOLD==="
echo "GOLD_SRC_FILES: {src_arg}"
if [ -n "{src_arg}" ]; then git checkout -q {target} -- {src_arg} >/dev/null 2>&1 || echo "GOLD_CHECKOUT_FAILED"; fi
pip install -e . --no-build-isolation -q >/dev/null 2>&1 || true
{pytest_cmd} 2>/dev/null || true
"""
    code, out = run_in_container(image, script, timeout=timeout)
    out = ANSI_RE.sub("", out)
    blocks = _re.split(r"===RUN\d===|===GOLD===", out)[1:]  # 3 runs + gold
    runs = [dict(PYTEST_RE.findall(b)) for b in blocks]
    if len(runs) < 4:
        return {"ok": False, "error": "incomplete runs", "raw": out[-800:]}
    starts, gold = runs[:3], runs[3]
    seen = set().union(*[set(r) for r in starts])
    stable = {t for t in seen if all(r.get(t) == "PASSED" for r in starts)}
    p2p = sorted(t for t in stable if gold.get(t) == "PASSED")
    flaky = {t for t in seen if not all(r.get(t) == "PASSED" for r in starts)
             and not all(r.get(t) != "PASSED" for r in starts)}
    return {
        "ok": True,
        "collected": len(seen),
        "stable_pass": len(stable),
        "flaky_excluded": len(flaky),
        "gold_src_files": src_paths,
        "gold_collected": len(gold),
        "pass_to_pass": len(p2p),
        "p2p_sample": p2p[:3],
    }


def derive_test_file(rec: dict) -> str | None:
    """Pick the test module that exercises the anchored source file.

    Prefer the PR's test_change_paths entry matching the anchored basename; else fall
    back to the conventional ``<dir>/tests/test_<basename>.py`` layout.
    """
    path = rec["locality"]["path"]
    base = path.rsplit("/", 1)[-1].removesuffix(".py").lstrip("_")
    tests = rec.get("correctness_oracle_proxy", {}).get("test_change_paths", [])
    for t in tests:
        if t.rsplit("/", 1)[-1] in (f"test_{base}.py", f"{base}_test.py"):
            return t
    if tests:
        return tests[0]
    d = path.rsplit("/", 1)[0]
    return f"{d}/tests/test_{base}.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--lang", default="Python")
    parser.add_argument("--py", default="3.12")
    parser.add_argument("--smoke", help="Optional: run this bash smoke command in the built image.")
    parser.add_argument("--oracle-task", type=Path, help="JSON task file: run the in-container PASS_TO_PASS oracle.")
    parser.add_argument("--test-file", help="Test file to run for --oracle-task (auto-derived if omitted).")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    image = slug(args.repo)
    if not args.oracle_task:
        ok, info = build_image(args.repo, args.lang, args.py)
        if not ok:
            print("BUILD FAILED:\n" + info)
            return 1
        print(f"built image: {info}")
        if args.smoke:
            code, out = run_in_container(image, args.smoke)
            print(f"smoke exit={code}\n{out[-2000:]}")
        return 0
    import json as _json
    rec = _json.loads(args.oracle_task.read_text())
    test_file = args.test_file or derive_test_file(rec)
    res = container_oracle_py(image, rec, test_file)
    res["test_file"] = test_file
    print(_json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
