import html
import io
import json
import logging
import shlex

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from . import claude
from .keyboards import stop_kb
from .render import LiveEditor, Transcript, send_long

log = logging.getLogger(__name__)
router = Router()

# Active runs keyed by (chat_id, thread_id) so /stop and the inline button work.
_ACTIVE: dict[tuple[int, int], claude.ClaudeRun] = {}

UPLOAD_DIR = ".claude/tg-uploads"
MAX_FILE = 20 * 1024 * 1024

PRIVATE_REDIRECT = (
    "💬 Чат с Claude идёт в рабочей группе, а не здесь.\n\n"
    "Открой /menu → «Проекты и сессии», выбери сессию — бот создаст тему в группе, "
    "и общайся там. Если группа не подключена, /menu подскажет, как это сделать."
)


def _key(message: Message) -> tuple[int, int]:
    return message.chat.id, message.message_thread_id or 0


async def _need_binding(message: Message, db):
    binding = await db.get_binding(*_key(message))
    if not binding or not binding.get("machine_id") or not binding.get("cwd"):
        await message.answer(
            "Сначала выбери машину и проект через /menu.",
            message_thread_id=message.message_thread_id,
        )
        return None, None
    machine = await db.machine(binding["machine_id"], binding["user_id"])
    if not machine:
        await message.answer("Машина не найдена. Открой /machines.")
        return None, None
    return binding, machine


# ---- file uploads ----

async def _upload_file(message: Message, ssh, machine: dict, cwd: str) -> str | None:
    if message.document:
        file_obj = message.document
        filename = file_obj.file_name or f"file_{file_obj.file_unique_id}"
    elif message.photo:
        file_obj = message.photo[-1]
        filename = f"photo_{file_obj.file_unique_id}.jpg"
    else:
        return None
    if getattr(file_obj, "file_size", 0) and file_obj.file_size > MAX_FILE:
        await message.answer("Файл больше 20 МБ, Telegram не отдаёт такие ботам.")
        return None

    buf = io.BytesIO()
    await message.bot.download(file_obj, destination=buf)
    data = buf.getvalue()

    safe = filename.replace("/", "_").replace("\\", "_").lstrip(".") or "file"
    remote_dir = f"{cwd.rstrip('/')}/{UPLOAD_DIR}"
    remote_path = f"{remote_dir}/{safe}"
    await ssh.run(machine, f"mkdir -p {shlex.quote(remote_dir)}", timeout=15)
    sftp = await ssh.sftp(machine)
    async with sftp.open(remote_path, "wb") as f:
        await f.write(data)
    return remote_path


@router.message(F.document | F.photo)
async def on_file(message: Message, db, ssh):
    if message.chat.type == "private":
        await message.answer(PRIVATE_REDIRECT)
        return
    binding, machine = await _need_binding(message, db)
    if not machine:
        return
    status = await message.answer(
        "⏳ Загружаю файл на сервер...", message_thread_id=message.message_thread_id
    )
    try:
        path = await _upload_file(message, ssh, machine, binding["cwd"])
    except Exception as e:
        await status.edit_text(f"❌ Не удалось загрузить: <code>{html.escape(str(e)[:200])}</code>")
        return
    if not path:
        await status.edit_text("Не понял вложение.")
        return

    pending = json.loads(binding.get("pending_files") or "[]")
    pending.append(path)
    await db.upsert_binding(*_key(message), message.from_user.id,
                            pending_files=json.dumps(pending))

    caption = (message.caption or "").strip()
    await status.edit_text(
        f"📎 Загружено: <code>{html.escape(path)}</code>\n"
        "Путь добавится к следующему сообщению. Можешь сразу написать задачу."
    )
    if caption:
        await _run_prompt(message, db, ssh, caption)


# ---- main chat ----

@router.message(Command("stop"))
async def cmd_stop(message: Message, ssh):
    run = _ACTIVE.get(_key(message))
    if not run:
        await message.answer("Сейчас ничего не выполняется.")
        return
    await run.stop(ssh)
    await message.answer("⏹ Останавливаю...")


@router.callback_query(F.data == "run:stop")
async def cb_stop(cb: CallbackQuery, ssh):
    run = _ACTIVE.get((cb.message.chat.id, cb.message.message_thread_id or 0))
    if not run:
        await cb.answer("Уже завершено", show_alert=True)
        return
    await run.stop(ssh)
    await cb.answer("Останавливаю...")


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, db, ssh):
    if message.chat.type == "private":
        await message.answer(PRIVATE_REDIRECT)
        return
    await _run_prompt(message, db, ssh, message.text.strip())


