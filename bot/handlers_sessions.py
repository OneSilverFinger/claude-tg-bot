import html
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from . import claude
from .keyboards import btn, kb
from .render import send_long, trunc

router = Router()

# In-memory caches so callback_data stays short; rebuilt on each list view.
_PROJECT_CACHE: dict[tuple[int, int], list[dict]] = {}
_SESSION_CACHE: dict[tuple[int, int], list[dict]] = {}
# Pending session-open awaiting a group choice (>1 group), keyed by user_id.
_PENDING_OPEN: dict[int, tuple] = {}
# Topics the bot created itself — so the forum_topic_created handler skips them.
_BOT_CREATED: set[tuple[int, int]] = set()

NO_GROUP = (
    "🧵 <b>Нужна рабочая группа</b>\n\n"
    "Чат с Claude идёт только в группе, отдельной темой на каждую сессию.\n\n"
    "1. Создай супергруппу в Telegram и включи в ней «Темы» (Topics).\n"
    "2. Добавь этого бота в группу и сделай админом (право управлять темами).\n"
    "3. Отправь в группе команду /bindgroup.\n\n"
    "После этого вернись сюда и выбери сессию: бот откроет её темой в группе."
)


class ManualPath(StatesGroup):
    cwd = State()


def ago(ts: float) -> str:
    if not ts:
        return ""
    diff = max(0, int(time.time() - ts))
    if diff < 3600:
        return f"{diff // 60}м назад"
    if diff < 86400:
        return f"{diff // 3600}ч назад"
    return f"{diff // 86400}д назад"


async def _binding_and_machine(db, chat_id: int, thread_id: int):
    binding = await db.get_binding(chat_id, thread_id)
    if not binding or not binding.get("machine_id"):
        return None, None
    machine = await db.machine(binding["machine_id"], binding["user_id"])
    return binding, machine


async def _require_machine_cb(db, cb: CallbackQuery):
    binding, machine = await _binding_and_machine(
        db, cb.message.chat.id, cb.message.message_thread_id or 0
    )
    if not machine:
        await cb.answer("Сначала выбери машину через /menu", show_alert=True)
    return machine


def _session_header(machine: dict, cwd: str, title: str | None, session_id: str | None) -> str:
    body = f"💬 {html.escape(trunc(title or '(без названия)', 200))}" if session_id else "🆕 Новая сессия"
    return (
        f"🖥 {html.escape(machine['name'])}\n"
        f"📁 <code>{html.escape(cwd)}</code>\n{body}"
    )


# ---- projects list (works in private as launcher, in a topic as switcher) ----

async def _show_projects(bot, db, ssh, chat_id: int, thread_id: int, user_id: int,
                         edit_message: Message | None = None):
    binding, machine = await _binding_and_machine(db, chat_id, thread_id)

    async def show(text: str, markup=None):
        if edit_message is not None:
            await edit_message.edit_text(text, reply_markup=markup)
        else:
            await bot.send_message(chat_id, text, reply_markup=markup,
                                   message_thread_id=thread_id or None)

    if not machine:
        await show(
            "Сначала выбери машину: /menu → «Машины».",
            kb([[btn("⬅️ Меню", "menu:main")]]),
        )
        return

    if edit_message is not None:
        await edit_message.edit_text("⏳ Читаю проекты на сервере...")
        status = edit_message
    else:
        status = await bot.send_message(
            chat_id, "⏳ Читаю проекты на сервере...", message_thread_id=thread_id or None
        )

    try:
        projects = await claude.list_projects(ssh, machine)
    except Exception as e:
        await status.edit_text(
            f"❌ Не удалось прочитать проекты: <code>{html.escape(str(e)[:200])}</code>",
            reply_markup=kb([[btn("⬅️ Меню", "menu:main")]]),
        )
        return

    _PROJECT_CACHE[(chat_id, thread_id)] = projects

    rows = []
    for i, p in enumerate(projects):
        label = trunc(p["cwd"], 45) + f"  · {p['count']}"
        rows.append([btn(f"📁 {label}", f"p:open:{i}")])
    rows.append([btn("✏️ Ввести путь вручную", "p:manual")])
    rows.append([btn("⬅️ Меню", "menu:main")])

    text = (
        f"📁 <b>Проекты на {html.escape(machine['name'])}</b>\n"
        "Выбери проект, чтобы увидеть его сессии."
        if projects
        else "На сервере пока нет проектов Claude. Введи путь вручную, чтобы начать новую сессию."
    )
    await status.edit_text(text, reply_markup=kb(rows))


