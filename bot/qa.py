"""Independent manual-QA testing on the same machine as the chat agent.

The dev agent (the session you chat with) orchestrates: it derives a test plan
from what it just built, then launches a *separate* `claude -p` process — an
independent tester — wired to the Playwright MCP server. The tester drives a
headless browser, records PASS/FAIL with screenshots into `qa/`, and the dev
agent summarizes the result. Because the dev session reads the report to
summarize, the testing context stays in that session: a later "исправь" just
continues with full knowledge of what failed.

This module owns the one-time toolset install, the orchestration prompt handed
to the dev agent, and pulling result screenshots back for Telegram.
"""

import asyncio
import logging

import asyncssh

from .ssh import login_shell

log = logging.getLogger(__name__)

QA_DIR = "qa"                                   # relative to the project cwd
SHOTS_DIR = f"{QA_DIR}/shots"
MCP_CONFIG = ".claude/qa-playwright-mcp.json"   # relative to remote home
MARKER = ".claude/.qa-installed"                # relative to remote home
MAX_SHOTS = 10
MAX_SHOT_BYTES = 10 * 1024 * 1024

_MCP_CONFIG_JSON = (
    '{\n'
    '  "mcpServers": {\n'
    '    "playwright": {\n'
    '      "command": "npx",\n'
    '      "args": ["-y", "@playwright/mcp@latest", "--headless", "--browser", "chromium"]\n'
    '    }\n'
    '  }\n'
    '}\n'
)


async def is_installed(ssh, machine: dict) -> bool:
    try:
        res = await ssh.run(machine, f"test -f ~/{MARKER} && echo ok", timeout=15)
        return "ok" in (res.stdout or "")
    except Exception:
        return False


_INSTALL_SCRIPT = r"""
_fail() { echo "ERROR:$*"; exit 1; }

command -v npx >/dev/null 2>&1 || _fail "npx/Node.js не найден — сначала установи Claude Code (он ставит Node)"

# Reuse the same user-local npm prefix the Claude installer set up.
NPM_PREFIX="${HOME}/.npm-global"
mkdir -p "${NPM_PREFIX}/bin"
npm config set prefix "${NPM_PREFIX}" 2>/dev/null || true
export PATH="${NPM_PREFIX}/bin:${PATH}"

echo "INFO:Ставлю Playwright и MCP-сервер..."
NPM_LOG=$(npm install -g playwright @playwright/mcp 2>&1)
[ $? -eq 0 ] || _fail "npm: $(echo "$NPM_LOG" | grep -i 'err' | tail -2 | tr '\n' ' ')"

echo "INFO:Скачиваю headless Chromium..."
PW_LOG=$(npx --yes playwright install chromium 2>&1)
[ $? -eq 0 ] || _fail "playwright install chromium: $(echo "$PW_LOG" | tail -2 | tr '\n' ' ')"

# System libs need root; best-effort, do not fail the install if unavailable.
if sudo -n true 2>/dev/null; then
    echo "INFO:Доустанавливаю системные библиотеки (sudo)..."
    sudo -n npx --yes playwright install-deps chromium >/dev/null 2>&1 || echo "INFO:install-deps не прошёл — обычно libs уже есть"
else
    echo "INFO:Без sudo пропускаю системные libs (обычно уже есть)"
fi

mkdir -p ~/.claude
cat > ~/__qa_mcp.json <<'JSON'
__MCP_JSON__
JSON
mv ~/__qa_mcp.json ~/__MCP_CONFIG__
touch ~/__MARKER__
echo "DONE:Playwright $(npx --yes playwright --version 2>/dev/null | tail -1)"
"""


def _install_script() -> str:
    return (_INSTALL_SCRIPT
            .replace("__MCP_JSON__", _MCP_CONFIG_JSON.rstrip("\n"))
            .replace("__MCP_CONFIG__", MCP_CONFIG)
            .replace("__MARKER__", MARKER))


