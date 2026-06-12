import html

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

from .keyboards import back_kb, btn, kb, main_kb, url_btn

router = Router()

WELCOME = (
    "👋 <b>Claude Code в Telegram</b>\n\n"
    "Настройка — здесь, в личке. Работа с Claude — в отдельной группе с темами: "
    "каждая сессия становится своей темой со своей историей.\n\n"
    "1. «Машины» — добавь сервер (SSH). Ключи безопаснее вводить тут, в личке.\n"
    "2. Подключи рабочую группу (кнопка ниже): бот попросит добавить его в группу "
    "и привяжется сам.\n"
    "3. «Проекты и сессии» — выбери сессию, бот откроет её темой в группе.\n"
    "4. Общайся с Claude и кидай файлы прямо в теме.\n\n"
    "Команды внутри темы: /status · /model · /sessions · /stop · /unbind"
)

_bot_username: str | None = None


def thread_key(message: Message) -> tuple[int, int]:
    return message.chat.id, message.message_thread_id or 0


async def _username(bot) -> str:
    global _bot_username
    if _bot_username is None:
        me = await bot.get_me()
        _bot_username = me.username
    return _bot_username


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, db):
    if message.chat.type != "private":
        await message.answer(
            "Это рабочая группа. Настройка и выбор сессий — в личном чате с ботом. "
            "Здесь общайся с Claude внутри тем."
        )
        return
    connected = bool(await db.get_forum_chat(message.from_user.id))
    await message.answer(WELCOME, reply_markup=main_kb(connected))


@router.callback_query(F.data == "menu:main")
async def cb_main(cb: CallbackQuery, db):
    await cb.answer()
    connected = bool(await db.get_forum_chat(cb.from_user.id))
    await cb.message.edit_text(WELCOME, reply_markup=main_kb(connected))


# ---- connect working group ----

@router.callback_query(F.data == "menu:connectgroup")
async def cb_connect_group(cb: CallbackQuery, db):
    await cb.answer()
    username = await _username(cb.message.bot)
    link = f"https://t.me/{username}?startgroup=connect"
    connected = bool(await db.get_forum_chat(cb.from_user.id))
    note = "✅ Группа уже подключена. Можешь подключить другую — последняя станет рабочей.\n\n" if connected else ""
    await cb.message.edit_text(
        note +
        "🔗 <b>Подключение рабочей группы</b>\n\n"
        "Бот не может создать группу сам (ограничение Telegram), но дальше всё "
        "автоматически:\n\n"
        "1. Нажми кнопку ниже и выбери группу (или создай новую прямо в диалоге).\n"
        "2. В настройках группы включи «Темы» (Topics) и сделай бота "
        "администратором с правом управлять темами.\n"
        "3. Как только бот станет админом в группе с темами, он привяжется сам "
        "и напишет об этом.\n\n"
        "Затем вернись сюда: «Проекты и сессии» → выбор сессии откроет её темой "
        "в этой группе.",
        reply_markup=kb([
            [url_btn("➕ Добавить бота в группу", link)],
            [btn("⬅️ Меню", "menu:main")],
        ]),
    )


@router.my_chat_member()
async def on_my_chat_member(update: ChatMemberUpdated, db):
    """Auto-bind the working group when the bot is added/promoted in a forum
    supergroup. Removes the need to type /bindgroup."""
    chat = update.chat
    if chat.type not in ("group", "supergroup"):
        return
    status = update.new_chat_member.status
    actor = update.from_user
    bot = update.bot

    if status in ("left", "kicked"):
        # If this was someone's working group, forget it.
        if await db.get_forum_chat(actor.id) == chat.id:
            await db.set_forum_chat(actor.id, None)
        return

    if status not in ("administrator", "member"):
        return

    # The chat object in the update can carry a stale is_forum; refresh it.
    is_forum = bool(getattr(chat, "is_forum", False))
    if status == "administrator":
        try:
            fresh = await bot.get_chat(chat.id)
            is_forum = bool(getattr(fresh, "is_forum", is_forum))
        except Exception:
            pass
    can_manage = status == "administrator" and bool(
        getattr(update.new_chat_member, "can_manage_topics", True)
    )

    if is_forum and can_manage:
        await db.set_forum_chat(actor.id, chat.id)
        await bot.send_message(
            chat.id,
            "✅ <b>Группа подключена.</b>\n"
            "Теперь в личном чате выбирай сессии — каждая откроется здесь отдельной темой.",
        )
    elif is_forum and status == "administrator":
        await bot.send_message(
            chat.id,
            "⚠️ Я админ, но без права управлять темами. Включи его в настройках "
            "администратора, и я подключу группу автоматически.",
        )
    elif status == "administrator" and not is_forum:
        await bot.send_message(
            chat.id,
            "⚠️ Включите в этой группе «Темы» (Topics) — без них я не смогу "
            "открывать сессии отдельными темами. После включения я подключусь сам.",
        )
    else:
        await bot.send_message(
            chat.id,
            "👋 Сделайте меня администратором (с правом управлять темами) и включите "
            "«Темы», тогда я автоматически подключу эту группу как рабочую.",
        )


# ---- model ----

