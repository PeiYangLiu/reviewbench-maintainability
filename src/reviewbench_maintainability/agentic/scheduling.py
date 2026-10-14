"""Bounded independent jobs; only the coordinator writes aggregate artifacts."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
from typing import Callable, Iterable, Iterator, TypeVar

from .contracts import ContractError

T = TypeVar("T")
R = TypeVar("R")


def worker_count(config: dict, args) -> int:
    execution = config.get("execution", {})
    if not isinstance(execution, dict):
        raise ContractError("execution must be a mapping")
    count = getattr(args, "workers", None)
    if count is None:
        count = execution.get("workers", 1)
    if type(count) is not int or count <= 0:
        raise ContractError("workers must be a positive integer")
    return count


def completed_jobs(function: Callable[[T], R], jobs: Iterable[T], workers: int) -> Iterator[tuple[T, R]]:
    """Yield completed jobs without submitting the entire matrix speculatively.

    The coordinator owns result aggregation, so worker threads never race on
    JSONL files. At most ``workers`` futures run or wait in the executor. A
    failed job stops new submissions; already running jobs finish their own
    durable per-task checkpoints before the error propagates.
    """
    if type(workers) is not int or workers <= 0:
        raise ContractError("workers must be a positive integer")
    if workers == 1:
        for job in jobs:
            yield job, function(job)
        return
    remaining = iter(jobs)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="reviewbench") as executor:
        pending = {}

        def fill():
            while len(pending) < workers:
                try:
                    job = next(remaining)
                except StopIteration:
                    return
                pending[executor.submit(function, job)] = job

        fill()
        try:
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                # Resolve all failures before scheduling more work.
                results = [(future, future.result()) for future in done]
                for future, result in results:
                    yield pending.pop(future), result
                fill()
        finally:
            for future in pending:
                future.cancel()


@contextmanager
def output_lock(root: Path):
    """Reject competing CLI writers instead of duplicating/resampling episodes.

    Keep the lock inode after release: unlinking a flock file creates a window
    in which two processes can lock different inodes for the same output path.
    """
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".agentic-command.lock"
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ContractError(f"another command is using this output directory: {root}") from exc
        try:
            stream.seek(0)
            stream.truncate()
            stream.write(f"pid={os.getpid()}\n")
            stream.flush()
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
