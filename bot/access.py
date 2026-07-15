import logging
import time

from aiogram import BaseMiddleware
from aiogram.types import Update

log = logging.getLogger(__name__)


class DedupMiddleware(BaseMiddleware):
    """Drops duplicate Telegram updates on two levels.

    1. By update_id (persisted): re-delivered updates (same update_id) from proxy
       re-fetches or restart re-delivery run only once, even across restarts.
    2. By content window (in-memory): the same text in the same chat/thread within
       CONTENT_WINDOW seconds is dropped. This catches the observed case where the
       *same message arrives as two different updates* ~1 min apart (right after the
       first Claude run finishes) — which update_id dedup can't see. Short texts are
       exempt so legitimately repeated commands ("да", "делай") still go through.
    """

    CONTENT_WINDOW = 150.0   # seconds
    MIN_LEN = 12             # only dedup texts at least this long

    def __init__(self, db):
        self.db = db
        # (chat_id, thread_id, text) -> (monotonic_ts, message_id, update_id, date)
        self._recent: dict[tuple, tuple] = {}

    def _content_key(self, event):
        msg = event.message
        if msg is None:
            return None, None
        text = (msg.text or msg.caption or "").strip()
        if len(text) < self.MIN_LEN:
            return None, None
        thread = msg.message_thread_id or 0
        return (msg.chat.id, thread, text), msg

    async def __call__(self, handler, event, data):
        if isinstance(event, Update):
            # Level 1: persisted update_id idempotency.
            try:
                if not await self.db.mark_update_seen(event.update_id, time.time()):
                    log.info("dropping duplicate update_id=%s", event.update_id)
                    return None
            except Exception:
                log.exception("dedup check failed; processing update anyway")

            # Level 2: content window (message text/caption).
            key, msg = self._content_key(event)
            if key is not None:
                now = time.monotonic()
                # prune stale entries
                for k in [k for k, v in self._recent.items()
                          if now - v[0] > self.CONTENT_WINDOW]:
                    self._recent.pop(k, None)
                prev = self._recent.get(key)
                if prev and now - prev[0] < self.CONTENT_WINDOW:
                    log.warning(
                        "CONTENT-DUP dropped: chat=%s thr=%s dt=%.1fs | "
                        "prev(msg_id=%s upd=%s date=%s) cur(msg_id=%s upd=%s date=%s) | %r",
                        key[0], key[1], now - prev[0],
                        prev[1], prev[2], prev[3],
                        msg.message_id, event.update_id, msg.date, key[2][:40],
                    )
                    return None
                self._recent[key] = (now, msg.message_id, event.update_id, msg.date)

        return await handler(event, data)


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
