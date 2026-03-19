# Channels

## Telegram

The simplest way to use Servclaw.

**Setup:**
The installer will ask for your bot token and Telegram user ID. To get a token: message `@BotFather` → `/newbot` → follow prompts.

**Easiest way to get your user ID:** once the bot is running, just message it. If your ID isn't authorized, the bot will reply with your user ID. Then run:

```bash
servclaw telegram allow <user-id>
servclaw telegram deny <user-id>
```

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
