"""Background job scheduler for Servclaw.

Two job kinds:
  direct_message — Pre-defined text sent to channels at fire time. No LLM.
  agent_task     — LLM agent brain invoked in background. Can use tools.

Schedule modes:
  heartbeat  — repeats every N minutes
  cron       — cron expression
  run_once   — single fire at a specific ISO datetime

Job schema (one entry in jobs.json):
{
    "id": "job_<hex>",
    "kind": "direct_message" | "agent_task",
    "description": "human-friendly description",

    // Schedule
    "schedule_mode": "heartbeat" | "cron" | "run_once",
    "interval_minutes": 15,       // heartbeat
    "cron": "*/30 * * * *",       // cron
    "run_at": "<iso>",            // run_once

    "cancel_after_run": false,

    // direct_message fields:
    "message": "Hit the gym!",
    "channels": ["telegram"],     // empty = all

    // agent_task fields:
    "command": "docker ps ...",   // optional pre-run shell command
    "context_mode": "contextual" | "isolated",

    // State
    "created_at": "<iso>",
    "next_run_at": "<iso>",
    "last_run_at": null,
    "run_count": 0
}
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from zoneinfo import ZoneInfo  # Python 3.9+ stdlib
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

from croniter import croniter

from servclaw_config import get_user_timezone, load_config
from scheduler.executor import execute_agent_task, execute_direct_message
from scheduler.store import JobStore

logger = logging.getLogger(__name__)

_TICK_SECONDS = 30
_MIN_INTERVAL = 1
_DEFAULT_INTERVAL = 15
_SCHEDULE_MODES = {"heartbeat", "cron", "run_once"}
_JOB_KINDS = {"direct_message", "agent_task"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _resolve_tz(tz_name: str | None) -> "ZoneInfo | None":
    """Return a ZoneInfo for the given IANA name, or None if empty/invalid."""
    if not tz_name:
        return None
    try:
        return ZoneInfo(tz_name.strip())
    except Exception:
        logger.warning("[scheduler] unknown timezone '%s' — defaulting to UTC", tz_name)
        return None


def _parse_iso(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _normalize_mode(value: Any) -> str:
    mode = str(value or "heartbeat").strip().lower()
    return mode if mode in _SCHEDULE_MODES else "heartbeat"


def _normalize_kind(value: Any) -> str:
    kind = str(value or "agent_task").strip().lower()
    return kind if kind in _JOB_KINDS else "agent_task"


class Scheduler:
    """Background thread that fires jobs when due."""

    def __init__(self, agent: Any, workspace_dir: Path) -> None:
        self._agent = agent
        self._store = JobStore(workspace_dir)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Channel push handlers — registered by each channel bot.
        self._push_handlers: list[Callable[[str], None]] = []

    # ── Public API ─────────────────────────────────────────────────────────

    def register_push_handler(self, handler: Callable[[str], None]) -> None:
        self._push_handlers.append(handler)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="scheduler",
        )
        self._thread.start()
        logger.info("[scheduler] started")
        print("[scheduler] background thread started", flush=True)

    def stop(self) -> None:
        self._stop_event.set()

    # ── Job CRUD ───────────────────────────────────────────────────────────

    def add_job(
        self,
        kind: str,
        description: str,
        schedule_mode: str = "heartbeat",
        interval_minutes: int = _DEFAULT_INTERVAL,
        cron: str | None = None,
        run_at: str | None = None,
        cancel_after_run: bool | None = None,
        # direct_message fields
        message: str | None = None,
        channels: list[str] | None = None,
        # agent_task fields
        command: str | None = None,
        context_mode: str | None = None,
        # timezone for cron/run_once interpretation
        timezone_name: str | None = None,
    ) -> dict:
        kind = _normalize_kind(kind)
        mode = _normalize_mode(schedule_mode)
        # Auto-load user timezone from config when not explicitly supplied.
        # This means "remind me at 9" always means 9 in the user's local time.
        if not timezone_name:
            timezone_name = get_user_timezone(load_config())
        tz = _resolve_tz(timezone_name)

        if cancel_after_run is None:
            cancel_after_run = (mode == "run_once")

        next_run = self._compute_next_run(mode, interval_minutes, cron, run_at, tz=tz)

        job: dict[str, Any] = {
            "id": f"job_{uuid.uuid4().hex[:10]}",
            "kind": kind,
            "description": description,
            "schedule_mode": mode,
            "cancel_after_run": bool(cancel_after_run),
            "created_at": _now_iso(),
            "next_run_at": _iso(next_run),
            "last_run_at": None,
            "run_count": 0,
        }

        # Store timezone so recurring jobs reuse the same zone on each advance.
        if tz is not None:
            job["timezone"] = str(tz)

        # Schedule fields
        if mode == "heartbeat":
            job["interval_minutes"] = max(_MIN_INTERVAL, int(interval_minutes or _DEFAULT_INTERVAL))
        elif mode == "cron":
            job["cron"] = (cron or "").strip()
        elif mode == "run_once":
            job["run_at"] = _iso(_parse_iso(run_at)) if run_at else _iso(next_run)

        # Kind-specific fields
        if kind == "direct_message":
            job["message"] = message or ""
            job["channels"] = channels or []
        else:  # agent_task
            ctx = (context_mode or "contextual").strip().lower()
            job["context_mode"] = ctx if ctx in {"contextual", "isolated"} else "contextual"
            if command and command.strip():
                job["command"] = command.strip()

        self._store.add(job)
        logger.info("[scheduler] added job %s (%s/%s tz=%s)", job["id"], kind, mode, job.get("timezone", "UTC"))
        return job

    def cancel_job(self, job_id: str) -> bool:
        ok = self._store.remove(job_id)
        if ok:
            logger.info("[scheduler] cancelled job %s", job_id)
        return ok

    def list_jobs(self) -> list[dict]:
        return self._store.load()

    def update_job(self, job_id: str, **kwargs) -> dict | None:
        """Update fields on an existing job. Returns updated job or None."""
        jobs = self._store.load()
        target = next((j for j in jobs if j.get("id") == job_id), None)
        if target is None:
            return None

        # Apply simple field updates
        for key in ("description", "message", "channels", "context_mode",
                     "cancel_after_run", "kind"):
            if key in kwargs and kwargs[key] is not None:
                target[key] = kwargs[key]

        # Command: empty string removes it
        if "command" in kwargs:
            cmd = kwargs["command"]
            if cmd is not None:
                if cmd.strip():
                    target["command"] = cmd.strip()
                elif "command" in target:
                    del target["command"]

        # Timezone update
        if "timezone_name" in kwargs and kwargs["timezone_name"]:
            tz = _resolve_tz(kwargs["timezone_name"])
            if tz is not None:
                target["timezone"] = str(tz)

        # Schedule rebuild if any schedule field changed
        schedule_keys = ("schedule_mode", "interval_minutes", "cron", "run_at")
        if any(kwargs.get(k) is not None for k in schedule_keys):
            mode = _normalize_mode(kwargs.get("schedule_mode") or target.get("schedule_mode"))
            interval = kwargs.get("interval_minutes") or target.get("interval_minutes", _DEFAULT_INTERVAL)
            cron_expr = kwargs.get("cron") or target.get("cron")
            run_at_val = kwargs.get("run_at") or target.get("run_at")
            tz = _resolve_tz(target.get("timezone"))

            target["schedule_mode"] = mode
            if mode == "heartbeat":
                target["interval_minutes"] = max(_MIN_INTERVAL, int(interval))
            elif mode == "cron":
                target["cron"] = (cron_expr or "").strip()
            elif mode == "run_once":
                target["run_at"] = run_at_val

            target["next_run_at"] = _iso(
                self._compute_next_run(mode, interval, cron_expr, run_at_val, tz=tz)
            )
        elif "timezone_name" in kwargs and kwargs["timezone_name"]:
            # Timezone changed but no schedule fields — recompute next_run with new tz.
            mode = _normalize_mode(target.get("schedule_mode"))
            tz = _resolve_tz(target.get("timezone"))
            target["next_run_at"] = _iso(
                self._compute_next_run(
                    mode,
                    target.get("interval_minutes", _DEFAULT_INTERVAL),
                    target.get("cron"),
                    target.get("run_at"),
                    tz=tz,
                )
            )

        self._store.save(jobs)
        logger.info("[scheduler] updated job %s", job_id)
        return target

    def job_exists(self, job_id: str) -> bool:
        return self._store.exists(job_id)

    # ── Schedule computation ───────────────────────────────────────────────

    def _compute_next_run(
        self,
        mode: str,
        interval_minutes: int = _DEFAULT_INTERVAL,
        cron_expr: str | None = None,
        run_at: str | None = None,
        from_time: datetime | None = None,
        tz: "ZoneInfo | None" = None,
    ) -> datetime | None:
        now = from_time or _now()

        if mode == "run_once":
            dt = _parse_iso(run_at)
            if dt:
                return dt
            # Fallback: interval from now
            return now + timedelta(minutes=max(_MIN_INTERVAL, int(interval_minutes or _DEFAULT_INTERVAL)))

        if mode == "cron":
            expr = (cron_expr or "").strip()
            if not expr:
                return None
            try:
                # Evaluate cron in the user's local timezone so "0 9 * * *"
                # means 09:00 in their zone, not 09:00 UTC.
                start = now.astimezone(tz) if tz else now
                nxt = croniter(expr, start).get_next(datetime)
                if nxt.tzinfo is None:
                    nxt = nxt.replace(tzinfo=tz or timezone.utc)
                return nxt.astimezone(timezone.utc)
            except Exception:
                return None

        # heartbeat
        return now + timedelta(minutes=max(_MIN_INTERVAL, int(interval_minutes or _DEFAULT_INTERVAL)))

    def _advance_schedule(self, job: dict) -> str | None:
        """Compute next_run_at after a successful fire. Returns ISO string or None."""
        mode = _normalize_mode(job.get("schedule_mode"))
        tz = _resolve_tz(job.get("timezone"))
        if mode == "heartbeat":
            interval = int(job.get("interval_minutes") or _DEFAULT_INTERVAL)
            return _iso(_now() + timedelta(minutes=max(_MIN_INTERVAL, interval)))
        if mode == "cron":
            expr = (job.get("cron") or "").strip()
            if expr:
                nxt = self._compute_next_run("cron", cron_expr=expr, tz=tz)
                if nxt:
                    return _iso(nxt)
            return None
        # run_once — no next run
        return None

    # ── Scheduler loop ─────────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=_TICK_SECONDS):
            try:
                self._tick()
            except Exception:
                logger.exception("[scheduler] tick error")

    def _tick(self) -> None:
        jobs = self._store.load()
        now = _now()
        due = []
        for job in jobs:
            next_at = _parse_iso(job.get("next_run_at"))
            if next_at is None:
                continue
            if next_at <= now:
                due.append(job)

        if due:
            print(f"[scheduler-tick] {len(due)} job(s) due", flush=True)

        for job in due:
            self._fire(job)

    def _fire(self, job: dict) -> None:
        jid = job["id"]
        kind = _normalize_kind(job.get("kind"))
        desc = (job.get("description") or "")[:60]
        print(f"[scheduler-fire] {jid} ({kind}): {desc}...", flush=True)

        # Advance schedule immediately to prevent double-fire.
        cancel = bool(job.get("cancel_after_run", False))

        def _advance(jobs: list[dict]) -> None:
            for j in jobs:
                if j.get("id") != jid:
                    continue
                j["last_run_at"] = _now_iso()
                j["run_count"] = int(j.get("run_count") or 0) + 1
                if cancel:
                    jobs.remove(j)
                else:
                    j["next_run_at"] = self._advance_schedule(j)
                break

        self._store.mutate(_advance)

        # Detect late fire: how many minutes past the scheduled time are we?
        next_at_before_advance = _parse_iso(job.get("next_run_at"))
        late_minutes: int = 0
        if next_at_before_advance is not None:
            delta = (_now() - next_at_before_advance).total_seconds() / 60
            if delta > 5:  # more than 5 min late → treat as a missed run
                late_minutes = int(delta)

        # Execute based on kind.
        if kind == "direct_message":
            execute_direct_message(
                job,
                channel_send=self._channel_send,
                memory_add=self._agent.memory.add_message,
            )
        else:
            execute_agent_task(job, self._agent, monitor_id=jid, late_minutes=late_minutes)

    def _channel_send(self, message: str, channels: list[str]) -> dict:
        """Send message through registered channel push handlers or agent's handlers."""
        # Use the agent's send_to_channels which handles channel routing.
        return self._agent.send_to_channels(message, channels)