@router.callback_query(F.data == "menu:projects")
async def cb_projects(cb: CallbackQuery, db, ssh):
    if not await _require_machine_cb(db, cb):
        return
    await cb.answer()
    await _show_projects(
        cb.message.bot, db, ssh, cb.message.chat.id,
        cb.message.message_thread_id or 0, cb.from_user.id, edit_message=cb.message,
    )


@router.message(Command("sessions"))
async def cmd_sessions(message: Message, db, ssh):
    await _show_projects(
        message.bot, db, ssh, message.chat.id,
        message.message_thread_id or 0, message.from_user.id,
    )


@router.callback_query(F.data.startswith("p:open:"))
async def cb_project_open(cb: CallbackQuery, db, ssh):
    machine = await _require_machine_cb(db, cb)
    if not machine:
        return
    key = (cb.message.chat.id, cb.message.message_thread_id or 0)
    idx = int(cb.data.split(":")[2])
    projects = _PROJECT_CACHE.get(key) or []
    if idx >= len(projects):
        await cb.answer("Список устарел, открой заново", show_alert=True)
        return
    project = projects[idx]
    await cb.answer()
    await cb.message.edit_text("⏳ Читаю сессии...")
    try:
        sessions = await claude.list_sessions(ssh, machine, project["dir"])
    except Exception as e:
        await cb.message.edit_text(
            f"❌ Ошибка: <code>{html.escape(str(e)[:200])}</code>",
            reply_markup=kb([[btn("⬅️ Проекты", "menu:projects")]]),
        )
        return
    _SESSION_CACHE[key] = sessions

    rows = [[btn("🆕 Новая сессия здесь", f"s:new:{idx}")]]
    for i, s in enumerate(sessions):
        label = f"💬 {trunc(s['title'], 40)} · {ago(s['mtime'])}"
        rows.append([btn(label, f"s:open:{idx}:{i}")])
    rows.append([btn("⬅️ Проекты", "menu:projects")])
    await cb.message.edit_text(
        f"📁 <code>{html.escape(project['cwd'])}</code>\n"
        f"Сессий: {len(sessions)}. Выбери, какую продолжить.",
        reply_markup=kb(rows),
    )


# ---- opening a session ----

async def _post_session_hints(bot, chat_id: int, thread_id: int):
    """Right after a session opens, nudge the user toward the per-topic settings
    worth applying first."""
    await bot.send_message(
        chat_id,
        "💡 <b>Можно сразу настроить тему:</b>\n"
        "• /confirm — режим с подтверждением (Claude сначала покажет план, "
        "выполнит по кнопке «Выполнить»)\n"
        "• /model — выбрать модель (opus / sonnet / haiku / fable)\n"
        "• /whichmodel — какая модель настроена и что реально было в последнем ответе",
        message_thread_id=thread_id or None,
    )


async def _open_session(msg: Message, chat_type: str, user_id: int, db, ssh,
                        machine: dict, project: dict, session: dict | None):
    """Open a session and route the chat to the right place.

    From a private chat: create a forum topic in the user's connected group
    and bind it there (work happens only in the group). From inside a group
    topic: rebind that topic in place (switch its session).
    """
    bot = msg.bot
    cwd = (session or {}).get("cwd") or project["cwd"]
    session_id = session["id"] if session else None
    title = session["title"] if session else None
    project_dir = project.get("dir")

    if chat_type != "private":
        chat_id = msg.chat.id
        thread_id = msg.message_thread_id or 0
        await db.upsert_binding(
            chat_id, thread_id, user_id,
            machine_id=machine["id"], cwd=cwd, session_id=session_id, title=title,
        )
        await msg.edit_text(_session_header(machine, cwd, title, session_id))
        if session_id and project_dir:
            await _post_recap(bot, ssh, machine, project_dir, session_id, chat_id, thread_id)
        else:
            await bot.send_message(
                chat_id, "🆕 Новая сессия. Напиши первое сообщение в этой теме.",
                message_thread_id=thread_id or None,
            )
        await _post_session_hints(bot, chat_id, thread_id)
        return

    groups = await db.list_user_groups(user_id)
    if not groups:
        await msg.edit_text(NO_GROUP, reply_markup=kb([[btn("⬅️ Меню", "menu:main")]]))
        return
    if len(groups) == 1:
        await _create_session_topic(
            bot, db, ssh, user_id, groups[0]["chat_id"],
            machine, cwd, session_id, title, project_dir, msg,
        )
        return
    # Several groups connected → ask which one to open the session in.
    _PENDING_OPEN[user_id] = (machine, cwd, session_id, title, project_dir)
    rows = [[btn(g.get("title") or f"группа {g['chat_id']}", f"g:open:{i}")]
            for i, g in enumerate(groups)]
    rows.append([btn("✖️ Отмена", "menu:main")])
    await msg.edit_text("🧵 В какую группу открыть сессию?", reply_markup=kb(rows))


