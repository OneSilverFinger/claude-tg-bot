import asyncio
import html
import io
import json
import logging
import posixpath
import re
import shlex
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from . import claude, qa, transcribe
from .keyboards import plan_kb, stop_kb, test_kb
from .render import LiveEditor, Transcript, send_long

log = logging.getLogger(__name__)
router = Router()

# Active runs keyed by (chat_id, thread_id) so /stop and the inline button work.
_ACTIVE: dict[tuple[int, int], claude.ClaudeRun] = {}

# In confirm mode: prompt awaiting «Выполнить», keyed by (chat_id, thread_id).
_PENDING_PLAN: dict[tuple[int, int], str] = {}

UPLOAD_DIR = ".claude/tg-uploads"
MAX_FILE = 20 * 1024 * 1024

PRIVATE_REDIRECT = (
    "💬 Чат с Claude идёт в рабочей группе, а не здесь.\n\n"
    "Открой /menu → «Проекты и сессии», выбери сессию — бот создаст тему в группе, "
    "и общайся там. Если группа не подключена, /menu подскажет, как это сделать."
)


def _key(message: Message) -> tuple[int, int]:
    return message.chat.id, message.message_thread_id or 0


# Common secret/token shapes. If a chat message matches, the bot deletes it and
# posts a redacted notice, but still passes the full original to the agent.
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"\bgsk_[A-Za-z0-9]{30,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b[ps]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
    re.compile(r"\bperm[-:][A-Za-z0-9._=\-]{20,}"),                                # YouTrack
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-]{20,}=*"),
    re.compile(r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)"
               r"\s*[:=]\s*\S{6,}"),
]


_GENERIC_TOKEN = re.compile(r"[A-Za-z0-9_\-]{25,}")
_REDACTED = "•••[скрыто]•••"


def _looks_like_token(tok: str) -> bool:
    """High-entropy heuristic: long mixed-case alnum (upper+lower+digit). Catches
    prefix-less tokens while skipping prose, lowercase slugs and hex hashes."""
    return (any(c.isupper() for c in tok)
            and any(c.islower() for c in tok)
            and any(c.isdigit() for c in tok))


def _redact_secrets(text: str) -> tuple[str, bool]:
    """Return (redacted_text, found). Found secrets become a placeholder."""
    found = False

    def repl(_m):
        nonlocal found
        found = True
        return _REDACTED

    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(repl, out)

    # Catch-all for prefix-less tokens.
    def generic_repl(m):
        nonlocal found
        tok = m.group(0)
        if _looks_like_token(tok):
            found = True
            return _REDACTED
        return tok

    out = _GENERIC_TOKEN.sub(generic_repl, out)
    return out, found


