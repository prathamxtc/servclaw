"""Atomic JSON persistence for scheduler jobs.

File: workspace/jobs.json
Format: [job_dict, ...]

Write strategy: temp file → rename (POSIX atomic).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_JOBS_FILE = "jobs.json"


class JobStore:
    """Thread-safe JSON store for scheduler jobs."""

    def __init__(self, workspace_dir: Path) -> None:
        self._path = workspace_dir / _JOBS_FILE
        self._lock = threading.Lock()

    # ── Read ───────────────────────────────────────────────────────────────

    def load(self) -> list[dict]:
        with self._lock:
            return self._read()

    def _read(self) -> list[dict]:
        """Unlocked read — caller must hold _lock or be init-only."""
        if not self._path.exists():
            return []
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception:
            logger.exception("[store] failed to read %s", self._path)
            return []

    # ── Write (atomic) ─────────────────────────────────────────────────────

    def save(self, jobs: list[dict]) -> None:
        with self._lock:
            self._write(jobs)

    def _write(self, jobs: list[dict]) -> None:
        """Atomic write: temp file → rename."""
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            fd, tmp = tempfile.mkstemp(
                dir=str(parent), prefix=".jobs_", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(jobs, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp, str(self._path))
        except Exception:
            logger.exception("[store] atomic write failed")
            # Fallback: direct write
            try:
                self._path.write_text(
                    json.dumps(jobs, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
            except Exception:
                logger.exception("[store] fallback write also failed")

    # ── Locked mutation helpers ────────────────────────────────────────────

    def add(self, job: dict) -> None:
        with self._lock:
            jobs = self._read()
            jobs.append(job)
            self._write(jobs)

    def remove(self, job_id: str) -> bool:
        with self._lock:
            jobs = self._read()
            before = len(jobs)
            jobs = [j for j in jobs if j.get("id") != job_id]
            if len(jobs) == before:
                return False
            self._write(jobs)
            return True

    def update(self, job_id: str, patch: dict) -> dict | None:
        """Apply patch dict to a job. Returns updated job or None."""
        with self._lock:
            jobs = self._read()
            target = next((j for j in jobs if j.get("id") == job_id), None)
            if target is None:
                return None
            target.update(patch)
            self._write(jobs)
            return dict(target)

    def exists(self, job_id: str) -> bool:
        with self._lock:
            return any(j.get("id") == job_id for j in self._read())

    def mutate(self, fn) -> None:
        """Run fn(jobs_list) under lock, then persist."""
        with self._lock:
            jobs = self._read()
            fn(jobs)
            self._write(jobs)
