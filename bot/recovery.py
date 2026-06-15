"""On startup, re-attach to runs that were in flight when the bot last stopped.

Each in-flight run was recorded in the active_runs table and its output streamed
to a file on the remote machine. After a restart we re-attach by run_id: follow
the output file to completion (the detached process keeps running across the
restart), deliver the result to the original status message, and persist the
session id so the dialog stays resumable. If the process did die without a
result, the user is told — the session context on disk is preserved either way.
"""

import asyncio
import logging

from . import claude
from .handlers_chat import _finalize
from .render import LiveEditor, Transcript

log = logging.getLogger(__name__)


async def recover(bot, db, ssh) -> None:
    rows = await db.list_active_runs()
    if not rows:
        return
    log.info("restart recovery: %d orphaned run(s)", len(rows))
    for r in rows:
        asyncio.create_task(_recover_one(bot, db, ssh, r))


async def _recover_one(bot, db, ssh, r: dict) -> None:
    chat_id, thread_id = r["chat_id"], r["thread_id"]
    try:
        machine = await db.machine(r["machine_id"], r["user_id"])
        if not machine:
            return
        run = claude.ClaudeRun(
            machine=machine, cwd=r["cwd"], resume_id=r.get("session_id"),
            model=r.get("model"), run_id=r["run_id"],
        )
        editor = LiveEditor(bot, chat_id, r["message_id"])
        transcript = Transcript()
        await editor.set("♻️ <b>Бот перезапускался</b> — восстанавливаю выполнявшийся запрос…")

        async def on_event(event: dict):
            transcript.feed(event)

        result = None
        error = None
        try:
            result = await run.follow(ssh, on_event)
        except Exception as e:
            log.exception("recovery follow failed")
            error = str(e)

        # Keep the session resumable regardless of outcome.
        if run.session_id:
            try:
                await db.upsert_binding(chat_id, thread_id, r["user_id"],
                                        session_id=run.session_id)
            except Exception:
                log.exception("recovery: persist session failed")

        if result is None and error is None:
            error = ("запрос был прерван перезапуском бота и не завершился. "
                     "Контекст сессии сохранён — можешь повторить или продолжить.")

        await _finalize(bot, chat_id, thread_id, editor, run, transcript, result, error)
        await run.cleanup(ssh)
    except Exception:
        log.exception("recovery failed for chat=%s thread=%s", chat_id, thread_id)
    finally:
        await db.delete_active_run(chat_id, thread_id)
