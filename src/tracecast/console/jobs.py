"""In-process job registry for console reruns (threads, single-flight, cancel).

Stage reruns make many provider calls and take minutes, so spend endpoints
return a job id immediately and the UI polls. One job at a time: reruns share
the provider budget and run naming, and a debug console has no need for
parallel spend. The registry is in-memory only -- the durable record of a rerun
is the run's ``console/manifest.json``, which the job runner updates on
completion/failure.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .errors import Conflict, NotFound

#: Job states surfaced to the UI.
_ACTIVE_STATES = ("pending", "running")


class JobCancelled(RuntimeError):
    """Raised inside a job when the user requested cancellation."""


@dataclass
class Job:
    """One background rerun and its observable state."""

    job_id: str
    kind: str
    run_id: str
    base_run_id: str | None
    state: str = "pending"
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    progress_done: int = 0
    progress_total: int | None = None
    progress_message: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def check_cancelled(self) -> None:
        """Cooperative cancel point; call before each provider call."""
        if self.cancel_event.is_set():
            raise JobCancelled(f"job {self.job_id} cancelled")

    def advance(self, message: str | None = None) -> None:
        self.progress_done += 1
        if message is not None:
            self.progress_message = message

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "run_id": self.run_id,
            "base_run_id": self.base_run_id,
            "state": self.state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": {
                "done": self.progress_done,
                "total": self.progress_total,
                "message": self.progress_message,
            },
            "error": self.error,
            "result": self.result,
        }


class JobRegistry:
    """Submit/inspect/cancel console jobs; enforces single-flight."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()

    def submit(
        self,
        *,
        kind: str,
        run_id: str,
        base_run_id: str | None,
        target: Callable[[Job], dict[str, Any]],
        progress_total: int | None = None,
    ) -> Job:
        """Start ``target(job)`` on a daemon thread; its return dict becomes ``result``."""
        with self._lock:
            active = [job for job in self._jobs.values() if job.state in _ACTIVE_STATES]
            if active:
                raise Conflict(
                    f"job {active[0].job_id} ({active[0].kind}) is still {active[0].state}; "
                    "one console job at a time"
                )
            job = Job(
                job_id=f"job-{uuid.uuid4().hex[:12]}",
                kind=kind,
                run_id=run_id,
                base_run_id=base_run_id,
                created_at=_now(),
                progress_total=progress_total,
            )
            self._jobs[job.job_id] = job
        thread = threading.Thread(
            target=self._run, args=(job, target), name=job.job_id, daemon=True
        )
        thread.start()
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise NotFound(f"unknown job {job_id!r}")
        return job

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [job.snapshot() for job in jobs]

    def request_cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        job.cancel_event.set()
        return job

    def _run(self, job: Job, target: Callable[[Job], dict[str, Any]]) -> None:
        job.state = "running"
        job.started_at = _now()
        try:
            job.result = target(job)
        except JobCancelled:
            job.state = "cancelled"
        except Exception as exc:  # noqa: BLE001 - job boundary reports, never crashes the app
            job.state = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        else:
            job.state = "succeeded"
        finally:
            job.finished_at = _now()


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()
