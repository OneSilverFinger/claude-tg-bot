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

from . import claude, machine_status, qa, tables, transcribe
from .keyboards import plan_kb, stop_kb, test_kb
from .render import LiveEditor, Transcript, send_long

log = logging.getLogger(__name__)
router = Router()

# Active runs keyed by (chat_id, thread_id) so /stop and the inline button work.
_ACTIVE: dict[tuple[int, int], claude.ClaudeRun] = {}

# In confirm mode: prompt awaiting «Выполнить», keyed by (chat_id, thread_id).
_PENDING_PLAN: dict[tuple[int, int], str] = {}

# Post-run session watchers keyed by (chat_id, thread_id). After a run finishes,
# the bot keeps tailing the session's jsonl for a while and forwards any *new*
# assistant messages that appear (e.g. from a VS Code session on the same file,
# or a background continuation) into the topic. Superseded by the next run.
_WATCHERS: dict[tuple[int, int], asyncio.Task] = {}
_WATCH_POLL = 5           # seconds between polls
_WATCH_WINDOW = 600       # total seconds to keep watching after a run
_WATCH_IDLE = 180         # stop early if nothing new for this long

# Background-task continuation: if a run registered long background tasks, the bot
# polls them and, when they finish, auto-launches a `claude --resume` turn with
# their output so the result reaches the user (the one-shot turn can't wait).
_BG_POLL = 15             # seconds between background-task checks
_BG_MAX_WAIT = 6 * 3600   # give up waiting after this long
_BG_MAX_CHAIN = 5         # cap chained continuations to avoid runaway loops

