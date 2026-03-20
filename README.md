# Servclaw

An AI agent that runs on your server and manages your Docker infrastructure. Talk to it over Telegram or Discord - it can run commands, inspect containers, read logs, and remember context between sessions.

---

## Requirements

- Docker + Docker Compose
- An OpenAI API key
- A Telegram bot token (or Discord bot token)

---

## Setup

```bash
git clone https://github.com/prathamxtc/servclaw
cd servclaw
bash install.sh
```

The installer will:
1. Detect your OS and install any missing dependencies
2. Walk you through the config wizard (API keys, bot tokens, allowed users)
3. Build and start the container

That's it. The agent is running.

> If `servclaw` isn't found immediately, run `source ~/.profile` or open a new terminal.

---

## Configuration

The installer handles everything interactively - API keys, bot tokens, and allowed users are all asked during setup.

All settings are saved to `servclaw.json` at the project root. You can edit it directly later if needed:

```json
{
  "secrets": {
    "openaiApiKey": "sk-..."
  },
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "123456:ABC...",
      "allowedUserIds": [123456789]
    },
    "discord": {
      "enabled": false,
      "token": "",
      "allowedUserIds": []
    }
  },
  "agents": {
    "defaults": {
      "model": {
        "primary": "openai/gpt-5-mini"
      }
    }
  }
}
```

After manually editing, restart with:

```bash
docker compose restart servclaw
```

---

## Telegram Setup

The installer will ask for your bot token and user ID during setup.

To get a bot token: open Telegram → message `@BotFather` → `/newbot` → follow prompts.

**Easiest way to get your user ID:** after the bot is running, just message it. If your ID isn't authorized yet, the bot will reply with your user ID directly. Then run:

```bash
servclaw telegram allow <user-id>
```

To revoke access:

```bash
servclaw telegram deny <user-id>
```

---

## Discord Setup

The installer will ask if you want to enable Discord and prompt for your bot token.

Before running the installer:
1. Create a bot at [discord.com/developers](https://discord.com/developers)
2. Under **Bot** settings, enable **Message Content Intent**
3. Copy the bot token - the installer will ask for it

The bot operates via DMs only - no server channel needed. Invite it to any server to be able to DM it.

**Easiest way to get your user ID:** after the bot is running, DM it once. If your ID isn't authorized, it will reply with your user ID. Then run:

```bash
servclaw discord allow <user-id>
```

To revoke access:

```bash
servclaw discord deny <user-id>
```

---

## CLI

The `servclaw` command is installed globally during setup.

```bash
servclaw telegram allow <user-id>    # authorize a Telegram user
servclaw telegram deny <user-id>     # revoke Telegram access

servclaw discord allow <user-id>     # authorize a Discord user
servclaw discord deny <user-id>      # revoke Discord access

docker compose ps                    # check container status
docker compose logs -f servclaw      # live logs
docker compose restart servclaw      # restart after config changes
```

---

## Command Safety & Confirmation

The agent classifies every shell command it runs as **read-only** or **state-changing** before executing it:

- **Read-only** (e.g. `ls`, `cat`, `docker ps`, `grep`, `df`) - runs immediately, no prompt.
- **State-changing** (e.g. `rm`, `docker restart`, `apt install`, `chmod`, `systemctl stop`) - paused and sent to you for approval first.

When a state-changing command is needed, you'll get a message like:

> I want to run the following command on host:
> ` docker restart nginx `
> This could modify system state. Allow me to proceed?

Both Telegram and Discord show **Allow** and **Deny** buttons on this message. Tap **Allow** to proceed or **Deny** to cancel.

The model itself decides the classification - if it's unsure, it defaults to requiring confirmation.

---

## Bot Commands

Send these directly in the chat (Telegram or Discord DM):

| Command | What it does |
|---------|-------------|
| `/stop` | Immediately cancel whatever the agent is doing - kills the running command and frees the chat |
| `/clear` | Wipe the in-memory session history. Useful if context gets confused |

`/stop` is the kill switch. If the agent is stuck in a loop, running a long command, or waiting on something - just send `/stop` and it cancels cleanly.

---

## Onboarding

On first run, the agent will walk you through a short setup: it reads `BOOTSTRAP.md` from its workspace and asks a few questions (your name, server role, preferences) to personalize itself.

If it doesn't start automatically, just ask:

> "Start onboarding process" or "let's do the setup"

Onboarding is one-time. Once complete, `BOOTSTRAP.md` is deleted from the workspace and won't run again.

---

## What Gets Created on First Run

| Path | Purpose |
|------|---------|
| `servclaw.json` | Main config (API keys, tokens, settings) |
| `workspace/` | Agent's identity, persona, and setup files |
| `memory/` | Long-term memory and session context |
| `logs/` | Command execution logs |

These are excluded from git automatically.

---

## Feedback & Contributions

Since Servclaw is in its early building phase, I’d love to hear your thoughts!

- Found a bug? [Open an Issue](https://github.com/prathamxtc/servclaw/issues/new)
- Want to chat or connect? [Twitter/X](https://twitter.com/prathamxtc)

## Docs

- [Memory system](docs/memory.md)
- [Channel setup](docs/channels.md)
