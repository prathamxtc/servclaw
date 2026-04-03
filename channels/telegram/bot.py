"""Telegram bot interface for the Servclaw AI agent.

Confirmation flow for non-readonly commands:
  - Agent returns a ConfirmationRequest instead of a string.
  - Bot sends the message with [Allow] / [Deny] inline buttons.
  - User can tap a button OR reply in natural language — both work.
  - Pending confirmations are stored per chat_id in bot_data["pending"].
"""

import asyncio
import logging
import os
import re
import uuid
from typing import Set

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent import ConfirmationRequest
from servclaw_config import get_telegram_allowed_user_ids, get_telegram_token, load_config

logger = logging.getLogger(__name__)
_TELEGRAM_TEXT_LIMIT = 4000


def _forbidden_text(user_id: str) -> str:
    return (
        "Forbidden: your Telegram user is not allowed to interact with this bot. "
        f"Your user ID is: {user_id}. "
        "Ask the bot owner to run: servclaw telegram allow <your-user-id>."
    )


_CONFIRM_YES_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|proceed|allow|confirm|run it|affirmative)\b",
    re.IGNORECASE,
)
_CONFIRM_NO_RE = re.compile(
    r"\b(no|nope|nah|cancel|deny|stop|don'?t|abort|negative|skip)\b",
    re.IGNORECASE,
)


def _chunk_text(text: str, limit: int = _TELEGRAM_TEXT_LIMIT) -> list[str]:
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


async def _send_text(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    parse_mode: str | None = None,
    reply_markup=None,
) -> None:
    chunks = _chunk_text(text)
    for idx, chunk in enumerate(chunks):
        markup = reply_markup if idx == 0 else None
        mode = parse_mode if idx == 0 else None
        await context.bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode=mode,
            reply_markup=markup,
        )


def _is_allowed_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    allowed_ids: set[int] = context.bot_data.get("allowed_user_ids", set())
    if not allowed_ids:
        return False
    user = update.effective_user
    if user is None:
        return False
    return int(user.id) in allowed_ids


async def _deny_if_not_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if _is_allowed_user(update, context):
        return False

    user = update.effective_user
    user_id = str(user.id) if user else "unknown"
    logger.warning("Blocked unauthorized Telegram user_id=%s", user_id)
    forbidden_text = _forbidden_text(user_id)

    if update.message:
        await update.message.reply_text(forbidden_text)
        return True

    if update.callback_query:
        await update.callback_query.answer("Forbidden", show_alert=True)
        chat = update.effective_chat
        if chat:
            await _send_text(context, chat.id, forbidden_text)
        return True

    chat = update.effective_chat
    if chat:
        await _send_text(context, chat.id, forbidden_text)
    return True


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_if_not_allowed(update, context):
        return
    # Route through the agent so the LLM drives the conversation naturally.
    # BOOTSTRAP.md is injected into system context when it exists.
    await _handle_message(update, context)


async def _cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_if_not_allowed(update, context):
        return
    agent = context.bot_data["agent"]
    agent.memory.clear_session()
    context.bot_data.setdefault("pending", {}).pop(update.effective_chat.id, None)
    await update.message.reply_text("Runtime history cleared. Fresh start!")


def _new_execution_id(chat_id: int) -> str:
    return f"tg:{chat_id}:{uuid.uuid4().hex[:10]}"


def _is_execution_current(context: ContextTypes.DEFAULT_TYPE, chat_id: int, execution_id: str | None) -> bool:
    active = context.bot_data.setdefault("active_executions", {})
    stopped = context.bot_data.setdefault("stopped_executions", set())
    if execution_id is None:
        return False
    if execution_id in stopped:
        return False
    current = active.get(chat_id)
    # If current is missing, prefer delivering the response instead of dropping silently.
    return current is None or current == execution_id


async def _cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_if_not_allowed(update, context):
        return
    chat_id = update.effective_chat.id
    agent = context.bot_data["agent"]
    pending: dict = context.bot_data.setdefault("pending", {})
    active: dict = context.bot_data.setdefault("active_executions", {})
    stopped: set = context.bot_data.setdefault("stopped_executions", set())

    had_pending = pending.pop(chat_id, None) is not None
    execution_id = active.pop(chat_id, None)

    if execution_id:
        stopped.add(execution_id)
        stopped_any = agent.stop_execution(execution_id)
        if stopped_any:
            await update.message.reply_text(
                "Stopped the current execution. This chat is free now — send your next instruction anytime."
            )
        else:
            await update.message.reply_text(
                "Stop requested. This chat is free now — any late result from the old run will be ignored."
            )
        return

    if had_pending:
        await update.message.reply_text(
            "Cancelled the pending action. This chat is free now — send your next instruction anytime."
        )
        return

    await update.message.reply_text("There is no active execution right now. This chat is already free.")