async def _need_binding(message: Message, db):
    binding = await db.get_binding(*_key(message))
    if not binding or not binding.get("machine_id") or not binding.get("cwd"):
        await message.answer(
            "Сначала выбери машину и проект через /menu.",
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
        "⏳ Загружаю файл на сервер...",
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


# ---- voice / video notes ----

@router.message(F.voice | F.video_note)
async def on_voice(message: Message, db, ssh, config):
    if message.chat.type == "private":
        await message.answer(PRIVATE_REDIRECT)
        return
    binding, machine = await _need_binding(message, db)
    if not machine:
        return
    if not config.stt_api_key:
        await message.answer(
            "🎤 Транскрипция голосовых не настроена. Задай <code>STT_API_KEY</code> "
            "в <code>.env</code> (по умолчанию используется Groq Whisper) и пересобери бота."
        )
        return

    if message.voice:
        file_obj, fname = message.voice, "voice.ogg"
    else:
        file_obj, fname = message.video_note, "note.mp4"
    if getattr(file_obj, "file_size", 0) and file_obj.file_size > MAX_FILE:
        await message.answer("Запись слишком большая (Telegram отдаёт ботам до 20 МБ).")
        return

    status = await message.answer("🎤 Распознаю...")
    try:
        buf = io.BytesIO()
        await message.bot.download(file_obj, destination=buf)
        text = await transcribe.transcribe(
            buf.getvalue(), fname,
            base_url=config.stt_base_url,
            api_key=config.stt_api_key,
            model=config.stt_model,
        )
    except Exception as e:
        log.exception("transcription failed")
        await status.edit_text(
            f"❌ Не удалось распознать: <code>{html.escape(str(e)[:200])}</code>"
        )
        return

    if not text:
        await status.edit_text("🎤 Ничего не распознал — пустая или неразборчивая запись.")
        return

    await status.edit_text("🎤 <i>" + html.escape(text) + "</i>")
    await _run_prompt(message, db, ssh, text)


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


# ---- confirm mode ----

@router.message(Command("confirm"))
async def cmd_confirm(message: Message, db):
    if message.chat.type == "private":
        await message.answer("Команда работает в теме сессии, а не в личном чате.")
        return
    key = _key(message)
    binding = await db.get_binding(*key)
    if not binding or not binding.get("machine_id"):
        await message.answer("Сначала открой сессию в этой теме (/menu).")
        return
    new = 0 if binding.get("confirm_mode") else 1
    await db.upsert_binding(*key, binding["user_id"], confirm_mode=new)
    if new:
        await message.answer(
            "🔒 <b>Режим с подтверждением ВКЛ</b>\nТеперь я сначала покажу план "
            "(ничего не меняя), а выполню только после кнопки «✅ Выполнить»."
        )
    else:
        _PENDING_PLAN.pop(key, None)
        await message.answer("⚡ <b>Режим с подтверждением ВЫКЛ</b>\nВыполняю сразу, как обычно.")


@router.callback_query(F.data == "plan:exec")
async def cb_plan_exec(cb: CallbackQuery, db, ssh):
    key = (cb.message.chat.id, cb.message.message_thread_id or 0)
    prompt = _PENDING_PLAN.pop(key, None)
    if not prompt:
        await cb.answer("Нет плана к выполнению", show_alert=True)
        return
    await cb.answer("Выполняю…")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _run_prompt(cb.message, db, ssh, prompt, force_execute=True)


@router.callback_query(F.data == "plan:cancel")
async def cb_plan_cancel(cb: CallbackQuery):
    key = (cb.message.chat.id, cb.message.message_thread_id or 0)
    _PENDING_PLAN.pop(key, None)
    await cb.answer("Отменено")
    try:
        await cb.message.edit_text("✖️ План отменён. Напиши, что поправить.")
    except Exception:
        pass


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, db, ssh):
    if message.chat.type == "private":
        await message.answer(PRIVATE_REDIRECT)
        return
    text = message.text.strip()
    redacted, has_secret = _redact_secrets(text)
    if has_secret:
        # Wipe the secret from the chat, but still pass the full text to the agent.
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(
            "🔒 <b>Заметил креды — скрыл их и удалил сообщение.</b>\n"
            "Агенту передано в полном виде. Видимая версия:\n\n"
            + html.escape(redacted[:3500])
        )
    await _run_prompt(message, db, ssh, text)