async def _create_session_topic(bot, db, ssh, user_id: int, forum_chat: int,
                                machine: dict, cwd: str, session_id, title,
                                project_dir, src_msg: Message):
    """Create a forum topic in the chosen group, bind the session, post recap+hints."""
    name = trunc(f"{machine['name']}: {title or 'новая сессия'}", 120)
    try:
        topic = await bot.create_forum_topic(forum_chat, name=name)
        _BOT_CREATED.add((forum_chat, topic.message_thread_id))
    except TelegramBadRequest as e:
        await src_msg.edit_text(
            f"❌ Не удалось создать тему: <code>{html.escape(str(e)[:200])}</code>\n"
            "Проверь, что бот админ в группе и «Темы» включены.",
            reply_markup=kb([[btn("⬅️ Меню", "menu:main")]]),
        )
        return

    thread_id = topic.message_thread_id
    await db.upsert_binding(
        forum_chat, thread_id, user_id,
        machine_id=machine["id"], cwd=cwd, session_id=session_id, title=title,
    )
    await bot.send_message(
        forum_chat, "🧵 <b>Сессия открыта</b>\n" + _session_header(machine, cwd, title, session_id),
        message_thread_id=thread_id,
    )
    if session_id and project_dir:
        await _post_recap(bot, ssh, machine, project_dir, session_id, forum_chat, thread_id)
    else:
        await bot.send_message(
            forum_chat, "🆕 Новая сессия. Напиши первое сообщение в этой теме.",
            message_thread_id=thread_id,
        )
    await _post_session_hints(bot, forum_chat, thread_id)
    await src_msg.edit_text(
        "✅ Открыл сессию отдельной темой в группе. Переходи туда — весь чат с Claude идёт там."
    )


@router.callback_query(F.data.startswith("g:open:"))
async def cb_group_open(cb: CallbackQuery, db, ssh):
    i = int(cb.data.split(":")[2])
    pending = _PENDING_OPEN.pop(cb.from_user.id, None)
    if not pending:
        await cb.answer("Выбор устарел — открой сессию заново", show_alert=True)
        return
    groups = await db.list_user_groups(cb.from_user.id)
    if i >= len(groups):
        await cb.answer("Список групп изменился", show_alert=True)
        return
    await cb.answer("Открываю…")
    machine, cwd, session_id, title, project_dir = pending
    await _create_session_topic(
        cb.message.bot, db, ssh, cb.from_user.id, groups[i]["chat_id"],
        machine, cwd, session_id, title, project_dir, cb.message,
    )


@router.callback_query(F.data.startswith("s:open:"))
async def cb_session_open(cb: CallbackQuery, db, ssh):
    machine = await _require_machine_cb(db, cb)
    if not machine:
        return
    key = (cb.message.chat.id, cb.message.message_thread_id or 0)
    _, _, p_idx, s_idx = cb.data.split(":")
    p_idx, s_idx = int(p_idx), int(s_idx)
    projects = _PROJECT_CACHE.get(key) or []
    sessions = _SESSION_CACHE.get(key) or []
    if p_idx >= len(projects) or s_idx >= len(sessions):
        await cb.answer("Список устарел", show_alert=True)
        return
    await cb.answer("Открываю...")
    await _open_session(
        cb.message, cb.message.chat.type, cb.from_user.id, db, ssh,
        machine, projects[p_idx], sessions[s_idx],
    )


@router.callback_query(F.data.startswith("s:new:"))
async def cb_session_new(cb: CallbackQuery, db, ssh):
    machine = await _require_machine_cb(db, cb)
    if not machine:
        return
    key = (cb.message.chat.id, cb.message.message_thread_id or 0)
    p_idx = int(cb.data.split(":")[2])
    projects = _PROJECT_CACHE.get(key) or []
    if p_idx >= len(projects):
        await cb.answer("Список устарел", show_alert=True)
        return
    await cb.answer("Открываю...")
    await _open_session(
        cb.message, cb.message.chat.type, cb.from_user.id, db, ssh,
        machine, projects[p_idx], None,
    )


# ---- manual project path ----

@router.callback_query(F.data == "p:manual")
async def cb_manual(cb: CallbackQuery, db, state: FSMContext):
    if not await _require_machine_cb(db, cb):
        return
    await cb.answer()
    await state.set_state(ManualPath.cwd)
    await cb.message.edit_text(
        "Введи абсолютный путь к проекту на сервере, например <code>/home/ubuntu/app</code>",
        reply_markup=kb([[btn("✖️ Отмена", "p:manual:cancel")]]),
    )


