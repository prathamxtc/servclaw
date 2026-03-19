#!/usr/bin/env python3
"""Servclaw CLI — the main AI agent.

First run: prompts for required values and generates servclaw.json.
Subsequent runs: loads servclaw.json and starts immediately.

All channel bots run in daemon threads.
The terminal REPL owns the main thread (or keeps the process alive).
"""

import re
import sys
import threading
import time
import os

from servclaw_config import (
    default_config,
    get_discord_token,
    get_openai_api_key,
    get_telegram_token,
    load_config,
    save_config,
)

# Words / phrases that mean "yes, go ahead" in natural language
_CONFIRM_YES_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|proceed|allow|confirm|run it|affirmative)\b",
    re.IGNORECASE,
)
# Words / phrases that mean "no, cancel"
_CONFIRM_NO_RE = re.compile(
    r"\b(no|nope|nah|cancel|deny|stop|don'?t|abort|negative|skip)\b",
    re.IGNORECASE,
)


def _ask_terminal_confirmation(agent, req) -> str:
    """Print the agent's confirmation request and loop until user decides.

    Understands natural-language replies — user is never forced to type a
    specific keyword.  Returns the final agent response string.
    """
    print(f"\nAgent: {req.message}\n")
    while True:
        try:
            answer = input("You [allow/deny]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nCommand cancelled.")
            return agent.deny(req)

        if not answer:
            continue

        if _CONFIRM_YES_RE.search(answer):
            print("Command allowed. Running...\n")
            result = agent.confirm_and_run(req)
            # confirm_and_run may chain into another confirmation (nested destructive command)
            from agent import ConfirmationRequest
            while isinstance(result, ConfirmationRequest):
                result = _ask_terminal_confirmation(agent, result)
            return result

        if _CONFIRM_NO_RE.search(answer):
            return agent.deny(req)

        # Unclear reply — let the LLM interpret it as part of the conversation
        print("  (Not sure if that's a yes or no — please say 'allow' or 'deny', or just explain.)")

def _prompt(label: str, required: bool = True) -> str:
    while True:
        value = input(label).strip()
        if value:
            return value
        if not required:
            return ""
        print("  This field is required. Please try again.")


def setup_config() -> dict:
    """Load servclaw.json config, creating it on first run.

    Legacy environment variables are accepted as migration input.
    """
    cfg = load_config()

    if get_openai_api_key(cfg) and (get_telegram_token(cfg) or get_discord_token(cfg)):
        print("✓ Configuration loaded from servclaw.json\n")
        return cfg

    if os.getenv("OPENAI_API_KEY") and os.getenv("TELEGRAM_BOT_TOKEN"):
        cfg.setdefault("secrets", {})["openaiApiKey"] = os.getenv("OPENAI_API_KEY", "")
        cfg.setdefault("channels", {}).setdefault("telegram", {})["token"] = os.getenv("TELEGRAM_BOT_TOKEN", "")
        save_config(cfg)
        print("✓ Configuration migrated from environment to servclaw.json\n")
        return cfg

    print("=" * 50)
    print("  Servclaw CLI — First Time Setup")
    print("=" * 50)
    print("No servclaw.json found with required keys. Let's configure.\n")

    openai_key = _prompt("OpenAI API key: ")
    telegram_token = _prompt("Telegram Bot token: ")

    cfg = default_config()
    cfg.setdefault("secrets", {})["openaiApiKey"] = openai_key
    cfg.setdefault("channels", {}).setdefault("telegram", {})["token"] = telegram_token
    save_config(cfg)

    print("\n✓ Configuration saved to servclaw.json\n")
    return cfg


def _terminal_status_callback(text: str) -> None:
    """Print a dim status line to the terminal."""
    print(f"  \033[2m{text}\033[0m")


def run_terminal_repl(agent) -> None:
    """Interactive REPL in the main thread.

    On restart: agent restores long-term memory from memory/memory.md and
    recent session context from memory/session.md.
    """
    agent._status_callback = _terminal_status_callback
    print("=" * 50)
    print("  Servclaw Terminal")
    print("  Type 'exit' or press Ctrl+C to quit.")
    print("  Type 'clear' to reset session context.")
    print("  Type 'save' to save a session summary to memory/session.md")
    print("=" * 50 + "\n")
    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye!")
            break
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q"):
            print("Bye!")
            break
        if user_input.lower() == "clear":
            agent.memory.clear_session()
            print("Runtime history cleared.\n")
            continue
        if user_input.lower() == "save":
            agent.memory.save_session_summary(
                "Session ended. Check memory/session.md for notes."
            )
            print("Session summary saved to memory/session.md\n")
            continue
        try:
            response = agent.chat(user_input)
        except Exception as exc:
            print(f"[error] {exc}\n")
            continue

        from agent import ConfirmationRequest
        while isinstance(response, ConfirmationRequest):
            response = _ask_terminal_confirmation(agent, response)

        print(f"Agent: {response}\n")


def main() -> None:
    cfg = setup_config()

    # Import after config is loaded so keys/settings are available
    from agent import ServclawAgent
    from channels.telegram.bot import start_telegram_bot
    from channels.discord.bot import start_discord_bot

    agent = ServclawAgent()

    enable_terminal_repl = os.getenv("SERVCLAW_ENABLE_TERMINAL_REPL", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }

    bot_threads: list[threading.Thread] = []

    if get_telegram_token(cfg):
        t = threading.Thread(
            target=start_telegram_bot,
            args=(agent,),
            daemon=True,
            name="telegram-bot",
        )
        t.start()
        bot_threads.append(t)

    if get_discord_token(cfg):
        t = threading.Thread(
            target=start_discord_bot,
            args=(agent,),
            daemon=True,
            name="discord-bot",
        )
        t.start()
        bot_threads.append(t)

    if enable_terminal_repl:
        run_terminal_repl(agent)
    else:
        print("✓ Terminal REPL disabled; running channel-only mode\n")
        try:
            while any(t.is_alive() for t in bot_threads):
                time.sleep(5)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