async def _run_prompt(message: Message, db, ssh, prompt: str, qa_run: bool = False,
                      force_execute: bool = False):
    key = _key(message)
    if key in _ACTIVE:
        await message.answer(
            "⏳ Предыдущий запрос ещё выполняется. Дождись его или нажми «Остановить».",
        )
        return

    binding, machine = await _need_binding(message, db)
    if not machine:
        return

    qa_since = time.time() if qa_run else 0.0
    # Confirm mode: first show a plan (read-only), execute only after «Выполнить».
    plan_mode = bool(binding.get("confirm_mode")) and not qa_run and not force_execute
    permission_mode = "plan" if plan_mode else "bypassPermissions"

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
        permission_mode=permission_mode,
    )
    _ACTIVE[key] = run

    status = await message.answer(
        "🤔 Claude думает...", reply_markup=stop_kb(),
    )
    # Record the in-flight run so a bot restart can re-attach / recover it.
    await db.add_active_run(
        key[0], key[1], status.message_id, run.run_id, machine["id"],
        message.from_user.id, binding["cwd"], run.session_id, binding.get("model"),
    )
    editor = LiveEditor(message.bot, status.chat.id, status.message_id)
    transcript = Transcript()
    new_summary: list[str] = []
    last_event = time.monotonic()

    async def on_event(event: dict):
        nonlocal last_event
        last_event = time.monotonic()
        transcript.feed(event)
        if event.get("type") == "system" and event.get("subtype") == "init":
            sid = event.get("session_id")
            if sid:
                # Persist immediately so an interrupted *new* session stays linked.
                await db.update_active_run_session(key[0], key[1], sid)
        if event.get("type") == "summary" and event.get("summary"):
            new_summary.append(event["summary"])
        elif event.get("type") in ("assistant", "user"):
            await editor.maybe_update("🤔 <b>Работаю...</b>\n\n" + transcript.tail(),
                                      reply_markup=stop_kb())

    started = time.monotonic()

    async def heartbeat():
        # During a long silent step (build, tests) no events arrive and the
        # status would look frozen. Show elapsed time so it's clearly alive.
        while True:
            await asyncio.sleep(20)
            if time.monotonic() - last_event < 18:
                continue
            elapsed = int(time.monotonic() - started)
            mm, ss = divmod(elapsed, 60)
            clock = f"{mm}м {ss:02d}с" if mm else f"{ss}с"
            tail = transcript.tail(2500)
            body = f"\n\n{tail}" if tail and tail != "…" else ""
            await editor.maybe_update(
                f"⏳ <b>Работаю… {clock}</b>{body}", reply_markup=stop_kb()
            )

    # Live QA demonstration: stream screenshots + step progress as the
    # independent tester works, so a multi-minute run isn't a black box.
    sent_shots: set[str] = set()
    qa_editor = None
    if qa_run:
        qa_msg = await message.answer(
            "🧪 <b>Тестирование запущено</b>\nСобираю кейсы и поднимаю браузер…"
        )
        qa_editor = LiveEditor(message.bot, qa_msg.chat.id, qa_msg.message_id, interval=1.0)

    async def qa_feed():
        last_progress = ""
        while True:
            await asyncio.sleep(8)
            try:
                shots = await qa.new_screenshots(
                    ssh, machine, binding["cwd"], qa_since, exclude=sent_shots
                )
                for name, data in shots:
                    sent_shots.add(name)
                    try:
                        await message.bot.send_photo(
                            message.chat.id, BufferedInputFile(data, filename=name),
                            caption="🧪 " + html.escape(name),
                            message_thread_id=message.message_thread_id or None,
                        )
                    except Exception:
                        log.exception("qa live screenshot failed")
                prog = await qa.read_progress(ssh, machine, binding["cwd"])
                if prog and prog != last_progress and qa_editor:
                    last_progress = prog
                    await qa_editor.set("🧪 <b>Ход теста</b>\n\n" + html.escape(prog[-3000:]))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("qa feed iteration failed")

    hb_task = asyncio.create_task(heartbeat())
    qa_task = asyncio.create_task(qa_feed()) if qa_run else None
    result = None
    error = None
    try:
        result = await run.execute(ssh, on_event)
    except Exception as e:
        log.exception("claude run failed")
        error = str(e)
    finally:
        hb_task.cancel()
        if qa_task:
            qa_task.cancel()
        for t in (hb_task, qa_task):
            if t:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        _ACTIVE.pop(key, None)
        await db.delete_active_run(key[0], key[1])
        await run.cleanup(ssh)

    # Persist the (possibly new) session id so the next message resumes it.
    if run.session_id and run.session_id != binding.get("session_id"):
        await db.upsert_binding(*key, message.from_user.id, session_id=run.session_id)

    # Sync forum topic name with Claude's auto-generated summary.
    if new_summary and message.message_thread_id and not error and not run.stopped:
        summary = new_summary[-1]
        topic_name = f"{machine['name']}: {summary}"[:128]
        try:
            await message.bot.edit_forum_topic(
                message.chat.id, message.message_thread_id, name=topic_name
            )
            await db.upsert_binding(*key, message.from_user.id, title=summary)
        except Exception:
            pass

    await _finalize(message.bot, ssh, message.chat.id, message.message_thread_id,
                    editor, run, transcript, result, error, qa_run=qa_run,
                    plan_mode=plan_mode, prompt=prompt)

    if qa_run and not error and not run.stopped:
        # Final sweep for any shots the live feed didn't catch (already-sent
        # ones are excluded so nothing is duplicated).
        await _send_qa_screenshots(message, ssh, machine, binding["cwd"], qa_since,
                                   exclude=sent_shots)


