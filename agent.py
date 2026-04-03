"""Servclaw — AI infrastructure agent with PTY execution, durable memory, and multi-channel support.

Context window layout (per chat() call):
  [SYSTEM_CORE]         — identity, Docker routing, target rules
  [SYSTEM_TOOL_RULES]   — tool usage, workspace, execution tracking
  [SYSTEM_STYLE]        — tone/response format rules
  [MEMORY SUMMARY]      — long-term facts + restored session context (from memory_manager)
  [BOOTSTRAP]           — onboarding guide (only on fresh session; removed after setup)
  [RECENT MESSAGES]     — restored runtime conversation (keep_last=20, tiered compaction)
  [TOOL DEFINITIONS]    — function schemas for run_command, workspace_*, execution_*, etc.
  [CURRENT USER MSG]    — the live user turn

Key subsystems:
  - PTY execution       — interactive shell sessions with waiting_for_input / send_process_input
  - Confirmation flow   — non-readonly commands require user approval before running
  - Sub-LLM summarizer  — auto-summarizes tool output >8 KB before injecting into context
  - Execution scratchpad — per-thread task plan + notes; promoted to memory after completion
  - Memory manager      — memory/memory.md (long-term) + memory/session.md (session)
  - Channels            — Terminal REPL, Telegram bot, Discord bot (edit-in-place status)
  - Model name          — read from servclaw.json (agents.defaults.model.primary) at startup
"""

import importlib
import json
import logging
import os
import pty
import re
import select
import shlex
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union

# Strip ANSI escape codes from raw PTY output before passing to LLM
_ANSI_ESCAPE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub('', text)


def _clip_text(text: str, limit: int = 1200) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]..."


def _is_context_length_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "context_length_exceeded" in msg or "input tokens exceed" in msg


def _looks_like_input_prompt(text: str) -> bool:
    """Heuristic: does the latest output look like it is asking for user input?"""
    if not text.strip():
        return False
    tail = text.strip().splitlines()[-1].strip().lower()
    prompt_patterns = [
        r"password",
        r"passphrase",
        r"enter ",
        r"input",
        r"select",
        r"choice",
        r"\[y/n\]",
        r"\(y/n\)",
        r"yes/no",
        r":$",
        r"\?$",
    ]
    return any(re.search(p, tail) for p in prompt_patterns)

from openai import OpenAI

from custom_skill_manager import CustomSkillManager
from memory_manager import MemoryManager
from servclaw_config import (
    get_model_name,
    get_openai_api_key,
    get_skill_config,
    get_user_timezone,
    load_config,
    save_config,
    update_user_location,
)


class ExecutionStopped(Exception):
    """Raised when a chat execution was cancelled by the user."""


@dataclass
class ConfirmationRequest:
    """Returned by chat() when agent wants to run a non-readonly command.

    Callers (terminal / telegram) must:
      1. Show the user the message + command.
      2. Ask for confirmation.
      3. Call agent.confirm_and_run(request) to proceed, or agent.deny(request) to cancel.
    """

    command: str
    message: str          # Agent's explanation / ask for permission
    call_id: str          # tool_call id — needed to resume the tool loop
    tool_name: str        # tool to execute after confirmation
    tool_args: dict       # original tool arguments
    approval_signature: str
    execution_id: str | None = None
    approved_signatures: set[str] = field(default_factory=set)
    pending_messages: list = field(default_factory=list)  # messages snapshot to resume

MODEL_NAME: str = get_model_name(load_config())

# Split into segments — mini models retain segmented system messages better.
SYSTEM_CORE = """You are Servclaw, an AI infrastructure agent. You run shell commands and manage Docker services on the user's server.
You run INSIDE a Docker container. run_command() with target=auto (default) routes to the HOST via docker.sock. Do NOT specify target=host. Only use target=local for rare in-container ops. NEVER ask "host or local?" — always assume host.
$HOME in container is /root. User's home is /home/<username> (check memory or ask once)."""

SYSTEM_TOOL_RULES = """## Tool & Execution Rules
- Interactive commands: when run_command returns {"waiting_for_input": true}, call send_process_input. If input is user-specific (password, key), ask user FIRST. Never fabricate secrets.
- Workspace files: ALWAYS workspace_read before workspace_write. Preserve all frontmatter/headings. Never rewrite from scratch.
- Execution tracking: for 2+ tool-call tasks, call execution_plan() first, then execution_update() after each step. Record key facts (discoveries, paths, results) as notes.
- Always call tools for fresh data. When you have the answer, give it — do NOT re-run commands already ran.
- Large command output is automatically summarized by a sub-model before you see it. You will see {auto_summarized: true} when this happens. Trust the summary — it captures the key content.
- Shell commands are for automation, file tasks, or system queries the user actually requests — not for general knowledge questions.
- Command history/logs: use logs_read("commands.jsonl") to inspect past commands, their outputs, and approval decisions. Default is last 30 — adjust limit as needed (e.g. limit=5 for a quick glance, limit=100 for deep history).
- Background jobs: use job_create, job_update, job_cancel, job_list for ALL watch/monitor/reminder/alert/scheduled tasks. Choose kind='direct_message' for simple reminders (pre-written text, no LLM at fire time) and kind='agent_task' for checks that need your brain at fire time. Never use shell commands, scripts, or external CLI tools to create or manage background jobs.
- Job deduplication (CRITICAL): ALWAYS call job_list before job_create. If a job already exists for the same intent (same time, same purpose), use job_update or job_cancel+job_create to replace it — never create a second overlapping job. When the user refines a request ("make it variable", "send on Discord"), that means UPDATE the existing job, not create a new one alongside it. One intent = one job.
- Jobs are the ONLY mechanism for reminders, schedules, and recurring tasks. NEVER use workspace files (REMINDERS.md, SCHEDULE.md, or any .md) to record or track jobs — that does nothing. The job system (job_create/list/update/cancel) is the single source of truth. Do not write job metadata to workspace files.
- Job timezones: ALWAYS pass timezone (IANA name, e.g. 'Asia/Kolkata') when creating or updating cron/run_once jobs. Derive it from user_timezone in msg_context. Never leave times as UTC unless the user explicitly says "UTC". If msg_context has no timezone but has a country/city, infer the correct IANA timezone yourself (e.g. India → 'Asia/Kolkata', UK → 'Europe/London').
- Custom skills: use skill_create to build a new reusable Python tool whenever the user asks you to automate or create a capability. Use skill_list/skill_read to review existing skills. Use skill_update to improve them. Newly created/updated skills are immediately callable in the next message as normal tools.
- message_user: call it AT MOST ONCE per execution in case of Jobs. Draft the message carefully, then send it — never call it again to rephrase the same content. If a background job fired late (device was offline), just perform the task normally; do NOT send extra messages explaining why it ran late."""

SYSTEM_STYLE = """## Response Style & Rules
- Short, natural messages. 1-3 sentences. One thought per message.
- No filler, no bullet dumps unless needed. Warm but direct.
- Use memory facts naturally. Never invent facts not in memory.
- NEVER greet mid-task or restart the conversation unprompted."""

MEMORY_SUMMARY = """
## Memory-Backed Context
memory/memory.md persists durable long-term facts.
memory/session.md persists current session summary and recent conversation for restart continuity.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run any shell command on the server. "
                "Before calling this tool you MUST classify the command as read-only or not. "
                "Read-only: commands that only observe (ls, cat, ps, df, docker ps, grep, etc.). "
                "Non-read-only: commands that mutate state (rm, mv, docker stop/start/restart, "
                "apt install, chmod, kill, systemctl restart, etc.). "
                "Set readonly=true only when you are certain the command cannot modify anything. "
                "If in doubt, set readonly=false."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                    "readonly": {
                        "type": "boolean",
                        "description": "True if the command is read-only / safe to run without confirmation.",
                    },
                    "target": {
                        "type": "string",
                        "description": (
                            "Execution target: auto|host|local. "
                            "Default is auto; when running inside Docker, auto means host execution via docker.sock."
                        ),
                        "enum": ["auto", "host", "local"],
                    },
                },
                "required": ["command", "readonly"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_host_command_via_docker",
            "description": (
                "Run a command on the host using Docker socket escalation. "
                "This starts a privileged helper container and executes the command in host root context. "
                "Use only when normal run_command fails due to permissions or user explicitly asks to use docker.sock. "
                "Before calling this tool, classify readonly exactly like run_command."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Host command to execute via docker.sock escalation.",
                    },
                    "readonly": {
                        "type": "boolean",
                        "description": "True only if this command is purely observational.",
                    },
                },
                "required": ["command", "readonly"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_process_input",
            "description": (
                "Send a line of text input to a running interactive process that is waiting for input. "
                "Use the session_id from a run_command response that had waiting_for_input=true. "
                "Rules: "
                "- If the process is asking a yes/no or simple choice you can determine, answer autonomously. "
                "- If the process needs a value only the user would know (password, custom path, name, key), "
                "  ask the user in plain text FIRST, wait for their reply, then call this tool. "
                "- Never invent secrets or sensitive values."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The session_id from the waiting_for_input response.",
                    },
                    "input_text": {
                        "type": "string",
                        "description": "The text to send to the process (newline appended automatically).",
                    },
                },
                "required": ["session_id", "input_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_read",
            "description": (
                "Read a file from the agent workspace (e.g. IDENTITY.md, USER.md, SOUL.md, BOOTSTRAP.md). "
                "No confirmation needed. Returns the file content or empty string if missing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Name of the workspace file to read (e.g. 'IDENTITY.md').",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_write",
            "description": (
                "Write or overwrite a file in the agent workspace. "
                "No confirmation needed. Use this to update IDENTITY.md, USER.md, SOUL.md, etc. "
                "The full content of the file is replaced."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Name of the workspace file to write (e.g. 'IDENTITY.md').",
                    },
                    "content": {
                        "type": "string",
                        "description": "The full content to write into the file.",
                    },
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_delete",
            "description": (
                "Delete a file from the agent workspace. "
                "No confirmation needed. Use this to remove BOOTSTRAP.md when onboarding is done."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Name of the workspace file to delete (e.g. 'BOOTSTRAP.md').",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execution_plan",
            "description": (
                "Set or replace your execution task plan for the current operation. "
                "Use this at the start of any multi-step task to lay out what you will do. "
                "Each task is a short description. Tasks start as 'todo'. "
                "This helps you track progress and avoid repeating or forgetting steps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of task descriptions (e.g. ['Check nginx status', 'Update config', 'Restart container']).",
                    },
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execution_update",
            "description": (
                "Update the status of a task in your execution plan, or add a note. "
                "Use this after completing a step, or to record an important finding. "
                "task_index is 0-based. Status can be: todo, in-progress, done, skipped, failed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_index": {
                        "type": "integer",
                        "description": "0-based index of the task to update. Use -1 to only add a note without updating a task.",
                    },
                    "status": {
                        "type": "string",
                        "description": "New status: todo, in-progress, done, skipped, failed.",
                        "enum": ["todo", "in-progress", "done", "skipped", "failed"],
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional note about the result or an important finding.",
                    },
                },
                "required": ["task_index", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "logs_read",
            "description": (
                "Read command execution logs. Each line in 'commands.jsonl' is a JSON record with "
                "fields: ts (timestamp), event, command, exit_code, output, approved, etc. "
                "Returns the last `limit` entries (default 30). Set limit to any number you need — "
                "use a small number for a quick glance, larger for deeper history. No confirmation needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Log file to read (e.g. 'commands.jsonl').",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "How many most-recent log entries to return. Defaults to 30.",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "job_create",
            "description": (
                "Create a background job. Two kinds:\n"
                "• direct_message — You pre-write the exact message now. At fire time it is sent directly "
                "to the user's channels with NO LLM call. Use for reminders, nudges, scheduled messages.\n"
                "• agent_task — At fire time YOUR brain is invoked in the background. You can use tools, "
                "run commands, think, and decide whether to contact the user. Use for checks, monitoring, "
                "anything that needs intelligence at fire time.\n"
                "Schedule modes: heartbeat (repeating interval), cron (cron expression), run_once (one-shot)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "direct_message (pre-written text, no LLM) or agent_task (LLM brain at fire time).",
                        "enum": ["direct_message", "agent_task"],
                    },
                    "description": {
                        "type": "string",
                        "description": "Human-readable purpose of this job.",
                    },
                    "schedule_mode": {
                        "type": "string",
                        "description": "heartbeat (periodic), cron (cron expression), or run_once (single fire).",
                        "enum": ["heartbeat", "cron", "run_once"],
                    },
                    "interval_minutes": {
                        "type": "integer",
                        "description": "Interval for heartbeat jobs (minutes). Minimum 1.",
                    },
                    "cron": {
                        "type": "string",
                        "description": "Cron expression for schedule_mode='cron'.",
                    },
                    "run_at": {
                        "type": "string",
                        "description": "ISO datetime for schedule_mode='run_once'.",
                    },
                    "cancel_after_run": {
                        "type": "boolean",
                        "description": "Auto-remove after one execution. Defaults true for run_once.",
                    },
                    "message": {
                        "type": "string",
                        "description": "(direct_message only) The exact message text to send when the job fires.",
                    },
                    "channels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "(direct_message only) Target channels e.g. ['telegram']. Empty = all.",
                    },
                    "command": {
                        "type": "string",
                        "description": (
                            "(agent_task only) Optional shell command to pre-run before the LLM check. "
                            "Output is passed to the agent brain for analysis."
                        ),
                    },
                    "context_mode": {
                        "type": "string",
                        "description": "(agent_task only) contextual (includes recent conversation) or isolated.",
                        "enum": ["contextual", "isolated"],
                    },
                },
                "required": ["kind", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "job_cancel",
            "description": "Cancel and remove a background job by its ID. Use job_list first to find IDs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job ID to cancel (e.g. 'job_abc123').",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "job_update",
            "description": (
                "Update an existing job's settings. Only provided fields change. "
                "Use job_list first to get the ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job ID to update.",
                    },
                    "description": {"type": "string"},
                    "schedule_mode": {
                        "type": "string",
                        "enum": ["heartbeat", "cron", "run_once"],
                    },
                    "interval_minutes": {"type": "integer"},
                    "cron": {"type": "string"},
                    "run_at": {"type": "string"},
                    "cancel_after_run": {"type": "boolean"},
                    "message": {"type": "string", "description": "New message text (direct_message jobs)."},
                    "channels": {"type": "array", "items": {"type": "string"}},
                    "command": {"type": "string", "description": "New command. Empty string removes it."},
                    "context_mode": {"type": "string", "enum": ["contextual", "isolated"]},
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "job_list",
            "description": (
                "List all background jobs with their IDs, kinds, descriptions, schedules, and status. "
                "Use to review jobs or find IDs before cancelling/updating."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "message_user",
            "description": (
                "You can use this any time you want to reach out to the user or initiate conversation with "
                "the user, such as to share important information, ask a question, or deliver a monitor alert "
                "through their messaging channel(s). By default sends to ALL active channels. "
                "To target a specific channel pass its name in 'channels' (e.g. ['telegram'])."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The message text to send.",
                    },
                    "channels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional channel names to target, e.g. ['telegram']. "
                            "Omit or pass an empty list to send to ALL active channels."
                        ),
                    },
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "channels_list",
            "description": (
                "List the communication channels configured in servclaw.json with their enabled status and "
                "whether each channel is currently active/registered for push delivery. "
                "Use when you need to know which channels are available, active, or enabled."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_create",
            "description": (
                "Create a new custom skill (a Python tool callable by you in future conversations). "
                "Skills live in workspace/skills/<tool_name>/. Each skill has:\n"
                "  • skill.py  — Python module you write; MUST define:\n"
                "      TOOL_SCHEMA = {\"type\":\"function\",\"function\":{\"name\":\"<tool_name>\","
                "\"description\":\"...\",\"parameters\":{\"type\":\"object\",\"properties\":{...},"
                "\"required\":[]}}}\n"
                "      def run(args: dict) -> dict: ...\n"
                "  • SKILL.md  — Markdown guide: when and how to use this skill.\n"
                "The skill is loaded immediately and available as a tool from the next message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": (
                            "Unique identifier for this skill/tool. "
                            "Lowercase letters, digits, underscores only. Must start with a letter. "
                            "E.g. 'ping_host', 'disk_usage', 'git_summary'."
                        ),
                    },
                    "skill_code": {
                        "type": "string",
                        "description": (
                            "Full content of skill.py. Must define TOOL_SCHEMA and run(args: dict) -> dict. "
                            "Do NOT import Servclaw internals — skills must be self-contained."
                        ),
                    },
                    "skill_guide": {
                        "type": "string",
                        "description": "Markdown content for SKILL.md — when and how to use this skill.",
                    },
                },
                "required": ["tool_name", "skill_code", "skill_guide"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_update",
            "description": (
                "Update an existing custom skill's code and/or guide. "
                "Provide only the fields you want to change. The skill is reloaded immediately."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "description": "The tool name of the skill to update."},
                    "skill_code": {"type": "string", "description": "New full content for skill.py."},
                    "skill_guide": {"type": "string", "description": "New full content for SKILL.md."},
                },
                "required": ["tool_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_delete",
            "description": "Permanently delete a custom skill and its files from workspace/skills/<tool_name>/.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "description": "The tool name of the skill to delete."},
                },
                "required": ["tool_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_list",
            "description": (
                "List all custom skills currently stored in workspace/skills/. "
                "Shows tool names, descriptions, and file paths."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skill_read",
            "description": "Read the full code (skill.py) and guide (SKILL.md) of a custom skill.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "description": "The tool name of the skill to read."},
                },
                "required": ["tool_name"],
            },
        },
    },
]

MEMORY_PLANNER_PROMPT = """You are a memory action planner for a personal AI assistant.

