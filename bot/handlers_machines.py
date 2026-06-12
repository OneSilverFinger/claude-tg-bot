import html
import logging

import asyncssh
from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from . import claude
from .keyboards import btn, kb

log = logging.getLogger(__name__)
router = Router()


class AddMachine(StatesGroup):
    name = State()
    host = State()
    port = State()
    username = State()
    auth = State()
    secret = State()
    passphrase = State()


class SetKey(StatesGroup):
    value = State()


def cancel_kb():
    return kb([[btn("✖️ Отмена", "m:cancel")]])


async def machines_view(db, user_id: int) -> tuple[str, object]:
    machines = await db.machines(user_id)
    binding = await db.get_binding(user_id, 0)
    current_id = binding.get("machine_id") if binding else None
    if machines:
        text = "🖥 <b>Твои машины</b>\n\nНажми на машину, чтобы выбрать её."
    else:
        text = "🖥 У тебя пока нет машин. Добавь первую."
    rows = []
    for m in machines:
        mark = "✅ " if m["id"] == current_id else ""
        key_mark = "🔑" if m.get("claude_key_enc") else "🔓"
        label = f"{mark}{m['name']} ({m['username']}@{m['host']})"
        rows.append([
            btn(label, f"m:sel:{m['id']}"),
            btn(key_mark, f"m:key:{m['id']}"),
            btn("🗑", f"m:del:{m['id']}"),
        ])
    rows.append([btn("➕ Добавить машину", "m:add")])
    rows.append([btn("⬅️ Меню", "menu:main")])
    return text, kb(rows)


@router.message(Command("machines"))
async def cmd_machines(message: Message, db):
    if message.chat.type != "private":
        await message.answer("Управление машинами доступно в личном чате с ботом.")
        return
    text, markup = await machines_view(db, message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "menu:machines")
async def cb_machines(cb: CallbackQuery, db):
    await cb.answer()
    text, markup = await machines_view(db, cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("m:sel:"))
async def cb_select(cb: CallbackQuery, db):
    machine_id = int(cb.data.split(":")[2])
    machine = await db.machine(machine_id, cb.from_user.id)
    if not machine:
        await cb.answer("Машина не найдена", show_alert=True)
        return
    await db.upsert_binding(
        cb.message.chat.id, 0, cb.from_user.id,
        machine_id=machine_id, cwd=None, session_id=None, title=None,
    )
    await cb.answer("Машина выбрана")
    await cb.message.edit_text(
        f"✅ Машина <b>{html.escape(machine['name'])}</b> выбрана.\n"
        "Теперь выбери проект и сессию.",
        reply_markup=kb([
            [btn("📁 Проекты и сессии", "menu:projects")],
            [btn("⬅️ Машины", "menu:machines")],
        ]),
    )


