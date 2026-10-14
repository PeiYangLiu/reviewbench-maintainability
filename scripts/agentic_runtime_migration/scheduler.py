"""Bound queued heavy jobs so they cannot consume every executor thread."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections import deque


def classified_jobs(function, jobs, workers, is_heavy, heavy_workers=3):
    if workers < 1 or heavy_workers < 1:
        raise ValueError("worker limits must be positive")
    heavy_workers = min(workers, heavy_workers)
    queue = deque((job, bool(is_heavy(job))) for job in jobs)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="runtime-verify") as pool:
        pending = {}
        try:
            while queue or pending:
                heavy = sum(flag for _, flag in pending.values())
                count = len(queue)
                for _ in range(count):
                    if len(pending) == workers:
                        break
                    job, flag = queue.popleft()
                    if flag and heavy >= heavy_workers:
                        queue.append((job, flag))
                        continue
                    pending[pool.submit(function, job)] = (job, flag)
                    heavy += int(flag)
                if not pending:
                    raise RuntimeError("no eligible jobs in scheduler")
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                results = [(future, future.result()) for future in done]
                for future, result in results:
                    job, _ = pending.pop(future)
                    yield job, result
        finally:
            for future in pending:
                future.cancel()
