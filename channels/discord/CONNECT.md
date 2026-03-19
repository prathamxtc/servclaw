# Discord Channel — Setup Guide

## What this enables
Servclaw will respond to direct messages (DMs) from allowed users on Discord.
Server messages are intentionally ignored — DM-only.

## Step 1: Create a Discord Application + Bot
1. Go to https://discord.com/developers/applications
2. Click **New Application** → give it a name.
3. Go to **Bot** tab → click **Add Bot**.
4. Under **Token** → click **Reset Token** and copy it — this is your `DISCORD_BOT_TOKEN`.
5. Under **Privileged Gateway Intents** enable:
   - **Message Content Intent** ← required so the bot can read your messages

## Step 2: Invite the bot to your account (DM-only, no server needed)
The bot only needs to be able to receive DMs from allowed users.
No server invite is required — users can just DM the bot directly.

To verify the bot is running, open a DM with it and type `/start`.

## Step 3: Get your Discord user ID
1. Open Discord Settings → Advanced → enable **Developer Mode**.
2. Right-click your username anywhere → **Copy User ID**.
3. Add it to the allowlist:
   ```
   servclaw discord allow <your-user-id>
   ```

## servclaw.json structure
```json
"channels": {
  "discord": {
    "enabled": true,
    "token": "YOUR_BOT_TOKEN_HERE",
    "allowedUserIds": [],
    "streaming": "off"
  }
}
```

## Available DM commands
| Command  | Description                           |
|----------|---------------------------------------|
| `/start` | Greet the bot and check status        |
| `/stop`  | Cancel the currently running command  |
| `/clear` | Reset session memory for this chat    |

## Security note
Keep `servclaw.json` out of version control — it contains your bot token.