@router.callback_query(F.data.startswith("m:del:"))
async def cb_delete(cb: CallbackQuery, db):
    machine_id = int(cb.data.split(":")[2])
    machine = await db.machine(machine_id, cb.from_user.id)
    if not machine:
        await cb.answer("Машина не найдена", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(
        f"Удалить машину <b>{html.escape(machine['name'])}</b>? "
        "SSH-ключ будет стёрт из базы бота.",
        reply_markup=kb([
            [btn("🗑 Да, удалить", f"m:delok:{machine_id}"), btn("✖️ Нет", "menu:machines")],
        ]),
    )


@router.callback_query(F.data.startswith("m:delok:"))
async def cb_delete_ok(cb: CallbackQuery, db, ssh):
    machine_id = int(cb.data.split(":")[2])
    await db.delete_machine(machine_id, cb.from_user.id)
    ssh.drop(machine_id)
    await cb.answer("Удалена")
    text, markup = await machines_view(db, cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=markup)


# ---- Claude auth per machine ----
#
# Subscription login only: a long-lived token from `claude setup-token`,
# exported as CLAUDE_CODE_OAUTH_TOKEN — billed to the Claude subscription
# (Pro/Max/...), not the usage-based API.


def _key_menu(machine: dict):
    has = bool(machine.get("claude_key_enc"))
    rows = [
        [btn("⚙️ Установить Claude Code", f"m:install:{machine['id']}")],
        [btn("🔐 Войти по подписке (claude setup-token)", f"m:keyset:{machine['id']}")],
    ]
    if has:
        rows.append([btn("🧹 Убрать авторизацию с сервера", f"m:keyclr:{machine['id']}")])
    rows.append([btn("⬅️ Машины", "menu:machines")])
    return kb(rows)


@router.callback_query(F.data.startswith("m:key:"))
async def cb_key_menu(cb: CallbackQuery, db):
    machine_id = int(cb.data.split(":")[2])
    machine = await db.machine(machine_id, cb.from_user.id)
    if not machine:
        await cb.answer("Машина не найдена", show_alert=True)
        return
    await cb.answer()
    status = "задана ✅" if machine.get("claude_key_enc") else "не задана"
    await cb.message.edit_text(
        f"🔑 <b>Авторизация Claude на {html.escape(machine['name'])}</b>\n\n"
        f"Состояние: {status}\n\n"
        "Нужна, если сервер «потерял» вход в Claude или там другой аккаунт. "
        "Используется токен подписки от <code>claude setup-token</code> — оплата "
        "идёт по твоей подписке Pro/Max, а не по API. Бот зашифрует токен и "
        "положит на сервер файлом 600; каждый запуск Claude подхватит его, не "
        "трогая обычный <code>claude auth</code>.",
        reply_markup=_key_menu(machine),
    )


@router.callback_query(F.data.startswith("m:keyset:"))
async def cb_key_set(cb: CallbackQuery, state: FSMContext):
    if cb.message.chat.type != "private":
        await cb.answer("Авторизация задаётся только в личном чате", show_alert=True)
        return
    machine_id = int(cb.data.split(":")[2])
    await state.set_state(SetKey.value)
    await state.update_data(machine_id=machine_id)
    await cb.answer()
    await cb.message.edit_text(
        "🔐 <b>Вход по подписке</b>\n\n"
        "На своей машине, где есть браузер, выполни:\n"
        "<code>claude setup-token</code>\n\n"
        "Залогинься в свой аккаунт Claude, скопируй выданный токен и пришли его "
        "сюда одним сообщением. Токен живёт около года.\n\n"
        "Сообщение будет удалено сразу после обработки.",
        reply_markup=kb([[btn("✖️ Отмена", "menu:machines")]]),
    )


@router.message(StateFilter(SetKey.value), F.text)
async def st_key_value(message: Message, state: FSMContext, db, ssh, crypto):
    token = message.text.strip()
    await _delete_quietly(message)
    data = await state.get_data()
    await state.clear()
    machine = await db.machine(data["machine_id"], message.from_user.id)
    if not machine:
        await message.answer("Машина не найдена.")
        return

    if len(token) < 20 or any(c.isspace() for c in token):
        await message.answer(
            "Не похоже на валидный токен (слишком короткий или с пробелами). "
            "Открой /machines и попробуй ещё раз."
        )
        return

    status = await message.answer("⏳ Кладу токен на сервер...")
    try:
        await claude.push_claude_key(ssh, machine, token)  # CLAUDE_CODE_OAUTH_TOKEN
    except Exception as e:
        await status.edit_text(
            f"❌ Не удалось записать на сервер: <code>{html.escape(str(e)[:200])}</code>"
        )
        return
    await db.set_claude_key(machine["id"], message.from_user.id, crypto.encrypt(token))
    await status.edit_text(
        f"✅ Вход по подписке установлен на <b>{html.escape(machine['name'])}</b>. "
        "Claude снова сможет авторизоваться при запуске.",
        reply_markup=kb([[btn("⬅️ Машины", "menu:machines")]]),
    )


@router.callback_query(F.data.startswith("m:keyclr:"))
async def cb_key_clear(cb: CallbackQuery, db, ssh):
    machine_id = int(cb.data.split(":")[2])
    machine = await db.machine(machine_id, cb.from_user.id)
    if not machine:
        await cb.answer("Машина не найдена", show_alert=True)
        return
    await cb.answer("Убираю ключ...")
    try:
        await claude.remove_claude_key(ssh, machine)
    except Exception:
        pass
    await db.set_claude_key(machine_id, cb.from_user.id, None)
    text, markup = await machines_view(db, cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=markup)


# ---- install claude ----

@router.callback_query(F.data.startswith("m:install:"))
async def cb_install(cb: CallbackQuery, db, ssh):
    machine_id = int(cb.data.split(":")[2])
    machine = await db.machine(machine_id, cb.from_user.id)
    if not machine:
        await cb.answer("Машина не найдена", show_alert=True)
        return
    await cb.answer()

    status = await cb.message.edit_text(
        f"⚙️ <b>Установка Claude Code на {html.escape(machine['name'])}</b>\n\n"
        "⏳ Подключаюсь...",
    )

    lines: list[str] = []

    async def on_progress(msg: str):
        lines.append(msg)
        body = "\n".join(f"  • {l}" for l in lines[-6:])
        try:
            await status.edit_text(
                f"⚙️ <b>Установка Claude Code на {html.escape(machine['name'])}</b>\n\n"
                f"{body}",
            )
        except Exception:
            pass

    try:
        version = await claude.install_claude(ssh, machine, on_progress)
    except Exception as e:
        await status.edit_text(
            f"❌ <b>Ошибка установки на {html.escape(machine['name'])}</b>\n\n"
            f"<code>{html.escape(str(e)[:400])}</code>",
            reply_markup=kb([
                [btn("🔄 Попробовать снова", f"m:install:{machine_id}")],
                [btn("⬅️ Машины", "menu:machines")],
            ]),
        )
        return

    await status.edit_text(
        f"✅ <b>Claude Code установлен на {html.escape(machine['name'])}</b>\n\n"
        f"Версия: <code>{html.escape(version)}</code>\n\n"
        "Теперь авторизуй Claude — нужен токен подписки (<code>claude setup-token</code>).",
        reply_markup=kb([
            [btn("🔐 Авторизовать", f"m:keyset:{machine_id}")],
            [btn("⬅️ Машины", "menu:machines")],
        ]),
    )


# ---- add machine flow ----

@router.callback_query(F.data == "m:cancel")
async def cb_cancel(cb: CallbackQuery, db, state: FSMContext):
    await state.clear()
    await cb.answer("Отменено")
    text, markup = await machines_view(db, cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "m:add")
async def cb_add(cb: CallbackQuery, state: FSMContext):
    if cb.message.chat.type != "private":
        await cb.answer("Добавлять машины можно только в личном чате", show_alert=True)
        return
    await cb.answer()
    await state.set_state(AddMachine.name)
    await cb.message.edit_text(
        "Как назвать машину? Например: <code>prod-1</code>",
        reply_markup=cancel_kb(),
    )


@router.message(StateFilter(AddMachine.name), F.text)
async def st_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:50])
    await state.set_state(AddMachine.host)
    await message.answer(
        "Хост или IP сервера (без логина), например: <code>server.example.com</code>",
        reply_markup=cancel_kb(),
    )


