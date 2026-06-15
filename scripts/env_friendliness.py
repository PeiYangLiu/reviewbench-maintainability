"""Environment-friendliness filter for Track A task selection (Option B).

Track A needs a runnable, reproducible test environment. The PoC showed the
bottleneck is environment setup, and that the popular Python repos in our pool
are exactly the compile-heavy ones (scikit-learn, scipy, conda, ...). This
script classifies each candidate repo by how cheaply its tests can be run:

- Python: "Track-A-ready now" iff it is pip/uv-installable with NO compiled
  extension (no Cython/C/C++/Fortran/CUDA build, no meson/CMake, no torch-class
  heavy deps) -- runnable with the existing pytest harness.
- Go / Rust / TypeScript / JavaScript: "friendly ecosystem" (go test / cargo
  test / npm test are reproducible) -- runnable once a per-ecosystem harness
  exists.
- Everything else: lower priority.

Signals: GitHub languages API (compiled-language byte fraction) + root build
markers (meson.build, CMakeLists.txt, setup.py Extension) + heavy deps in
pyproject/setup. Validated against PoC ground truth (ansible friendly;
sklearn/scipy/conda/dask/diffusers not).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import enrich_cases  # reuse gh_json


# Compiled languages whose presence in a *Python* repo implies a native build step.
NATIVE_LANGS = {"C", "C++", "Cython", "Fortran", "FORTRAN", "Cuda", "CUDA", "Assembly", "Objective-C", "Objective-C++"}
FRIENDLY_ECOSYSTEMS = {"Go", "Rust", "TypeScript", "JavaScript"}
HEAVY_DEPS = ("torch", "pytorch", "torchvision", "tensorflow", "keras", "transformers", "jax", "jaxlib",
              "cupy", "numba", "pyarrow", "scipy", "scikit-learn", "ray", "menuinst", "pycosat", "conda-build")
# Repos known (from the PoC or common knowledge) to need bespoke/heavy environments
# despite looking pure-Python by language stats (compiled build deps, torch tests, huge dep trees).
CURATED_HEAVY = ("conda/conda", "conda/conda-build", "huggingface/diffusers", "huggingface/transformers",
                 "ray-project/ray", "apache/airflow", "home-assistant/core", "keras-team", "tensorflow/",
                 "pytorch/", "numpy/numpy", "pandas-dev", "matplotlib/matplotlib", "modin-project")
NATIVE_BUILD_FILES = ("meson.build", "CMakeLists.txt", "binding.gyp", "Cargo.toml")  # Cargo at py-root => pyo3/maturin


def get_languages(repo: str) -> dict[str, int]:
    try:
        data = enrich_cases.gh_json(f"/repos/{repo}/languages")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def root_listing(repo: str) -> list[str]:
    try:
        data = enrich_cases.gh_json(f"/repos/{repo}/contents")
        return [item.get("name", "") for item in data] if isinstance(data, list) else []
    except Exception:
        return []


def fetch_text(repo: str, path: str) -> str:
    try:
        import base64
        raw = enrich_cases.gh_json(f"/repos/{repo}/contents/{path}", jq=".content")
        return base64.b64decode(raw).decode("utf-8", "replace") if raw else ""
    except Exception:
        return ""


def classify_repo(repo: str, primary_lang: str) -> dict[str, Any]:
    langs = get_languages(repo)
    total = sum(langs.values()) or 1
    native_bytes = sum(b for l, b in langs.items() if l in NATIVE_LANGS)
    native_frac = round(native_bytes / total, 3)
    python_frac = round(langs.get("Python", 0) / total, 3)
    primary = max(langs, key=langs.get) if langs else (primary_lang or "?")

    info: dict[str, Any] = {
        "repo": repo,
        "primary_lang": primary,
        "python_frac": python_frac,
        "native_frac": native_frac,
        "languages": dict(sorted(langs.items(), key=lambda x: -x[1])[:5]),
    }

    # Python repos: ready now iff no native build + no heavy deps.
    if primary == "Python" or primary_lang == "Python":
        reasons = []
        if any(h in repo for h in CURATED_HEAVY):
            reasons.append("curated_heavy_repo")
        root = root_listing(repo)
        build_markers = [f for f in NATIVE_BUILD_FILES if f in root]
        has_cython = langs.get("Cython", 0) > 0
        heavy = []
        for cfg in ("pyproject.toml", "setup.py", "setup.cfg"):
            if cfg in root:
                txt = fetch_text(repo, cfg).lower()
                if "ext_modules" in txt or "extension(" in txt or "cythonize" in txt or "pybind11" in txt:
                    build_markers.append(f"{cfg}:ext")
                heavy += [d for d in HEAVY_DEPS if d in txt]
                break
        if native_frac >= 0.05:
            reasons.append(f"native_frac={native_frac}")
        if has_cython:
            reasons.append("cython")
        if build_markers:
            reasons.append("build:" + ",".join(build_markers))
        if heavy:
            reasons.append("heavy_dep:" + ",".join(sorted(set(heavy))))
        ready = not reasons
        info.update({"ecosystem": "Python", "track_a_ready": ready, "friendly": ready, "reasons": reasons})
        return info

    # Non-Python: reproducible test tooling ecosystems.
    if primary in FRIENDLY_ECOSYSTEMS or primary_lang in FRIENDLY_ECOSYSTEMS:
        eco = primary if primary in FRIENDLY_ECOSYSTEMS else primary_lang
        info.update({"ecosystem": eco, "track_a_ready": False, "friendly": True,
                     "reasons": [f"{eco}: reproducible tooling, harness not built yet"]})
        return info

    info.update({"ecosystem": primary, "track_a_ready": False, "friendly": False,
                 "reasons": [f"{primary}: ecosystem not prioritized"]})
    return info


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", type=Path, required=True, help="Valid tasks JSONL (combined_v3).")
    parser.add_argument("--out-repos", type=Path, required=True, help="Per-repo classification JSON.")
    parser.add_argument("--out-tasks", type=Path, help="Tasks annotated with env info JSONL.")
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    tasks = [json.loads(l) for l in args.input.read_text(encoding="utf-8").splitlines() if l.strip()]
    tasks = [t for t in tasks if t.get("difficulty", {}).get("tier") != "unknown"]

    repo_lang = {}
    for t in tasks:
        repo_lang.setdefault(t["repo"], t.get("inferred_language", "?"))

    cache: dict[str, Any] = {}
    if args.resume and args.out_repos.exists():
        cache = {r["repo"]: r for r in json.loads(args.out_repos.read_text())}

    repos: list[dict[str, Any]] = []
    for i, (repo, lang) in enumerate(sorted(repo_lang.items()), 1):
        if repo in cache:
            repos.append(cache[repo]); continue
        print(f"[{i}/{len(repo_lang)}] {repo} ...", flush=True)
        info = classify_repo(repo, lang)
        repos.append(info)
        args.out_repos.parent.mkdir(parents=True, exist_ok=True)
        args.out_repos.write_text(json.dumps(repos, ensure_ascii=False, indent=2), encoding="utf-8")

    by_repo = {r["repo"]: r for r in repos}
    if args.out_tasks:
        with args.out_tasks.open("w", encoding="utf-8") as fh:
            for t in tasks:
                t["env"] = {k: by_repo[t["repo"]].get(k) for k in ("ecosystem", "track_a_ready", "friendly", "reasons")}
                fh.write(json.dumps(t, ensure_ascii=False) + "\n")

    report(tasks, by_repo)
    return 0


def report(tasks: list[dict[str, Any]], by_repo: dict[str, Any]) -> None:
    def tier(t):
        return t["difficulty"]["tier"]

    py_ready = [t for t in tasks if by_repo[t["repo"]].get("ecosystem") == "Python" and by_repo[t["repo"]].get("track_a_ready")]
    friendly = [t for t in tasks if by_repo[t["repo"]].get("friendly")]
    print("\n=== Env-friendliness report ===")
    print(f"valid tasks: {len(tasks)} across {len(by_repo)} repos")
    print(f"\nPython Track-A-READY-NOW tasks: {len(py_ready)}  (by tier: {dict(Counter(tier(t) for t in py_ready))})")
    ready_repos = sorted({t['repo'] for t in py_ready})
    print(f"  ready Python repos ({len(ready_repos)}): {', '.join(ready_repos)}")
    print(f"\nFriendly-ecosystem tasks (runnable with a harness): {len(friendly)}")
    eco_tier = defaultdict(Counter)
    for t in friendly:
        eco_tier[by_repo[t['repo']]['ecosystem']][tier(t)] += 1
    for eco, ctr in sorted(eco_tier.items(), key=lambda x: -sum(x[1].values())):
        print(f"  {eco:<12} total={sum(ctr.values()):<4} hard={ctr['hard']} medium={ctr['medium']} easy={ctr['easy']}")
    # validation against PoC
    print("\nPoC ground-truth check:")
    for repo, expect in [("ansible/metrics-service", True), ("scikit-learn/scikit-learn", False), ("scipy/scipy", False), ("conda/conda", False), ("dask/dask", False), ("huggingface/diffusers", False)]:
        r = by_repo.get(repo)
        if r:
            got = r.get("track_a_ready")
            ok = "OK" if got == expect else "MISMATCH"
            print(f"  {repo:<28} ready={got} expected={expect} [{ok}] reasons={r.get('reasons')}")


if __name__ == "__main__":
    raise SystemExit(main())
