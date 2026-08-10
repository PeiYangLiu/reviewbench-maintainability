#!/usr/bin/env python3
"""Build Git caches containing only exact task commits, trees, and blobs."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any


MARKER = ".reviewbench-minimal-cache.json"
CACHE_REF = "refs/heads/reviewbench-cache"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--source-cache", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def run(
    command: list[str],
    cwd: Path | None = None,
    input_text: str | None = None,
) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"{' '.join(command)} failed: {completed.stderr[-2000:]}"
        )
    return completed.stdout


def repo_slug(repo: str) -> str:
    return repo.replace("/", "__")


def task_fingerprint(tasks: list[dict[str, Any]]) -> str:
    payload = [
        {
            "id": task["id"],
            "repo": task["repo"],
            "start_sha": task["start_sha"],
            "path": str((task.get("locality") or {}).get("path") or ""),
        }
        for task in sorted(tasks, key=lambda item: item["id"])
    ]
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def object_ids(source: Path, starts: set[str]) -> set[str]:
    result = set(starts)
    for start in sorted(starts):
        result.add(
            run(
                ["git", "rev-parse", f"{start}^{{tree}}"],
                source,
            ).strip()
        )
        output = run(
            ["git", "ls-tree", "-r", "-t", start],
            source,
        )
        for line in output.splitlines():
            metadata, _, _ = line.partition("\t")
            _, object_type, object_id = metadata.split()
            if object_type in {"tree", "blob"}:
                result.add(object_id)
    return result


def write_exact_pack(source: Path, destination: Path, ids: set[str]) -> None:
    pack = subprocess.Popen(
        ["git", "pack-objects", "--stdout"],
        cwd=source,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    assert pack.stdin is not None
    assert pack.stdout is not None
    index = subprocess.Popen(
        ["git", "--git-dir", str(destination), "index-pack", "--stdin"],
        stdin=pack.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    pack.stdout.close()
    pack.stdin.write(
        "".join(f"{object_id}\n" for object_id in sorted(ids)).encode()
    )
    pack.stdin.close()
    index_stdout, index_stderr = index.communicate()
    pack_stderr = pack.stderr.read() if pack.stderr else b""
    pack_returncode = pack.wait()
    if pack_returncode or index.returncode:
        raise RuntimeError(
            "Git object packing failed: "
            f"pack={pack_returncode} index={index.returncode}\n"
            f"{pack_stderr[-2000:].decode(errors='replace')}\n"
            f"{index_stderr[-2000:].decode(errors='replace')}"
        )


def validate_cache(
    destination: Path,
    tasks: list[dict[str, Any]],
) -> None:
    for task in tasks:
        start = task["start_sha"]
        path = str((task.get("locality") or {}).get("path") or "")
        run(
            [
                "git",
                "-C",
                str(destination),
                "cat-file",
                "-e",
                f"{start}^{{commit}}",
            ]
        )
        if path:
            run(
                [
                    "git",
                    "-C",
                    str(destination),
                    "cat-file",
                    "-e",
                    f"{start}:{path}",
                ]
            )


def ensure_cache_ref(destination: Path, starts: set[str]) -> None:
    """Keep the repository recognizable after empty directories reach Blob."""
    reference = destination / ".git" / CACHE_REF
    reference.parent.mkdir(parents=True, exist_ok=True)
    reference.write_text(f"{min(starts)}\n", encoding="ascii")


def build_repo(
    repo: str,
    tasks: list[dict[str, Any]],
    source_cache: Path,
    out_dir: Path,
) -> dict[str, Any]:
    source = source_cache / repo_slug(repo)
    destination = out_dir / repo_slug(repo)
    fingerprint = task_fingerprint(tasks)
    marker = destination / MARKER
    if marker.is_file():
        existing = json.loads(marker.read_text(encoding="utf-8"))
        if existing.get("task_fingerprint") == fingerprint:
            ensure_cache_ref(
                destination,
                {str(task["start_sha"]) for task in tasks},
            )
            validate_cache(destination, tasks)
            return {**existing, "status": "reused"}

    if not source.is_dir():
        raise FileNotFoundError(f"missing source repository cache: {source}")
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "init", str(temporary)])
    starts = {str(task["start_sha"]) for task in tasks}
    ids = object_ids(source, starts)
    write_exact_pack(source, temporary / ".git", ids)
    ensure_cache_ref(temporary, starts)
    validate_cache(temporary, tasks)
    record = {
        "repo": repo,
        "tasks": len(tasks),
        "start_commits": len(starts),
        "objects": len(ids),
        "task_fingerprint": fingerprint,
        "bytes": sum(
            path.stat().st_size
            for path in temporary.rglob("*")
            if path.is_file()
        ),
    }
    (temporary / MARKER).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if destination.exists():
        shutil.rmtree(destination)
    temporary.replace(destination)
    return {**record, "status": "built"}


def main() -> int:
    args = parse_args()
    tasks = load_jsonl(args.tasks)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        grouped.setdefault(str(task["repo"]), []).append(task)

    records = []
    errors = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                build_repo,
                repo,
                repo_tasks,
                args.source_cache,
                args.out_dir,
            ): repo
            for repo, repo_tasks in grouped.items()
        }
        for future in as_completed(futures):
            repo = futures[future]
            try:
                record = future.result()
                records.append(record)
                print(
                    f"[{record['status']}] {repo}: "
                    f"{record['tasks']} tasks, "
                    f"{record['bytes'] / 1024**2:.1f} MiB",
                    flush=True,
                )
            except Exception as exc:
                errors.append({"repo": repo, "error": str(exc)[:2000]})
                print(f"[failed] {repo}: {exc}", flush=True)

    report = {
        "schema_version": "reviewbench-minimal-repo-cache/v1",
        "tasks": len(tasks),
        "repositories": len(grouped),
        "completed_repositories": len(records),
        "failed_repositories": len(errors),
        "bytes": sum(record["bytes"] for record in records),
        "records": sorted(records, key=lambda item: item["repo"]),
        "errors": sorted(errors, key=lambda item: item["repo"]),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "tasks",
                    "repositories",
                    "completed_repositories",
                    "failed_repositories",
                    "bytes",
                )
            },
            indent=2,
        )
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