async def install(ssh, machine: dict, on_progress) -> str:
    """Install the QA toolset on the remote machine, streaming progress lines.

    Returns a short status string, raises RuntimeError on failure.
    """
    conn = await ssh.connect(machine)
    proc = await conn.create_process(login_shell(_install_script()),
                                     encoding="utf-8", errors="replace")
    stderr_lines: list[str] = []

    async def drain():
        try:
            async for line in proc.stderr:
                s = line.strip()
                if s:
                    stderr_lines.append(s)
        except Exception:
            pass

    drain_task = asyncio.create_task(drain())
    result = error = None
    try:
        async for raw in proc.stdout:
            line = raw.strip()
            if line.startswith("INFO:"):
                await on_progress(line[5:])
            elif line.startswith("DONE:"):
                result = line[5:].strip() or "установлено"
            elif line.startswith("ERROR:"):
                error = line[6:].strip()
    finally:
        drain_task.cancel()
        try:
            await drain_task
        except (asyncio.CancelledError, Exception):
            pass

    if error:
        tail = "\n".join(stderr_lines[-4:])
        raise RuntimeError(error + (f"\n{tail}" if tail else ""))
    if result is None:
        raise RuntimeError("установка завершилась без подтверждения")
    return result


async def new_screenshots(ssh, machine: dict, cwd: str,
                          since_mtime: float) -> list[tuple[str, bytes]]:
    """PNG/JPG files under <cwd>/qa/shots newer than since_mtime, as (name, bytes)."""
    sftp = await ssh.sftp(machine)
    shots_dir = f"{cwd.rstrip('/')}/{SHOTS_DIR}"
    try:
        entries = await sftp.readdir(shots_dir)
    except (asyncssh.SFTPError, OSError):
        return []

    picked = []
    for e in entries:
        if e.filename in (".", ".."):
            continue
        if not e.filename.lower().endswith((".png", ".jpg", ".jpeg")):
            continue
        if (e.attrs.mtime or 0) < since_mtime - 1:
            continue
        if (e.attrs.size or 0) > MAX_SHOT_BYTES:
            continue
        picked.append(e)

    picked.sort(key=lambda e: e.attrs.mtime or 0)
    out: list[tuple[str, bytes]] = []
    for e in picked[:MAX_SHOTS]:
        try:
            async with sftp.open(f"{shots_dir}/{e.filename}", "rb") as f:
                out.append((e.filename, await f.read()))
        except Exception:
            continue
    return out


ORCHESTRATION_PROMPT = f"""\
Пользователь просит протестировать то, что мы только что сделали — независимым \
ручным QA-прогоном через браузер. Действуй сам, шаг за шагом:

1. По контексту нашей работы определи, что именно нужно проверить, и сформулируй \
критерии приёмки. Запиши тест-кейсы (шаги + ожидаемый результат) в `{QA_DIR}/test-plan.md`.
2. Определи URL запущенного приложения (ты только что его разрабатывал — проверь, \
запущен ли дев-сервер и на каком порту). Если URL никак не определить или нужен \
тестовый аккаунт, которого у тебя нет — задай ОДИН короткий вопрос пользователю и \
останови работу, не запуская тест.
3. Создай папку `{SHOTS_DIR}`.
4. Запусти НЕЗАВИСИМОГО тестировщика отдельным процессом (дай ему до 10 минут — \
тест идёт через браузер):

   claude -p --mcp-config ~/{MCP_CONFIG} --permission-mode bypassPermissions \\
     --append-system-prompt "Ты независимый ручной QA. Выполняй кейсы строго по плану через инструменты Playwright. НЕ меняй код приложения. Для каждого кейса фиксируй PASS/FAIL, actual vs expected; при падении делай скриншот в {SHOTS_DIR}/<номер-кейса>.png и прикладывай ошибки из консоли и сети." \\
     "Прогони {QA_DIR}/test-plan.md против <URL приложения> и запиши итог в {QA_DIR}/report.md. Скриншоты складывай в {SHOTS_DIR}/."

5. Прочитай `{QA_DIR}/report.md` и дай мне краткий понятный итог: сколько кейсов \
прошло/упало и что именно сломано. Скриншоты падений ты сложил в `{SHOTS_DIR}/` — \
я их покажу в чате автоматически.

Сам код приложения на этом шаге НЕ правь — просто протестируй и доложи. Если \
потом я скажу «исправь», у тебя уже будет полный контекст теста.
"""