@router.callback_query(F.data == "p:manual:cancel")
async def cb_manual_cancel(cb: CallbackQuery, state: FSMContext, db, ssh):
    await state.clear()
    await cb.answer("Отменено")
    await _show_projects(
        cb.message.bot, db, ssh, cb.message.chat.id,
        cb.message.message_thread_id or 0, cb.from_user.id, edit_message=cb.message,
    )


@router.message(StateFilter(ManualPath.cwd), F.text)
async def st_manual(message: Message, state: FSMContext, db, ssh):
    cwd = message.text.strip()
    await state.clear()
    binding, machine = await _binding_and_machine(
        db, message.chat.id, message.message_thread_id or 0
    )
    if not machine:
        await message.answer("Машина не выбрана. Открой /menu → «Машины».")
        return
    if not cwd.startswith("/"):
        await message.answer("Нужен абсолютный путь (начинается с /). Попробуй /sessions ещё раз.")
        return
    status = await message.answer("⏳ Открываю...")
    await _open_session(
        status, message.chat.type, message.from_user.id, db, ssh,
        machine, {"dir": None, "cwd": cwd}, None,
    )


# ---- recap ----

async def _post_recap(bot, ssh, machine, project_dir, session_id, chat_id, thread_id):
    try:
        tail = await claude.session_tail(ssh, machine, project_dir, session_id)
    except Exception:
        tail = []
    if not tail:
        await bot.send_message(
            chat_id, "📜 История пуста или недоступна. Можно продолжать.",
            message_thread_id=thread_id or None,
        )
        return
    await bot.send_message(
        chat_id, "📜 <b>Последние сообщения этой сессии:</b>",
        message_thread_id=thread_id or None,
    )
    for role, text in tail:
        if role == "user":
            body = html.escape(trunc(text, 1500))[:3800]
            await bot.send_message(
                chat_id, "🧑 <b>Ты:</b>\n" + body,
                message_thread_id=thread_id or None,
            )
        else:
            await bot.send_message(
                chat_id, "🤖 <b>Claude:</b>", message_thread_id=thread_id or None
            )
            await send_long(bot, chat_id, text, thread_id=thread_id)
    await bot.send_message(
        chat_id, "— продолжай диалог в этой теме —",
        message_thread_id=thread_id or None,
    )


# ---- auto-setup when a user creates a forum topic ----

@router.message(F.forum_topic_created)
async def on_topic_created(message: Message, db, ssh):
    """When a user creates a new forum topic in their group, offer the same
    session setup flow as the bot's menu (machine → project → session)."""
    chat_id = message.chat.id
    thread_id = message.message_thread_id or 0
    key = (chat_id, thread_id)
    # Skip topics the bot created itself.
    if key in _BOT_CREATED:
        _BOT_CREATED.discard(key)
        return
    binding = await db.get_binding(chat_id, thread_id)
    if binding and binding.get("machine_id"):
        return  # already set up
    owner = await db.group_owner(chat_id)
    if owner is None:
        return  # not a connected group
    machines = await db.machines(owner)
    if not machines:
        await message.answer(
            "🆕 Тема создана, но у тебя ещё нет машин. Добавь сервер в личке с ботом "
            "(/machines), потом вернись сюда и выбери сессию.",
        )
        return
    rows = [[btn(f"{m['name']} ({m['username']}@{m['host']})", f"ts:mach:{m['id']}")]
            for m in machines]
    await message.answer(
        "🆕 <b>Новая тема — настроим сессию?</b>\nВыбери машину:",
        reply_markup=kb(rows),
    )


@router.callback_query(F.data.startswith("ts:mach:"))
async def cb_ts_machine(cb: CallbackQuery, db, ssh):
    machine_id = int(cb.data.split(":")[2])
    chat_id = cb.message.chat.id
    thread_id = cb.message.message_thread_id or 0
    owner = await db.group_owner(chat_id)
    machine = await db.machine(machine_id, owner) if owner is not None else None
    if not machine:
        await cb.answer("Машина не найдена", show_alert=True)
        return
    # Bind the machine to this user-made topic; keep_name=1 so the bot won't
    # rename the topic the user named.
    await db.upsert_binding(
        chat_id, thread_id, owner,
        machine_id=machine_id, cwd=None, session_id=None, title=None, keep_name=1,
    )
    await cb.answer()
    await _show_projects(cb.message.bot, db, ssh, chat_id, thread_id, owner,
                         edit_message=cb.message)
