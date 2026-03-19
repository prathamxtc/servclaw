---
title: "Telegram Channel Connection Guide"
summary: "Canonical setup and support instructions for Telegram channel"
read_when:
  - User asks to connect Telegram
  - User asks BotFather setup questions
  - Telegram token/setup troubleshooting is requested
---

# Telegram Channel Guide

Use this guide as the source of truth when helping users connect Telegram.

## What to Collect
- Telegram bot token from BotFather
- Allowed Telegram user IDs (comma-separated)

## Setup Steps
1. Open Telegram and start a chat with `@BotFather`.
2. Send `/newbot` and follow prompts for bot name and username.
3. Copy the bot token BotFather returns.
4. Put token into `servclaw.json` at `channels.telegram.token`.
5. Put allowed IDs into `servclaw.json` at `channels.telegram.allowedUserIds`.
6. Restart service:
   - `docker compose -f /mnt/data/Projects/servclaw/docker-compose.yml --project-directory /mnt/data/Projects/servclaw up -d --build servclaw`

## User ID Authorization
- Ask user to message the bot once.
- If blocked, forbidden message shows their user ID.
- Owner can allow with:
  - `servclaw telegram allow <user-id>`

## Verification Checklist
- Bot logs show: `Telegram bot polling started`
- Bot logs show allowlist state
- Authorized user gets responses
- Unauthorized user gets forbidden notice with their user ID

## Troubleshooting
- `Forbidden` for everyone: ensure `channels.telegram.allowedUserIds` is not empty.
- No bot startup: verify `channels.telegram.token` is valid.
- Changes not applied: rebuild/restart compose service.

## Agent Behavior Rule
When user asks Telegram setup/connect questions:
- Use this guide for instructions.
- Give concise step-by-step help.
- Ask only for missing values.
- Offer to verify by checking logs when requested.
