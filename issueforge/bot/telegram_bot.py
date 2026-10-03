import asyncio
import ssl
from pathlib import Path
from typing import Optional
from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import TelegramObject, User

from issueforge.bot.handlers import router
from issueforge.bot.messages import (
    build_branch_selection_keyboard,
    build_new_task_keyboard,
    build_question_keyboard,
    build_review_confirmation_keyboard,
    format_branch_selection_message,
    format_new_task_message,
    format_question_message,
    format_review_confirmation_message,
    format_task_completed_message,
    format_task_failure_message,
)
from issueforge.config import settings
from issueforge.core.database import save_task
from issueforge.core.models import Task, TaskQuestion


class AuthMiddleware(BaseMiddleware):
    """Restricts bot access strictly to allowed user IDs. Fails closed: an empty allowlist drops everyone."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user: Optional[User] = data.get("event_from_user")
        # Conversational takeover auto-approves and runs agents, so an unknown sender gets nothing.
        if not user or user.id not in settings.telegram_allowed_user_ids:
            return  # Silently ignore unauthorized requests

        return await handler(event, data)


class TelegramBotManager:
    """Manages Telegram Bot polling lifecycle and proactive message dispatches."""

    def __init__(self):
        self.bot: Optional[Bot] = None
        self.dp: Optional[Dispatcher] = None
        self._polling_task: Optional[asyncio.Task] = None

    def initialize(self) -> bool:
        if not settings.telegram_enabled or not settings.telegram_bot_token:
            return False

        try:
            session = None
            ca_path = Path("/etc/ssl/certs/ca-certificates.crt")
            if ca_path.is_file() or settings.telegram_proxy:
                session = AiohttpSession(proxy=settings.telegram_proxy)
                if ca_path.is_file():
                    session._connector_init["ssl"] = ssl.create_default_context(cafile=str(ca_path))

            self.bot = Bot(token=settings.telegram_bot_token.strip(), session=session)
            self.dp = Dispatcher()
            self.dp.message.middleware(AuthMiddleware())
            self.dp.callback_query.middleware(AuthMiddleware())
            self.dp.include_router(router)
            return True
        except Exception as e:
            print(f"[Telegram Warning] TELEGRAM_BOT_TOKEN error: {e}. Bot disabled, Web Dashboard will continue running.")
            self.bot = None
            self.dp = None
            return False

    async def start(self) -> None:
        """Verify connectivity and start long-polling in background."""
        if not self.initialize() or self.bot is None or self.dp is None:
            return
        if not settings.telegram_allowed_user_ids:
            print("[Telegram Warning] TELEGRAM_ALLOWED_USER_IDS is empty: the bot will ignore everyone until it is set.")

        try:
            bot_info = await self.bot.get_me()
            print(f"[Telegram Bot] Connected successfully as @{bot_info.username} (ID: {bot_info.id})")
            # handle_signals=False: aiogram's own SIGINT/SIGTERM handlers replace uvicorn's
            # and are never removed, so Ctrl+C and `systemctl stop` hung until SIGKILL.
            self._polling_task = asyncio.create_task(self.dp.start_polling(self.bot, handle_signals=False))
        except Exception as e:
            err_str = str(e)
            if "zscaler" in err_str.lower() or "403" in err_str or "blocked" in err_str.lower():
                print(f"[Telegram Notice] Telegram API is blocked by corporate firewall/proxy (Zscaler: Online Chat Category Denied). Telegram bot is inactive, but Web Dashboard on http://localhost:8000 is 100% functional.")
            elif "certificate verify failed" in err_str.lower():
                print(f"[Telegram Notice] Telegram API SSL certificate verification failed. Telegram bot is inactive.")
            else:
                print(f"[Telegram Notice] Could not connect to Telegram API ({e}). Telegram bot is inactive.")
            await self.stop()

    async def stop(self) -> None:
        """Stop bot and cleanup session."""
        if self.dp:
            try:
                await self.dp.stop_polling()
            except Exception:
                pass
        if self.bot and self.bot.session:
            try:
                await self.bot.session.close()
            except Exception:
                pass
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()

    async def _send_to_allowed_users(self, text: str, reply_markup=None) -> Optional[int]:
        if not self.bot:
            return None

        allowed_ids = settings.telegram_allowed_user_ids
        last_msg_id = None
        for uid in allowed_ids:
            try:
                msg = await self.bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )
                last_msg_id = msg.message_id
            except Exception as e:
                print(f"[Telegram Error] Failed to send message to user {uid}: {e}")
        return last_msg_id

    async def send_new_task_alert(self, task: Task) -> None:
        """Send new task notification with inline approval buttons."""
        if not self.bot:
            return

        text = format_new_task_message(task)
        keyboard = build_new_task_keyboard(task.id)
        msg_id = await self._send_to_allowed_users(text, reply_markup=keyboard)
        if msg_id:
            task.telegram_message_id = msg_id
            await save_task(task)

    async def send_question_alert(self, task: Task, question: TaskQuestion) -> None:
        """Send Orchestrator question card to Telegram."""
        if not self.bot:
            return

        text = format_question_message(task, question)
        keyboard = build_question_keyboard(task.id, question)
        msg_id = await self._send_to_allowed_users(text, reply_markup=keyboard)
        if msg_id:
            task.telegram_message_id = msg_id
            await save_task(task)

    async def send_branch_selection(self, task: Task) -> None:
        """Ask the operator which branch this work should target, before any code is written."""
        if not self.bot:
            return

        candidates = task.target_branch_candidates or ["main", "develop", "master", "staging"]
        text = format_branch_selection_message(task)
        keyboard = build_branch_selection_keyboard(task.id, candidates)
        msg_id = await self._send_to_allowed_users(text, reply_markup=keyboard)
        if msg_id:
            task.telegram_message_id = msg_id
            await save_task(task)

    async def send_review_confirmation(self, task: Task) -> None:
        """Send diff review and test report card with confirmation buttons."""
        if not self.bot:
            return

        text = format_review_confirmation_message(task)
        keyboard = build_review_confirmation_keyboard(task.id)
        msg_id = await self._send_to_allowed_users(text, reply_markup=keyboard)
        if msg_id:
            task.telegram_message_id = msg_id
            await save_task(task)

    async def send_task_completed(self, task: Task) -> None:
        """Send completion notice with PR link."""
        if not self.bot:
            return

        text = format_task_completed_message(task)
        await self._send_to_allowed_users(text)

    async def send_task_failure(self, task: Task) -> None:
        """Send failure alert."""
        if not self.bot:
            return

        text = format_task_failure_message(task)
        await self._send_to_allowed_users(text)


telegram_manager = TelegramBotManager()