Goal: produce minimal, accurate edits for memory/memory.md as CURRENT TRUTH.

Rules:
- Do not invent facts.
- If user asks to remember something, add a concise note using remember_note.
- If user mentions a task, goal, or thing they need to do — even casually (e.g. "I need to fix X", "remember I have to update Y", "don't forget I want to do Z") — add a remember_note for it. These are pending to-dos the user wants to persist across sessions.
- If user asks to forget something specific, remove only matching memory.
- If user asks to forget everything about themselves, clear user memory.
- If user says vague forget words like "forget it/this/that", remove only the most recent remembered note, not all user memory.
- Prefer updating existing facts over appending conflicting ones.
- IMPORTANT: When the user mentions their country or city (even casually, e.g. "my country", "I'm in India", "I live in Mumbai"), always capture it using set_user_info with key="Country" and/or key="City". This is used to auto-set timezone for scheduling.
- Output JSON only with this shape:
    {
        "actions": [
            {"type":"set_user_info","key":"Name","value":"Alex"},
            {"type":"set_user_info","key":"Country","value":"India"},
            {"type":"set_user_info","key":"City","value":"Mumbai"},
            {"type":"remove_user_info","key":"Preference[pizza]"},
            {"type":"remember_note","note":"user likes jazz music"},
            {"type":"remove_session_note","snippet":"jazz"},
            {"type":"forget_all_user_memory"},
            {"type":"forget_last_remembered"}
        ]
    }
- If no changes are needed, return {"actions":[]}.
"""

SESSION_SUMMARIZER_PROMPT = """You maintain two layers of memory for a local assistant.

Files:
- memory/session.md:
    - Session Summary: cumulative important details for the current session only
    - Recent Conversation: latest 20 user/assistant messages only
- memory/memory.md:
    - long-term durable memory for stable facts worth remembering after this session

Task:
- Read the existing session summary and the latest recent conversation.
- Produce an updated Session Summary that preserves session-important facts, decisions, progress, state changes, user instructions, unresolved tasks, key ports/paths/services, and current status.
- The session summary should be cumulative for the current session, but concise.
- Also decide whether anything from this conversation should be promoted into long-term memory.

Rules:
- Summarize in a human-meaningful way, not raw transcript form.
- Keep Session Summary focused on this session, not permanent facts unless needed for this session.
- Promote to long-term memory only if the fact is durable and useful beyond this session.
- Do not invent facts.
- Avoid duplication.
- Output JSON only in this shape:
    {
        "session_summary_bullets": [
            "User asked to move demo-webapp from host port 5666 to 8081.",
            "demo-webapp is currently running on host port 8081 using nginx:alpine.",
            "Compose file may still need updating to match runtime state."
        ],
        "long_term_actions": [
            {"type":"set_infrastructure_note","key":"Deployment stage","value":"prod"},
            {"type":"set_user_info","key":"Name","value":"Pratham"}
        ]
    }
