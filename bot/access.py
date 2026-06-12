import logging

from aiogram import BaseMiddleware
from aiogram.types import Update

log = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    """Drops every update from users that are not whitelisted.

    Messages from strangers in private chats get a single reply with their
    Telegram ID so the admin can whitelist them if needed.
    """

    def __init__(self, allowed_user_ids: set[int]):
        self.allowed = allowed_user_ids

    async def __call__(self, handler, event, data):
        if not isinstance(event, Update):
            return await handler(event, data)

        obj = (
            event.message
            or event.edited_message
            or event.callback_query
            or event.my_chat_member
        )
        user = obj.from_user if obj else None
        if user is None:
            return None

        if user.id not in self.allowed:
            msg = event.message
            if msg is not None and msg.chat.type == "private":
                try:
                    await msg.answer(
                        "⛔ Доступ запрещён.\n"
                        f"Твой Telegram ID: <code>{user.id}</code>\n"
                        "Передай его администратору бота, чтобы попасть в whitelist."
                    )
                except Exception:
                    log.exception("failed to reply to non-whitelisted user")
            return None

        return await handler(event, data)
