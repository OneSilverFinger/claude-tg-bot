"""Rendering of Claude output into Telegram messages."""

import asyncio
import html
import json
import logging
import re
import time

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

log = logging.getLogger(__name__)

MAX_MSG = 3900


def trunc(s: str, n: int) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def trunc_text(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_line(block: dict) -> str:
    name = block.get("name", "?")
    inp = block.get("input") or {}
    if name == "Bash":
        detail = inp.get("command", "")
    elif name in ("Read", "Edit", "Write", "NotebookEdit"):
        detail = inp.get("file_path", "")
    elif name in ("Grep", "Glob"):
        detail = inp.get("pattern", "")
    elif name in ("WebSearch", "WebFetch"):
        detail = inp.get("query") or inp.get("url") or ""
    elif name in ("Task", "Agent"):
        detail = inp.get("description", "")
    elif name == "TodoWrite":
        detail = f"{len(inp.get('todos') or [])} задач"
    else:
        try:
            detail = json.dumps(inp, ensure_ascii=False)
        except Exception:
            detail = ""
    line = f"🔧 <b>{html.escape(name)}</b>"
    if detail:
        line += f" <code>{html.escape(trunc(detail, 120))}</code>"
    return line


def _tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                return item.get("text") or ""
    return ""


class Transcript:
    """Accumulates stream-json events into ready-to-show HTML blocks."""

    def __init__(self):
        self.blocks: list[str] = []

    def feed(self, event: dict) -> None:
        t = event.get("type")
        if t == "assistant":
            for block in ((event.get("message") or {}).get("content") or []):
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        self.blocks.append(html.escape(trunc_text(text, 700)))
                elif bt == "tool_use":
                    self.blocks.append(tool_line(block))
        elif t == "user":
            content = (event.get("message") or {}).get("content")
            if not isinstance(content, list):
                return
            for block in content:
                if (isinstance(block, dict) and block.get("type") == "tool_result"
                        and block.get("is_error")):
                    text = trunc(_tool_result_text(block), 200)
                    if text:
                        self.blocks.append(f"⚠️ <i>{html.escape(text)}</i>")

    def tail(self, limit: int = 3000) -> str:
        picked: list[str] = []
        total = 0
        for block in reversed(self.blocks):
            if total + len(block) + 2 > limit:
                break
            picked.append(block)
            total += len(block) + 2
        return "\n\n".join(reversed(picked)) or "…"


class LiveEditor:
    """Throttled edits of a single status message."""

    def __init__(self, bot, chat_id: int, message_id: int, interval: float = 2.0):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.interval = interval
        self._last_text: str | None = None
        self._not_before = 0.0

    async def maybe_update(self, text: str, reply_markup=None) -> None:
        if time.monotonic() < self._not_before:
            return
        await self._edit(text, reply_markup)

    async def set(self, text: str, reply_markup=None) -> None:
        await self._edit(text, reply_markup, force=True)

    async def _edit(self, text: str, reply_markup, force: bool = False) -> None:
        if text == self._last_text and not force:
            return
        try:
            await self.bot.edit_message_text(
                text, chat_id=self.chat_id, message_id=self.message_id,
                reply_markup=reply_markup,
            )
            self._last_text = text
            self._not_before = time.monotonic() + self.interval
        except TelegramRetryAfter as e:
            self._not_before = time.monotonic() + e.retry_after + 1
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                self._last_text = text
                return
            # Broken HTML is not expected (blocks are escaped), but never
            # let a render problem kill the run.
            log.warning("edit failed: %s", e)
            self._not_before = time.monotonic() + self.interval


# ---- final answer: markdown -> telegram html ----

MD_CODEBLOCK = re.compile(r"```[\w+\-./]*\n?(.*?)(?:```|\Z)", re.DOTALL)
MD_FENCE_SPLIT = re.compile(r"(```[\w+\-./]*\n?.*?(?:```|\Z))", re.DOTALL)


def _inline_md(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", text)
    return text


def md_to_html(md: str) -> str:
    out = []
    pos = 0
    for m in MD_CODEBLOCK.finditer(md):
        out.append(_inline_md(md[pos:m.start()]))
        out.append("<pre>" + html.escape(m.group(1).strip("\n")) + "</pre>")
        pos = m.end()
    out.append(_inline_md(md[pos:]))
    return "".join(out)


def chunk_markdown(md: str, limit: int = MAX_MSG) -> list[str]:
    """Split markdown into message-sized chunks, keeping code fences atomic
    when possible."""
    segments = [s for s in MD_FENCE_SPLIT.split(md) if s]
    chunks: list[str] = []
    current = ""
    for seg in segments:
        while len(seg) > limit:
            if current.strip():
                chunks.append(current)
            current = ""
            chunks.append(seg[:limit])
            seg = seg[limit:]
        if len(current) + len(seg) > limit:
            if current.strip():
                chunks.append(current)
            current = seg
        else:
            current += seg
    if current.strip():
        chunks.append(current)
    return chunks or [md]


async def send_long(bot, chat_id: int, md_text: str,
                    thread_id: int | None = None, suffix_html: str = "") -> None:
    chunks = chunk_markdown(md_text)
    for i, chunk in enumerate(chunks):
        text = md_to_html(chunk)
        if suffix_html and i == len(chunks) - 1:
            text += suffix_html
        for attempt in (1, 2, 3):
            try:
                await bot.send_message(
                    chat_id, text, message_thread_id=thread_id or None
                )
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramBadRequest:
                await bot.send_message(
                    chat_id, chunk[:4096], parse_mode=None,
                    message_thread_id=thread_id or None,
                )
                break