# Debounce/batching of incoming text: a long message split by Telegram into
# several parts (or a burst of quick follow-ups) should reach Claude as ONE
# prompt. Buffer text per (chat, thread); each new part resets a 15s timer; on
# silence the parts are joined and run once.
_BATCH: dict[tuple[int, int], dict] = {}
_BATCH_WINDOW = 3.0       # seconds of silence before the batch fires

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
    chat_id, thread_id = _key(message)
    binding = await db.get_binding(chat_id, thread_id)
    if not binding or not binding.get("machine_id") or not binding.get("cwd"):
        # Common mistake: typing in the group's General instead of a session
        # topic. If the group has topic-bound sessions, point there.
        hint = ""
        if thread_id == 0 and message.chat.type != "private":
            try:
                if await db.chat_has_topic_bindings(chat_id):
                    hint = ("\n\n⚠️ Похоже, ты пишешь в <b>общий чат</b> группы, а не "
                            "в тему сессии. Открой нужную тему и пиши в ней — "
                            "Claude отвечает только внутри тем.")
            except Exception:
                pass
        await message.answer(
            "Сначала выбери машину и проект через /menu." + hint,
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
    try:
        async with sftp.open(remote_path, "wb") as f:
            await f.write(data)
    finally:
        try:
            sftp.exit()
        except Exception:
            pass
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

_MODEL_LABELS = {"opus": "Opus", "sonnet": "Sonnet", "haiku": "Haiku", "fable": "Fable"}


@router.message(Command("whichmodel"))
async def cmd_whichmodel(message: Message, db, ssh, crypto):
    binding, machine = await _need_binding(message, db)
    if not machine:
        return
    cfg = binding.get("model")
    cfg_label = _MODEL_LABELS.get(cfg) or "по умолчанию (модель выбирает сам Claude Code)"
    status = await message.answer("⏳ Проверяю модель…")
    actual = None
    sid = binding.get("session_id")
    if sid:
        try:
            actual = await claude.last_session_model(ssh, machine, sid)
        except Exception:
            log.exception("last_session_model failed")
    all_machines = await db.machines(message.from_user.id)
    tok_line = machine_status.token_line(crypto, machine, all_machines)
    await status.edit_text(
        "🧠 <b>Модель этой темы</b>\n"
        f"• Настроено (<code>/model</code>): <b>{html.escape(cfg_label)}</b>\n"
        f"• Фактически в последнем ответе: <code>{html.escape(actual or '— (ещё не было ответов)')}</code>\n"
        f"🖥 {html.escape(machine['name'])}\n"
        f"{tok_line}"
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message, ssh):
    key = _key(message)
    run = _ACTIVE.get(key)
    if not run:
        if key in _BATCH:
            _cancel_batch(key)
            await message.answer("⏹ Отложенные сообщения сброшены.")
        elif key in _WATCHERS:
            _cancel_watcher(key)
            await message.answer("⏹ Слежение за сессией остановлено.")
        else:
            await message.answer("Сейчас ничего не выполняется.")
        return
    await run.stop(ssh)
    await message.answer("⏹ Останавливаю...")


@router.callback_query(F.data == "run:stop")
async def cb_stop(cb: CallbackQuery, ssh):
    key = (cb.message.chat.id, cb.message.message_thread_id or 0)
    run = _ACTIVE.get(key)
    if not run:
        if key in _WATCHERS:
            _cancel_watcher(key)
            await cb.answer("Слежение за сессией остановлено")
        else:
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


def _cancel_batch(key: tuple[int, int]):
    entry = _BATCH.pop(key, None)
    if entry and entry.get("timer") and not entry["timer"].done():
        entry["timer"].cancel()
    return entry


async def _batch_timer(key: tuple[int, int], delay: float):
    try:
        await asyncio.sleep(delay)
        await _flush_batch(key)
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("batch flush failed")


async def _flush_batch(key: tuple[int, int]):
    entry = _BATCH.get(key)
    if not entry:
        return
    # A run is still going on this topic — wait for it, keep the buffer intact.
    if key in _ACTIVE:
        entry["timer"] = asyncio.create_task(_batch_timer(key, 5.0))
        return
    _BATCH.pop(key, None)
    if entry.get("status_id"):
        try:
            await entry["message"].bot.delete_message(key[0], entry["status_id"])
        except Exception:
            pass
    combined = "\n\n".join(entry["parts"])
    await _run_prompt(entry["message"], entry["db"], entry["ssh"], combined)


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

    # Debounce: collect parts for 15s, then run them as one prompt.
    key = _key(message)
    entry = _BATCH.get(key)
    if entry is None:
        entry = {"parts": [], "message": message, "db": db, "ssh": ssh,
                 "timer": None, "status_id": None}
        _BATCH[key] = entry
        try:
            status = await message.answer("⏳ Жду продолжение (3с)…")
            entry["status_id"] = status.message_id
        except Exception:
            pass
    entry["parts"].append(text)
    entry["message"] = message  # latest, for chat/thread/bot context on flush
    if entry.get("timer") and not entry["timer"].done():
        entry["timer"].cancel()
    entry["timer"] = asyncio.create_task(_batch_timer(key, _BATCH_WINDOW))


def _cancel_watcher(key: tuple[int, int]):
    task = _WATCHERS.pop(key, None)
    if task and not task.done():
        task.cancel()


async def _watch_session(bot, ssh, machine, cwd, session_id, chat_id, thread_id, key):
    """Tail the session jsonl after a run and forward new assistant messages.

    In `-p` (one-shot) mode a finished run writes nothing more on its own, but the
    same session file is also written by a VS Code session or any later run — this
    surfaces those messages in the topic instead of silently missing them.
    """
    # Reuse ONE SFTP client for the whole loop (opening one per poll leaks
    # channels and exhausts sshd MaxSessions → every run's follow() then can't
    # read output and all sessions hang on "Claude works").
    sftp = None
    try:
        path = await claude.find_session_file(ssh, machine, session_id)
        if not path:
            return
        try:
            sftp = await ssh.sftp(machine)
            offset = (await sftp.stat(path)).size or 0
        except Exception:
            return
        started = time.monotonic()
        last_new = started
        while True:
            await asyncio.sleep(_WATCH_POLL)
            now = time.monotonic()
            if now - started > _WATCH_WINDOW or now - last_new > _WATCH_IDLE:
                break
            # A new run on this topic supersedes passive watching.
            if key in _ACTIVE:
                break
            if sftp is None:
                try:
                    sftp = await ssh.sftp(machine)
                except Exception:
                    continue
            try:
                offset, texts = await claude.new_assistant_messages(sftp, path, offset)
            except asyncio.CancelledError:
                raise
            except Exception:
                # channel/connection went bad — drop it, reopen next poll
                try:
                    sftp.exit()
                except Exception:
                    pass
                sftp = None
                continue
            if not texts:
                continue
            last_new = time.monotonic()
            for text in texts:
                try:
                    await send_long(
                        bot, chat_id, "💬 _новое сообщение в сессии_",
                        thread_id=thread_id or None,
                    )
                    await _send_answer(bot, chat_id, thread_id, text)
                    await _send_referenced_files(
                        bot, ssh, machine, cwd, chat_id, thread_id, text
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("session watch delivery failed")
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("session watcher crashed")
    finally:
        if sftp is not None:
            try:
                sftp.exit()
            except Exception:
                pass
        if _WATCHERS.get(key) is asyncio.current_task():
            _WATCHERS.pop(key, None)


async def _run_continuation(bot, db, ssh, machine, cwd, session_id, model,
                            chat_id, thread_id, key, prompt, depth):
    """Run a `claude --resume` turn not triggered by a user message (used to
    deliver background-task results), streaming it into the topic like a normal
    run. Re-arms watching afterwards (chained background or passive)."""
    if key in _ACTIVE:
        return  # a real user run is in progress — don't clobber it
    run = claude.ClaudeRun(
        machine=machine, cwd=cwd, prompt=prompt,
        resume_id=session_id, model=model,
    )
    _ACTIVE[key] = run
    status = await bot.send_message(
        chat_id, "🤔 Claude продолжает…",
        message_thread_id=thread_id or None, reply_markup=stop_kb(),
    )
    editor = LiveEditor(bot, status.chat.id, status.message_id)
    transcript = Transcript()

    async def on_event(event: dict):
        transcript.feed(event)
        if event.get("type") in ("assistant", "user"):
            await editor.maybe_update("🤔 <b>Продолжаю…</b>\n\n" + transcript.tail(),
                                      reply_markup=stop_kb())

    result = None
    error = None
    try:
        result = await run.execute(ssh, on_event)
    except Exception as e:
        log.exception("continuation run failed")
        error = str(e)
    finally:
        _ACTIVE.pop(key, None)
        await run.cleanup(ssh)

    if run.session_id and run.session_id != session_id:
        await db.upsert_binding(*key, None, session_id=run.session_id)

    await _finalize(bot, ssh, chat_id, thread_id, editor, run, transcript,
                    result, error, qa_run=False, plan_mode=False, prompt=prompt)

    # Re-arm: the continuation may itself have spawned background tasks.
    if error or run.stopped or not run.session_id or key in _ACTIVE:
        return
    try:
        bg = await claude.bg_tasks(ssh, machine, run.run_id)
    except Exception:
        bg = []
    if bg and depth < _BG_MAX_CHAIN:
        _WATCHERS[key] = asyncio.create_task(_watch_background(
            bot, db, ssh, machine, cwd, run.session_id, model,
            chat_id, thread_id, key, run.run_id, depth + 1))
    else:
        _WATCHERS[key] = asyncio.create_task(_watch_session(
            bot, ssh, machine, cwd, run.session_id, chat_id, thread_id, key))


async def _watch_background(bot, db, ssh, machine, cwd, session_id, model,
                            chat_id, thread_id, key, run_id, depth):
    """Wait for a run's registered background tasks, then continue the session
    with their output. Superseded by a new user run on the same topic."""
    try:
        started = time.monotonic()
        while True:
            await asyncio.sleep(_BG_POLL)
            if key in _ACTIVE:
                return  # a new user run took over this topic
            try:
                tasks = await claude.bg_tasks(ssh, machine, run_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("bg task poll failed")
                continue
            if not tasks:
                return  # nothing registered / already cleaned up
            if any(t["running"] for t in tasks):
                if time.monotonic() - started > _BG_MAX_WAIT:
                    await bot.send_message(
                        chat_id, "⌛️ Фоновая задача идёт слишком долго — "
                        "перестаю ждать. Напиши, когда захочешь проверить.",
                        message_thread_id=thread_id or None)
                    return
                continue
            break  # all tasks finished

        # Collect outputs, then remove the marker dir so we don't re-trigger.
        parts = []
        for t in tasks:
            body = (await claude.read_bg_output(ssh, machine, t["out"])).strip()
            parts.append(f"### {t['name']} (pid {t['pid']})\n{body or '(без вывода)'}")
        summary = "\n\n".join(parts)
        try:
            await ssh.run(
                machine,
                f'rm -rf ~/{claude.RUNS_DIR}/{shlex.quote(run_id)}.bg',
                timeout=15)
        except Exception:
            pass

        await bot.send_message(
            chat_id, "✅ Фоновая задача завершилась — продолжаю сессию…",
            message_thread_id=thread_id or None)
        cont_prompt = (
            "Фоновые задачи, которые ты ранее запустил, завершились. Их вывод "
            "ниже — проанализируй и доложи результат пользователю (или продолжи "
            "работу, если нужно):\n\n" + summary)
        await _run_continuation(bot, db, ssh, machine, cwd, session_id, model,
                                chat_id, thread_id, key, cont_prompt, depth)
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("background watcher crashed")
    finally:
        if _WATCHERS.get(key) is asyncio.current_task():
            _WATCHERS.pop(key, None)


async def _run_prompt(message: Message, db, ssh, prompt: str, qa_run: bool = False,
                      force_execute: bool = False):
    key = _key(message)
    # A fresh user message supersedes any passive session watcher on this topic.
    _cancel_watcher(key)
    if key in _ACTIVE:
        await message.answer(
            "⏳ Предыдущий запрос ещё выполняется. Дождись его или нажми «Остановить».",
        )
        return

    binding, machine = await _need_binding(message, db)
    if not machine:
        return

    # Pre-flight: ensure the working dir exists (create it if missing) — otherwise
    # `cd` in the runner fails and the run dies with a confusing "no response".
    try:
        q = shlex.quote(binding["cwd"])
        res = await ssh.run(
            machine,
            f"if [ -d {q} ]; then echo exists; else mkdir -p {q} && echo created; fi",
            timeout=15,
        )
        if "created" in (res.stdout or ""):
            await message.answer(
                "📁 Рабочей папки не было — создал:\n"
                f"<code>{html.escape(binding['cwd'])}</code>"
            )
    except Exception:
        pass  # connectivity problems are handled by the run itself

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
        for attempt in (1, 2):
            result = None
            error = None
            try:
                result = await run.execute(ssh, on_event)
            except Exception as e:
                log.exception("claude run failed (attempt %d)", attempt)
                error = str(e)
            # Retry ONLY an early failure that produced no output — safe, the
            # agent did nothing to duplicate. Never retry after work was streamed
            # or the user stopped it.
            failed = bool(error) or result is None or bool(result.get("is_error"))
            if failed and not run.stopped and not transcript.blocks and attempt < 2:
                await run.cleanup(ssh)
                await editor.set("⚠️ Сбой на старте, повторяю…", reply_markup=stop_kb())
                await asyncio.sleep(3)
                run = claude.ClaudeRun(
                    machine=machine, cwd=binding["cwd"], prompt=prompt,
                    resume_id=binding.get("session_id"), model=binding.get("model"),
                    permission_mode=permission_mode,
                )
                _ACTIVE[key] = run
                await db.add_active_run(
                    key[0], key[1], status.message_id, run.run_id, machine["id"],
                    message.from_user.id, binding["cwd"], run.session_id,
                    binding.get("model"),
                )
                continue
            break
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

    # Sync forum topic name with Claude's auto-generated summary — unless the
    # user named the topic themselves (keep_name).
    if (new_summary and message.message_thread_id and not error and not run.stopped
            and not binding.get("keep_name")):
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

    # After a normal turn, keep an eye on the topic. If claude launched a long
    # background task, wait for it and auto-continue with its result; otherwise
    # passively mirror any new assistant messages (VS Code on the same session).
    if (not qa_run and not plan_mode and not error and not run.stopped
            and run.session_id and key not in _ACTIVE):
        _cancel_watcher(key)
        try:
            bg = await claude.bg_tasks(ssh, machine, run.run_id)
        except Exception:
            bg = []
        if bg:
            names = ", ".join(t["name"] for t in bg)
            await message.answer(
                f"⏳ Claude запустил фоновую задачу ({html.escape(names)}) — "
                "прослежу и продолжу сессию с результатом, когда она завершится.\n"
                "<i>/stop — отменить ожидание.</i>"
            )
            _WATCHERS[key] = asyncio.create_task(_watch_background(
                message.bot, db, ssh, machine, binding["cwd"], run.session_id,
                binding.get("model"), message.chat.id,
                message.message_thread_id or 0, key, run.run_id, 0,
            ))
        else:
            _WATCHERS[key] = asyncio.create_task(_watch_session(
                message.bot, ssh, machine, binding["cwd"], run.session_id,
                message.chat.id, message.message_thread_id or 0, key,
            ))


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
    r"csv|tsv|xlsx?|docx?|pptx?|md|txt|mp4|mov|mp3|wav|log|ya?ml|json))\b",
    re.IGNORECASE,
)
MAX_SEND_FILE = 20 * 1024 * 1024


async def _resolve_in_subdir(ssh, machine, cwd, relpath):
    """Locate a relative path nested in a subdir of cwd (project under cwd/…).
    Returns the absolute path only if exactly one match exists (unambiguous)."""
    pat = "*/" + relpath.lstrip("./")
    try:
        res = await ssh.run(
            machine,
            f"find {shlex.quote(cwd)} -maxdepth 8 -type f -path {shlex.quote(pat)} "
            "2>/dev/null | head -3",
            timeout=15,
        )
    except Exception:
        return None
    found = [l.strip() for l in (res.stdout or "").splitlines() if l.strip()]
    return found[0] if len(found) == 1 else None


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
    try:
        sent, done = 0, set()
        for p in cands:
            if sent >= 5:
                break
            abspath = p if p.startswith("/") else f"{cwd.rstrip('/')}/{p}"
            abspath = posixpath.normpath(abspath)
            # Same file referenced more than once (abs + rel) → send once.
            if abspath in done:
                continue
            done.add(abspath)
            try:
                st = await sftp.stat(abspath)
            except Exception:
                st = None
            # The project often lives in a subdir of the session cwd (e.g. the app
            # is in <cwd>/tally/, the answer cites assets/images/icon.png). If the
            # direct path misses, search subdirs and use an unambiguous match.
            if st is None and not p.startswith("/"):
                alt = await _resolve_in_subdir(ssh, machine, cwd, p)
                if alt and alt not in done:
                    done.add(alt)
                    abspath = alt
                    try:
                        st = await sftp.stat(abspath)
                    except Exception:
                        st = None
            if st is None:
                continue
            try:
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
    finally:
        try:
            sftp.exit()
        except Exception:
            pass


async def _send_answer(bot, chat_id, thread_id, text):
    """Send an answer in order, rendering each Markdown table to a PNG image in
    place (text before → table image → text after), since Telegram can't display
    tables readably."""
    if not text:
        return
    for i, (kind, seg) in enumerate(tables.segments(text)):
        if kind == "text":
            await send_long(bot, chat_id, seg, thread_id=thread_id)
            continue
        png = None
        try:
            png = tables.render_png(seg)
        except Exception:
            log.exception("table render failed")
        if png:
            try:
                await bot.send_photo(
                    chat_id, BufferedInputFile(png, f"table_{i + 1}.png"),
                    message_thread_id=thread_id or None,
                )
                continue
            except Exception:
                log.exception("send table photo failed")
        # Fallback: original table as a monospace code block.
        await send_long(bot, chat_id, "```\n" + seg + "\n```", thread_id=thread_id)


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
            await _send_answer(bot, chat_id, thread_id, answer)
        return

    # Offer one-tap QA after a normal run; skip it on the QA run itself.
    done_kb = None if qa_run else test_kb()

    if not answer:
        await editor.set("✅ Готово (без текстового ответа).\n\n" + transcript.tail(2000)
                         + meta, reply_markup=done_kb)
        return

    # Replace the live status with a short header, then post the full answer.
    await editor.set("✅ <b>Готово</b>" + meta, reply_markup=done_kb)
    await _send_answer(bot, chat_id, thread_id, answer)
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
