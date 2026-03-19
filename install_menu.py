#!/usr/bin/env python3
"""Interactive setup wizard for Servclaw.

This script intentionally uses only Python standard library modules.
It provides arrow-key radio menus and required text prompts, then writes
servclaw.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import termios
import tty
from pathlib import Path
from typing import Any


def default_config() -> dict[str, Any]:
    return {
        "meta": {
            "lastTouchedVersion": "2026.3.19",
        },
        "agents": {
            "defaults": {
                "model": {
                    "primary": "openai/gpt-5-mini",
                },
                "workspace": "/app/workspace",
                "compaction": {
                    "mode": "safeguard",
                },
            }
        },
        "channels": {
            "telegram": {
                "enabled": True,
                "token": "",
                "allowedUserIds": [],
                "streaming": "off",
            }
        },
        "secrets": {
            "openaiApiKey": "",
        },
    }


def clear_screen() -> None:
    # Keep terminal history scrollable: no full-screen clear.
    print()


def draw_header(title: str, subtitle: str = "") -> None:
    print("+-----------------------------------------------------------+")
    print("|                    Servclaw Setup Wizard                 |")
    print("+-----------------------------------------------------------+")
    print(f"\n{title}")
    if subtitle:
        print(subtitle)
    print()


CYAN = "\033[36m"
RESET = "\033[0m"


def build_menu_lines(title: str, options: list[str], selected: int) -> list[str]:
    lines = [
        "+-----------------------------------------------------------+",
        "|                    Servclaw Setup Wizard                 |",
        "+-----------------------------------------------------------+",
        "",
        title,
        "",
    ]

    for idx, option in enumerate(options):
        if idx == selected:
            lines.append(f"  {CYAN}\u25cf {option}{RESET}")
        else:
            lines.append(f"  \u25cb {option}")

    lines.extend(["", "\u2191\u2193 navigate   Enter confirm   q/Ctrl+C quit"])
    return lines


def build_checkbox_lines(title: str, options: list[str], focus: int, checked: set) -> list[str]:
    lines = [
        "+-----------------------------------------------------------+",
        "|                    Servclaw Setup Wizard                 |",
        "+-----------------------------------------------------------+",
        "",
        title,
        "",
    ]

    for idx, option in enumerate(options):
        cursor = ">" if idx == focus else " "
        if idx in checked:
            lines.append(f" {cursor} {CYAN}[x] {option}{RESET}")
        else:
            lines.append(f" {cursor} [ ] {option}")

    lines.extend(["", "\u2191\u2193 navigate   Space select/deselect   Enter confirm   q/Ctrl+C quit"])
    return lines


def redraw_in_place(lines: list[str], previous_line_count: int) -> int:
    if previous_line_count > 0:
        # Move cursor back to the first line of the previously rendered block.
        sys.stdout.write(f"\033[{previous_line_count}A\r")

    for line in lines:
        # Write line then erase any leftover characters to the right.
        sys.stdout.write(line + "\033[K\n")

    sys.stdout.flush()
    return len(lines)


def read_key() -> str:
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x03":
            return "interrupt"
        if ch == "\x1b":
            seq = sys.stdin.read(2)
            if seq == "[A":
                return "up"
            if seq == "[B":
                return "down"
            return "escape"
        if ch in ("\r", "\n"):
            return "enter"
        if ch == " ":
            return "space"
        if ch in ("q", "Q"):
            return "quit"
        return "other"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def menu_radio(title: str, options: list[str], default_index: int = 0) -> int:
    selected = default_index if options else 0
    rendered_lines = 0

    while True:
        lines = build_menu_lines(title, options, selected)
        rendered_lines = redraw_in_place(lines, rendered_lines)
        key = read_key()

        if key == "up":
            selected = (selected - 1) % len(options)
        elif key == "down":
            selected = (selected + 1) % len(options)
        elif key == "enter":
            return selected
        elif key in ("quit", "interrupt"):
            raise KeyboardInterrupt("Setup canceled by user")


def menu_checkbox(title: str, options: list[str], default_checked: list[int] | None = None) -> list[int]:
    """Multi-select checkbox menu.

    Arrow keys move the cursor only — they do NOT change the selection.
    Space toggles the focused item in or out of the selection.
    At least one item must remain checked (can't deselect the last one).
    Enter confirms all currently checked items.
    """
    initial = default_checked if default_checked is not None else [0]
    checked: set[int] = set(initial)
    focus = initial[0] if initial else 0
    rendered_lines = 0

    while True:
        lines = build_checkbox_lines(title, options, focus, checked)
        rendered_lines = redraw_in_place(lines, rendered_lines)
        key = read_key()

        if key == "up":
            focus = (focus - 1) % len(options)
        elif key == "down":
            focus = (focus + 1) % len(options)
        elif key == "space":
            if focus in checked:
                if len(checked) > 1:      # keep at least one checked
                    checked.discard(focus)
            else:
                checked.add(focus)
        elif key == "enter":
            return sorted(checked)
        elif key in ("quit", "interrupt"):
            raise KeyboardInterrupt("Setup canceled by user")


def prompt_required(label: str, default: str = "") -> str:
    while True:
        if default:
            value = input(f"{label} [{default}]: ").strip()
            if not value:
                value = default
        else:
            value = input(f"{label}: ").strip()

        if value:
            return value

        print("This field is required.")


def load_existing_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_string(cfg: dict[str, Any], key_path: list[str]) -> str:
    cur: Any = cfg
    for key in key_path:
        if not isinstance(cur, dict) or key not in cur:
            return ""
        cur = cur[key]
    return cur if isinstance(cur, str) else ""


def write_config(path: Path, cfg: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def section_header(title: str) -> None:
    print(f"\n{CYAN}{title}{RESET}")
    print("-" * len(title))


def _channel_configured(existing: dict, channel: str) -> bool:
    """Return True if channel already has a token stored in the config."""
    return bool(get_string(existing, ["channels", channel.lower(), "token"]))


def run_wizard(app_dir: Path, config_file: Path) -> int:
    existing = load_existing_config(config_file)

    cfg = default_config()
    merged: dict = existing.copy() if isinstance(existing, dict) else {}

    # ── Step 1: Channel selection (multi-select) + credentials per channel ─
    channel_options = ["Telegram", "Discord"]

    # Pre-check channels that are already configured in the JSON.
    # If none are configured yet, start with nothing checked so the user
    # explicitly chooses what they want.
    pre_checked = [
        i for i, ch in enumerate(channel_options) if _channel_configured(existing, ch)
    ]
    if not pre_checked:
        pre_checked = [0]  # First-run default: Telegram

    selected_channel_indices = menu_checkbox("Step 1: Chat Channels", channel_options, default_checked=pre_checked)
    selected_channels = [channel_options[i] for i in selected_channel_indices]

    telegram_token = get_string(existing, ["channels", "telegram", "token"])
    discord_token = get_string(existing, ["channels", "discord", "token"])

    if "Telegram" in selected_channels:
        if not telegram_token:
            # Not yet configured — ask for it.
            section_header("Telegram credentials")
            telegram_token = prompt_required("Bot token (from @BotFather)")
        else:
            print(f"\n{CYAN}Telegram{RESET}  already configured — skipping.")

    if "Discord" in selected_channels:
        if not discord_token:
            # Not yet configured — ask for it.
            section_header("Discord credentials")
            discord_token = prompt_required("Bot token (from Discord Developer Portal)")
        else:
            print(f"{CYAN}Discord{RESET}   already configured — skipping.")

    # ── Step 2: Model provider selection + immediate API key ──────────────
    provider_options = ["OpenAI"]
    selected_provider_idx = menu_radio("Step 2: Model Provider", provider_options, default_index=0)
    provider_name = provider_options[selected_provider_idx]

    existing_openai = get_string(existing, ["secrets", "openaiApiKey"])
    if provider_name == "OpenAI":
        if not existing_openai:
            section_header("OpenAI credentials")
            openai_api_key = prompt_required("API key")
        else:
            print(f"{CYAN}OpenAI{RESET}    already configured — skipping.")
            openai_api_key = existing_openai
    else:
        openai_api_key = existing_openai

    # ── Write config ──────────────────────────────────────────────────────
    merged.setdefault("meta", {}).setdefault("lastTouchedVersion", "2026.3.19")

    merged.setdefault("agents", {}).setdefault("defaults", {}).setdefault("model", {})["primary"] = cfg["agents"]["defaults"]["model"]["primary"]
    merged["agents"]["defaults"].setdefault("workspace", cfg["agents"]["defaults"]["workspace"])
    merged["agents"]["defaults"].setdefault("compaction", cfg["agents"]["defaults"]["compaction"])

    merged.setdefault("channels", {})

    merged["channels"].setdefault("telegram", {})
    merged["channels"]["telegram"]["enabled"] = "Telegram" in selected_channels
    merged["channels"]["telegram"].setdefault("allowedUserIds", [])
    merged["channels"]["telegram"].setdefault("streaming", "off")
    if telegram_token:
        merged["channels"]["telegram"]["token"] = telegram_token

    merged["channels"].setdefault("discord", {})
    merged["channels"]["discord"]["enabled"] = "Discord" in selected_channels
    merged["channels"]["discord"].setdefault("allowedUserIds", [])
    merged["channels"]["discord"].setdefault("streaming", "off")
    if discord_token:
        merged["channels"]["discord"]["token"] = discord_token

    merged.setdefault("secrets", {})["openaiApiKey"] = openai_api_key

    write_config(config_file, merged)

    print(f"\n{CYAN}Setup complete.{RESET}")
    print(f"  Active:    {', '.join(selected_channels)}")
    print(f"  Provider:  {provider_name}")
    print(f"  Config:    {config_file}")
    print()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Servclaw interactive setup wizard")
    parser.add_argument("--app-dir", required=True, help="Servclaw app directory")
    parser.add_argument("--config", required=True, help="Path to servclaw.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app_dir = Path(args.app_dir).resolve()
    config_file = Path(args.config).resolve()

    if not app_dir.exists():
        print(f"Error: app dir not found: {app_dir}", file=sys.stderr)
        return 1

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Error: interactive TTY is required for menu setup.", file=sys.stderr)
        return 1

    try:
        return run_wizard(app_dir, config_file)
    except KeyboardInterrupt:
        print("\nSetup canceled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
