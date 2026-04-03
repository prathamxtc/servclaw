"""Discord DM bot interface for the Servclaw AI agent.

Only responds to direct messages (DMs). Server messages are ignored.

Commands (send in DM):
  /start  — say hello and check bot status
  /stop   — cancel the currently running execution
  /clear  — reset session memory for this chat

Confirmation flow for non-readonly commands:
  - Agent returns a ConfirmationRequest instead of a string.
  - Bot replies with [Allow] / [Deny] buttons.
  - User can also reply in natural language — both work.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any

import discord

from agent import ConfirmationRequest
from servclaw_config import (
    get_discord_allowed_user_ids,
    get_discord_token,
    load_config,
)

logger = logging.getLogger(__name__)
_DISCORD_TEXT_LIMIT = 1900  # Stay under Discord's 2000-char message limit


_CONFIRM_YES_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|proceed|allow|confirm|run it|affirmative)\b",
    re.IGNORECASE,
)
_CONFIRM_NO_RE = re.compile(
    r"\b(no|nope|nah|cancel|deny|stop|don'?t|abort|negative|skip)\b",
    re.IGNORECASE,
)


def _chunk_text(text: str, limit: int = _DISCORD_TEXT_LIMIT) -> list[str]:
    payload = (text or "(no response)").strip()
    if not payload:
        return ["(no response)"]
    chunks: list[str] = []
    remaining = payload
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < int(limit * 0.5):
            split_at = remaining.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            split_at = len(chunk)
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def _send_text(channel: discord.DMChannel, text: str) -> None:
    for chunk in _chunk_text(text):
        await channel.send(chunk)


def _forbidden_text(user_id: int) -> str:
    return (
        f"Forbidden: your Discord user ID `{user_id}` is not on the allowlist.\n"
        f"Ask the owner to run: `servclaw discord allow {user_id}`"
    )


def _new_execution_id(user_id: int) -> str:
    return f"dc:{user_id}:{uuid.uuid4().hex[:10]}"


def _make_status_callback(
    channel: discord.DMChannel,
    loop: asyncio.AbstractEventLoop,
):
    """Return a callback that accumulates status lines in a single editable message.

    Instead of sending a new Discord message for every status update (which
    triggers rate limits), we send one message on the first call and then
    edit it in-place for subsequent updates.
    """
    state: dict = {"msg": None, "lines": []}

    async def _update(text: str) -> None:
        try:
            state["lines"].append(text)
            content = "\n".join(state["lines"])
            # Keep within Discord's 2000 char limit — trim oldest lines
            while len(content) > 1800 and len(state["lines"]) > 1:
                state["lines"].pop(0)
                content = "\n".join(state["lines"])
            if state["msg"] is None:
                state["msg"] = await channel.send(content)
            else:
                await state["msg"].edit(content=content)
        except Exception:
            pass

    def callback(text: str) -> None:
        asyncio.run_coroutine_threadsafe(_update(text), loop)

    return callback


class _ConfirmView(discord.ui.View):
    """Allow / Deny buttons for a pending ConfirmationRequest."""

    def __init__(
        self,
        client: "ServclawDiscordClient",
        user_id: int,
        req: ConfirmationRequest,
        channel: discord.DMChannel,
    ) -> None:
        super().__init__(timeout=300)
        self._client = client
        self._user_id = user_id
        self._req = req
        self._channel = channel

    @discord.ui.button(label="Allow", style=discord.ButtonStyle.green)
    async def allow_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self._user_id:
            await interaction.response.send_message(
                "This confirmation is not for you.", ephemeral=True
            )
            return

        self.stop()
        await interaction.response.edit_message(
            content=(interaction.message.content or "") + "\n\n*[Allowed — running...]*",
            view=None,
        )

        client = self._client
        loop = asyncio.get_event_loop()
        client.agent._status_callback = _make_status_callback(self._channel, loop)
        try:
            response = await loop.run_in_executor(
                None, client.agent.confirm_and_run, self._req
            )
        except Exception:
            logger.exception("confirm_and_run failed")
            await self._channel.send("Error executing command.")
            return
        finally:
            client.agent._status_callback = None

        if not client._is_execution_current(self._user_id, self._req.execution_id):
            return

        await _finish_response(self._channel, client, self._user_id, response)
        if not isinstance(response, ConfirmationRequest):
            client._active_executions.pop(self._user_id, None)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red)
    async def deny_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self._user_id:
            await interaction.response.send_message(
                "This confirmation is not for you.", ephemeral=True
            )
            return

        self.stop()
        await interaction.response.edit_message(
            content=(interaction.message.content or "") + "\n\n*[Denied]*",
            view=None,
        )

        client = self._client
        loop = asyncio.get_event_loop()
        try:
            response = await loop.run_in_executor(None, client.agent.deny, self._req)
        except Exception:
            logger.exception("deny failed")
            await self._channel.send("Command cancelled.")
            return

        if not client._is_execution_current(self._user_id, self._req.execution_id):
            return

        await _send_text(self._channel, response or "Command cancelled.")
        client._active_executions.pop(self._user_id, None)

    async def on_timeout(self) -> None:
        client = self._client
        req = self._req
        client._pending.pop(self._user_id, None)
        client._active_executions.pop(self._user_id, None)
        try:
            await self._channel.send("Confirmation timed out — command cancelled.")
        except Exception:
            pass
        try:
            client.agent.deny(req)
        except Exception:
            pass


async def _finish_response(
    channel: discord.DMChannel,
    client: "ServclawDiscordClient",
    user_id: int,
    response: Any,
) -> None:
    if isinstance(response, ConfirmationRequest):
        if not client._is_execution_current(user_id, response.execution_id):
            return
        client._pending[user_id] = response
        view = _ConfirmView(client, user_id, response, channel)
        chunks = _chunk_text(response.message)
        if len(chunks) > 1:
            for chunk in chunks[:-1]:
                await channel.send(chunk)
        await channel.send(chunks[-1], view=view)
    else:
        await _send_text(channel, response or "(no response)")


class ServclawDiscordClient(discord.Client):
    """Discord DM-only client for the Servclaw agent."""

    def __init__(self, agent: Any, allowed_user_ids: set[int]) -> None:
        intents = discord.Intents.default()
        # message_content is a privileged intent required only for guild messages.
        # DMs deliver message content without it, so we don't request it.
        super().__init__(intents=intents)
        self.agent = agent
        self.allowed_user_ids = allowed_user_ids
        self._pending: dict[int, ConfirmationRequest] = {}
        self._active_executions: dict[int, str] = {}
        self._stopped_executions: set[str] = set()
        self._dm_channels: dict[int, discord.DMChannel] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    async def on_ready(self) -> None:
        logger.info("Discord bot connected as %s (id=%s)", self.user, self.user.id)
        print(f"\n✓ Discord bot ready: {self.user}\n")
        # Cache the running event loop so push_notification can schedule coroutines
        # from non-async threads without relying on the deprecated client.loop property.
        self._loop = asyncio.get_running_loop()
        # Pre-fetch DM channels for all allowed users so push works even if they
        # have never sent a message to the bot on Discord.
        if self.allowed_user_ids:
            for uid in self.allowed_user_ids:
                try:
                    user = await self.fetch_user(uid)
                    dm = await user.create_dm()
                    self._dm_channels.setdefault(uid, dm)
                except Exception as exc:
                    logger.warning("Could not pre-fetch DM channel for user %s: %s", uid, exc)

    def _is_execution_current(self, user_id: int, execution_id: str | None) -> bool:
        active = self._active_executions
        stopped = self._stopped_executions
        if execution_id is None:
            return False
        if execution_id in stopped:
            return False
        current = active.get(user_id)
        # If current is missing, prefer delivering the response instead of dropping silently.
        return current is None or current == execution_id

    async def on_message(self, message: discord.Message) -> None:
        # DMs only — ignore server messages
        if not isinstance(message.channel, discord.DMChannel):
            return
        # Ignore messages from self
        if message.author == self.user:
            return

        user_id: int = message.author.id

        if not self.allowed_user_ids or user_id not in self.allowed_user_ids:
            await message.channel.send(_forbidden_text(user_id))
            return

        content = (message.content or "").strip()

        # Track DM channel for push notifications
        self._dm_channels[user_id] = message.channel

        if content == "/stop":
            await self._cmd_stop(message)
        elif content == "/clear":
            await self._cmd_clear(message)
        else:
            # All messages (including /start) go through the agent.
            # BOOTSTRAP.md is injected into the LLM system prompt when it exists,
            # so the model drives the onboarding conversation naturally.
            await self._handle_message(message)



    async def _cmd_clear(self, message: discord.Message) -> None:
        self.agent.memory.clear_session()
        self._pending.pop(message.author.id, None)
        await message.channel.send("Runtime history cleared. Fresh start!")

    async def _cmd_stop(self, message: discord.Message) -> None:
        user_id = message.author.id
        had_pending = self._pending.pop(user_id, None) is not None
        execution_id = self._active_executions.pop(user_id, None)

        if execution_id:
            self._stopped_executions.add(execution_id)
            stopped_any = self.agent.stop_execution(execution_id)
            if stopped_any:
                await message.channel.send(
                    "Stopped the current execution. Send your next message anytime."
                )
            else:
                await message.channel.send(
                    "Stop requested. Any late result from the old run will be ignored."
                )
            return

        if had_pending:
            await message.channel.send(
                "Cancelled the pending action. Send your next message anytime."
            )
            return

        await message.channel.send(
            "There is no active execution right now. This chat is already free."
        )

    async def _handle_message(self, message: discord.Message) -> None:
        user_id = message.author.id
        user_text = message.content

        if not user_text:
            return

        channel: discord.DMChannel = message.channel

        # Natural-language confirmation reply
        req = self._pending.get(user_id)
        if req is not None:
            if _CONFIRM_YES_RE.search(user_text):
                del self._pending[user_id]
                loop = asyncio.get_event_loop()
                self.agent._status_callback = _make_status_callback(channel, loop)
                try:
                    response = await loop.run_in_executor(
                        None, self.agent.confirm_and_run, req
                    )
                except Exception:
                    logger.exception("confirm_and_run failed")
                    await channel.send("Error executing command.")
                    return
                finally:
                    self.agent._status_callback = None
                if not self._is_execution_current(user_id, req.execution_id):
                    return
                await _finish_response(channel, self, user_id, response)
                return

            if _CONFIRM_NO_RE.search(user_text):
                del self._pending[user_id]
                loop = asyncio.get_event_loop()
                try:
                    response = await loop.run_in_executor(None, self.agent.deny, req)
                except Exception:
                    logger.exception("deny failed")
                    await channel.send("Command cancelled.")
                    return
                if not self._is_execution_current(user_id, req.execution_id):
                    return
                await _send_text(channel, response or "Command cancelled.")
                return

        # Normal message
        loop = asyncio.get_event_loop()
        execution_id = _new_execution_id(user_id)
        self._active_executions[user_id] = execution_id
        self.agent._status_callback = _make_status_callback(channel, loop)
        cfg = load_config()
        user_cfg = cfg.get("user", {})
        msg_ctx = {
            "channel": "discord",
            "timestamp": message.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "user_id": user_id,
            "country": user_cfg.get("country"),
            "city": user_cfg.get("city"),
            "timezone": user_cfg.get("timezone"),
        }
        try:
            response = await loop.run_in_executor(
                None, lambda: self.agent.chat(user_text, execution_id, msg_ctx)
            )
        except Exception:
            logger.exception("discord message handling failed")
            await channel.send(
                "I hit an internal error while processing that message. Please try again."
            )
            if self._active_executions.get(user_id) == execution_id:
                self._active_executions.pop(user_id, None)
            return
        finally:
            self.agent._status_callback = None

        if not self._is_execution_current(user_id, execution_id):
            current = self._active_executions.get(user_id)
            logger.warning(
                "Dropping stale Discord response user_id=%s old=%s current=%s",
                user_id,
                execution_id,
                current,
            )
            return

        try:
            await _finish_response(channel, self, user_id, response)
        except Exception:
            logger.exception("discord final response delivery failed")
            await _send_text(channel, response or "(no response)")
        if not isinstance(response, ConfirmationRequest):
            self._active_executions.pop(user_id, None)

    def push_notification(self, text: str) -> None:
        """Thread-safe: send text to all DM channels where allowed users have chatted."""
        if not self.is_ready() or self._loop is None:
            return

        async def _send_all() -> None:
            for channel in list(self._dm_channels.values()):
                try:
                    await channel.send(text)
                except Exception:
                    pass

        asyncio.run_coroutine_threadsafe(_send_all(), self._loop)


def start_discord_bot(agent: Any, scheduler=None) -> None:
    cfg = load_config()
    token = get_discord_token(cfg)
    enabled = bool(cfg.get("channels", {}).get("discord", {}).get("enabled", True))

    if not enabled:
        logger.warning("Discord channel disabled in servclaw.json")
        return

    if not token:
        logger.warning("Discord token missing in servclaw.json — Discord bot disabled.")
        return

    logging.basicConfig(
        format="%(asctime)s [discord] %(levelname)s: %(message)s",
        level=logging.WARNING,
    )

    allowed_user_ids = get_discord_allowed_user_ids(cfg)
    if allowed_user_ids:
        print(f"✓ Discord allowlist enabled for user IDs: {sorted(allowed_user_ids)}")
    else:
        print("! Discord allowlist is empty; all users are blocked until IDs are added.")

    client = ServclawDiscordClient(agent, allowed_user_ids)
    if scheduler is not None:
        scheduler.register_push_handler(client.push_notification)
    agent.register_channel_push("discord", client.push_notification)
    client.run(token, log_handler=None)
