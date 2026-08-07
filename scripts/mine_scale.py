#!/usr/bin/env python3
"""Run resumable large-scale ReviewBench candidate mining batches."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any


DEFAULT_ORGS = (
    "apache",
    "denoland",
    "dotnet",
    "elastic",
    "electron",
    "facebook",
    "grafana",
    "grpc",
    "hashicorp",
    "huggingface",
    "JetBrains",
    "kubernetes",
    "laravel",
    "llvm",
    "microsoft",
    "nodejs",
    "open-telemetry",
    "opencv",
    "prometheus",
    "pytest-dev",
    "python",
    "symfony",
    "vercel",
    "WordPress",
)

DEFAULT_QUERIES = (
    "is:pr is:merged refactor test",
    "is:pr is:merged cleanup test",
    "is:pr is:merged simplify test",
    'is:pr is:merged "remove unused"',
    'is:pr is:merged "dead code"',
    "is:pr is:merged duplicate",
    "is:pr is:merged readability",
    "is:pr is:merged maintainability",
    "is:pr is:merged modularity",
    "is:pr is:merged testability",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--target-candidates", type=int, default=10_000)
    parser.add_argument("--limit-per-batch", type=int, default=60)
    parser.add_argument("--max-batches", type=int)
    parser.add_argument("--workers", type=int, default=1, help="Number of batch workers to run concurrently.")
    parser.add_argument("--min-core-remaining", type=int, default=350)
    parser.add_argument("--min-graphql-remaining", type=int, default=150)
    parser.add_argument("--min-search-remaining", type=int, default=2)
    parser.add_argument("--sleep-safety-seconds", type=int, default=90)
    parser.add_argument("--gh", default="gh")
    parser.add_argument(
        "--resume-manifest",
        action="store_true",
        help="Reuse target, batch size, orgs, and queries from out-dir/manifest.json.",
    )
    parser.add_argument(
        "--query-filter",
        help="Regex applied to manifest queries while preserving original batch indexes.",
    )
    parser.add_argument("--org", action="append", dest="orgs")
    parser.add_argument("--query", action="append", dest="queries")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    # A manifest resume must not silently override a worker count the caller
    # asked for on the command line.
    args.workers_explicit = any(
        value == "--workers" or value.startswith("--workers=")
        for value in sys.argv[1:]
    )
    args.limit_per_batch_explicit = any(
        value == "--limit-per-batch" or value.startswith("--limit-per-batch=")
        for value in sys.argv[1:]
    )
    return args


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "batches").mkdir(exist_ok=True)
    (args.out_dir / "snapshots").mkdir(exist_ok=True)

    if args.resume_manifest:
        orgs, queries = apply_resume_manifest(args)
    else:
        orgs = tuple(args.orgs or DEFAULT_ORGS)
        queries = tuple(args.queries or DEFAULT_QUERIES)
    all_batches = [
        {"index": index, "org": org, "query": query}
        for index, (query, org) in enumerate(
            (item for query in queries for item in ((query, org) for org in orgs)),
            start=1,
        )
    ]
    batches = filter_batches(all_batches, args.query_filter)
    manifest = {
        "target_candidates": args.target_candidates,
        "limit_per_batch": args.limit_per_batch,
        "workers": args.workers,
        "orgs": orgs,
        "queries": queries,
        "batch_count": len(all_batches),
    }
    if not args.resume_manifest:
        write_json(args.out_dir / "manifest.json", manifest)

    print(
        f"Prepared {len(batches)} selected batch(es) "
        f"from {len(all_batches)} in {args.out_dir}",
        flush=True,
    )
    print(f"Using {args.workers} worker(s)", flush=True)
    if args.workers > 1:
        run_parallel(args, repo_root, batches)
    else:
        run_serial(args, repo_root, batches)
    write_summary(args.out_dir)
    return 0


def apply_resume_manifest(args: argparse.Namespace) -> tuple[tuple[str, ...], tuple[str, ...]]:
    path = args.out_dir / "manifest.json"
    if not path.is_file():
        raise ValueError(f"Cannot resume: manifest does not exist at {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    orgs = tuple(str(item) for item in manifest.get("orgs") or ())
    queries = tuple(str(item) for item in manifest.get("queries") or ())
    if not orgs or not queries:
        raise ValueError(f"Cannot resume: manifest has no orgs/queries at {path}")
    args.target_candidates = int(manifest["target_candidates"])
    if not getattr(args, "limit_per_batch_explicit", False):
        args.limit_per_batch = int(manifest["limit_per_batch"])
    if not getattr(args, "workers_explicit", False):
        args.workers = int(manifest.get("workers", args.workers))
    return orgs, queries


def filter_batches(
    batches: list[dict[str, Any]],
    query_filter: str | None,
) -> list[dict[str, Any]]:
    if not query_filter:
        return batches
    pattern = re.compile(query_filter, re.IGNORECASE)
    return [batch for batch in batches if pattern.search(str(batch["query"]))]


def run_serial(args: argparse.Namespace, repo_root: Path, batches: list[dict[str, Any]]) -> None:
    completed_this_run = 0
    for batch in batches:
        if args.max_batches is not None and completed_this_run >= args.max_batches:
            print(f"Reached --max-batches={args.max_batches}", flush=True)
            break
        batch_dir = args.out_dir / "batches" / batch_id(batch)
        done_marker = batch_dir / "done.json"
        if done_marker.exists():
            continue
        # Counting scans every batch's candidates.jsonl, so it must happen only
        # for a batch that is actually going to run. Doing it first made a
        # resume over a large finished manifest spend hours re-counting
        # candidates it then skipped.
        current_count = count_unique_candidates(args.out_dir)
        if current_count >= args.target_candidates:
            print(f"Reached target unique candidates: {current_count}", flush=True)
            break

        print(
            f"[batch {batch['index']}/{len(batches)}] org={batch['org']} "
            f"query={batch['query']!r} current_unique={current_count}",
            flush=True,
        )
        status = run_batch(args, repo_root, batch, len(batches))
        print(
            f"[batch {batch['index']}] returncode={status['returncode']} "
            f"candidates={status['candidate_count']} "
            f"unique={status['unique_candidates_after_batch']} "
            f"elapsed={status.get('elapsed_seconds')}s",
            flush=True,
        )
        completed_this_run += 1


def run_parallel(args: argparse.Namespace, repo_root: Path, batches: list[dict[str, Any]]) -> None:
    if args.dry_run:
        selected = select_pending_batches(args, batches)
        for batch in selected:
            batch_dir = args.out_dir / "batches" / batch_id(batch)
            print(" ".join(build_command(args, repo_root, batch, batch_dir)), flush=True)
        return

    pending = select_pending_batches(args, batches)
    print(f"Submitting {len(pending)} pending batch(es)", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        pending_iter = iter(pending)
        futures: dict[Any, dict[str, Any]] = {}

        def submit_next() -> bool:
            try:
                next_batch = next(pending_iter)
            except StopIteration:
                return False
            futures[executor.submit(run_batch, args, repo_root, next_batch, len(batches))] = next_batch
            return True

        for _ in range(args.workers):
            submit_next()

        while futures:
            for future in as_completed(list(futures)):
                batch = futures.pop(future)
                break
            try:
                status = future.result()
            except Exception as exc:  # noqa: BLE001 - keep long mining runs alive.
                batch_dir = args.out_dir / "batches" / batch_id(batch)
                batch_dir.mkdir(parents=True, exist_ok=True)
                status = {
                    "batch": batch,
                    "returncode": -1,
                    "error": repr(exc),
                    "candidate_count": count_jsonl(batch_dir / "candidates.jsonl"),
                    "unique_candidates_after_batch": count_unique_candidates(args.out_dir),
                }
                write_json(batch_dir / "failed.json", status)
            print(
                f"[batch {batch['index']}] returncode={status['returncode']} "
                f"candidates={status['candidate_count']} "
                f"unique={status['unique_candidates_after_batch']} "
                f"elapsed={status.get('elapsed_seconds')}s",
                flush=True,
            )
            if count_unique_candidates(args.out_dir) < args.target_candidates:
                submit_next()


def select_pending_batches(args: argparse.Namespace, batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = []
    if count_unique_candidates(args.out_dir) >= args.target_candidates:
        return selected
    for batch in batches:
        batch_dir = args.out_dir / "batches" / batch_id(batch)
        if (batch_dir / "done.json").exists():
            continue
        selected.append(batch)
        if args.max_batches is not None and len(selected) >= args.max_batches:
            break
    return selected


def run_batch(
    args: argparse.Namespace,
    repo_root: Path,
    batch: dict[str, Any],
    total_batches: int,
) -> dict[str, Any]:
    batch_dir = args.out_dir / "batches" / batch_id(batch)
    done_marker = batch_dir / "done.json"
    if done_marker.exists():
        return {
            "batch": batch,
            "returncode": 0,
            "skipped": True,
            "candidate_count": count_jsonl(batch_dir / "candidates.jsonl"),
            "unique_candidates_after_batch": count_unique_candidates(args.out_dir),
            "elapsed_seconds": 0,
        }
    wait_for_rate_limit(args)
    batch_dir.mkdir(parents=True, exist_ok=True)
    write_json(batch_dir / "batch.json", batch)
    command = build_command(args, repo_root, batch, batch_dir)
    print(
        f"[batch {batch['index']}/{total_batches}] org={batch['org']} "
        f"query={batch['query']!r}",
        flush=True,
    )
    if args.dry_run:
        return {
            "batch": batch,
            "command": command,
            "returncode": 0,
            "candidate_count": 0,
            "unique_candidates_after_batch": count_unique_candidates(args.out_dir),
            "elapsed_seconds": 0,
        }
    started = time.time()
    completed = subprocess.run(
        command,
        cwd=repo_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=child_env(repo_root),
    )
    elapsed = round(time.time() - started, 2)
    status = {
        "batch": batch,
        "command": command,
        "elapsed_seconds": elapsed,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-20_000:],
        "stderr": completed.stderr[-20_000:],
        "candidate_count": count_jsonl(batch_dir / "candidates.jsonl"),
        "unique_candidates_after_batch": count_unique_candidates(args.out_dir),
    }
    marker = done_marker if completed.returncode == 0 else batch_dir / "failed.json"
    write_json(marker, status)
    return status


def build_command(
    args: argparse.Namespace,
    repo_root: Path,
    batch: dict[str, Any],
    batch_dir: Path,
) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "reviewbench_maintainability.build_dataset",
    ]
    # org "*" (or empty) means a query-only search across all of GitHub, which
    # scales far past per-org curation (the query itself carries language:/created:
    # qualifiers). Otherwise restrict the search to the org.
    org = str(batch.get("org") or "").strip()
    if org and org != "*":
        cmd += ["--org", org]
    cmd += [
        "--query",
        str(batch["query"]),
        "--limit",
        str(args.limit_per_batch),
        "--out",
        str(batch_dir / "candidates.jsonl"),
        "--report",
        str(batch_dir / "report.json"),
        "--gh",
        args.gh,
        "--patch-source",
        "pr-files",
        "--addressed-policy",
        "resolved-or-region-touched",
    ]
    return cmd


def wait_for_rate_limit(args: argparse.Namespace) -> None:
    while True:
        limits = rate_limit(args.gh)
        core = limits.get("core", {})
        graphql = limits.get("graphql", {})
        search = limits.get("search", {})
        low_core = int(core.get("remaining", 0)) < args.min_core_remaining
        low_graphql = int(graphql.get("remaining", 0)) < args.min_graphql_remaining
        low_search = int(search.get("remaining", 0)) < args.min_search_remaining
        if not (low_core or low_graphql or low_search):
            return
        reset_at = max(
            int(core.get("reset", 0)) if low_core else 0,
            int(graphql.get("reset", 0)) if low_graphql else 0,
            int(search.get("reset", 0)) if low_search else 0,
        )
        sleep_for = max(60, reset_at - int(time.time()) + args.sleep_safety_seconds)
        print(
            "Rate limit low; sleeping "
            f"{sleep_for}s (core={core.get('remaining')}, "
            f"graphql={graphql.get('remaining')}, search={search.get('remaining')})",
            flush=True,
        )
        time.sleep(sleep_for)


def rate_limit(gh_binary: str) -> dict[str, Any]:
    completed = subprocess.run(
        [gh_binary, "api", "rate_limit"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        print(completed.stderr.strip() or completed.stdout.strip(), file=sys.stderr, flush=True)
        time.sleep(60)
        return {}
    payload = json.loads(completed.stdout)
    return payload.get("resources", {})


def child_env(repo_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    src = str(repo_root / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not existing else f"{src}{os.pathsep}{existing}"
    return env


def batch_id(batch: dict[str, Any]) -> str:
    query = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(batch["query"])).strip("_")
    org = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(batch["org"])).strip("_")
    return f"{int(batch['index']):04d}__{org}__{query[:80]}"


def count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def count_unique_candidates(out_dir: Path) -> int:
    ids = set()
    for path in (out_dir / "batches").glob("*/candidates.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            candidate_id = item.get("id")
            if candidate_id:
                ids.add(candidate_id)
    return len(ids)


def write_summary(out_dir: Path) -> None:
    summary = {
        "unique_candidates": count_unique_candidates(out_dir),
        "completed_batches": len(list((out_dir / "batches").glob("*/done.json"))),
        "failed_batches": len(list((out_dir / "batches").glob("*/failed.json"))),
    }
    write_json(out_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
