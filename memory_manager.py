"""Memory management for Servclaw agent.

Files under memory/:
    - memory.md  -> long-term durable memory
    - session.md -> current session summary + recent conversation snapshot

On restart, recent conversation is restored from session.md.
"""

import re
from datetime import datetime
from pathlib import Path

MEMORY_DIR = Path(__file__).parent / "memory"
MEMORY_FILE = MEMORY_DIR / "memory.md"
SESSION_FILE = MEMORY_DIR / "session.md"
RUNTIME_MESSAGE_LIMIT = 20
SESSION_CONVERSATION_LIMIT = 20
LONG_TERM_SUMMARY_SECTION = "Global Summary"
LEGACY_LONG_TERM_SUMMARY_SECTION = "Session Summary"

# Default memory template
DEFAULT_MEMORY = """# Servclaw Memory

## User Info
- (none yet)

## Infrastructure Notes
- Command execution: local shell + optional docker.sock escalation
- Docker socket: /var/run/docker.sock

## Global Summary
- Last session: (none)
"""

DEFAULT_SESSION = """# Servclaw Session

## Session Summary
- (none yet)

## Recent Conversation
- (none yet)
"""


class MemoryManager:
    def __init__(self):
        self.memory_dir = MEMORY_DIR
        self.memory_file = MEMORY_FILE
        self.session_file = SESSION_FILE
        self.runtime_messages: list[dict] = []
        self.load_memory()

    def _ensure_storage(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)

        if not self.memory_file.exists():
            self.memory_file.write_text(DEFAULT_MEMORY)

        if not self.session_file.exists():
            self.session_file.write_text(DEFAULT_SESSION)

    def _ensure_section_exists(self, file_path: Path, section_title: str, default_body: str) -> None:
        """Ensure markdown section exists in file; append it with a default body when missing."""
        content = file_path.read_text() if file_path.exists() else ""
        if f"## {section_title}" in content:
            return

        if content and not content.endswith("\n"):
            content += "\n"
        if content and not content.endswith("\n\n"):
            content += "\n"

        body = default_body.strip()
        content += f"## {section_title}\n{body}\n"
        file_path.write_text(content)

    def _ensure_templates(self) -> None:
        """Self-heal memory templates if sections are missing from existing files."""
        self._ensure_section_exists(self.memory_file, "User Info", "- (none yet)")
        self._ensure_section_exists(
            self.memory_file,
            "Infrastructure Notes",
            "- Command execution: local shell + optional docker.sock escalation\n- Docker socket: /var/run/docker.sock",
        )
        self._ensure_section_exists(
            self.memory_file,
            LONG_TERM_SUMMARY_SECTION,
            "- Last session: (none)",
        )

        self._ensure_section_exists(self.session_file, "Session Summary", "- (none yet)")
        self._ensure_section_exists(self.session_file, "Recent Conversation", "- (none yet)")

    def load_memory(self) -> dict:
        """Load long-term memory and restore recent session context."""
        self._ensure_storage()
        self._migrate_legacy_long_term_summary_section()
        self._ensure_templates()

        self._normalize_session_summary_timestamps()
        self.runtime_messages = self._load_runtime_messages_from_session()
        content = self.memory_file.read_text()
        return self._parse_memory(content)

    def _migrate_legacy_long_term_summary_section(self) -> None:
        """Rename legacy long-term heading from Session Summary to Global Summary."""
        content = self.memory_file.read_text()
        if f"## {LONG_TERM_SUMMARY_SECTION}" in content:
            return

        lines = content.split("\n")
        changed = False
        for index, line in enumerate(lines):
            if line.strip() == f"## {LEGACY_LONG_TERM_SUMMARY_SECTION}":
                lines[index] = f"## {LONG_TERM_SUMMARY_SECTION}"
                changed = True
                break

        if changed:
            self.memory_file.write_text("\n".join(lines).rstrip() + "\n")
            return

        if content and not content.endswith("\n"):
            content += "\n"
        if content and not content.endswith("\n\n"):
            content += "\n"
        content += f"## {LONG_TERM_SUMMARY_SECTION}\n- Last session: (none)\n"
        self.memory_file.write_text(content)

    def _parse_memory(self, content: str) -> dict:
        """Parse markdown memory file into structured data."""
        sections = {}
        current_section = None
        lines: list[str] = []

        for line in content.split("\n"):
            if line.startswith("## "):
                if current_section:
                    sections[current_section] = "\n".join(lines).strip()
                current_section = line.replace("## ", "").strip().lower()
                lines = []
            else:
                lines.append(line)

        if current_section:
            sections[current_section] = "\n".join(lines).strip()

        return sections

    def get_memory_summary(self) -> str:
        """Get formatted memory summary for context injection."""
        parsed = self._parse_memory(self.memory_file.read_text())
        session_parsed = self._parse_memory(self.session_file.read_text())
        user_info = parsed.get("user info", "(none)")
        infrastructure = parsed.get("infrastructure notes", "(none)")
        session_summary = parsed.get(
            LONG_TERM_SUMMARY_SECTION.lower(),
            parsed.get(LEGACY_LONG_TERM_SUMMARY_SECTION.lower(), "(none)"),
        )
        active_session_summary = session_parsed.get("session summary", "(none)")
        # NOTE: "recent conversation" from session.md is intentionally NOT included
        # here to avoid duplication — get_recent_messages() already injects those
        # as individual messages in the conversation history.
        return f"""## Memory Summary

### User Info
{user_info}

### Infrastructure Notes
{infrastructure}

### Long-Term Notes
{session_summary}

### Session Summary
{active_session_summary}"""

    def _replace_section(self, section_title: str, new_body: str) -> None:
        content = self.memory_file.read_text()
        lines = content.split("\n")
        result: list[str] = []
        in_target_section = False
        replaced = False

        for line in lines:
            if line.startswith(f"## {section_title}"):
                if not replaced:
                    result.append(line)
                    if new_body:
                        result.extend(new_body.split("\n"))
                    replaced = True
                in_target_section = True
                continue

            if in_target_section and line.startswith("## "):
                in_target_section = False
                result.append(line)
                continue

            if not in_target_section:
                result.append(line)

        if not replaced:
            if result and result[-1] != "":
                result.append("")
            result.append(f"## {section_title}")
            if new_body:
                result.extend(new_body.split("\n"))

        self.memory_file.write_text("\n".join(result).rstrip() + "\n")

    def _get_section_lines(self, section_title: str) -> list[str]:
        parsed = self._parse_memory(self.memory_file.read_text())
        section = parsed.get(section_title.lower(), "")
        if not section:
            return []
        return [line for line in section.split("\n") if line.strip()]

    def _replace_session_section(self, section_title: str, new_body: str) -> None:
        content = self.session_file.read_text()
        lines = content.split("\n")
        result: list[str] = []
        in_target_section = False
        replaced = False

        for line in lines:
            if line.startswith(f"## {section_title}"):
                if not replaced:
                    result.append(line)
                    if new_body:
                        result.extend(new_body.split("\n"))
                    replaced = True
                in_target_section = True
                continue

            if in_target_section and line.startswith("## "):
                in_target_section = False
                result.append(line)
                continue

            if not in_target_section:
                result.append(line)

        if not replaced:
            if result and result[-1] != "":
                result.append("")
            result.append(f"## {section_title}")
            if new_body:
                result.extend(new_body.split("\n"))

        self.session_file.write_text("\n".join(result).rstrip() + "\n")

    def _get_session_section_lines(self, section_title: str) -> list[str]:
        parsed = self._parse_memory(self.session_file.read_text())
        section = parsed.get(section_title.lower(), "")
        if not section:
            return []
        return [line for line in section.split("\n") if line.strip()]

    def get_session_summary_text(self) -> str:
        parsed = self._parse_memory(self.session_file.read_text())
        return parsed.get("session summary", "(none)")

    def set_session_summary_bullets(self, bullets: list[str]) -> None:
        normalized: list[str] = []
        seen: set[str] = set()

        for bullet in bullets:
            cleaned = " ".join(str(bullet).split()).strip()
            if not cleaned:
                continue
            line = cleaned if cleaned.startswith("-") else f"- {cleaned}"
            key = line.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(line)

        if not normalized:
            normalized = ["- (none yet)"]

        self._replace_session_section("Session Summary", "\n".join(normalized))

    def _serialize_runtime_message(self, role: str, content: str) -> str:
        cleaned = self._normalize_memory_note(content, limit=500)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return f"- [{now}] {role}: {cleaned}"

    def _parse_session_message_line(self, line: str) -> dict | None:
        match = re.match(
            r"^-\s*(?:\[(?P<ts>[^\]]+)\]\s+)?(?P<role>user|assistant):\s*(?P<content>.*)$",
            line.strip(),
            re.IGNORECASE,
        )
        if not match:
            return None
        role = match.group("role").lower()
        content = match.group("content").strip()
        if not content:
            return None
        return {"role": role, "content": content}

    def _persist_runtime_messages_to_session(self) -> None:
        lines: list[str] = []
        for message in self.runtime_messages[-SESSION_CONVERSATION_LIMIT:]:
            role = str(message.get("role", "")).strip().lower()
            content = str(message.get("content", "")).strip()
            if role not in {"user", "assistant"} or not content:
                continue
            lines.append(self._serialize_runtime_message(role, content))

        if not lines:
            lines = ["- (none yet)"]
        self._replace_session_section("Recent Conversation", "\n".join(lines))

    def _load_runtime_messages_from_session(self) -> list[dict]:
        lines = self._get_session_section_lines("Recent Conversation")
        messages: list[dict] = []
        for line in lines:
            parsed = self._parse_session_message_line(line)
            if parsed:
                messages.append(parsed)
        return messages[-RUNTIME_MESSAGE_LIMIT:]

    def _set_bullet_value(self, section_title: str, key: str, value: str) -> None:
        lines = self._get_section_lines(section_title)
        updated = False
        output: list[str] = []

        for line in lines:
            if line.startswith(f"- {key}:"):
                output.append(f"- {key}: {value}")
                updated = True
            elif line != "- (none yet)":
                output.append(line)

        if not updated:
            output.append(f"- {key}: {value}")

        self._replace_section(section_title, "\n".join(output))

    def _set_or_append_bullet(self, section_title: str, key: str, value: str) -> None:
        """Set `- key: value` if key exists, else append it."""
        self._set_bullet_value(section_title, key, value)

    def _is_timestamped_bullet(self, line: str) -> bool:
        return bool(re.match(r"^-\s*\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s+", line))

    def _to_timestamped_bullet(self, text: str) -> str:
        cleaned = text.strip()
        if not cleaned:
            return ""
        if not cleaned.startswith("-"):
            cleaned = f"- {cleaned}"
        if self._is_timestamped_bullet(cleaned):
            return cleaned
        content = cleaned.lstrip("- ").strip()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return f"- [{now}] {content}"

    def _normalize_bullet_for_dedupe(self, line: str) -> str:
        """Return a semantic key for a bullet, ignoring timestamp wrapper differences."""
        cleaned = line.strip()
        if not cleaned:
            return ""
        if not cleaned.startswith("-"):
            cleaned = f"- {cleaned}"
        cleaned = re.sub(r"^-\s*\[[^\]]+\]\s+", "- ", cleaned)
        return " ".join(cleaned.lower().split())

    def _normalize_session_summary_timestamps(self) -> None:
        lines = self._get_section_lines(LONG_TERM_SUMMARY_SECTION)
        if not lines:
            return

        changed = False
        normalized: list[str] = []
        seen: set[str] = set()
        for line in lines:
            if line == "- (none yet)":
                continue
            ts_line = self._to_timestamped_bullet(line)
            dedupe_key = self._normalize_bullet_for_dedupe(ts_line)
            if dedupe_key in seen:
                changed = True
                continue
            seen.add(dedupe_key)
            normalized.append(ts_line)
            if ts_line != line:
                changed = True

        if not normalized:
            normalized = ["- (none yet)"]
            if lines != normalized:
                changed = True

        if changed:
            self._replace_section(LONG_TERM_SUMMARY_SECTION, "\n".join(normalized))

    def _append_free_bullet(self, section_title: str, text: str) -> None:
        lines = self._get_section_lines(section_title)
        cleaned = text.strip()
        if not cleaned:
            return

        bullet = cleaned if cleaned.startswith("-") else f"- {cleaned}"
        if section_title == LONG_TERM_SUMMARY_SECTION:
            bullet = self._to_timestamped_bullet(bullet)
            incoming_key = self._normalize_bullet_for_dedupe(bullet)
            existing_keys = {
                self._normalize_bullet_for_dedupe(line)
                for line in lines
                if line != "- (none yet)"
            }
            if incoming_key in existing_keys:
                return

        if bullet in lines:
            return
        lines = [line for line in lines if line != "- (none yet)"]
        lines.append(bullet)
        self._replace_section(section_title, "\n".join(lines))

    def _remove_bullets_by_snippet(self, section_title: str, snippet: str) -> int:
        needle = " ".join(snippet.lower().split())
        if not needle:
            return 0
        lines = self._get_section_lines(section_title)
        kept: list[str] = []
        removed = 0
        for line in lines:
            if needle in line.lower():
                removed += 1
                continue
            kept.append(line)
        if not kept:
            kept = ["- (none yet)"]
        self._replace_section(section_title, "\n".join(kept))
        return removed

    def get_user_info_dict(self) -> dict[str, str]:
        """Return user info bullets as key/value pairs."""
        data: dict[str, str] = {}
        for line in self._get_section_lines("User Info"):
            if not line.startswith("-"):
                continue
            body = line.lstrip("- ").strip()
            if ":" not in body:
                continue
            key, value = body.split(":", 1)
            data[key.strip()] = value.strip()
        return data

    def get_name(self) -> str | None:
        value = self.get_user_info_dict().get("Name", "").strip()
        return value or None

    def get_preferences(self) -> dict[str, str]:
        """Return topic preferences parsed from Preference[topic]: value entries."""
        prefs: dict[str, str] = {}
        for key, value in self.get_user_info_dict().items():
            if key.startswith("Preference[") and key.endswith("]"):
                topic = key[len("Preference[") : -1].strip()
                if topic:
                    prefs[topic] = value
        return prefs

    def get_known_user_facts(self) -> list[str]:
        """Return compact list of durable user facts for direct answers."""
        facts: list[str] = []
        info = self.get_user_info_dict()

        name = info.get("Name", "").strip()
        if name:
            facts.append(f"Your name is {name}.")

        prefs = self.get_preferences()
        for topic, value in prefs.items():
            if value == "like":
                facts.append(f"You like {topic}.")
            elif value == "dislike":
                facts.append(f"You don't like {topic}.")
            else:
                facts.append(f"Preference for {topic}: {value}.")

        return facts

    def _remove_user_info_lines_matching(self, predicate) -> int:
        lines = self._get_section_lines("User Info")
        kept: list[str] = []
        removed = 0
        for line in lines:
            if predicate(line):
                removed += 1
                continue
            kept.append(line)
        if not kept:
            kept = ["- (none yet)"]
        self._replace_section("User Info", "\n".join(kept))
        return removed

    def _remove_session_lines_matching(self, predicate) -> int:
        lines = self._get_section_lines(LONG_TERM_SUMMARY_SECTION)
        kept: list[str] = []
        removed = 0
        for line in lines:
            if predicate(line):
                removed += 1
                continue
            kept.append(line)
        if not kept:
            kept = ["- (none yet)"]
        self._replace_section(LONG_TERM_SUMMARY_SECTION, "\n".join(kept))
        return removed

    def add_message(self, role: str, content: str) -> None:
        """Add message to runtime history and persist session conversation."""
        self.runtime_messages.append({"role": role, "content": content})
        if len(self.runtime_messages) > RUNTIME_MESSAGE_LIMIT:
            self.runtime_messages = self.runtime_messages[-RUNTIME_MESSAGE_LIMIT:]
        self._persist_runtime_messages_to_session()

    def get_recent_messages(self) -> list[dict]:
        """Return current runtime message history."""
        return list(self.runtime_messages)

    def clear_session(self) -> None:
        """Clear runtime messages and reset session.md."""
        self.runtime_messages = []
        self._replace_session_section("Session Summary", "- (none yet)")
        self._replace_session_section("Recent Conversation", "- (none yet)")

    def _last_message_by_role(self, role: str) -> str:
        for message in reversed(self.runtime_messages):
            if message.get("role") == role:
                return str(message.get("content", "")).strip()
        return ""

    def _extract_topic_from_statement(self, text: str) -> str | None:
        """Extract topic from simple preference statements."""
        value = text.strip()
        patterns = [
            r"\bi\s+(?:love|like)\s+([A-Za-z][A-Za-z\s\-']{0,60})\b",
            r"\bi\s+(?:do\s+not|don't|dont)\s+(?:love|like)\s+([A-Za-z][A-Za-z\s\-']{0,60})\b",
            r"\b(?:loves|likes)\s+([A-Za-z][A-Za-z\s\-']{0,60})\b",
            r"\babout\s+([A-Za-z][A-Za-z\s\-']{0,60})\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, value, re.IGNORECASE)
            if match:
                return match.group(1).strip(" .,!?")
        return None

    def _infer_recent_forget_target(self) -> str | None:
        """Infer target for vague 'forget it/this/that' from recent runtime conversation."""
        for message in reversed(self.runtime_messages):
            content = str(message.get("content", "")).strip()
            if not content:
                continue
            topic = self._extract_topic_from_statement(content)
            if topic:
                return topic

        # fallback to latest remembered line in long-term memory
        lines = self._get_section_lines(LONG_TERM_SUMMARY_SECTION)
        for line in reversed(lines):
            if line == "- (none yet)":
                continue
            topic = self._extract_topic_from_statement(line)
            if topic:
                return topic
        return None

    def _remove_memory_by_snippet(self, snippet: str) -> int:
        """Remove memory entries containing a free-text snippet across user + summary sections."""
        needle = " ".join(snippet.lower().split())
        if not needle:
            return 0

        removed_user = self._remove_user_info_lines_matching(
            lambda line: needle in line.lower()
        )
        removed_session = self._remove_session_lines_matching(
            lambda line: needle in line.lower()
        )
        return removed_user + removed_session

    def _normalize_memory_note(self, text: str, limit: int = 800) -> str:
        cleaned = " ".join(text.split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 3].rstrip() + "..."

    def remember_note(self, note: str) -> None:
        """Persist a durable note into Global Summary for use after restart."""
        normalized = self._normalize_memory_note(note)
        if not normalized:
            return

        existing_lines = self._get_section_lines(LONG_TERM_SUMMARY_SECTION)
        preserved_lines = []
        for line in existing_lines:
            if line == "- (none yet)":
                continue
            if line.startswith("- Remembered ("):
                if normalized in line:
                    return
            preserved_lines.append(line)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        preserved_lines.append(f"- Remembered ({now}): {normalized}")
        self._replace_section(LONG_TERM_SUMMARY_SECTION, "\n".join(preserved_lines))

    def _topic_key(self, topic: str) -> str:
        return " ".join(topic.lower().split())

    def update_topic_preference(self, topic: str, preference: str) -> None:
        """Persist canonical topic preference and remove conflicting remembered notes."""
        canonical_topic = self._topic_key(topic)
        if not canonical_topic:
            return
        self.update_user_info(f"Preference[{canonical_topic}]", preference)

        # Remove stale remembered notes that mention this topic to avoid contradictions.
        self._remove_session_lines_matching(
            lambda line: line.startswith("- Remembered (")
            and canonical_topic in line.lower()
        )

    def forget_topic(self, topic: str) -> int:
        """Forget a topic from both user info and remembered notes."""
        canonical_topic = self._topic_key(topic)
        if not canonical_topic:
            return 0

        removed_user = self._remove_user_info_lines_matching(
            lambda line: canonical_topic in line.lower()
        )
        removed_session = self._remove_session_lines_matching(
            lambda line: canonical_topic in line.lower()
        )
        return removed_user + removed_session

    def forget_last_remembered(self) -> bool:
        """Forget the most recent remembered note from Global Summary."""
        lines = self._get_section_lines(LONG_TERM_SUMMARY_SECTION)
        if not lines:
            return False

        last_index = -1
        for idx, line in enumerate(lines):
            if line != "- (none yet)":
                last_index = idx

        if last_index == -1:
            return False

        del lines[last_index]
        if not lines:
            lines = ["- (none yet)"]
        self._replace_section(LONG_TERM_SUMMARY_SECTION, "\n".join(lines))
        return True

    def forget_all_user_memory(self) -> None:
        """Clear all durable user-specific memory."""
        self._replace_section("User Info", "- (none yet)")
        self._replace_section(LONG_TERM_SUMMARY_SECTION, "- (none yet)")

    def get_memory_text(self) -> str:
        return self.memory_file.read_text()

    def apply_memory_actions(self, actions: list[dict]) -> int:
        """Apply model-proposed memory actions as read-modify-rewrite updates.

        Returns number of applied actions.
        """
        applied = 0
        for action in actions:
            action_type = str(action.get("type", "")).strip()

            if action_type == "set_user_info":
                key = str(action.get("key", "")).strip()
                value = str(action.get("value", "")).strip()
                if key and value:
                    self._set_or_append_bullet("User Info", key, value)
                    applied += 1
                continue

            if action_type == "remove_user_info":
                key = str(action.get("key", "")).strip().lower()
                snippet = str(action.get("snippet", "")).strip().lower()
                if key:
                    removed = self._remove_user_info_lines_matching(
                        lambda line: line.lower().startswith(f"- {key}:")
                    )
                    if removed > 0:
                        applied += 1
                elif snippet:
                    removed = self._remove_user_info_lines_matching(
                        lambda line: snippet in line.lower()
                    )
                    if removed > 0:
                        applied += 1
                continue

            if action_type == "remember_note":
                note = str(action.get("note", "")).strip()
                if note:
                    self._append_free_bullet(LONG_TERM_SUMMARY_SECTION, note)
                    applied += 1
                continue

            if action_type == "remove_session_note":
                snippet = str(action.get("snippet", "")).strip()
                if snippet:
                    removed = self._remove_bullets_by_snippet(
                        LONG_TERM_SUMMARY_SECTION, snippet
                    )
                    if removed > 0:
                        applied += 1
                continue

            if action_type == "set_infrastructure_note":
                key = str(action.get("key", "")).strip()
                value = str(action.get("value", "")).strip()
                if key and value:
                    self._set_or_append_bullet("Infrastructure Notes", key, value)
                    applied += 1
                continue

            if action_type == "remove_infrastructure_note":
                snippet = str(action.get("snippet", "")).strip()
                if snippet:
                    removed = self._remove_bullets_by_snippet(
                        "Infrastructure Notes", snippet
                    )
                    if removed > 0:
                        applied += 1
                continue

            if action_type == "forget_all_user_memory":
                self.forget_all_user_memory()
                applied += 1
                continue

            if action_type == "forget_last_remembered":
                if self.forget_last_remembered():
                    applied += 1
                continue

        self._normalize_session_summary_timestamps()
        return applied

    def save_session_summary(self, summary: str) -> None:
        """Save end-of-session summary to session.md for restart continuity."""
        bullet = self._to_timestamped_bullet(summary)
        lines = [
            line for line in self._get_session_section_lines("Session Summary")
            if line != "- (none yet)"
        ]
        lines.append(bullet)
        self._replace_session_section("Session Summary", "\n".join(lines))

    def update_user_info(self, key: str, value: str) -> None:
        """Update user info in memory.md."""
        self._set_bullet_value("User Info", key, value)

    def learn_from_message(self, message: str) -> bool:
        """Extract durable user facts from plain user text and persist them."""
        text = message.strip()
        lowered = text.lower()
        updated = False

        name_match = re.search(
            r"\b(?:my name is|i am|i'm|hi i am|hi i'm|call me)\s+([A-Za-z][A-Za-z\-']{0,49})\b",
            text,
            re.IGNORECASE,
        )
        if name_match:
            name = name_match.group(1).strip().title()
            self.update_user_info("Name", name)
            updated = True

        if "prefer short answers" in lowered or "prefer brief answers" in lowered:
            self.update_user_info("Response style", "short")
            updated = True
        elif "prefer detailed answers" in lowered or "prefer long answers" in lowered:
            self.update_user_info("Response style", "detailed")
            updated = True

        dislike_match = re.search(
            r"\bi\s+(?:do\s+not|don't|dont)\s+(?:love|like)\s+([A-Za-z][A-Za-z\s\-']{0,60})\b",
            text,
            re.IGNORECASE,
        )
        if dislike_match:
            topic = dislike_match.group(1).strip(" .,!?")
            self.update_topic_preference(topic, "dislike")
            updated = True

        like_match = re.search(
            r"\bi\s+(?:love|like)\s+([A-Za-z][A-Za-z\s\-']{0,60})\b",
            text,
            re.IGNORECASE,
        )
        if like_match and not re.search(r"\bdo\s+i\b", lowered):
            topic = like_match.group(1).strip(" .,!?")
            self.update_topic_preference(topic, "like")
            updated = True

        return updated

    def learn_forget_request(self, message: str) -> bool:
        """Handle explicit forget requests and delete matching long-term memory."""
        text = message.strip()
        lowered = text.lower()
        if "forget" not in lowered:
            return False

        if re.search(
            r"\bforget\s+(?:everything|all|whatever)\s+(?:you\s+know\s+)?about\s+me\b",
            lowered,
        ) or re.search(r"\bforget\s+me\b", lowered):
            self.forget_all_user_memory()
            return True

        if re.search(r"\bforget\s+(it|this|that)\s*$", lowered):
            # Prefer targeted forget based on most recent discussed topic.
            target = self._infer_recent_forget_target()
            if target:
                return self.forget_topic(target) > 0
            return self.forget_last_remembered()

        topic_match = re.search(r"\bforget\s+(.+)", text, re.IGNORECASE)
        if topic_match:
            target_text = topic_match.group(1).strip(" .,!?")
            topic = self._extract_topic_from_statement(target_text) or target_text
            removed = self.forget_topic(topic)
            if removed > 0:
                return True
            return self._remove_memory_by_snippet(target_text) > 0

        return False

    def learn_memory_request(self, message: str) -> bool:
        """Persist explicit user requests to remember something long-term."""
        text = message.strip()
        lowered = text.lower()

        remember_phrases = (
            "remember",
            "keep this in mind",
            "keep that in mind",
            "store this",
            "save this",
        )
        if not any(phrase in lowered for phrase in remember_phrases):
            return False

        if any(
            phrase in lowered
            for phrase in (
                "current state",
                "the current state",
                "remember this",
                "remember that",
                "remember it",
                "keep this in mind",
                "keep that in mind",
            )
        ):
            last_assistant = self._last_message_by_role("assistant")
            if last_assistant:
                self.remember_note(last_assistant)
                return True

        explicit_match = re.search(
            r"\bremember(?:\s+that)?\s+(.+)", text, re.IGNORECASE
        )
        if explicit_match:
            self.remember_note(explicit_match.group(1).strip())
            return True

        return False
