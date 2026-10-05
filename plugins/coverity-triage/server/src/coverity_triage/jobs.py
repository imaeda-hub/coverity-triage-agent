"""Long-running tools within the client's per-call time limit (spec D-83).

The Copilot runtime cancels an MCP tool call after 30 seconds by default, and a plugin's
``mcp.json`` cannot raise it. A long tool therefore runs in a background thread: the call
waits up to :data:`WAIT_SECONDS` and returns the result if it finished, or
``{"status": "running", "job_id": ...}``; the agent then calls ``wait_job`` until it is done.
Jobs live in the server process only; a restarted server reports the job as unknown.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable

WAIT_SECONDS = 20.0


class Job:
    def __init__(self, name: str, func: Callable[..., Any], args: tuple, kwargs: dict):
        self.id = uuid.uuid4().hex[:12]
        self.name = name
        self.key = f"{name}{args!r}{sorted(kwargs.items())!r}"
        self.started = time.monotonic()
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, args=(func, args, kwargs),
                                       name=f"job-{name}-{self.id}", daemon=True)

    def _run(self, func: Callable[..., Any], args: tuple, kwargs: dict) -> None:
        try:
            self.result = func(*args, **kwargs)
        except BaseException as exc:  # handed to the caller of wait()
            self.error = exc
        finally:
            self.done.set()


_jobs: dict[str, Job] = {}
_lock = threading.Lock()


def run(name: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Start ``func`` in the background and wait for it up to WAIT_SECONDS.

    The same call while an identical one is still running joins it instead of starting it
    twice (an agent that calls apply_approvals or verify_run again must not repeat the work).
    """
    job = Job(name, func, args, kwargs)
    with _lock:
        running = next((j for j in _jobs.values() if j.key == job.key and not j.done.is_set()), None)
        if running is None:
            _jobs[job.id] = job
            job.thread.start()
        else:
            job = running
    return _collect(job, WAIT_SECONDS)


def wait(job_id: str, seconds: float | None = None) -> Any:
    """Wait for a job started by :func:`run`; returns its result, or the running status again."""
    with _lock:
        job = _jobs.get(job_id)
    if job is None:
        raise ValueError(f"処理 {job_id} が見つかりません（MCP サーバの再起動などで中断されたか、結果を受け取り済みです）。"
                         "元のツールをもう一度呼んでやり直してください")
    return _collect(job, WAIT_SECONDS if seconds is None else min(seconds, WAIT_SECONDS))


def _collect(job: Job, seconds: float) -> Any:
    if not job.done.wait(seconds):
        elapsed = int(time.monotonic() - job.started)
        return {"status": "running", "job_id": job.id, "job": job.name, "elapsed_seconds": elapsed,
                "next": f"まだ処理中です（{elapsed} 秒経過）。wait_job(\"{job.id}\") を呼んで待ってください。"
                        "結果はそのとき返ります"}
    with _lock:
        _jobs.pop(job.id, None)
    if job.error is not None:
        raise job.error
    return job.result