async def _run_prompt(message: Message, db, ssh, prompt: str):
    key = _key(message)
    if key in _ACTIVE:
        await message.answer(
            "⏳ Предыдущий запрос ещё выполняется. Дождись его или нажми «Остановить».",
            message_thread_id=message.message_thread_id,
        )
        return

    binding, machine = await _need_binding(message, db)
    if not machine:
        return

    pending = json.loads(binding.get("pending_files") or "[]")
    if pending:
        files_note = "\n".join(f"- {p}" for p in pending)
        prompt = (
            f"Я загрузил файлы на сервер по этим путям:\n{files_note}\n\n{prompt}"
        )
        await db.upsert_binding(*key, message.from_user.id, pending_files="[]")

    run = claude.ClaudeRun(
        machine=machine,
        cwd=binding["cwd"],
        prompt=prompt,
        resume_id=binding.get("session_id"),
        model=binding.get("model"),
    )
    _ACTIVE[key] = run

    status = await message.answer(
        "🤔 Claude думает...", reply_markup=stop_kb(),
        message_thread_id=message.message_thread_id,
    )
    editor = LiveEditor(message.bot, status.chat.id, status.message_id)
    transcript = Transcript()

    async def on_event(event: dict):
        transcript.feed(event)
        if event.get("type") in ("assistant", "user"):
            await editor.maybe_update("🤔 <b>Работаю...</b>\n\n" + transcript.tail(),
                                      reply_markup=stop_kb())

    result = None
    error = None
    try:
        result = await run.execute(ssh, on_event)
    except Exception as e:
        log.exception("claude run failed")
        error = str(e)
    finally:
        _ACTIVE.pop(key, None)

    # Persist the (possibly new) session id so the next message resumes it.
    if run.session_id and run.session_id != binding.get("session_id"):
        await db.upsert_binding(*key, message.from_user.id, session_id=run.session_id)

    await _finalize(message, editor, run, transcript, result, error)


async def _finalize(message, editor, run, transcript, result, error):
    thread_id = message.message_thread_id

    if run.stopped:
        await editor.set("⏹ Остановлено.\n\n" + transcript.tail(2000))
        return

    if error or result is None:
        detail = error or (run.stderr.strip()[-500:] if run.stderr else "процесс завершился без ответа")
        hint = (
            "\n\n🔑 Похоже на проблему авторизации Claude. Восстанови вход для этой "
            "машины: в личном чате /machines → 🔑 → «Токен подписки» (от "
            "<code>claude setup-token</code>)."
            if claude.looks_like_auth_error(detail)
            else "\n\nПроверь, что claude установлен и авторизован на сервере (claude auth)."
        )
        await editor.set(
            "❌ <b>Ошибка запуска Claude</b>\n"
            f"<code>{html.escape(detail[:600])}</code>" + hint
        )
        return

    if result.get("is_error") or result.get("subtype") not in (None, "success"):
        sub = result.get("subtype")
        body = (result.get("result") or "") + " " + (run.stderr or "")
        hint = (
            "\n\n🔑 Похоже на авторизацию Claude. Восстанови вход: /machines → 🔑 → "
            "«Войти по подписке»."
            if claude.looks_like_auth_error(body)
            else ""
        )
        await editor.set(
            f"⚠️ Claude завершился со статусом <code>{html.escape(str(sub))}</code>.{hint}\n\n"
            + transcript.tail(2000)
        )
        return

    answer = (result.get("result") or "").strip()
    meta = _meta_suffix(result)

    if not answer:
        await editor.set("✅ Готово (без текстового ответа).\n\n" + transcript.tail(2000)
                         + meta)
        return

    # Replace the live status with a short header, then post the full answer.
    await editor.set("✅ <b>Готово</b>" + meta)
    await send_long(message.bot, message.chat.id, answer, thread_id=thread_id)


def _meta_suffix(result: dict) -> str:
    parts = []
    cost = result.get("total_cost_usd")
    if isinstance(cost, (int, float)) and cost:
        parts.append(f"${cost:.3f}")
    dur = result.get("duration_ms")
    if isinstance(dur, (int, float)) and dur:
        parts.append(f"{dur / 1000:.0f}s")
    usage = result.get("usage") or {}
    out_tok = usage.get("output_tokens")
    if out_tok:
        parts.append(f"{out_tok} tok")
    return f"\n<i>{' · '.join(parts)}</i>" if parts else ""
