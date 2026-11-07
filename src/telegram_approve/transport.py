"""Transport abstraction and the python-telegram-bot implementation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, Union

if TYPE_CHECKING:
    from .approver import Approver

#: A file path, or (filename, raw bytes).
Attachment = Union[str, Path, "tuple[str, bytes]"]


class Transport(Protocol):
    """What the Approver needs from a chat backend. Tests use a fake one."""

    def bind(self, approver: Approver) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(
        self,
        chat_id: int,
        text: str,
        buttons: list[tuple[str, str]],
        attachments: list[Attachment],
    ) -> int:
        """Send text with inline buttons (label, callback_data); return the message id."""

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        """Replace the message text and remove its buttons."""

    async def reply(self, chat_id: int, text: str) -> int: ...
    async def answer(self, callback_id: str, text: str) -> None: ...


class TelegramTransport:
    """python-telegram-bot (v21+, async) transport using long polling."""

    def __init__(self, bot_token: str) -> None:
        from telegram.ext import Application

        self.app = Application.builder().token(bot_token).build()
        self._approver: Approver | None = None

    def bind(self, approver: Approver) -> None:
        from telegram.ext import CallbackQueryHandler, MessageHandler, filters

        self._approver = approver
        self.app.add_handler(CallbackQueryHandler(self._on_callback, pattern=r"^ta:"))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(allowed_updates=["message", "callback_query"])

    async def stop(self) -> None:
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    async def send(
        self,
        chat_id: int,
        text: str,
        buttons: list[tuple[str, str]],
        attachments: list[Attachment],
    ) -> int:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton(label, callback_data=data) for label, data in buttons]]
        )
        msg = await self.app.bot.send_message(chat_id, text, reply_markup=markup)
        for att in attachments:
            if isinstance(att, tuple):
                name, data = att
                await self.app.bot.send_document(chat_id, data, filename=name)
            else:
                with open(att, "rb") as fh:
                    await self.app.bot.send_document(chat_id, fh, filename=Path(att).name)
        return msg.message_id

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        await self.app.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)

    async def reply(self, chat_id: int, text: str) -> int:
        msg = await self.app.bot.send_message(chat_id, text)
        return msg.message_id

    async def answer(self, callback_id: str, text: str) -> None:
        await self.app.bot.answer_callback_query(callback_id, text=text)

    async def _on_callback(self, update: Any, context: Any) -> None:
        q = update.callback_query
        if q is None or self._approver is None:
            return
        msg = q.message
        await self._approver.handle_callback(
            q.from_user.id,
            q.data or "",
            callback_id=q.id,
            chat_id=getattr(getattr(msg, "chat", None), "id", None),
            message_id=getattr(msg, "message_id", None),
        )

    async def _on_text(self, update: Any, context: Any) -> None:
        m = update.effective_message
        user = update.effective_user
        if m is None or user is None or self._approver is None:
            return
        await self._approver.handle_text(user.id, m.text or "", chat_id=m.chat_id)
