# Servclaw

  

An AI agent that lives on your server. Talk to it over Telegram or Discord — it manages your Docker infrastructure, runs scheduled jobs, searches the web, remembers everything, and can be extended with custom skills you write at runtime.

  

---

  

## Requirements

  

- Docker + Docker Compose

- An OpenAI API key

- A Telegram / Discord bot token

  

---

  

## Setup

  

```bash

git clone https://github.com/prathamxtc/servclaw

cd servclaw

bash install.sh

```

  

The installer will:

1. Detect your OS and install any missing dependencies

2. Walk you through the config wizard (API keys, bot tokens, channels, skills, user profile)

3. Build and start the container

  

> If `servclaw` isn't found immediately, run `source ~/.profile` or open a new terminal.

  

---

  

## What It Can Do

  

### Docker & Server Management

Inspect containers, read logs, restart services, run shell commands — with a confirmation step before anything state-changing touches your system.

  

### Scheduled Jobs

Tell the agent to do something later or on a repeat — it schedules it internally and fires without you being online.

  

Two job types — the agent picks the right one automatically:

- **`agent_task`** — the agent's full brain runs at fire time. Checks, monitors, reactive logic — anything that needs thinking.

- **`direct_message`** — no LLM, just delivers a pre-written message at the scheduled time. For one-time reminders or nudges.

Scheduling modes are chosen automatically based on what you describe — no need to specify. Under the hood there are three:

- **`heartbeat`** — repeats on a fixed interval (e.g. every 15 minutes)
- **`cron`** — fires on a cron schedule (e.g. every Monday at 9am)
- **`once`** — fires once at a specific time, then cancels itself

Examples you can say:

> "Check disk usage every hour and warn me if any partition is over 80%"

> "Remind me to update the SSL cert on the 1st of every month"

> "In 20 minutes, restart the nginx container"

  

### Skills

Skills are Python functions the agent can call as tools. You can add, update, or delete them at runtime just by asking — no restart needed.

  

**Built-in skill: Tavily Web Search**

Enable it during setup (or in `servclaw.json`) and the agent can search the web mid-conversation. Useful when you ask something it can't answer from memory alone.

  

**Custom skills**

Tell the agent to create a skill — it writes the Python code, loads it live, and can use it from that point on.

Each skill gets its own folder under `workspace/skills/<skill-name>/` containing a `skill.py` (the code and tool definition) and an optional `SKILL.md` (a guide the agent reads to know how and when to use it).

Example:

> "Create a skill that fetches the latest price of a stock symbol using yfinance"

The agent writes the skill, loads it, and immediately has access to it in the same conversation.

  

### Long-Term Memory

The agent maintains a persistent memory across sessions. It summarizes and compacts older context automatically so the most relevant information is always available — even across container restarts.

  

### Location & Timezone Awareness

Set your city and country once (during install or by telling the agent). Timezone is auto-derived and baked into its context — so it understands "every morning at 8" as your local time, not UTC.

  

---

  

## Configuration

  

All settings live in `servclaw.json` at the project root. The installer sets everything up interactively, but you can edit it directly:

  

```json

{

"secrets": {

"openaiApiKey": "sk-...",

"tavilyApiKey": ""

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

"skills": {

"tavily_search": {

"enabled": false

}

},

"user": {

"name": "Pratham",

"city": "Mumbai",

"country": "India",

"timezone": "Asia/Kolkata"

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

  

After editing, restart:

  

```bash

docker compose restart servclaw

```

  

---

  

## Telegram Setup

  

To get a bot token: open Telegram — message `@BotFather` — `/newbot` — follow prompts.

  

**Easiest way to find your user ID:** message the bot once — if your ID isn't authorized, it replies with your ID directly. Then:

  

```bash

servclaw telegram allow <user-id>

servclaw telegram deny <user-id>

```

  

---

  

## Discord Setup

  

1. Create a bot at [discord.com/developers](https://discord.com/developers)

2. Under **Bot** settings, enable **Message Content Intent**

3. Copy the token — the installer will ask for it

  

The bot works via DMs only. Invite it to any server to be able to DM it.

  

```bash

servclaw discord allow <user-id>

servclaw discord deny <user-id>

```

  

---

  

## CLI

  

```bash

servclaw telegram allow <user-id> # authorize a Telegram user

servclaw telegram deny <user-id> # revoke Telegram access

servclaw discord allow <user-id> # authorize a Discord user

servclaw discord deny <user-id> # revoke Discord access

  

docker compose ps # container status

docker compose logs -f servclaw # live logs

docker compose restart servclaw # restart after config changes

```

  

---

  

## Command Safety

  

The agent classifies every shell command as **read-only** or **state-changing** before running it.

  

- **Read-only** (`ls`, `cat`, `docker ps`, `df`, —) — runs immediately.

- **State-changing** (`rm`, `docker restart`, `apt install`, —) — paused for your approval first.

  

Both Telegram and Discord show **Allow** / **Deny** buttons on the confirmation message.

  

---

  

## Bot Commands

  

| Command | What it does |

|---------|-------------|

| `/stop` | Cancel whatever the agent is currently doing |

| `/clear` | Wipe the in-memory session history |

  

---

  

## Onboarding

  

On first run the agent reads `BOOTSTRAP.md` from its workspace and introduces itself. It'll ask a few questions to personalize its identity and understand your server setup.

  

Onboarding is one-time — once done, `BOOTSTRAP.md` is removed from the workspace and won't run again.

  

---

  

## What Gets Created on First Run

  

| Path | Purpose |

|------|---------|

| `servclaw.json` | Main config |

| `workspace/` | Agent identity, persona, active jobs |

| `memory/` | Long-term memory and session context |

| `workspace/logs/` | Command execution logs |

| `skills/` | Custom skills (auto-created when you add one) |

  

All excluded from git automatically.

  

---

  

## Feedback & Contributions

  

- Found a bug? [Open an Issue](https://github.com/prathamxtc/servclaw/issues/new)

- Want to chat or connect? [Twitter/X](https://twitter.com/prathamxtc)