async def _send_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, req: ConfirmationRequest) -> None:
    chat_id = update.effective_chat.id
    context.bot_data.setdefault("pending", {})[chat_id] = req

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Allow", callback_data="confirm:allow"),
            InlineKeyboardButton("Deny", callback_data="confirm:deny"),
        ]
    ])
    await _send_text(
        context,
        chat_id,
        req.message,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def _finish_response(update: Update, context: ContextTypes.DEFAULT_TYPE, response) -> None:
    chat_id = update.effective_chat.id
    if isinstance(response, ConfirmationRequest):
        if not _is_execution_current(context, chat_id, response.execution_id):
            return
        await _send_confirmation(update, context, response)
    else:
        await _send_text(context, chat_id, response or "(no response)")


def _make_status_callback(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    loop: asyncio.AbstractEventLoop,
):
    """Return a callback that accumulates status lines in a single editable message.

    Instead of sending a new Telegram message for every status update (which
    clutters the chat), we send one message on the first call and then edit
    it in-place for subsequent updates.
    """
    state: dict = {"msg_id": None, "lines": []}

    async def _update(text: str) -> None:
        try:
            state["lines"].append(text)
            content = "\n".join(state["lines"])
            # Keep within Telegram's 4096 char limit — trim oldest lines
            while len(content) > 3800 and len(state["lines"]) > 1:
                state["lines"].pop(0)
                content = "\n".join(state["lines"])
            if state["msg_id"] is None:
                msg = await context.bot.send_message(
                    chat_id=chat_id, text=content, parse_mode="Markdown"
                )
                state["msg_id"] = msg.message_id
            else:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=state["msg_id"],
                    text=content,
                    parse_mode="Markdown",
                )
        except Exception:
            pass

    def callback(text: str) -> None:
        asyncio.run_coroutine_threadsafe(_update(text), loop)

    return callback


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_if_not_allowed(update, context):
        return
    agent = context.bot_data["agent"]
    chat_id = update.effective_chat.id
    user_text = update.message.text

    if not user_text:
        return

    pending: dict = context.bot_data.setdefault("pending", {})
    req = pending.get(chat_id)

    if req is not None:
        if _CONFIRM_YES_RE.search(user_text):
            del pending[chat_id]
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            loop = asyncio.get_event_loop()
            agent._status_callback = _make_status_callback(context, chat_id, loop)
            try:
                response = await loop.run_in_executor(None, agent.confirm_and_run, req)
            except Exception:
                logger.exception("confirm_and_run failed")
                await update.message.reply_text("Error executing command.")
                return
            finally:
                agent._status_callback = None
            if not _is_execution_current(context, chat_id, req.execution_id):
                return
            await _finish_response(update, context, response)
            return

        if _CONFIRM_NO_RE.search(user_text):
            del pending[chat_id]
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
            loop = asyncio.get_event_loop()
            try:
                response = await loop.run_in_executor(None, agent.deny, req)
            except Exception:
                logger.exception("deny failed")
                await update.message.reply_text("Command cancelled.")
                return
            if not _is_execution_current(context, chat_id, req.execution_id):
                return
            await _send_text(context, chat_id, response or "Command cancelled.")
            return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    loop = asyncio.get_event_loop()
    execution_id = _new_execution_id(chat_id)
    context.bot_data.setdefault("active_executions", {})[chat_id] = execution_id
    agent._status_callback = _make_status_callback(context, chat_id, loop)
    cfg = load_config()
    user_cfg = cfg.get("user", {})
    msg_ctx = {
        "channel": "telegram",
        "timestamp": update.message.date.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "user_id": chat_id,
        "country": user_cfg.get("country"),
        "city": user_cfg.get("city"),
        "timezone": user_cfg.get("timezone"),
    }
    try:
        response = await loop.run_in_executor(
            None, lambda: agent.chat(user_text, execution_id, msg_ctx)
        )
    except Exception:
        logger.exception("telegram message handling failed")
        await update.message.reply_text(
            "I hit an internal error while processing that message. Please try again."
        )
        if context.bot_data.setdefault("active_executions", {}).get(chat_id) == execution_id:
            context.bot_data["active_executions"].pop(chat_id, None)
        return
    finally:
        agent._status_callback = None

    if not _is_execution_current(context, chat_id, execution_id):
        current = context.bot_data.setdefault("active_executions", {}).get(chat_id)
        logger.warning(
            "Dropping stale Telegram response chat_id=%s old=%s current=%s",
            chat_id,
            execution_id,
            current,
        )
        return

    try:
        await _finish_response(update, context, response)
    except Exception:
        logger.exception("telegram final response delivery failed")
        await _send_text(context, chat_id, response or "(no response)")
    if not isinstance(response, ConfirmationRequest):
        context.bot_data.setdefault("active_executions", {}).pop(chat_id, None)


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _deny_if_not_allowed(update, context):
        return
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    pending: dict = context.bot_data.setdefault("pending", {})
    req = pending.get(chat_id)

    if req is None:
        await query.edit_message_text("This confirmation has already been handled.")
        return

    action = query.data
    del pending[chat_id]

    agent = context.bot_data["agent"]
    loop = asyncio.get_event_loop()

    if action == "confirm:allow":
        await query.edit_message_text(query.message.text + "\n\n_[Allowed - running...]_", parse_mode="Markdown")
        agent._status_callback = _make_status_callback(context, chat_id, loop)
        try:
            response = await loop.run_in_executor(None, agent.confirm_and_run, req)
        except Exception:
            logger.exception("confirm_and_run failed")
            await _send_text(context, chat_id, "Error executing command.")
            return
        finally:
            agent._status_callback = None
        if not _is_execution_current(context, chat_id, req.execution_id):
            return
        await _finish_response(update, context, response)
        if not isinstance(response, ConfirmationRequest):
            context.bot_data.setdefault("active_executions", {}).pop(chat_id, None)
    else:
        await query.edit_message_text(query.message.text + "\n\n_[Denied]_", parse_mode="Markdown")
        try:
            response = await loop.run_in_executor(None, agent.deny, req)
        except Exception:
            logger.exception("deny failed")
            await _send_text(context, chat_id, "Command cancelled.")
            return
        if not _is_execution_current(context, chat_id, req.execution_id):
            return
        await _send_text(context, chat_id, response or "Command cancelled.")
        context.bot_data.setdefault("active_executions", {}).pop(chat_id, None)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram error", exc_info=context.error)