MODELS = [("Opus", "opus"), ("Sonnet", "sonnet"), ("Haiku", "haiku")]


def model_kb(current: str | None, private: bool):
    rows = [
        [btn(("✅ " if current == alias else "") + label, f"model:{alias}")]
        for label, alias in MODELS
    ]
    rows.append([btn(("✅ " if not current else "") + "По умолчанию", "model:default")])
    if private:
        rows.append([btn("⬅️ Меню", "menu:main")])
    return kb(rows)


async def _current_model(db, chat_id: int, thread_id: int) -> str | None:
    binding = await db.get_binding(chat_id, thread_id)
    return binding.get("model") if binding else None


@router.message(Command("model"))
async def cmd_model(message: Message, db):
    chat_id, thread_id = thread_key(message)
    current = await _current_model(db, chat_id, thread_id)
    await message.answer(
        "🧠 Модель для этого чата:",
        reply_markup=model_kb(current, message.chat.type == "private"),
    )


@router.callback_query(F.data == "menu:model")
async def cb_model_menu(cb: CallbackQuery, db):
    await cb.answer()
    chat_id = cb.message.chat.id
    thread_id = cb.message.message_thread_id or 0
    current = await _current_model(db, chat_id, thread_id)
    await cb.message.edit_text(
        "🧠 Модель для этого чата:",
        reply_markup=model_kb(current, cb.message.chat.type == "private"),
    )


@router.callback_query(F.data.startswith("model:"))
async def cb_model_set(cb: CallbackQuery, db):
    alias = cb.data.split(":", 1)[1]
    model = None if alias == "default" else alias
    chat_id = cb.message.chat.id
    thread_id = cb.message.message_thread_id or 0
    await db.upsert_binding(chat_id, thread_id, cb.from_user.id, model=model)
    await cb.answer("Модель сохранена")
    await cb.message.edit_text(
        "🧠 Модель для этого чата:",
        reply_markup=model_kb(model, cb.message.chat.type == "private"),
    )


# ---- status ----

async def status_text(db, chat_id: int, thread_id: int, user_id: int) -> str:
    binding = await db.get_binding(chat_id, thread_id)
    if not binding:
        return "Чат пока ни к чему не привязан. Открой /menu и выбери машину."
    machine = None
    if binding.get("machine_id"):
        machine = await db.machine(binding["machine_id"], binding["user_id"])
    lines = ["ℹ️ <b>Статус</b>"]
    if machine:
        lines.append(
            f"🖥 {html.escape(machine['name'])} "
            f"(<code>{html.escape(machine['username'])}@{html.escape(machine['host'])}</code>)"
        )
    else:
        lines.append("🖥 машина не выбрана")
    lines.append(f"📁 <code>{html.escape(binding['cwd'])}</code>" if binding.get("cwd")
                 else "📁 проект не выбран")
    if binding.get("session_id"):
        title = binding.get("title") or ""
        lines.append(f"💬 сессия <code>{binding['session_id'][:8]}</code> {html.escape(title)}")
    else:
        lines.append("💬 новая сессия (создастся при первом сообщении)")
    lines.append(f"🧠 модель: {binding.get('model') or 'по умолчанию'}")
    forum = await db.get_forum_chat(user_id)
    if forum:
        lines.append("🧵 группа с темами подключена")
    return "\n".join(lines)


@router.message(Command("status"))
async def cmd_status(message: Message, db):
    chat_id, thread_id = thread_key(message)
    await message.answer(await status_text(db, chat_id, thread_id, message.from_user.id))


@router.callback_query(F.data == "menu:status")
async def cb_status(cb: CallbackQuery, db):
    await cb.answer()
    text = await status_text(
        db, cb.message.chat.id, cb.message.message_thread_id or 0, cb.from_user.id
    )
    await cb.message.edit_text(text, reply_markup=back_kb("menu:main"))


# ---- forum group binding ----

@router.message(Command("bindgroup"))
async def cmd_bindgroup(message: Message, db):
    if message.chat.type not in ("group", "supergroup"):
        await message.answer(
            "Эту команду нужно отправить в группе, которую ты хочешь использовать "
            "для тем-сессий."
        )
        return
    if not message.chat.is_forum:
        await message.answer(
            "В этой группе не включены темы (topics). Включи их в настройках группы "
            "(группа должна быть супергруппой) и отправь /bindgroup ещё раз."
        )
        return
    await db.set_forum_chat(message.from_user.id, message.chat.id)
    await message.answer(
        "✅ Группа привязана. Теперь выбирай сессии в личном чате с ботом — "
        "каждая откроется здесь отдельной темой."
    )


@router.message(Command("unbind"))
async def cmd_unbind(message: Message, db):
    chat_id, thread_id = thread_key(message)
    if message.chat.type == "private":
        await message.answer("В личном чате привязка переключается через /menu.")
        return
    binding = await db.get_binding(chat_id, thread_id)
    if not binding:
        await message.answer("Эта тема ни к чему не привязана.")
        return
    await db.delete_binding(chat_id, thread_id)
    await message.answer("✅ Тема отвязана от сессии. Сама сессия на сервере не тронута.")
