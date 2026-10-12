"""One background run-then-publish job per ISO week."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from groww_pulse.model import ModelClient
from groww_pulse.pipeline import STATUS_READY, publish_week, run_week
from groww_pulse.publish.mcp import McpClient

IDLE = "idle"
RUNNING = "running"
FINISHED = "finished"


class JobBusy(Exception):
    """A run for this week is already in progress."""

    def __init__(self, week: str) -> None:
        super().__init__(f"a run for {week} is already in progress")
        self.week = week


@dataclass(frozen=True)
class JobSnapshot:
    week: str
    state: str
    status: str
    error: str

    def as_dict(self) -> dict[str, str]:
        return {
            "week": self.week,
            "state": self.state,
            "status": self.status,
            "error": self.error,
        }


class JobRunner:
    """Runs ``run_week`` and, when that status is ``ready``, ``publish_week``."""

    def __init__(
        self,
        config_path: Path | str,
        *,
        runs_dir: Path | str | None = None,
        model_client: ModelClient | None = None,
        mcp_client: McpClient | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.runs_dir = None if runs_dir is None else Path(runs_dir)
        self.model_client = model_client
        self.mcp_client = mcp_client
        self._lock = threading.Lock()
        self._jobs: dict[str, JobSnapshot] = {}

    def snapshot(self, week: str) -> JobSnapshot:
        with self._lock:
            current = self._jobs.get(week)
            if current is None:
                return JobSnapshot(week=week, state=IDLE, status="", error="")
            return current

    def running(self, week: str) -> bool:
        return self.snapshot(week).state == RUNNING

    def start(self, week: str) -> JobSnapshot:
        """Start a job. Rejects a second start for the same week while it runs."""
        with self._lock:
            current = self._jobs.get(week)
            if current is not None and current.state == RUNNING:
                raise JobBusy(week)
            started = JobSnapshot(week=week, state=RUNNING, status="", error="")
            self._jobs[week] = started
        threading.Thread(target=self._execute, args=(week,), daemon=True).start()
        return started

    def _execute(self, week: str) -> None:
        try:
            result = run_week(
                self.config_path,
                week,
                client=self.model_client,
                runs_dir=self.runs_dir,
            )
            if result.status == STATUS_READY:
                result = publish_week(
                    self.config_path,
                    week,
                    client=self.mcp_client,
                    runs_dir=self.runs_dir,
                )
            self._finish(week, result.status, "")
        except Exception as exc:
            self._finish(week, "failed", str(exc))

    def _finish(self, week: str, status: str, error: str) -> None:
        with self._lock:
            self._jobs[week] = JobSnapshot(
                week=week, state=FINISHED, status=status, error=error
            )
