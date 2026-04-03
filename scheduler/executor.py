"""Job execution logic for the two job kinds.

direct_message — Send pre-defined text to channels. No LLM.
agent_task     — Optionally run a command, then invoke LLM agent brain.
"""

from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from scheduler import Scheduler

logger = logging.getLogger(__name__)


def execute_direct_message(
    job: dict,
    channel_send: Callable[[str, list[str]], dict],
    memory_add: Callable[[str, str], None],
) -> None:
    """Send a pre-defined message directly — no LLM involved.

    channel_send(message, channels) → {"sent_to": [...], "failed": [...]}
    memory_add(role, content) stores the message in conversation history.
    """
    message = job.get("message", "")
    channels = job.get("channels") or []
    if not message:
        logger.warning("[executor] direct_message job %s has no message", job.get("id"))
        return

    result = channel_send(message, channels)
    sent = result.get("sent_to", [])
    if sent:
        # Store in conversation history at send time so it feels natural.
        memory_add("assistant", message)
    failed = result.get("failed", [])
    if failed:
        logger.warning("[executor] direct_message %s failed on: %s", job.get("id"), failed)
    logger.info("[executor] direct_message %s sent to %s", job.get("id"), sent)


def execute_agent_task(
    job: dict,
    agent: Any,
    monitor_id: str,
    late_minutes: int = 0,
) -> None:
    """Invoke the LLM agent brain for a background task.

    If the job has a command, pre-run it and inject output into the prompt.
    Then call agent.monitor_check() which runs the full tool loop.
    """
    import json as _json

    description = job.get("description", "")
    command = (job.get("command") or "").strip()
    context_mode = (job.get("context_mode") or "contextual").strip().lower()

    # Build late-run note to suppress timing analysis and prevent spam.
    late_note = ""
    if late_minutes > 0:
        late_note = (
            f"\n\n[SYSTEM NOTE — DO NOT MENTION THIS TO THE USER]\n"
            f"This job fired {late_minutes} minutes late because the device was offline at the scheduled time. "
            f"This is expected behaviour. "
            f"Do NOT report, analyse, or mention the timing discrepancy to the user. "
            f"Simply perform the task as normal and send at most ONE message_user call. "
            f"The user has already been notified about this delay automatically — do not repeat it."
        )

    if command:
        output = _run_command(command)
        prompt = (
            f"[BACKGROUND CHECK]\n\n"
            f"Task: {description}\n\n"
            f"Command already run: {command}\n"
            f"Output:\n{output}"
            f"{late_note}"
        )
    else:
        prompt = (
            f"[BACKGROUND CHECK]\n\n"
            f"Task: {description}\n"
            f"Schedule: {_json.dumps(job.get('schedule') or {}, ensure_ascii=True)}\n"
            f"Lifecycle: cancel_after_run={job.get('cancel_after_run', False)}"
            f"{late_note}"
        )

    try:
        agent.monitor_check(prompt, context_mode=context_mode, monitor_id=monitor_id)
    except Exception:
        logger.exception("[executor] agent_task error for job %s", monitor_id)


def _run_command(command: str) -> str:
    """Run a shell command with timeout and safety checks. Returns output string."""
    tokens = command.split()
    if tokens and tokens[0] == "sudo":
        return "(sudo is blocked in background tasks)"
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        if not output.strip():
            output = f"(exit code {proc.returncode}, no output)"
    except subprocess.TimeoutExpired:
        output = "(command timed out after 30s)"
    except Exception as e:
        output = f"(error running command: {e})"

    # Clip long output
    if len(output) > 3000:
        output = output[:2997] + "\n...[truncated]"
    return output
