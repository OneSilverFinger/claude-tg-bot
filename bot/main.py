import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .access import AccessMiddleware
from .config import load_config
from .crypto import Crypto
from .db import Database
from .ssh import SSHManager
from . import handlers_chat, handlers_machines, handlers_menu, handlers_sessions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")


async def main():
    config = load_config()
    crypto = Crypto(config.master_key)
    db = Database(config.db_path)
    await db.init()
    ssh = SSHManager(crypto)

    bot = Bot(
        config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Dependencies injected into every handler via keyword args.
    dp["db"] = db
    dp["ssh"] = ssh
    dp["crypto"] = crypto
    dp["config"] = config

    dp.update.outer_middleware(AccessMiddleware(config.allowed_user_ids))

    # Order matters: specific command/callback routers before the catch-all
    # text handler in handlers_chat.
    dp.include_router(handlers_menu.router)
    dp.include_router(handlers_machines.router)
    dp.include_router(handlers_sessions.router)
    dp.include_router(handlers_chat.router)

    me = await bot.get_me()
    log.info("Starting bot @%s, %d whitelisted user(s)",
             me.username, len(config.allowed_user_ids))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
