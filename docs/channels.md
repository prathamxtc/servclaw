# Channels

## Command Safety & Confirmation

The agent classifies every command it executes as **read-only** or **state-changing**:

- **Read-only** (`ls`, `cat`, `docker ps`, `grep`, `df`, …) — runs silently and immediately.
- **State-changing** (`rm`, `docker restart`, `apt install`, `chmod`, `systemctl stop`, …) — paused until you approve.

When a state-changing command is queued you'll receive a message with the exact command and a prompt to allow or deny. On Telegram this comes with inline **Allow / Deny** buttons. On Discord, reply `yes` / `no` (or any natural equivalent).

The model decides the classification. When in doubt it defaults to asking. You can always approve the whole sequence in one go by saying *"yes go ahead"* after the first prompt — already-approved commands in that sequence won't be asked again.

---

## Telegram

The simplest way to use Servclaw.

**Setup:**
The installer will ask for your bot token and Telegram user ID. To get a token: message `@BotFather` → `/newbot` → follow prompts.

**Easiest way to get your user ID:** once the bot is running, just message it. If your ID isn't authorized, the bot will reply with your user ID. Then run:

```bash
servclaw telegram allow <user-id>
servclaw telegram deny <user-id>
```

**Bot commands:**

| Command | Effect |
|---------|--------|
| `/stop` | Cancel the current execution immediately |
| `/clear` | Clear in-memory session history |

**Onboarding:** On first run the agent walks you through a short setup based on `BOOTSTRAP.md`. If it doesn't start on its own, just say "let's do the setup" or "introduce yourself".

---

## Discord

Servclaw operates via DMs only — no server channel required.

**Before running the installer:**
1. Go to [discord.com/developers](https://discord.com/developers) → New Application → Bot
2. Under **Bot** settings, enable **Message Content Intent**
3. Copy the bot token — the installer will ask for it
4. Invite the bot to your server so you can DM it

**Easiest way to get your user ID:** once the bot is running, DM it once. If your ID isn't authorized, it will reply with your user ID. Then run:

```bash
servclaw discord allow <user-id>
servclaw discord deny <user-id>
```

**Bot commands:**

| Command | Effect |
|---------|--------|
| `/stop` | Cancel the current execution immediately |
| `/clear` | Clear in-memory session history |

**Onboarding:** Same as Telegram — the agent reads `BOOTSTRAP.md` on first run and guides you through setup. If it doesn't kick off automatically, ask it to introduce itself.
