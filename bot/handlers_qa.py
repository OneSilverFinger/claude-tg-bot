import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from . import handlers_chat, qa

log = logging.getLogger(__name__)
router = Router()


async def _resolve(message: Message, db):
    """Binding + machine for the topic, or (None, None) with a reply sent."""
    binding = await db.get_binding(message.chat.id, message.message_thread_id or 0)
    if not binding or not binding.get("machine_id") or not binding.get("cwd"):
        await message.answer("Сначала выбери проект и сессию через /menu.")
        return None, None
    machine = await db.machine(binding["machine_id"], binding["user_id"])
    if not machine:
        await message.answer("Машина не найдена. Открой /machines.")
        return None, None
    return binding, machine


async def _ensure_tools(message: Message, ssh, machine: dict) -> bool:
    if await qa.is_installed(ssh, machine):
        return True
    status = await message.answer("🧪 Разворачиваю QA-инструменты (Playwright)...")
    lines: list[str] = []

    async def progress(msg: str):
        lines.append(msg)
        try:
            await status.edit_text("🧪 <b>Установка QA-инструментов</b>\n\n"
                                   + "\n".join(f"  • {l}" for l in lines[-6:]))
        except Exception:
            pass

    try:
        await qa.install(ssh, machine, progress)
    except Exception as e:
        await status.edit_text(
            f"❌ Не удалось поставить QA-инструменты:\n<code>{html.escape(str(e)[:400])}</code>"
        )
        return False
    await status.edit_text("✅ QA-инструменты готовы. Запускаю тест...")
    return True


async def _start_test(message: Message, db, ssh):
    if message.chat.type == "private":
        await message.answer(
            "Тестирование запускается в теме сессии, а не в личном чате."
        )
        return
    binding, machine = await _resolve(message, db)
    if not machine:
        return
    if not await _ensure_tools(message, ssh, machine):
        return
    await handlers_chat._run_prompt(message, db, ssh, qa.ORCHESTRATION_PROMPT, qa_run=True)


@router.message(Command("test"))
async def cmd_test(message: Message, db, ssh):
    await _start_test(message, db, ssh)


@router.callback_query(F.data == "qa:run")
async def cb_test(cb: CallbackQuery, db, ssh):
    await cb.answer("Запускаю тестирование...")
    await _start_test(cb.message, db, ssh)