async def _send_qa_screenshots(message, ssh, machine, cwd, since_mtime, exclude=None):
    try:
        shots = await qa.new_screenshots(ssh, machine, cwd, since_mtime, exclude=exclude)
    except Exception:
        log.exception("fetching qa screenshots failed")
        return
    if not shots:
        return
    thread_id = message.message_thread_id
    for name, data in shots:
        try:
            await message.bot.send_photo(
                message.chat.id, BufferedInputFile(data, filename=name),
                caption=html.escape(name), message_thread_id=thread_id or None,
            )
        except Exception:
            log.exception("sending qa screenshot %s failed", name)


# Deliverable-looking files the agent may reference in its answer — send the
# actual file, not just the path. Source/config extensions are deliberately
# excluded so a code discussion doesn't dump source files.
_FILE_RE = re.compile(
    r"(/?[\w.\-]+(?:/[\w.\-]+)*\.(?:png|jpe?g|gif|webp|svg|pdf|zip|tar|gz|tgz|"
    r"csv|xlsx?|docx?|pptx?|md|txt|mp4|mov|mp3|wav|log))\b",
    re.IGNORECASE,
)
MAX_SEND_FILE = 20 * 1024 * 1024


async def _send_referenced_files(bot, ssh, machine, cwd, chat_id, thread_id, text):
    """If the answer references existing deliverable files, send them as documents."""
    if not text:
        return
    cands = [m.group(1) for m in _FILE_RE.finditer(text)]
    if not cands:
        return
    try:
        sftp = await ssh.sftp(machine)
    except Exception:
        return
    sent, done = 0, set()
    for p in cands:
        if sent >= 5:
            break
        abspath = p if p.startswith("/") else f"{cwd.rstrip('/')}/{p}"
        abspath = posixpath.normpath(abspath)
        # Same file referenced more than once (e.g. absolute + relative) → send once.
        if abspath in done:
            continue
        done.add(abspath)
        try:
            st = await sftp.stat(abspath)
            size = st.size or 0
            if size == 0 or size > MAX_SEND_FILE:
                continue
            async with sftp.open(abspath, "rb") as f:
                data = await f.read()
        except Exception:
            continue
        name = abspath.rsplit("/", 1)[-1]
        try:
            await bot.send_document(
                chat_id, BufferedInputFile(data, filename=name),
                caption=f"📎 <code>{html.escape(abspath)}</code>",
                message_thread_id=thread_id or None,
            )
            sent += 1
        except Exception:
            log.exception("send_document failed for %s", abspath)


async def _finalize(bot, ssh, chat_id, thread_id, editor, run, transcript, result, error,
                    qa_run=False, plan_mode=False, prompt=None):
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

    if plan_mode:
        # Confirm mode: this was a read-only plan. Show it + «Выполнить»/«Отмена».
        _PENDING_PLAN[(chat_id, thread_id)] = prompt
        await editor.set("📋 <b>План готов — подтверди выполнение</b>" + meta,
                         reply_markup=plan_kb())
        if answer:
            await send_long(bot, chat_id, answer, thread_id=thread_id)
        return

    # Offer one-tap QA after a normal run; skip it on the QA run itself.
    done_kb = None if qa_run else test_kb()

    if not answer:
        await editor.set("✅ Готово (без текстового ответа).\n\n" + transcript.tail(2000)
                         + meta, reply_markup=done_kb)
        return

    # Replace the live status with a short header, then post the full answer.
    await editor.set("✅ <b>Готово</b>" + meta, reply_markup=done_kb)
    await send_long(bot, chat_id, answer, thread_id=thread_id)
    # If the answer points at real deliverable files, send them too.
    await _send_referenced_files(bot, ssh, run.machine, run.cwd, chat_id, thread_id, answer)


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