- If nothing should be promoted to long-term memory, use an empty list for long_term_actions.
- Session summary bullets should usually be 3-12 bullets.
"""


class ServclawAgent:
    """AI agent with durable long-term memory and restored session context.

    On restart: memory/memory.md survives and memory/session.md restores the latest session context.
    """

    def __init__(self):
        cfg = load_config()
        self.client = OpenAI(api_key=get_openai_api_key(cfg))
        self.memory = MemoryManager()
        self._running_in_docker = self._detect_running_in_docker()
        # Workspace dir must be set before _command_log_path default
        self._workspace_dir = Path(__file__).resolve().parent / "workspace"
        self._workspace_dir.mkdir(parents=True, exist_ok=True)
        self._command_log_path = Path(
            os.getenv(
                "SERVCLAW_COMMAND_LOG_PATH",
                str(self._workspace_dir / "logs" / "commands.jsonl"),
            )
        )
        (self._workspace_dir / "logs").mkdir(parents=True, exist_ok=True)
        self._command_log_lock = threading.Lock()
        self._command_logger = logging.getLogger("servclaw.commands")
        if not self._command_logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("[servclaw-command] %(message)s"))
            self._command_logger.addHandler(handler)
            self._command_logger.setLevel(logging.INFO)
            self._command_logger.propagate = False
        # Active interactive process sessions: {session_id: {proc, master_fd}}
        self._process_sessions: dict = {}
        self._sessions_lock = threading.Lock()
        self._execution_local = threading.local()
        self._cancelled_execution_ids: set[str] = set()
        self._cancelled_lock = threading.Lock()
        # Per-execution scratchpad for task tracking (thread-local).
        # Stores {"plan": [{"task": str, "status": str}], "notes": [str]}
        # Injected into the tool loop so the LLM always sees its own progress.
        # Never persisted into user-agent conversation history.
        # Optional status callback: called with a short string before each tool run.
        # Set externally (e.g. by Telegram bot) to stream live progress to the user.
        self._status_callback: "callable | None" = None
        # _workspace_dir already set above; just create remaining dirs
        self._workspace_lock = threading.Lock()
        self._channels_dir = Path(__file__).resolve().parent / "channels"
        # Skills system
        self._tools: list = list(TOOLS)
        self._skill_handlers: dict = {}
        self._skill_guides: list[str] = []
        self._load_skills(cfg)
        self._builtin_skill_guide_count = len(self._skill_guides)
        # Custom (user-created) skills
        self._custom_skills = CustomSkillManager(self._workspace_dir)
        self._custom_skills.load_all()
        self._skill_guides.extend(self._custom_skills.get_guides())
        # Scheduler (set by main.py after construction)
        self.scheduler: Any = None
        # Channel push handlers: name → callable(text)
        self._channel_push_handlers: dict = {}
        # Ensure workspace files are initialized from templates
        self._ensure_workspace_templates()

    # ── Skills ────────────────────────────────────────────────────────────

    def _load_skills(self, cfg: dict) -> None:
        """Read skills/manifest.json, import each enabled skill, and register its tool."""
        skills_dir = Path(__file__).resolve().parent / "skills"
        manifest_path = skills_dir / "manifest.json"
        if not manifest_path.exists():
            return
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return
        for skill_def in manifest.get("builtin", []):
            skill_id = skill_def.get("id", "")
            skill_cfg = cfg.get("skills", {}).get(skill_id, {})
            if not skill_cfg.get("enabled", False):
                continue
            req_secret = skill_def.get("requires_secret")
            api_key = ""
            if req_secret:
                api_key = str(cfg.get("secrets", {}).get(req_secret, "") or "")
                if not api_key:
                    logging.warning("[skills] %s enabled but secret '%s' is empty — skipping", skill_id, req_secret)
                    continue
            try:
                mod = importlib.import_module(skill_def["module"])
                tool_name = mod.TOOL_SCHEMA["function"]["name"]
                self._tools.append(mod.TOOL_SCHEMA)
                self._skill_handlers[tool_name] = lambda args, _k=api_key, _m=mod: _m.execute(args, _k)
                skill_md_path = skills_dir / skill_id / "skill.md"
                if skill_md_path.exists():
                    guide = skill_md_path.read_text(encoding="utf-8").strip()
                    if guide:
                        self._skill_guides.append(guide)
                logging.info("[skills] loaded: %s (%s)", skill_id, tool_name)
            except Exception as e:
                logging.warning("[skills] failed to load %s: %s", skill_id, e)

    def _refresh_custom_skill_guides(self) -> None:
        """Rebuild _skill_guides to reflect current custom skills."""
        builtin_count = getattr(self, "_builtin_skill_guide_count", 0)
        self._skill_guides = self._skill_guides[:builtin_count] + self._custom_skills.get_guides()

    # ── Logs ──────────────────────────────────────────────────────────────

    def _logs_read(self, filename: str, limit: int = 30) -> dict:
        """Read the last `limit` lines from a log file in workspace/logs/."""
        safe = Path(filename).name
        logs_dir = self._workspace_dir / "logs"
        filepath = logs_dir / safe
        if not filepath.exists():
            return {"error": f"Log file not found: {safe}"}
        try:
            lines = filepath.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = lines[-limit:] if limit > 0 else lines
            return {"content": "\n".join(tail), "total_entries": len(lines), "returned": len(tail)}
        except Exception as e:
            return {"error": str(e)}

    # ── Location sync ─────────────────────────────────────────────────────

    def _sync_location_to_config(self, actions: list[dict]) -> None:
        """If memory actions recorded Country or City, save them to servclaw.json."""
        country = next(
            (a.get("value") for a in actions
             if a.get("type") == "set_user_info"
             and a.get("key", "").strip().lower() in ("country",)),
            None,
        )
        city = next(
            (a.get("value") for a in actions
             if a.get("type") == "set_user_info"
             and a.get("key", "").strip().lower() in ("city",)),
            None,
        )
        if not country and not city:
            return
        try:
            cfg = load_config()
            changed = update_user_location(cfg, city or "", country or "")
            if changed:
                save_config(cfg)
                tz = cfg.get("user", {}).get("timezone", "UTC")
                logging.info("[agent] user location synced to config: country=%s city=%s tz=%s",
                             country, city, tz)
        except Exception:
            pass

    # ── Channel push ──────────────────────────────────────────────────────

    def register_channel_push(self, name: str, handler) -> None:
        """Register a per-channel push callable so the agent can target specific channels."""
        self._channel_push_handlers[name.lower()] = handler

    def _tool_message_user(self, message: str, channels: list) -> dict:
        """Send a message through channels. Used by the message_user tool."""
        if not message:
            return {"error": "message is required"}
        if not self._channel_push_handlers:
            return {"error": "no active push channels are registered — channels may not be set up yet"}
        if getattr(self._execution_local, "monitor_mode", False):
            mid = getattr(self._execution_local, "monitor_id", None)
            if mid and self.scheduler is not None:
                if not self.scheduler.job_exists(mid):
                    logging.info("[message_user] dropped: job %s was cancelled mid-flight", mid)
                    return {"skipped": f"job {mid} was cancelled — message not sent"}
        targets = [c.lower() for c in channels] if channels else list(self._channel_push_handlers.keys())
        sent, failed = [], []
        for ch in targets:
            handler = self._channel_push_handlers.get(ch)
            if handler is None:
                failed.append(f"{ch} (not registered)")
                continue
            try:
                handler(message)
                sent.append(ch)
            except Exception as exc:
                failed.append(f"{ch} (error: {exc})")
        if sent:
            self.memory.add_message("assistant", message)
        return {"sent_to": sent, "failed": failed}

    def send_to_channels(self, message: str, channels: list[str]) -> dict:
        """Public API for the scheduler to send messages through channels."""
        if not message:
            return {"sent_to": [], "failed": ["no message"]}
        if not self._channel_push_handlers:
            return {"sent_to": [], "failed": ["no channels registered"]}
        targets = [c.lower() for c in channels] if channels else list(self._channel_push_handlers.keys())
        sent, failed = [], []
        for ch in targets:
            handler = self._channel_push_handlers.get(ch)
            if handler is None:
                failed.append(f"{ch} (not registered)")
                continue
            try:
                handler(message)
                sent.append(ch)
            except Exception as exc:
                failed.append(f"{ch} (error: {exc})")
        return {"sent_to": sent, "failed": failed}

    def _tool_channels_list(self) -> dict:
        """Return configured channels from servclaw.json with enabled + push-active status."""
        cfg = load_config()
        channels_cfg = cfg.get("channels", {})
        result = []
        for ch_name in ("telegram", "discord"):
            ch = channels_cfg.get(ch_name, {})
            result.append({
                "name": ch_name,
                "enabled": ch.get("enabled", True),
                "push_active": ch_name in self._channel_push_handlers,
            })
        return {"channels": result}

    # ── Monitor check (called by scheduler) ──────────────────────────────

    def monitor_check(self, prompt: str, context_mode: str = "contextual", monitor_id: str | None = None) -> None:
        """Called by Scheduler for agent_task jobs — runs the full tool loop in background."""
        exec_id = f"job:{uuid.uuid4().hex[:8]}"
        self._set_current_execution_id(exec_id)
        self._clear_execution_cancelled(exec_id)
        self._execution_local.monitor_mode = True
        self._execution_local.monitor_id = monitor_id

        _BLOCKED_TOOLS = {"job_create"}
        restricted_tools = [t for t in self._tools if t["function"]["name"] not in _BLOCKED_TOOLS]

        mode = (context_mode or "contextual").strip().lower()
        if mode not in {"contextual", "isolated"}:
            mode = "contextual"

        system_prompt = (
            "You are Servclaw, the user's infrastructure assistant running a background task.\n"
            "Do the task described. Use your tools freely.\n"
            "If something needs the user's attention → call message_user() with your message.\n"
            "If everything is normal → finish silently without calling message_user.\n"
            "Message style when notifying: first line of a conversation you are opening — "
            "natural, warm, direct, as if you just walked up to start a chat. "
            "No REMINDER/ALERT/FYI labels. One thought only.\n"
            "DO NOT use sudo or privileged commands. DO NOT create new background jobs."
        )

        try:
            self._clear_scratchpad()
            recent = self.memory.get_recent_messages()
            convo_lines: list[str] = []
            for item in recent:
                role = item.get("role")
                content = item.get("content")
                if role not in {"user", "assistant"}:
                    continue
                if not isinstance(content, str):
                    continue
                text = content.strip()
                if not text:
                    continue
                prefix = "USER" if role == "user" else "ASSISTANT"
                clipped = text if len(text) <= 220 else text[:217] + "..."
                convo_lines.append(f"- {prefix}: {clipped}")
            convo_lines = convo_lines[-8:]
            convo_snapshot = (
                "Recent conversation (use for tone and continuity — not as evidence of new events):\n"
                + "\n".join(convo_lines)
            ) if convo_lines else "Recent conversation: (none)"

            messages = [
                {"role": "system", "content": SYSTEM_CORE},
                {"role": "system", "content": SYSTEM_TOOL_RULES},
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
            if mode == "contextual":
                messages.insert(3, {"role": "system", "content": convo_snapshot})
            self._run_tool_loop(messages, _tools_override=restricted_tools)
            self._maybe_refresh_session_memory()
        except Exception:
            logging.exception("[monitor_check] error for %s", exec_id)
        finally:
            self._execution_local.monitor_mode = False
            self._execution_local.monitor_id = None
            self._clear_current_execution_id()

    # ── Custom skill tools ────────────────────────────────────────────────

    def _tool_skill_create(self, tool_name: str, skill_code: str, skill_guide: str) -> dict:
        if not tool_name:
            return {"error": "tool_name is required"}
        if not skill_code:
            return {"error": "skill_code is required"}
        if not skill_guide:
            return {"error": "skill_guide is required"}
        result = self._custom_skills.create(tool_name, skill_code, skill_guide)
        if result.get("ok"):
            self._refresh_custom_skill_guides()
            logging.info("[agent] custom skill created: %s", tool_name)
        return result

    def _tool_skill_update(self, tool_name: str, skill_code: str | None, skill_guide: str | None) -> dict:
        if not tool_name:
            return {"error": "tool_name is required"}
        result = self._custom_skills.update(tool_name, skill_code, skill_guide)
        if result.get("ok"):
            self._refresh_custom_skill_guides()
            logging.info("[agent] custom skill updated: %s", tool_name)
        return result

    def _tool_skill_delete(self, tool_name: str) -> dict:
        if not tool_name:
            return {"error": "tool_name is required"}
        result = self._custom_skills.delete(tool_name)
        if result.get("ok"):
            self._refresh_custom_skill_guides()
            logging.info("[agent] custom skill deleted: %s", tool_name)
        return result

    def _tool_skill_list(self) -> dict:
        skills = self._custom_skills.list_skills()
        return {"custom_skills": skills, "count": len(skills)}

    def _tool_skill_read(self, tool_name: str) -> dict:
        if not tool_name:
            return {"error": "tool_name is required"}
        return self._custom_skills.read_skill(tool_name)

    def _log_command_event(self, event: str, **fields) -> None:
        payload = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
            **fields,
        }
        line = json.dumps(payload, ensure_ascii=True)
        self._command_logger.info(line)
        try:
            self._command_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._command_log_lock:
                with self._command_log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception:
            pass

    def _detect_running_in_docker(self) -> bool:
        if os.path.exists("/.dockerenv"):
            return True
        try:
            with open("/proc/1/cgroup", "r", encoding="utf-8") as fh:
                data = fh.read()
            return any(tag in data for tag in ("docker", "containerd", "kubepods"))
        except Exception:
            return False

    # ── Workspace file management ─────────────────────────────────────────────

    def _ensure_workspace_templates(self) -> None:
        """Copy template MD files to workspace if they don't exist yet."""
        templates_dir = Path(__file__).resolve().parent / "templates"
        if not templates_dir.exists():
            return
        
        try:
            for template_file in templates_dir.glob("*.md"):
                target_file = self._workspace_dir / template_file.name
                if not target_file.exists():
                    target_file.write_text(template_file.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass

    def _read_workspace_file(self, filename: str) -> str:
        """Read a workspace MD file (e.g., 'SOUL.md', 'IDENTITY.md')."""
        filepath = self._workspace_dir / filename
        if filepath.exists():
            try:
                return filepath.read_text(encoding="utf-8")
            except Exception:
                return ""
        return ""

    def _read_channel_guide(self, channel: str, guide_file: str = "CONNECT.md") -> str:
        """Read channel setup guide markdown, if present."""
        filepath = self._channels_dir / channel / guide_file
        if filepath.exists():
            try:
                return filepath.read_text(encoding="utf-8")
            except Exception:
                return ""
        return ""

    def _write_workspace_file(self, filename: str, content: str) -> None:
        """Write a workspace MD file atomically."""
        filepath = self._workspace_dir / filename
        try:
            with self._workspace_lock:
                filepath.write_text(content, encoding="utf-8")
        except Exception:
            pass

    def _update_workspace_section(self, filename: str, section: str, new_value: str) -> None:
        """Update a specific field in a workspace MD file (simple key: value matching)."""
        content = self._read_workspace_file(filename)
        if not content:
            return
        
        # Replace "key: (old_value)" with "key: new_value"
        pattern = rf"(\- \*\*{re.escape(section)}\*\*: ).*?(?=\n|$)"
        updated = re.sub(pattern, rf"\1{new_value}", content, flags=re.IGNORECASE)
        
        if updated != content:
            self._write_workspace_file(filename, updated)

    def _is_onboarding_complete(self) -> bool:
        """Check if onboarding has been completed (BOOTSTRAP.md is gone)."""
        bootstrap_file = self._workspace_dir / "BOOTSTRAP.md"
        return not bootstrap_file.exists()

    def _update_heartbeat(self, status: str = "Ready", current_task: str = "") -> None:
        """Update HEARTBEAT.md with current runtime state."""
        heartbeat = self._read_workspace_file("HEARTBEAT.md")
        import re as re_module
        
        # Update Overall State
        heartbeat = re_module.sub(
            r"(\- \*\*Overall State\*\*: ).*?(?=\n|$)",
            rf"\1{status}",
            heartbeat,
            flags=re_module.IGNORECASE
        )
        
        # Update Last Activity
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")
        heartbeat = re_module.sub(
            r"(\- \*\*Last Activity\*\*: ).*?(?=\n|$)",
            rf"\1{current_time}",
            heartbeat,
            flags=re_module.IGNORECASE
        )
        
        # Update Current Task
        heartbeat = re_module.sub(
            r"(\- \*\*Current Task\*\*: ).*?(?=\n|$)",
            rf"\1{current_task}",
            heartbeat,
            flags=re_module.IGNORECASE
        )
        
        self._write_workspace_file("HEARTBEAT.md", heartbeat)

    # ── Workspace tools (no confirmation needed) ─────────────────────────

    def _workspace_read(self, filename: str) -> dict:
        """Read a workspace file. Returns {"content": ...} or {"error": ...}."""
        safe = Path(filename).name  # prevent path traversal
        content = self._read_workspace_file(safe)
        if content:
            return {"content": content}
        filepath = self._workspace_dir / safe
        if not filepath.exists():
            return {"error": f"File not found: {safe}"}
        return {"content": ""}

    def _workspace_write(self, filename: str, content: str) -> dict:
        """Write a workspace file. Returns {"ok": True} or {"error": ...}."""
        safe = Path(filename).name
        try:
            self._write_workspace_file(safe, content)
            return {"ok": True, "file": safe}
        except Exception as e:
            return {"error": str(e)}

    def _workspace_delete(self, filename: str) -> dict:
        """Delete a workspace file. Returns {"ok": True} or {"error": ...}."""
        safe = Path(filename).name
        filepath = self._workspace_dir / safe
        try:
            if filepath.exists():
                filepath.unlink()
                return {"ok": True, "deleted": safe}
            return {"error": f"File not found: {safe}"}
        except Exception as e:
            return {"error": str(e)}

    def _delete_onboarding_file(self) -> None:
        """Delete BOOTSTRAP.md after onboarding completion."""
        filepath = self._workspace_dir / "BOOTSTRAP.md"
        try:
            if filepath.exists():
                filepath.unlink()
        except Exception:
            pass

    # ── Execution scratchpad (per-thread, per-execution) ─────────────────

    def _get_scratchpad(self) -> dict:
        """Get or create the scratchpad for the current execution thread."""
        if not hasattr(self._execution_local, "scratchpad"):
            self._execution_local.scratchpad = {"plan": [], "notes": []}
        return self._execution_local.scratchpad

    def _clear_scratchpad(self) -> None:
        """Reset the scratchpad at the start of a new execution."""
        self._execution_local.scratchpad = {"plan": [], "notes": []}

    def _execution_plan(self, tasks: list[str]) -> dict:
        """Set or replace the execution plan."""
        pad = self._get_scratchpad()
        pad["plan"] = [{"task": t, "status": "todo"} for t in tasks]
        return {"ok": True, "tasks": len(tasks)}

    def _execution_update(self, task_index: int, status: str, note: str | None = None) -> dict:
        """Update a task status and optionally add a note."""
        pad = self._get_scratchpad()
        valid_statuses = {"todo", "in-progress", "done", "skipped", "failed"}
        if status not in valid_statuses:
            status = "done"

        if task_index >= 0:
            if task_index < len(pad["plan"]):
                pad["plan"][task_index]["status"] = status
            else:
                return {"error": f"task_index {task_index} out of range (plan has {len(pad['plan'])} items)"}

        if note:
            pad["notes"].append(note)

        return {"ok": True}

    def _format_scratchpad(self) -> str | None:
        """Format the scratchpad as a concise system message, or None if empty."""
        pad = self._get_scratchpad()
        if not pad["plan"] and not pad["notes"]:
            return None

        parts: list[str] = ["## Execution Progress"]
        if pad["plan"]:
            status_icons = {
                "todo": "⬚",
                "in-progress": "▶",
                "done": "✓",
                "skipped": "⊘",
                "failed": "✗",
            }
            for i, item in enumerate(pad["plan"]):
                icon = status_icons.get(item["status"], "?")
                parts.append(f"{i+1}. [{icon}] {item['task']}")

        if pad["notes"]:
            parts.append("\nFindings:")
            for note in pad["notes"][-12:]:  # keep last 12 notes to avoid bloat
                parts.append(f"- {note}")

        return "\n".join(parts)

    def _promote_scratchpad_to_memory(self) -> None:
        """After execution completes, promote important facts from the scratchpad to long-term memory.

        This runs in a separate lightweight LLM call and does NOT affect the
        user-agent conversation context/session/trim in any way.
        """
        pad = self._get_scratchpad()
        if not pad["plan"] and not pad["notes"]:
            return

        scratchpad_text = self._format_scratchpad() or ""
        if not scratchpad_text.strip():
            return

        # Only promote if there was meaningful work (at least one done/failed task or notes)
        has_work = any(t["status"] in {"done", "failed"} for t in pad["plan"]) or pad["notes"]
        if not has_work:
            return

        try:
            response = self.client.chat.completions.create(
                model=MODEL_NAME,
                temperature=0,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a memory extractor. Given an execution scratchpad from a completed task, "
                            "extract ONLY durable, important facts worth remembering long-term. "
                            "Examples: service ports, config file paths, container names, deployment states, "
                            "user preferences discovered during work, infrastructure changes made.\n\n"
                            "Rules:\n"
                            "- Output JSON only: {\"actions\": [...]}\n"
                            "- Each action: {\"type\": \"remember_note\", \"note\": \"...\"} or "
                            "{\"type\": \"set_infrastructure_note\", \"key\": \"...\", \"value\": \"...\"}\n"
                            "- Maximum 5 actions. Be very selective — only truly durable facts.\n"
                            "- If nothing is worth remembering, return {\"actions\": []}\n"
                            "- Do not invent facts. Only extract from the scratchpad."
                        ),
                    },
                    {"role": "user", "content": scratchpad_text},
                ],
            )
            raw = response.choices[0].message.content or "{}"
            parsed = self._extract_json_object(raw)
            actions = parsed.get("actions", [])
            if isinstance(actions, list) and actions:
                safe_actions = []
                for a in actions[:5]:
                    if isinstance(a, dict) and a.get("type") in {
                        "remember_note",
                        "set_infrastructure_note",
                        "set_user_info",
                    }:
                        safe_actions.append(a)
                if safe_actions:
                    self.memory.apply_memory_actions(safe_actions)
        except Exception:
            pass

    def _looks_like_telegram_setup_question(self, user_message: str) -> bool:
        """Detect requests about Telegram connection/setup/help."""
        text = (user_message or "").lower()
        if not text.strip():
            return False
        patterns = [
            r"telegram",
            r"botfather",
            r"tele\s*gram",
            r"bot token",
            r"allowed user",
            r"allowlist",
            r"connect.*telegram",
            r"setup.*telegram",
        ]
        return any(re.search(p, text) for p in patterns)

    def _answer_from_telegram_guide(self, user_message: str) -> str | None:
        """Use channels/telegram/CONNECT.md to answer Telegram setup questions."""
        guide = self._read_channel_guide("telegram", "CONNECT.md")
        if not guide:
            return None
        try:
            response = self.client.chat.completions.create(
                model=MODEL_NAME,
                temperature=0,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are helping with Telegram connection/setup. "
                            "Use ONLY the provided guide content as source of truth. "
                            "Give concise step-by-step instructions and ask only for missing values."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Telegram guide:\n"
                            f"{guide}\n\n"
                            "User request:\n"
                            f"{user_message}"
                        ),
                    },
                ],
            )
            return (response.choices[0].message.content or "").strip() or None
        except Exception:
            return None

    # ── Status notifications ──────────────────────────────────────────────────

    def _notify_status(self, message: str) -> None:
        """Fire status message to the registered callback (e.g. Telegram bot).
        Errors are swallowed so a bad callback never crashes the agent."""
        if self._status_callback is not None:
            try:
                self._status_callback(message)
            except Exception:
                pass

    def _tool_status_label(self, name: str, args: dict) -> str:
        """Return a short, human-readable description of the tool being invoked."""
        if name in {"run_command", "run_host_command_via_docker"}:
            cmd = args.get("command", "")
            label = cmd if len(cmd) <= 90 else cmd[:87] + "…"
            readonly = args.get("readonly", True)
            icon = "🔍" if readonly else "🔧"
            if name == "run_host_command_via_docker":
                return f"🔧 Host: `{label}`"
            target = args.get("target")
            suffix = f"  _(on {target})_" if target else ""
            return f"{icon} `{label}`{suffix}"
        if name == "send_process_input":
            inp = args.get("input_text", "")
            label = inp if len(inp) <= 60 else inp[:57] + "…"
            return f"⌨️ Sending input: `{label}`"
        if name == "workspace_read":
            return f"📖 Reading `{args.get('filename', '?')}`"
        if name == "workspace_write":
            return f"✏️ Writing `{args.get('filename', '?')}`"
        if name == "workspace_delete":
            return f"🗑️ Deleting `{args.get('filename', '?')}`"
        if name == "execution_plan":
            count = len(args.get("tasks", []))
            return f"📋 Planning {count} tasks"
        if name == "execution_update":
            return None  # silent — don't spam status for internal tracking
        if name in {"job_create", "job_cancel", "job_list", "job_update"}:
            return None  # silent — let agent reply naturally after tool completes
        if name == "tavily_web_search":
            query = args.get("query", "")
            label = query if len(query) <= 80 else query[:77] + "…"
            return f"🔎 Searching web: {label}"
        if name == "message_user":
            msg = args.get("message", "")
            preview = msg if len(msg) <= 60 else msg[:57] + "…"
            chans = args.get("channels") or []
            target = f" → {', '.join(chans)}" if chans else " → all channels"
            return f"💬 Messaging{target}: {preview}"
        if name == "channels_list":
            return "📡 Checking configured channels"
        if name == "skill_create":
            return f"🔨 Creating skill `{args.get('tool_name', '?')}`"
        if name == "skill_update":
            return f"✏️ Updating skill `{args.get('tool_name', '?')}`"
        if name == "skill_delete":
            return f"🗑️ Deleting skill `{args.get('tool_name', '?')}`"
        if name == "skill_list":
            return None  # silent
        if name == "skill_read":
            return f"📖 Reading skill `{args.get('tool_name', '?')}`"
        if self._custom_skills.is_skill(name):
            return f"⚙️ Running `{name}`"
        return f"⚙️ {name}"

    def _tool_done_label(self, name: str, args: dict, result_json: str) -> str | None:
        """Return a short human-friendly completion label, or None to skip."""
        try:
            result = json.loads(result_json)
        except (json.JSONDecodeError, TypeError):
            result = {}

        if name in {"run_command", "run_host_command_via_docker"}:
            if result.get("waiting_for_input"):
                prompt = result.get("prompt", "")
                label = prompt if len(prompt) <= 60 else prompt[:57] + "…"
                return f"⏳ Waiting for input: {label}" if label else "⏳ Process waiting for input"
            ec = result.get("exit_code")
            if ec is not None:
                return "✓ Command finished" if ec == 0 else f"✗ Command failed (exit {ec})"
            if result.get("error"):
                return "✗ Command error"
            return "✓ Done"

        if name == "send_process_input":
            if result.get("waiting_for_input"):
                return "⏳ Still waiting for input"
            ec = result.get("exit_code")
            if ec is not None:
                return "✓ Process finished" if ec == 0 else f"✗ Process exited ({ec})"
            return "✓ Input sent"

        if name == "workspace_read":
            fname = args.get("filename", "?")
            if result.get("error"):
                return f"✗ {fname} not found"
            return f"✓ Read {fname}"

        if name == "workspace_write":
            return f"✓ Wrote {args.get('filename', '?')}"

        if name == "workspace_delete":
            fname = args.get("filename", "?")
            if result.get("error"):
                return f"✗ {fname} not found"
            return f"✓ Deleted {fname}"

        if name in {"execution_plan", "execution_update"}:
            return None  # silent — internal tracking

        if name in {"job_create", "job_cancel", "job_update", "job_list"}:
            return None  # silent — let agent reply naturally

        if name == "message_user":
            sent = result.get("sent_to", [])
            failed = result.get("failed", [])
            if sent:
                return f"✓ Message sent → {', '.join(sent)}"
            if failed:
                return f"✗ Message failed → {', '.join(failed)}"
            return "✓ Message sent"

        if name == "channels_list":
            return None  # silent

        if name in {"skill_create", "skill_update", "skill_delete"}:
            if result.get("error"):
                return f"✗ {result['error']}"
            return f"✓ Skill `{args.get('tool_name', '?')}` updated"

        if name in {"skill_list", "skill_read"}:
            return None  # silent

        return "✓ Done"

    # ─────────────────────────────────────────────────────────────────────────

    def _set_current_execution_id(self, execution_id: str | None) -> None:
        self._execution_local.execution_id = execution_id

    def _get_current_execution_id(self) -> str | None:
        return getattr(self._execution_local, "execution_id", None)

    def _clear_current_execution_id(self) -> None:
        if hasattr(self._execution_local, "execution_id"):
            del self._execution_local.execution_id

    def _mark_execution_cancelled(self, execution_id: str | None) -> None:
        if not execution_id:
            return
        with self._cancelled_lock:
            self._cancelled_execution_ids.add(execution_id)

    def _clear_execution_cancelled(self, execution_id: str | None) -> None:
        if not execution_id:
            return
        with self._cancelled_lock:
            self._cancelled_execution_ids.discard(execution_id)

    def _is_execution_cancelled(self, execution_id: str | None = None) -> bool:
        execution_id = execution_id or self._get_current_execution_id()
        if not execution_id:
            return False
        with self._cancelled_lock:
            return execution_id in self._cancelled_execution_ids

    def _raise_if_execution_cancelled(self, execution_id: str | None = None) -> None:
        if self._is_execution_cancelled(execution_id):
            raise ExecutionStopped()

    def stop_execution(self, execution_id: str | None) -> bool:
        """Cancel a running execution and terminate any active subprocesses it owns."""
        if not execution_id:
            return False

        self._mark_execution_cancelled(execution_id)
        stopped_any = False

        with self._sessions_lock:
            matching = [
                (session_id, session)
                for session_id, session in self._process_sessions.items()
                if session.get("execution_id") == execution_id
            ]

        for session_id, session in matching:
            proc = session.get("proc")
            master_fd = session.get("master_fd")
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=1)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                stopped_any = True
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
            with self._sessions_lock:
                self._process_sessions.pop(session_id, None)
            self._log_command_event(
                "command_stopped",
                session_id=session_id,
                execution_id=execution_id,
                tool_name=session.get("tool_name", "run_command"),
                execution_target=session.get("execution_target", "local"),
                command=session.get("display_command", ""),
            )

        return stopped_any

    def _resolve_run_target(self, target: str | None) -> str:
        t = (target or "auto").strip().lower()
        if t not in {"auto", "host", "local"}:
            t = "auto"
        if t == "auto":
            return "host" if self._running_in_docker else "local"
        return t

    def _preview_execution_target(self, tool_name: str, args: dict) -> str:
        if tool_name == "run_host_command_via_docker":
            return "host"
        if tool_name == "run_command":
            return self._resolve_run_target(args.get("target"))
        return "local"

    def _run_command(self, command: str, target: str | None) -> dict:
        resolved = self._resolve_run_target(target)
        if resolved == "host":
            result = self._run_host_command_via_docker(command, source_tool="run_command")
        else:
            result = self._start_shell_command(
                command,
                display_command=command,
                execution_target=resolved,
                tool_name="run_command",
            )
        if isinstance(result, dict):
            result.setdefault("execution_target", resolved)
        return result

    def _command_signature(self, tool_name: str, args: dict) -> str:
        command = str(args.get("command", "")).strip()
        target = self._preview_execution_target(tool_name, args)
        return f"{target}|{command}"

    def _execute_tool(self, name: str, args: dict) -> str:
        if name == "run_command":
            result = self._run_command(args.get("command", ""), args.get("target"))
        elif name == "run_host_command_via_docker":
            result = self._run_host_command_via_docker(args.get("command", ""))
        elif name == "send_process_input":
            result = self._send_process_input(
                args.get("session_id", ""),
                args.get("input_text", ""),
            )
        elif name == "workspace_read":
            result = self._workspace_read(args.get("filename", ""))
        elif name == "workspace_write":
            result = self._workspace_write(args.get("filename", ""), args.get("content", ""))
        elif name == "workspace_delete":
            result = self._workspace_delete(args.get("filename", ""))
        elif name == "execution_plan":
            result = self._execution_plan(args.get("tasks", []))
        elif name == "execution_update":
            result = self._execution_update(
                args.get("task_index", -1),
                args.get("status", "done"),
                args.get("note"),
            )
        elif name == "logs_read":
            result = self._logs_read(args.get("filename", "commands.jsonl"), args.get("limit", 30))
        elif name == "job_create":
            if self.scheduler is None:
                result = {"error": "scheduler is not running"}
            else:
                job = self.scheduler.add_job(
                    kind=args.get("kind", "agent_task"),
                    description=args.get("description", ""),
                    schedule_mode=args.get("schedule_mode", "heartbeat"),
                    interval_minutes=int(args.get("interval_minutes", 15) or 15),
                    cron=args.get("cron"),
                    run_at=args.get("run_at"),
                    cancel_after_run=args.get("cancel_after_run"),
                    message=args.get("message"),
                    channels=args.get("channels"),
                    command=args.get("command"),
                    context_mode=args.get("context_mode"),
                )
                from datetime import datetime, timezone as _tz
                fires_at = job.get("next_run_at")
                human_time = ""
                if fires_at:
                    try:
                        dt = datetime.fromisoformat(fires_at.replace("Z", "+00:00"))
                        mins = round((dt - datetime.now(_tz.utc)).total_seconds() / 60)
                        human_time = f"in about {mins} minute{'s' if mins != 1 else ''}" if mins > 0 else "shortly"
                    except Exception:
                        pass
                result = {
                    "ok": True,
                    "job_id": job.get("id"),
                    "kind": job.get("kind"),
                    "scheduled": human_time or "as requested",
                    "mode": job.get("schedule_mode", "heartbeat"),
                    "cancel_after_run": job.get("cancel_after_run", False),
                }
        elif name == "job_cancel":
            if self.scheduler is None:
                result = {"error": "scheduler is not running"}
            else:
                ok = self.scheduler.cancel_job(args.get("job_id", ""))
                result = {"ok": ok, "job_id": args.get("job_id", "")} if ok else {"error": "job not found", "job_id": args.get("job_id", "")}
        elif name == "job_update":
            if self.scheduler is None:
                result = {"error": "scheduler is not running"}
            else:
                updated = self.scheduler.update_job(args.get("job_id", ""), **{
                    k: v for k, v in args.items() if k != "job_id"
                })
                result = updated if updated is not None else {"error": "job not found"}
        elif name == "job_list":
            if self.scheduler is None:
                result = {"jobs": [], "note": "scheduler is not running"}
            else:
                result = {"jobs": self.scheduler.list_jobs()}
        elif name == "message_user":
            result = self._tool_message_user(args.get("message", ""), args.get("channels") or [])
        elif name == "channels_list":
            result = self._tool_channels_list()
        elif name == "skill_create":
            result = self._tool_skill_create(
                args.get("tool_name", ""), args.get("skill_code", ""), args.get("skill_guide", "")
            )
        elif name == "skill_update":
            result = self._tool_skill_update(
                args.get("tool_name", ""), args.get("skill_code"), args.get("skill_guide")
            )
        elif name == "skill_delete":
            result = self._tool_skill_delete(args.get("tool_name", ""))
        elif name == "skill_list":
            result = self._tool_skill_list()
        elif name == "skill_read":
            result = self._tool_skill_read(args.get("tool_name", ""))
        elif name in self._skill_handlers:
            result = self._skill_handlers[name](args)
        elif self._custom_skills.is_skill(name):
            result = self._custom_skills.execute(name, args)
        else:
            result = {"error": f"Unknown tool: {name}"}
        compact_result = self._compact_tool_result_for_model(result)
        return json.dumps(compact_result)

    # Regex to detect leaked internal backend artifacts in model text.
    # Narrow patterns only — must NOT catch JSON/code the model intentionally
    # shows the user (configs, examples, tutorials).
    _LEAK_RE = re.compile(
        r'"to"\s*:\s*"functions\.'    # hallucinated tool call
        r'|"tool_call_id"\s*:'
        r'|"started_at"\s*:\s*"\d'
        r'|^\s*PHASE:\s'
        r'|^\s*RULE:\s'
        r'|^\s*\[FOCUS\]',
        re.MULTILINE,
    )

    def _sanitize_response(self, text: str) -> str:
        """Strip raw backend artifacts from model text before showing to user."""
        if not text or not text.strip():
            return ""
        lines = text.splitlines()
        clean = [ln for ln in lines if not self._LEAK_RE.search(ln)]
        result = "\n".join(clean).strip()
        return result if len(result) >= 3 else ""

    def _summarize_large_output(self, text: str, context_hint: str = "") -> str:
        """Use a sub-LLM call to summarize large command output.

        Returns a concise summary that preserves key data, URLs, names,
        numbers, and findings from the raw output.
        """
        prompt = (
            "You are a concise data-extraction assistant. "
            "The user ran a command and got a large output. "
            "Extract and summarize ALL key information: names, URLs, numbers, "
            "dates, error messages, status codes, and important findings. "
            "Preserve exact data — do NOT paraphrase URLs, names, or numbers. "
            "Be thorough but concise. No filler. Output plain text, not JSON."
        )
        user_content = text
        if context_hint:
            user_content = f"Command context: {context_hint}\n\n{text}"
        # Clip the input to the summarizer to avoid blowing its context
        if len(user_content) > 60000:
            user_content = user_content[:30000] + "\n...middle omitted...\n" + user_content[-30000:]
        try:
            resp = self.client.chat.completions.create(
                model=MODEL_NAME,
                temperature=0,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_content},
                ],
            )
            summary = (resp.choices[0].message.content or "").strip()
            if summary:
                return summary
        except Exception:
            pass
        # Fallback: just clip if summarization fails
        return _clip_text(text, 6000)

    def _compact_tool_result_for_model(self, result: dict) -> dict:
        """Reduce oversized tool payloads before sending them back into model context.

        Strategy: be lenient with small/medium outputs (let the model see raw data).
        For large outputs, auto-summarize via a sub-LLM call so the model gets
        useful content instead of truncated garbage.
        """
        if not isinstance(result, dict):
            return {"result": _clip_text(str(result), 4000)}

        # Generous raw limit — only summarize when truly large
        SUMMARIZE_THRESHOLD = 8000  # per-field: summarize above this
        OVERALL_LIMIT = 16000       # total serialized: summarize largest field

        compact = dict(result)
        for key in ("stdout", "output"):
            value = compact.get(key)
            if not isinstance(value, str):
                continue
            if len(value) > SUMMARIZE_THRESHOLD:
                # Build a context hint from the command if available
                hint = ""
                if isinstance(compact.get("command"), str):
                    hint = compact["command"]
                summary = self._summarize_large_output(value, context_hint=hint)
                compact[key] = summary
                compact[f"{key}_auto_summarized"] = True
                compact[f"{key}_original_chars"] = len(value)

        # Clip small fields that don't need summarization
        for key, limit in (("prompt", 800), ("error", 1500)):
            value = compact.get(key)
            if isinstance(value, str) and len(value) > limit:
                compact[key] = _clip_text(value, limit)

        serialized = json.dumps(compact)
        if len(serialized) > OVERALL_LIMIT:
            # Find the largest text field and summarize it
            largest_key, largest_len = None, 0
            for k, v in compact.items():
                if isinstance(v, str) and len(v) > largest_len:
                    largest_key, largest_len = k, len(v)
            if largest_key and largest_len > 2000:
                compact[largest_key] = self._summarize_large_output(
                    compact[largest_key], context_hint="overflow reduction"
                )
                compact[f"{largest_key}_auto_summarized"] = True
            # Final safety clip
            serialized = json.dumps(compact)
            if len(serialized) > OVERALL_LIMIT:
                for k in ("stdout", "output"):
                    if isinstance(compact.get(k), str) and len(compact[k]) > 4000:
                        compact[k] = _clip_text(compact[k], 4000)

        return compact

    def _compact_messages_for_context(self, messages: list[dict], keep_last: int = 20) -> list[dict]:
        """Shrink message payloads using tiered clipping.

        Recent messages get generous limits (tool results need to be visible).
        Older messages get progressively more aggressive clipping.
        The first user message is always preserved so the model never forgets
        the original request.
        """
        if not messages:
            return messages

        system_messages = [m for m in messages if m.get("role") == "system"]
        other_messages = [m for m in messages if m.get("role") != "system"]

        # Always preserve the first AND latest user messages.
        # First = original intent. Latest = current task. Both survive compaction.
        first_user = None
        first_user_idx = -1
        last_user = None
        last_user_idx = -1
        for i, m in enumerate(other_messages):
            if m.get("role") == "user":
                if first_user is None:
                    first_user = m
                    first_user_idx = i
                last_user = m
                last_user_idx = i

        tail = other_messages[-keep_last:]

        # If the first user message was trimmed, prepend it
        if first_user and first_user_idx >= 0 and other_messages[first_user_idx] not in tail:
            tail = [first_user] + tail

        # If the latest user message was also trimmed (different from first), append it
        if (
            last_user
            and last_user is not first_user
            and last_user_idx >= 0
            and other_messages[last_user_idx] not in tail
        ):
            tail.append(last_user)

        # Tiered clipping: recent messages get more room
        n = len(tail)
        compacted: list[dict] = []

        # System messages: clip to 3200 each
        for message in system_messages:
            item = dict(message)
            content = item.get("content")
            if isinstance(content, str):
                item["content"] = _clip_text(content, 3200)
            compacted.append(item)

        # Non-system messages: tiered limits
        for i, message in enumerate(tail):
            item = dict(message)
            content = item.get("content")
            if isinstance(content, str):
                age = n - i  # 1 = newest, n = oldest
                if age <= 5:  # very recent: full detail for tool results
                    limit = 6000
                elif age <= 10:  # middle: moderate clipping
                    limit = 3000
                else:  # old: aggressive
                    limit = 1200
                # User messages always get at least 2000 chars
                if item.get("role") == "user" and limit < 2000:
                    limit = 2000
                item["content"] = _clip_text(content, limit)
            compacted.append(item)

        # Sanitize: remove orphaned tool messages whose assistant+tool_calls
        # was trimmed away. The API rejects tool messages without a preceding
        # assistant message that references them via tool_calls.
        valid_tool_call_ids: set[str] = set()
        sanitized: list[dict] = []
        for msg in compacted:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id:
                        valid_tool_call_ids.add(tc_id)
            if msg.get("role") == "tool":
                if msg.get("tool_call_id") not in valid_tool_call_ids:
                    continue  # orphaned — drop it
            sanitized.append(msg)
        return sanitized

    # ------------------------------------------------------------------
    # PTY-based interactive process execution
    # ------------------------------------------------------------------

    def _prepare_shell_command(self, command: str) -> str:
        """Normalize command for interactive mediation.

        sudo often reads from /dev/tty, which can bypass chat mediation and
        block the host terminal directly. Force stdin-based prompt handling.
        """
        stripped = command.lstrip()
        if re.match(r"^sudo(?:\s|$)", stripped) and " -S" not in f" {stripped} ":
            # Keep prompt short and machine-detectable in PTY output.
            return re.sub(r"^\s*sudo\b", "sudo -S -p '[sudo] password: '", command, count=1)
        return command

    def _start_shell_command(
        self,
        command: str,
        *,
        display_command: str | None = None,
        execution_target: str = "local",
        tool_name: str = "run_command",
    ) -> dict:
        """Start command under a PTY. Returns completed result or waiting_for_input."""
        if not command.strip():
            return {"error": "Empty command"}
        command = self._prepare_shell_command(command)
        shown_command = display_command or command
        try:
            master_fd, slave_fd = pty.openpty()
        except Exception as exc:
            return {"error": f"PTY creation failed: {exc}"}
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                start_new_session=True,
            )
        except Exception as exc:
            os.close(master_fd)
            os.close(slave_fd)
            return {"error": str(exc)}
        os.close(slave_fd)  # parent closes its slave copy; child keeps the fd
        session_id = uuid.uuid4().hex[:8]
        self._log_command_event(
            "command_started",
            session_id=session_id,
            execution_id=self._get_current_execution_id(),
            tool_name=tool_name,
            execution_target=execution_target,
            command=shown_command,
            actual_command=command,
        )
        with self._sessions_lock:
            self._process_sessions[session_id] = {
                "proc": proc,
                "master_fd": master_fd,
                "tool_name": tool_name,
                "execution_target": execution_target,
                "display_command": shown_command,
                "actual_command": command,
                "execution_id": self._get_current_execution_id(),
            }
        return self._collect_session_output(session_id)

    def _collect_session_output(self, session_id: str, quiet_timeout: float = 2.0) -> dict:
        """Read output until the process exits OR goes quiet (waiting for input).

        Returns:
          {stdout, exit_code}                          — process finished
          {waiting_for_input, session_id, output, prompt} — process paused
        """
        with self._sessions_lock:
            session = self._process_sessions.get(session_id)
        if not session:
            return {"error": f"Session '{session_id}' not found."}

        self._raise_if_execution_cancelled(session.get("execution_id"))

        proc = session["proc"]
        master_fd = session["master_fd"]
        tool_name = session.get("tool_name", "run_command")
        execution_target = session.get("execution_target", "local")
        display_command = session.get("display_command", "")
        chunks: list[str] = []
        last_data_at = time.monotonic()
        idle_rounds = 0
        max_idle_rounds = 30

        while True:
            elapsed_quiet = time.monotonic() - last_data_at
            remaining = quiet_timeout - elapsed_quiet
            if remaining <= 0:
                if proc.poll() is None:
                    current_output = _strip_ansi("".join(chunks))
                    if _looks_like_input_prompt(current_output):
                        break
                    idle_rounds += 1
                    if idle_rounds < max_idle_rounds:
                        last_data_at = time.monotonic()
                        continue
                break

            try:
                ready, _, _ = select.select([master_fd], [], [], min(remaining, 0.15))
            except (ValueError, OSError):
                break  # master fd closed

            if ready:
                try:
                    data = os.read(master_fd, 4096)
                    if data:
                        chunks.append(data.decode("utf-8", errors="replace"))
                        last_data_at = time.monotonic()
                        idle_rounds = 0
                except OSError:
                    break  # PTY closed (process exited)

            if proc.poll() is not None:
                # Drain any remaining output
                while True:
                    try:
                        r, _, _ = select.select([master_fd], [], [], 0.1)
                        if not r:
                            break
                        data = os.read(master_fd, 4096)
                        if data:
                            chunks.append(data.decode("utf-8", errors="replace"))
                    except OSError:
                        break
                break

        output = _strip_ansi("".join(chunks))

        if proc.poll() is not None:
            # Process finished — clean up session
            with self._sessions_lock:
                self._process_sessions.pop(session_id, None)
            try:
                os.close(master_fd)
            except OSError:
                pass
            result = {"stdout": output, "exit_code": proc.returncode}
            self._log_command_event(
                "command_finished",
                session_id=session_id,
                execution_id=session.get("execution_id"),
                tool_name=tool_name,
                execution_target=execution_target,
                command=display_command,
                exit_code=proc.returncode,
                output_tail=_clip_text(output),
            )
            return result

        # Process still running — extract last non-empty line as the prompt hint
        lines = [ln for ln in output.splitlines() if ln.strip()]
        prompt = lines[-1] if lines else ""
        result = {
            "waiting_for_input": True,
            "session_id": session_id,
            "output": output,
            "prompt": prompt,
        }
        self._log_command_event(
            "command_waiting_for_input",
            session_id=session_id,
            execution_id=session.get("execution_id"),
            tool_name=tool_name,
            execution_target=execution_target,
            command=display_command,
            prompt=prompt,
            output_tail=_clip_text(output),
        )
        return result

    def _send_process_input(self, session_id: str, input_text: str) -> dict:
        """Feed a line of input to a running session and collect the response."""
        with self._sessions_lock:
            session = self._process_sessions.get(session_id)
        if not session:
            return {"error": f"Session '{session_id}' not found or already completed."}
        self._raise_if_execution_cancelled(session.get("execution_id"))
        master_fd = session["master_fd"]
        payload = input_text if input_text.endswith("\n") else input_text + "\n"
        self._log_command_event(
            "command_input_sent",
            session_id=session_id,
            execution_id=session.get("execution_id"),
            tool_name=session.get("tool_name", "run_command"),
            execution_target=session.get("execution_target", "local"),
            command=session.get("display_command", ""),
            input="[redacted]",
            input_length=len(input_text),
        )
        try:
            os.write(master_fd, payload.encode("utf-8"))
        except OSError as exc:
            return {"error": f"Failed to write to process: {exc}"}
        time.sleep(0.1)  # brief pause to let process process the input
        return self._collect_session_output(session_id)

    def _run_host_command_via_docker(self, command: str, source_tool: str = "run_host_command_via_docker") -> dict:
        """Execute command on host via docker.sock using a privileged helper container."""
        if not command.strip():
            return {"error": "Empty command"}

        # Helper container enters host root and runs command as host-root context.
        helper = (
            "docker run --rm --privileged -v /:/host alpine "
            "sh -lc "
            + shlex.quote(f"chroot /host sh -lc {shlex.quote(command)}")
        )
        return self._start_shell_command(
            helper,
            display_command=command,
            execution_target="host",
            tool_name=source_tool,
        )

    def _build_base_messages(self) -> list[dict]:
        messages = [
            {"role": "system", "content": SYSTEM_CORE},
            {"role": "system", "content": SYSTEM_TOOL_RULES},
            {"role": "system", "content": SYSTEM_STYLE},
        ]
        if self._skill_guides:
            skill_guide_block = "\n\n---\n\n".join(self._skill_guides)
            messages.append({"role": "system", "content": f"## Skill Guides\n\n{skill_guide_block}"})
        messages.append({"role": "system", "content": self.memory.get_memory_summary()})
        # When BOOTSTRAP.md exists the LLM has never been set up yet.
        # Only inject if this is truly a fresh session (no prior conversation).
        # If there are already messages, the user is mid-conversation and
        # BOOTSTRAP causes the model to re-greet / lose context.
        bootstrap_file = self._workspace_dir / "BOOTSTRAP.md"
        recent = self.memory.get_recent_messages()
        if bootstrap_file.exists() and not recent:
            try:
                bootstrap_content = bootstrap_file.read_text(encoding="utf-8")
                messages.append({
                    "role": "system",
                    "content": (
                        "## First-Run Setup Instructions (BOOTSTRAP.md)\n\n"
                        + bootstrap_content
                        + "\n\n---\nIMPORTANT: Follow these instructions to drive the setup "
                        "conversation naturally. Use the workspace tools (workspace_read, "
                        "workspace_write, workspace_delete) to update files freely — "
                        "these never need confirmation. "
                        "When setup is fully complete, call workspace_delete('BOOTSTRAP.md')."
                    ),
                })
            except Exception:
                pass
        messages.extend(self.memory.get_recent_messages() if not recent else recent)
        return self._compact_messages_for_context(messages)

    def _extract_json_object(self, text: str) -> dict:
        candidate = text.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            candidate = "\n".join(lines).strip()
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass

        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                parsed = json.loads(candidate[start : end + 1])
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    def _apply_session_long_term_actions(self, actions: list[dict]) -> None:
        if not isinstance(actions, list):
            return

        allowed = {
            "set_user_info",
            "remove_user_info",
            "remember_note",
            "remove_session_note",
            "set_infrastructure_note",
            "remove_infrastructure_note",
        }

        safe_actions: list[dict] = []
        for action in actions[:8]:
            if not isinstance(action, dict):
                continue
            action_type = str(action.get("type", "")).strip()
            if action_type in allowed:
                safe_actions.append(action)

        if safe_actions:
            self.memory.apply_memory_actions(safe_actions)

    def _fallback_session_summary_bullets(self, recent_messages: list[dict]) -> list[str]:

        user_items = [
            str(m.get("content", "")).strip()
            for m in recent_messages
            if str(m.get("role", "")).strip().lower() == "user"
            and str(m.get("content", "")).strip()
        ]
        assistant_items = [
            str(m.get("content", "")).strip()
            for m in recent_messages
            if str(m.get("role", "")).strip().lower() == "assistant"
            and str(m.get("content", "")).strip()
        ]

        bullets: list[str] = []
        if user_items:
            bullets.append(
                "Recent user intents: "
                + " | ".join(_clip_text(text, 120).replace("\n", " ") for text in user_items[-3:])
            )
        if assistant_items:
            bullets.append(
                "Latest assistant actions: "
                + " | ".join(_clip_text(text, 120).replace("\n", " ") for text in assistant_items[-2:])
            )

        joined = "\n".join(user_items[-8:] + assistant_items[-6:]).lower()
        if "demo-webapp" in joined and ("8081" in joined or "host port" in joined):
            bullets.append("demo-webapp work happened in this session, including host port 8081 context.")
        if "la pino" in joined and "lucknow" in joined:
            bullets.append("Session included fetching La Pino'z Lucknow menu/location details.")

        cleaned_bullets: list[str] = []
        seen: set[str] = set()
        for bullet in bullets:
            clean = " ".join(bullet.split()).strip()
            key = clean.lower()
            if not clean or key in seen:
                continue
            seen.add(key)
            cleaned_bullets.append(clean)

        if not cleaned_bullets:
            return ["Session context retained from the latest conversation window."]

        return cleaned_bullets[:8]

    def _fallback_long_term_actions(self, recent_messages: list[dict]) -> list[dict]:
        actions: list[dict] = []
        joined_lines = [str(m.get("content", "")).strip() for m in recent_messages]

        # Durable user preference extraction (e.g., "I love pizza").
        for line in joined_lines[-12:]:
            match = re.search(
                r"\bi\s+(?:love|like)\s+([A-Za-z][A-Za-z\s\-']{0,60})\b",
                line,
                re.IGNORECASE,
            )
            if not match:
                continue
            topic = " ".join(match.group(1).strip(" .,!?\"").lower().split())
            if not topic:
                continue
            actions.append(
                {
                    "type": "set_user_info",
                    "key": f"Preference[{topic}]",
                    "value": "like",
                }
            )
            actions.append(
                {
                    "type": "remember_note",
                    "note": f"User likes {topic}.",
                }
            )
            break

        # Durable infra extraction for stable host port statements.
        block = "\n".join(joined_lines).lower()
        port_match = re.search(r"demo-webapp.*?(?:host\s+port\s*|0\.0\.0\.0:)(\d{2,5})", block)
        if port_match:
            port = port_match.group(1)
            actions.append(
                {
                    "type": "set_infrastructure_note",
                    "key": "demo-webapp host port",
                    "value": port,
                }
            )
            actions.append(
                {
                    "type": "remember_note",
                    "note": f"demo-webapp mapped on host port {port}.",
                }
            )

        deduped: list[dict] = []
        seen: set[str] = set()
        for action in actions:
            key = json.dumps(action, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(action)
        return deduped[:6]

    def _maybe_refresh_session_memory(self) -> None:
        recent_messages = self.memory.get_recent_messages()
        if len(recent_messages) < 20:
            return

        summarizer_input = {
            "memory_md": self.memory.get_memory_text(),
            "current_session_summary": self.memory.get_session_summary_text(),
            "recent_conversation": recent_messages,
        }

        parsed: dict = {}
        for extra_args in (
            {"response_format": {"type": "json_object"}},
            {},
        ):
            try:
                response = self.client.chat.completions.create(
                    model=MODEL_NAME,
                    temperature=0,
                    messages=[
                        {"role": "system", "content": SESSION_SUMMARIZER_PROMPT},
                        {"role": "user", "content": json.dumps(summarizer_input)},
                    ],
                    **extra_args,
                )
            except Exception:
                continue

            raw = response.choices[0].message.content or "{}"
            parsed = self._extract_json_object(raw)
            if parsed:
                break

        session_summary_bullets = parsed.get("session_summary_bullets", [])
        if not isinstance(session_summary_bullets, list) or not session_summary_bullets:
            session_summary_bullets = self._fallback_session_summary_bullets(
                recent_messages,
            )
        self.memory.set_session_summary_bullets(session_summary_bullets)

        long_term_actions = parsed.get("long_term_actions", [])
        if not isinstance(long_term_actions, list):
            long_term_actions = []
        if not long_term_actions:
            long_term_actions = self._fallback_long_term_actions(recent_messages)
        self._apply_session_long_term_actions(long_term_actions)

    def _plan_memory_actions(self, user_message: str) -> list[dict]:
        """Let the model infer memory intent and return editable actions."""
        memory_text = self.memory.get_memory_text()
        recent = self.memory.get_recent_messages()[-8:]

        planner_input = {
            "memory_md": memory_text,
            "recent_messages": recent,
            "current_user_message": user_message,
        }

        try:
            planner_resp = self.client.chat.completions.create(
                model=MODEL_NAME,
                temperature=0,
                messages=[
                    {"role": "system", "content": MEMORY_PLANNER_PROMPT},
                    {"role": "user", "content": json.dumps(planner_input)},
                ],
            )
        except Exception:
            return []

        raw = planner_resp.choices[0].message.content or "{}"
        parsed = self._extract_json_object(raw)
        actions = parsed.get("actions", [])
        if not isinstance(actions, list):
            return []

        # Keep planner safe and bounded.
        safe_actions: list[dict] = []
        allowed = {
            "set_user_info",
            "remove_user_info",
            "remember_note",
            "remove_session_note",
            "set_infrastructure_note",
            "remove_infrastructure_note",
            "forget_all_user_memory",
            "forget_last_remembered",
        }

        lowered = user_message.lower()
        broad_forget_intent = bool(
            re.search(
                r"\bforget\s+(?:everything|all|whatever)\s+(?:you\s+know\s+)?about\s+me\b",
                lowered,
            )
            or re.search(r"\bforget\s+me\b", lowered)
        )

        vague_forget_intent = bool(re.search(r"\bforget\s+(it|this|that)\s*$", lowered))

        for action in actions[:8]:
            if not isinstance(action, dict):
                continue
            t = str(action.get("type", "")).strip()
            if t in allowed:
                # Safety policy: avoid accidental global wipes for ambiguous forget commands.
                if t == "forget_all_user_memory" and not broad_forget_intent:
                    continue
                if t == "forget_last_remembered" and broad_forget_intent:
                    continue
                safe_actions.append(action)

        # If user said vague forget and planner didn't propose a delete, do minimal safe delete.
        if vague_forget_intent and not any(
            a.get("type") in {"forget_last_remembered", "remove_session_note", "remove_user_info"}
            for a in safe_actions
        ):
            safe_actions.append({"type": "forget_last_remembered"})

        return safe_actions

    # ── Onboarding flow ───────────────────────────────────────────────────────

    def has_pending_onboarding(self) -> bool:
        """Check if onboarding is pending (first-time setup)."""
        return not self._is_onboarding_complete()

    def onboard_step(self, step_number: int, user_response: str) -> tuple[str, bool]:
        """Execute one step of the onboarding flow.
        
        Args:
            step_number: Current step (1-6)
            user_response: User's answer to the current step
            
        Returns:
            (next_message, is_complete)
        """
        import re as re_module
        
        try:
            if step_number == 1:
                # Get agent name
                agent_name = user_response.strip()
                self._update_workspace_section("IDENTITY.md", "Name", f"`{agent_name}`")
                return (
                    f"Great! I'll be called **{agent_name}**. 🎉\n\n"
                    f"Next: What's my purpose or nature? (e.g., 'Infrastructure AI', 'DevOps Agent')",
                    False
                )

            elif step_number == 2:
                # Get agent nature
                agent_nature = user_response.strip()
                self._update_workspace_section("IDENTITY.md", "Nature", f"`{agent_nature}`")
                # Also update SOUL.md
                soul_content = self._read_workspace_file("SOUL.md")
                soul_content = re_module.sub(
                    r"(\- \*\*Nature\*\*: ).*?(?=\n|$)",
                    rf"\1`{agent_nature}`",
                    soul_content,
                    flags=re_module.IGNORECASE
                )
                self._write_workspace_file("SOUL.md", soul_content)
                return (
                    f"Excellent! I'm a **{agent_nature}**. 🚀\n\n"
                    f"Next: Pick an emoji for me (e.g., 🤖 🔧 ⚙️ 🐳)",
                    False
                )

            elif step_number == 3:
                # Get agent emoji
                agent_emoji = user_response.strip()
                # Extract just the emoji
                emoji_match = re_module.search(r'[\U0001F300-\U0001F9FF]', agent_emoji)
                if emoji_match:
                    agent_emoji = emoji_match.group(0)
                self._update_workspace_section("IDENTITY.md", "Emoji", agent_emoji)
                return (
                    f"Perfect! {agent_emoji} is my new look.\n\n"
                    f"Next: What's your name?",
                    False
                )

            elif step_number == 4:
                # Get user name
                user_name = user_response.strip()
                self._update_workspace_section("USER.md", "Name", f"`{user_name}`")
                # Also update memory
                mem_content = self.memory.get_memory_text()
                mem_content = re_module.sub(
                    r"(\- ).*?(?=\n|$)",
                    rf"\1**{user_name}'s user**",
                    mem_content,
                    count=1,
                    flags=re_module.IGNORECASE
                )
                return (
                    f"Nice to meet you, **{user_name}**! 👋\n\n"
                    f"Next: How would you like me to communicate? (brief/detailed/friendly/formal)",
                    False
                )

            elif step_number == 5:
                # Get communication preference
                comm_style = user_response.strip().lower()
                self._update_workspace_section("USER.md", "Communication Style", f"`{comm_style}`")
                # Also update SOUL.md
                soul_content = self._read_workspace_file("SOUL.md")
                soul_content = re_module.sub(
                    r"(\- ).*?(?=\n|$)",
                    rf"\1{comm_style.capitalize()}",
                    soul_content,
                    count=1,
                    flags=re_module.IGNORECASE
                )
                return (
                    f"Got it! I'll be **{comm_style}** in my responses.\n\n"
                    f"Next: What's your timezone? (e.g., UTC, EST, IST, PST)",
                    False
                )

            elif step_number == 6:
                # Get timezone
                timezone = user_response.strip().upper()
                self._update_workspace_section("USER.md", "Timezone", f"`{timezone}`")
                
                # Delete BOOTSTRAP.md to mark onboarding as complete
                self._delete_onboarding_file()
                
                # Update HEARTBEAT
                heartbeat_content = self._read_workspace_file("HEARTBEAT.md")
                heartbeat_content = heartbeat_content.replace(
                    "Awaiting first interaction",
                    "Ready and personalized"
                )
                self._write_workspace_file("HEARTBEAT.md", heartbeat_content)
                
                return (
                    f"🎉 **Setup complete!**\n\n"
                    f"I'm all personalized and ready to help you manage your Docker infrastructure. "
                    f"What would you like me to do?",
                    True
                )

            else:
                return ("Invalid step.", True)

        except Exception as e:
            return (f"Setup error: {str(e)}", True)

    def chat(self, user_message: str, execution_id: str | None = None, msg_context: dict | None = None) -> Union[str, ConfirmationRequest]:
        """Process a user message using long-term memory + restored session context.

        Returns either:
          - str: a plain text response
          - ConfirmationRequest: agent wants to run a destructive command and
            needs user confirmation before proceeding

        msg_context: optional metadata dict injected silently into the LLM context.
          Keys: 'channel' (str), 'timestamp' (str ISO), 'user_id' (int|str),
                'country' (str), 'city' (str), 'timezone' (str)
        On restart: agent has system prompt + memory/memory.md + memory/session.md + new messages.
        """
        self._set_current_execution_id(execution_id)
        self._clear_execution_cancelled(execution_id)
        try:
            self._raise_if_execution_cancelled(execution_id)
            
            # Update HEARTBEAT: processing started
            self._update_heartbeat("Busy", f"Processing: {user_message[:50]}...")

            # Autonomously update long-term memory via model-planned edit actions.
            actions = self._plan_memory_actions(user_message)
            self.memory.apply_memory_actions(actions)
            # Side-effect: if memory recorded Country or City, sync to config so
            # the scheduler can auto-derive the user's timezone for future jobs.
            self._sync_location_to_config(actions)

            # Add user message to runtime history
            self.memory.add_message("user", user_message)

            # Channel-aware guidance: answer Telegram setup from channels/telegram/CONNECT.md.
            if self._looks_like_telegram_setup_question(user_message):
                guided = self._answer_from_telegram_guide(user_message)
                if guided:
                    self.memory.add_message("assistant", guided)
                    self._update_heartbeat("Ready", "")
                    return guided

            # Build messages for LLM: memory summary + recent runtime messages.
            # Tool-call traffic is kept transient and is never persisted into runtime history.
            self._clear_scratchpad()
            messages = self._build_base_messages()

            # Silently inject incoming message metadata (channel, timestamp, user_id) when present.
            # This is transient — never stored in memory or session history.
            if msg_context:
                parts = []
                if ch := msg_context.get("channel"):
                    parts.append(f"channel: {ch}")
                if ts := msg_context.get("timestamp"):
                    parts.append(f"time: {ts}")
                if uid := msg_context.get("user_id"):
                    parts.append(f"user_id: {uid}")
                if country := msg_context.get("country"):
                    parts.append(f"user_country: {country}")
                if city := msg_context.get("city"):
                    parts.append(f"user_city: {city}")
                if tz := msg_context.get("timezone"):
                    parts.append(f"user_timezone: {tz}")
                if parts:
                    messages.append({
                        "role": "system",
                        "content": f"[Incoming message context — {', '.join(parts)}]",
                    })

            result = self._run_tool_loop(messages)
            
            # Promote important execution facts to long-term memory
            # (separate LLM call — does NOT affect user-agent context or session)
            self._promote_scratchpad_to_memory()
            
            # Update HEARTBEAT: processing complete
            self._update_heartbeat("Ready", "")
            
            return result
        except ExecutionStopped:
            self._update_heartbeat("Ready", "")
            return "Execution stopped. I'm ready for your next instruction."
        finally:
            self._clear_current_execution_id()

    def _run_tool_loop(
        self, messages: list[dict], approved_signatures: set[str] | None = None,
        _tools_override: list | None = None,
    ) -> Union[str, ConfirmationRequest]:
        """Core LLM + tool execution loop. Resumable after confirmation."""
        approved_signatures = approved_signatures or set()
        # Merge built-in tools with any currently loaded custom skill schemas.
        base_tools = self._tools + self._custom_skills.get_schemas()
        active_tools = _tools_override if _tools_override is not None else base_tools
        context_retry_used = False
        iteration = 0
        max_iterations = 30
        # Track repeated tool calls: signature -> count
        call_counts: dict[str, int] = {}

        while True:
            iteration += 1
            if iteration > max_iterations:
                # Don't return a canned response — ask the model to summarize
                # what it accomplished using the scratchpad and conversation.
                llm_messages = self._compact_messages_for_context(list(messages), keep_last=8)
                scratchpad_text = self._format_scratchpad()
                summary_parts = [
                    "You have reached the iteration limit. You MUST respond NOW.",
                    "Answer the user's question using the data from tool results above.",
                    "Present the INFORMATION you found — facts, data, names, URLs, details.",
                    "Do NOT describe what you searched or which tools you used.",
                    "Do NOT call any tools. Just give your final answer.",
                ]
                if scratchpad_text:
                    summary_parts.append(f"\n{scratchpad_text}")
                llm_messages.insert(0, {
                    "role": "system",
                    "content": "\n".join(summary_parts),
                })
                try:
                    response = self.client.chat.completions.create(
                        model=MODEL_NAME,
                        messages=llm_messages,
                    )
                    text = response.choices[0].message.content or ""
                except Exception:
                    text = "I ran out of steps. Here's what I got done so far — ask me to continue if you need more."
                self.memory.add_message("assistant", text)
                return text

            self._raise_if_execution_cancelled()
            if iteration == 1:
                self._notify_status("💭 Thinking...")

            # Inject execution scratchpad so the LLM always sees its progress,
            # even after message compaction. This is transient — never persisted.
            scratchpad_text = self._format_scratchpad()
            llm_messages = list(messages)

            # ── Phase-based dynamic control ───────────────────────────────
            # Auto-detect what phase we're in and inject ONE clear directive.
            # Mini models work best with "do THIS now" not "here's everything".
            pad = self._get_scratchpad()
            has_plan = bool(pad["plan"])
            all_done = has_plan and all(
                t["status"] in ("done", "skipped", "failed") for t in pad["plan"]
            )

            # Detect stale execution: model did significant tool work but
            # never updated the scratchpad (or has no plan).  After enough
            # iterations with tool results, force a RESPONSE so it summarises.
            tool_msg_count = sum(1 for m in messages if m.get("role") == "tool")
            stale_execution = (
                not all_done
                and iteration > 3
                and tool_msg_count >= 3
                and (
                    not has_plan
                    or not any(t["status"] in ("done", "failed") for t in pad["plan"])
                )
            )

            if iteration == 1 and not has_plan and not any(
                m.get("role") == "tool" for m in messages
            ):
                # Fresh turn, no prior tool work — let the model decide.
                # Simple messages ("hi", "thanks") get a direct reply.
                # Complex tasks get planned naturally.
                phase = "THINKING"
                phase_instruction = (
                    "PHASE: THINKING\n"
                    "Read the user's message.\n"
                    "If it needs multiple steps, call execution_plan() to plan.\n"
                    "If it's simple (greeting, question, single action), respond directly."
                )
            elif all_done or stale_execution:
                phase = "RESPONSE"
                phase_instruction = (
                    "PHASE: RESPONSE\n"
                    "Answer the user's question using the data from tool results above.\n"
                    "Present the INFORMATION — facts, data, names, numbers, URLs, key findings.\n"
                    "Do NOT narrate what you searched or which commands you ran.\n"
                    "Do NOT re-greet, re-introduce yourself, or ask setup questions again.\n"
                    "No tool calls."
                )
            else:
                phase = "EXECUTION"
                phase_instruction = (
                    "PHASE: EXECUTION\n"
                    "Execute the NEXT step from the plan using tools.\n"
                    "After each tool result, call execution_update() to mark the step done "
                    "and record key findings in the note.\n"
                    "When all steps are done, respond to the user with your findings."
                )

            # Find the latest user message for task context
            latest_request = None
            for m in reversed(messages):
                if m.get("role") == "user":
                    latest_request = (m.get("content") or "")[:500]
                    break

            phase_content = phase_instruction + (
                "\n\nRULE:\n"
                "You MUST follow this phase strictly.\n"
                "If you break phase:\n"
                "- your response is invalid\n"
                "- do not mix phases"
            )
            if latest_request:
                phase_content += f"\n\nCurrent task: {latest_request}"

            # Strip BOOTSTRAP.md once tool work has started.
            # On iteration 1 with no tool messages, BOOTSTRAP is needed for onboarding.
            # After that, it causes context pollution and re-greeting.
            has_tool_work = any(m.get("role") == "tool" for m in messages)
            if has_tool_work or iteration > 1:
                llm_messages = [
                    m for m in llm_messages
                    if not (
                        m.get("role") == "system"
                        and isinstance(m.get("content"), str)
                        and "First-Run Setup Instructions (BOOTSTRAP.md)" in m["content"]
                    )
                ]

            # Order: [PHASE] [SCRATCHPAD] [CORE] [TOOL_RULES] [STYLE] [MEMORY] [MESSAGES]
            # Phase control goes first — mini models have strong primacy bias.
            llm_messages.insert(0, {
                "role": "system",
                "content": phase_content,
            })

            if scratchpad_text:
                # Insert scratchpad right after phase (position 1)
                llm_messages.insert(1, {
                    "role": "system",
                    "content": scratchpad_text,
                })

            try:
                response = self.client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=llm_messages,
                    tools=active_tools,
                    tool_choice="auto",
                )
            except Exception as exc:
                if _is_context_length_error(exc):
                    if not context_retry_used:
                        messages = self._compact_messages_for_context(messages, keep_last=8)
                        context_retry_used = True
                        continue
                    return (
                        "The working context became too large due to very heavy command output. "
                        "I can continue in compact mode and focus only on key results, or if you need raw/full output "
                        "I will fetch it in smaller filtered chunks. Reply with: compact or raw."
                    )
                raise

            self._raise_if_execution_cancelled()

            msg = response.choices[0].message

            # No tool calls — plain text reply
            if not msg.tool_calls:
                text = msg.content or ""
                text = self._sanitize_response(text)
                if not text:
                    # Entirely empty after sanitization — nudge a retry
                    messages.append({"role": "assistant", "content": ""})
                    messages.append({"role": "user", "content": "Please give me your answer."})
                    continue
                self.memory.add_message("assistant", text)
                self._maybe_refresh_session_memory()
                return text

            # ── Detect repeated tool calls ────────────────────────────────
            repeat_limit = 2  # same tool+args allowed at most this many times
            plan_limit = 1    # execution_plan allowed only once per turn
            blocked_ids: set[str] = set()
            for tc in msg.tool_calls:
                sig = f"{tc.function.name}|{tc.function.arguments}"
                call_counts[sig] = call_counts.get(sig, 0) + 1
                if call_counts[sig] > repeat_limit:
                    blocked_ids.add(tc.id)
                # execution_plan: only allow once per turn (any args)
                if tc.function.name == "execution_plan":
                    plan_key = "__execution_plan__"
                    call_counts[plan_key] = call_counts.get(plan_key, 0) + 1
                    if call_counts[plan_key] > plan_limit:
                        blocked_ids.add(tc.id)

            # If ALL tool calls in this iteration are repeats, force a
            # summary LLM call instead of returning a canned string.
            if blocked_ids and blocked_ids == {tc.id for tc in msg.tool_calls}:
                # Build a compact context and ask the model to summarise.
                summary_msgs = self._compact_messages_for_context(list(messages), keep_last=8)
                sp = self._format_scratchpad()
                summary_parts = [
                    "You already completed your tool work. Now respond to the user.",
                    "Answer the user's question using the data from tool results above.",
                    "Present the INFORMATION — facts, data, names, URLs, key findings.",
                    "Do NOT describe what you searched or which tools you used.",
                    "Do NOT call any tools. Just give your final answer.",
                ]
                if sp:
                    summary_parts.append(f"\n{sp}")
                summary_msgs.insert(0, {
                    "role": "system",
                    "content": "\n".join(summary_parts),
                })
                try:
                    summary_resp = self.client.chat.completions.create(
                        model=MODEL_NAME,
                        messages=summary_msgs,
                    )
                    text = summary_resp.choices[0].message.content or ""
                    text = self._sanitize_response(text)
                except Exception:
                    text = ""
                if not text:
                    text = msg.content or "I've completed the work. Let me know if you need details."
                self.memory.add_message("assistant", text)
                self._maybe_refresh_session_memory()
                return text

            assistant_message = {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }

            # IMPORTANT: Accumulate tool history on top of existing messages
            # instead of rebuilding from scratch. This prevents the model from
            # forgetting the tool calls it already made earlier in this execution.
            messages.append(assistant_message)

            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")

                # Skip blocked repeat calls — return a hint instead
                if tc.id in blocked_ids:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps({"note": "Already done in a previous step. Move on."}),
                    })
                    continue

                cmd_sig = self._command_signature(tc.function.name, args)

                # Potentially mutating command tools require user confirmation.
                if tc.function.name in {"run_command", "run_host_command_via_docker"} and not args.get("readonly", True):
                    if cmd_sig in approved_signatures:
                        self._notify_status(self._tool_status_label(tc.function.name, args))
                        result = self._execute_tool(tc.function.name, args)
                        done = self._tool_done_label(tc.function.name, args, result)
                        if done:
                            self._notify_status(done)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result,
                            }
                        )
                        continue

                    target = self._preview_execution_target(tc.function.name, args)
                    agent_pretext = msg.content or ""
                    confirm_msg = (
                        f"{agent_pretext}\n\n"
                        if agent_pretext.strip()
                        else ""
                    ) + (
                        f"I want to run the following command on {target}:\n"
                        f"```\n{args['command']}\n```\n"
                        f"This could modify system state. Allow me to proceed?"
                    )
                    return ConfirmationRequest(
                        command=args["command"],
                        message=confirm_msg.strip(),
                        call_id=tc.id,
                        tool_name=tc.function.name,
                        tool_args=args,
                        approval_signature=cmd_sig,
                        execution_id=self._get_current_execution_id(),
                        approved_signatures=set(approved_signatures),
                        pending_messages=list(messages),
                    )

                label = self._tool_status_label(tc.function.name, args)
                if label:
                    self._notify_status(label)
                result = self._execute_tool(tc.function.name, args)
                done = self._tool_done_label(tc.function.name, args, result)
                if done:
                    self._notify_status(done)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    }
                )

            # Compact only when messages grow large — don't rebuild from scratch
            messages = self._compact_messages_for_context(messages)

    def confirm_and_run(self, req: ConfirmationRequest) -> Union[str, ConfirmationRequest]:
        """User confirmed — execute the pending command and resume the tool loop."""
        self._set_current_execution_id(req.execution_id)
        try:
            self._raise_if_execution_cancelled(req.execution_id)
            result = self._execute_tool(req.tool_name, req.tool_args)
            resumed = list(req.pending_messages)
            resumed.append(
                {
                    "role": "tool",
                    "tool_call_id": req.call_id,
                    "content": result,
                }
            )
            approved = set(req.approved_signatures)
            approved.add(req.approval_signature)
            response = self._run_tool_loop(resumed, approved_signatures=approved)
            return response
        except ExecutionStopped:
            return "Execution stopped. I'm ready for your next instruction."
        finally:
            self._clear_current_execution_id()

    def deny(self, req: ConfirmationRequest) -> str:
        """User denied — tell the agent the command was cancelled and get a reply."""
        self._set_current_execution_id(req.execution_id)
        try:
            resumed = list(req.pending_messages)
            resumed.append(
                {
                    "role": "tool",
                    "tool_call_id": req.call_id,
                    "content": json.dumps({"cancelled": True, "reason": "User denied permission."}),
                }
            )
            response = self._run_tool_loop(resumed)
            return response if isinstance(response, str) else "Command denied."
        except ExecutionStopped:
            return "Execution stopped."
        finally:
            self._clear_current_execution_id()

