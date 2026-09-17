"""Small owner-scoped background job store for long model diagnoses."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time
import uuid
from typing import Any, Callable


@dataclass
class _DiagnosisJob:
    owner_id: str
    fingerprint: str
    runner: Callable[[], dict[str, Any]]
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    state: str = "pending"
    result: dict[str, Any] | None = None
    local_result: dict[str, Any] | None = None
    error: str = ""
    done: threading.Event = field(default_factory=threading.Event)


class DiagnosisJobStore:
    """Keep a bounded set of resumable, session-owned diagnosis jobs."""

    def __init__(self, *, max_jobs: int = 128) -> None:
        self.max_jobs = max(8, int(max_jobs))
        self._lock = threading.RLock()
        self._jobs: dict[str, _DiagnosisJob] = {}

    def submit(
        self,
        owner_id: str,
        fingerprint: str,
        runner: Callable[[], dict[str, Any]],
        local_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        owner = str(owner_id or "")
        key = str(fingerprint or "")
        if not owner or not key:
            raise ValueError("诊断任务必须绑定会话和异常指纹")
        with self._lock:
            for job_id, job in self._jobs.items():
                if (
                    job.owner_id == owner
                    and job.fingerprint == key
                    and job.state in {"pending", "running"}
                ):
                    return self._snapshot(job_id, job)
            job_id = uuid.uuid4().hex
            job = _DiagnosisJob(
                owner_id=owner,
                fingerprint=key,
                runner=runner,
                local_result=dict(local_result) if isinstance(local_result, dict) else None,
            )
            self._jobs[job_id] = job
            self._prune_locked()
            thread = threading.Thread(
                target=self._run,
                args=(job_id, job),
                name=f"afp-diagnosis-{job_id[:8]}",
                daemon=True,
            )
            thread.start()
            return self._snapshot(job_id, job)

    def get(self, owner_id: str, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(str(job_id or ""))
            if job is None or job.owner_id != str(owner_id or ""):
                return None
            return self._snapshot(str(job_id), job)

    def wait_for(
        self,
        owner_id: str,
        job_id: str,
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(str(job_id or ""))
            if job is None or job.owner_id != str(owner_id or ""):
                return None
            done = job.done
        done.wait(max(0.0, float(timeout)))
        return self.get(owner_id, job_id)

    def _run(self, job_id: str, job: _DiagnosisJob) -> None:
        with self._lock:
            if self._jobs.get(job_id) is not job:
                return
            job.state = "running"
            job.started_at = time.time()
        try:
            result = job.runner()
            if not isinstance(result, dict):
                raise TypeError("诊断任务必须返回 JSON 对象")
            with self._lock:
                job.result = dict(result)
                job.state = "success"
        except Exception as exc:
            with self._lock:
                job.error = str(exc) or exc.__class__.__name__
                job.state = "failed"
        finally:
            with self._lock:
                job.finished_at = time.time()
                job.done.set()

    def _snapshot(self, job_id: str, job: _DiagnosisJob) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "fingerprint": job.fingerprint,
            "state": job.state,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "result": dict(job.result) if isinstance(job.result, dict) else None,
            "local_result": dict(job.local_result) if isinstance(job.local_result, dict) else None,
            "phase": "complete" if job.state in {"success", "failed"} else "model_pending",
            "elapsed_seconds": round(
                max(0.0, (job.finished_at or time.time()) - job.created_at), 3
            ),
            "error": job.error,
        }

    def _prune_locked(self) -> None:
        if len(self._jobs) <= self.max_jobs:
            return
        completed = sorted(
            (
                (job.finished_at or job.created_at, job_id)
                for job_id, job in self._jobs.items()
                if job.state in {"success", "failed"}
            )
        )
        for _finished_at, job_id in completed[: max(0, len(self._jobs) - self.max_jobs)]:
            self._jobs.pop(job_id, None)