@router.message(StateFilter(AddMachine.host), F.text)
async def st_host(message: Message, state: FSMContext):
    host = message.text.strip()
    if "@" in host:
        username, host = host.split("@", 1)
        await state.update_data(username=username.strip())
    await state.update_data(host=host)
    await state.set_state(AddMachine.port)
    await message.answer(
        "Порт SSH:",
        reply_markup=kb([[btn("22", "m:port:22")], [btn("✖️ Отмена", "m:cancel")]]),
    )


async def _ask_username(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("username"):
        await state.set_state(AddMachine.auth)
        await _ask_auth(message)
    else:
        await state.set_state(AddMachine.username)
        await message.answer(
            "Логин SSH, например: <code>kim</code>", reply_markup=cancel_kb()
        )


@router.callback_query(StateFilter(AddMachine.port), F.data == "m:port:22")
async def cb_port_default(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.update_data(port=22)
    await _ask_username(cb.message, state)


@router.message(StateFilter(AddMachine.port), F.text)
async def st_port(message: Message, state: FSMContext):
    try:
        port = int(message.text.strip())
        if not 0 < port < 65536:
            raise ValueError
    except ValueError:
        await message.answer("Порт должен быть числом 1-65535. Попробуй ещё раз.")
        return
    await state.update_data(port=port)
    await _ask_username(message, state)


async def _ask_auth(message: Message):
    await message.answer(
        "Как подключаться?",
        reply_markup=kb([
            [btn("🔑 Приватный ключ", "m:auth:key")],
            [btn("🔒 Пароль", "m:auth:password")],
            [btn("✖️ Отмена", "m:cancel")],
        ]),
    )


@router.message(StateFilter(AddMachine.username), F.text)
async def st_username(message: Message, state: FSMContext):
    await state.update_data(username=message.text.strip())
    await state.set_state(AddMachine.auth)
    await _ask_auth(message)


@router.callback_query(StateFilter(AddMachine.auth), F.data.startswith("m:auth:"))
async def cb_auth(cb: CallbackQuery, state: FSMContext):
    auth_type = cb.data.split(":")[2]
    await cb.answer()
    await state.update_data(auth_type=auth_type)
    await state.set_state(AddMachine.secret)
    if auth_type == "key":
        await cb.message.edit_text(
            "Пришли <b>приватный</b> SSH-ключ: файлом или текстом.\n"
            "Сообщение с ключом будет удалено сразу после обработки, "
            "ключ хранится в зашифрованном виде.",
            reply_markup=cancel_kb(),
        )
    else:
        await cb.message.edit_text(
            "Пришли пароль SSH. Сообщение будет удалено сразу после обработки.",
            reply_markup=cancel_kb(),
        )


async def _delete_quietly(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


@router.message(StateFilter(AddMachine.secret), F.document)
async def st_secret_file(message: Message, state: FSMContext, db, ssh, crypto):
    if message.document.file_size and message.document.file_size > 64 * 1024:
        await message.answer("Файл слишком большой для ключа.")
        return
    import io
    buf = io.BytesIO()
    await message.bot.download(message.document, destination=buf)
    await _delete_quietly(message)
    try:
        secret = buf.getvalue().decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        await message.answer("Не похоже на текстовый ключ. Пришли ключ в формате PEM/OpenSSH.")
        return
    await _handle_secret(message, state, db, ssh, crypto, secret)


@router.message(StateFilter(AddMachine.secret), F.text)
async def st_secret_text(message: Message, state: FSMContext, db, ssh, crypto):
    secret = message.text.strip()
    await _delete_quietly(message)
    await _handle_secret(message, state, db, ssh, crypto, secret)


async def _handle_secret(message: Message, state: FSMContext, db, ssh, crypto, secret: str):
    data = await state.get_data()
    if data.get("auth_type") == "key":
        try:
            asyncssh.import_private_key(secret)
        except asyncssh.KeyImportError as e:
            if "passphrase" in str(e).lower():
                await state.update_data(secret=secret)
                await state.set_state(AddMachine.passphrase)
                await message.answer(
                    "Ключ зашифрован. Пришли passphrase (сообщение будет удалено).",
                    reply_markup=cancel_kb(),
                )
                return
            await message.answer(
                f"Не удалось разобрать ключ: {html.escape(str(e))}\n"
                "Пришли приватный ключ в формате PEM или OpenSSH.",
                reply_markup=cancel_kb(),
            )
            return
    await state.update_data(secret=secret)
    await _test_and_save(message, state, db, ssh, crypto)


@router.message(StateFilter(AddMachine.passphrase), F.text)
async def st_passphrase(message: Message, state: FSMContext, db, ssh, crypto):
    passphrase = message.text.strip()
    await _delete_quietly(message)
    data = await state.get_data()
    try:
        asyncssh.import_private_key(data["secret"], passphrase)
    except asyncssh.KeyImportError:
        await message.answer("Passphrase не подошёл. Попробуй ещё раз.", reply_markup=cancel_kb())
        return
    await state.update_data(passphrase=passphrase)
    await _test_and_save(message, state, db, ssh, crypto)


async def _test_and_save(message: Message, state: FSMContext, db, ssh, crypto):
    data = await state.get_data()
    status = await message.answer("⏳ Проверяю подключение...")

    machine = {
        "id": -1,
        "name": data["name"],
        "host": data["host"],
        "port": data.get("port", 22),
        "username": data["username"],
        "auth_type": data["auth_type"],
        "secret_enc": crypto.encrypt(data["secret"]),
        "passphrase_enc": crypto.encrypt(data["passphrase"]) if data.get("passphrase") else None,
    }
    ok, info = await ssh.test(machine)
    if not ok:
        await state.clear()
        await status.edit_text(
            f"❌ Не удалось подключиться: <code>{html.escape(info[:300])}</code>\n"
            "Ничего не сохранено. Начни заново: /machines",
        )
        return

    machine_id = await db.add_machine(
        message.from_user.id, machine["name"], machine["host"], machine["port"],
        machine["username"], machine["auth_type"], machine["secret_enc"],
        machine["passphrase_enc"],
    )
    await db.upsert_binding(
        message.chat.id, 0, message.from_user.id,
        machine_id=machine_id, cwd=None, session_id=None, title=None,
    )
    await state.clear()

    claude_line = (
        f"Claude CLI: <code>{html.escape(info)}</code>"
        if info
        else "⚠️ Claude CLI не найден на сервере. Установи и авторизуй его, иначе ничего не заработает."
    )
    await status.edit_text(
        f"✅ Машина <b>{html.escape(machine['name'])}</b> добавлена и выбрана.\n{claude_line}",
        reply_markup=kb([
            [btn("📁 Проекты и сессии", "menu:projects")],
            [btn("⬅️ Меню", "menu:main")],
        ]),
    )