def start_telegram_bot(agent, scheduler=None) -> None:
    cfg = load_config()
    token = get_telegram_token(cfg)
    enabled = bool(cfg.get("channels", {}).get("telegram", {}).get("enabled", True))
    if not enabled:
        logger.warning("Telegram channel disabled in servclaw.json")
        return

    if not token:
        logger.warning("Telegram token missing in servclaw.json — Telegram bot disabled.")
        return

    logging.basicConfig(
        format="%(asctime)s [telegram] %(levelname)s: %(message)s",
        level=logging.WARNING,
    )

    print("\n✓ Telegram bot polling started\n")

    allowed_user_ids = get_telegram_allowed_user_ids(cfg)
    if allowed_user_ids:
        print(f"✓ Telegram allowlist enabled for user IDs: {sorted(allowed_user_ids)}")
    else:
        print("! Telegram allowlist is empty; all users are blocked until IDs are added.")

    app = ApplicationBuilder().token(token).concurrent_updates(True).build()
    app.bot_data["agent"] = agent
    app.bot_data["allowed_user_ids"] = allowed_user_ids
    app.bot_data["pending"] = {}
    app.bot_data["active_executions"] = {}
    app.bot_data["stopped_executions"] = set()
    app.bot_data["known_chat_ids"] = set()

    # In Telegram, user_id == chat_id for private/DM chats.
    # Use allowedUserIds from config directly — no need to wait for the user to speak first.
    _loop_ref: list[asyncio.AbstractEventLoop] = []

    def push_notification(text: str) -> None:
        if not _loop_ref:
            logger.warning("push_notification: event loop not ready yet")
            return
        loop = _loop_ref[0]
        target_ids = allowed_user_ids  # int set from config
        if not target_ids:
            logger.warning("push_notification: no allowedUserIds configured")
            return

        async def _send_all() -> None:
            for chat_id in target_ids:
                try:
                    await app.bot.send_message(chat_id=chat_id, text=text)
                except Exception as exc:
                    logger.error("push_notification failed for chat_id=%s: %s", chat_id, exc)

        future = asyncio.run_coroutine_threadsafe(_send_all(), loop)
        try:
            future.result(timeout=15)
        except Exception as exc:
            logger.error("push_notification future error: %s", exc)

    if scheduler is not None:
        scheduler.register_push_handler(push_notification)
    agent.register_channel_push("telegram", push_notification)

    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("clear", _cmd_clear))
    app.add_handler(CommandHandler("stop", _cmd_stop))
    app.add_handler(CallbackQueryHandler(_handle_callback, pattern="^confirm:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))
    app.add_error_handler(_error_handler)

    # run_polling() installs Unix signal handlers which only work on the main
    # thread.  We run in a daemon thread, so we drive the event loop manually.
    async def _run() -> None:
        _loop_ref.append(asyncio.get_running_loop())
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        # Block forever — the daemon thread is killed when the process exits.
        await asyncio.Event().wait()

    asyncio.run(_run())
