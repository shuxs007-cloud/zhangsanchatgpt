"""Bounded local jobs over immutable inputs; no browser/network implementation.

Callers explicitly enumerate independent jobs and own distinct output files.
This is not the executor for business writes, DB commits or workbook saving.
"""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import time


def run_local_jobs(jobs, workers=3):
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError("本地线程数必须为1至4的整数")
    if not jobs or any(not isinstance(name, str) or not name or not callable(job)
                       for name, job in jobs.items()):
        raise ValueError("本地任务必须有明确名称及独立处理函数")
    started = time.monotonic()
    actual_workers = min(workers, len(jobs))
    results, timings, error = {}, {}, None

    def measured(job):
        began = time.monotonic()
        return job(), round(time.monotonic() - began, 6)

    with ThreadPoolExecutor(max_workers=actual_workers, thread_name_prefix="opensms-local") as pool:
        pending = {pool.submit(measured, job): name for name, job in jobs.items()}
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                name = pending.pop(future)
                if future.cancelled():
                    continue
                try:
                    results[name], timings[name] = future.result()
                except Exception as exc:
                    if error is None:
                        error = exc
                        # Queued reads need not start after a failure. Running
                        # reads are always drained before leaving this barrier.
                        for queued in pending:
                            queued.cancel()
    if error is not None:
        raise error
    return ({name: results[name] for name in jobs}, {
        "jobs": list(jobs), "actualWorkers": actual_workers,
        "wallSeconds": round(time.monotonic() - started, 6),
        "jobSeconds": {name: timings[name] for name in jobs},
        "allJoinedBeforeWrite": True, "liveWritesRemainSerial": True,
    })
