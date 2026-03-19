# Servclaw

An AI agent that runs on your server and manages your Docker infrastructure. Talk to it over Telegram or Discord — it can run commands, inspect containers, read logs, and remember context between sessions.

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

The installer handles everything interactively — API keys, bot tokens, and allowed users are all asked during setup.

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
3. Copy the bot token — the installer will ask for it

The bot operates via DMs only — no server channel needed. Invite it to any server to be able to DM it.

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

## What Gets Created on First Run

| Path | Purpose |
|------|---------|
| `servclaw.json` | Main config (API keys, tokens, settings) |
| `workspace/` | Agent's identity, persona, and setup files |
| `memory/` | Long-term memory and session context |
| `logs/` | Command execution logs |

These are excluded from git automatically.

---

## Docs

- [Memory system](docs/memory.md)
- [Channel setup](docs/channels.md)
