"""Everything that talks to the Claude Code CLI on remote machines."""

import asyncio
import json
import logging
import shlex
import uuid

import asyncssh

from .ssh import login_shell

log = logging.getLogger(__name__)

PROJECTS_DIR = ".claude/projects"
KEY_FILE = ".claude/.tg-anthropic-key"  # relative to remote home
RUNS_DIR = ".claude/tg-runs"            # detached run artifacts, relative to home
HEAD_BYTES = 65536
TAIL_BYTES = 131072

AUTH_ERROR_MARKERS = (
    "invalid api key", "authentication", "unauthorized", "please run",
    "log in", "login", "not authenticated", "oauth", "credit balance",
    "claude auth", "api key", "403",
)


def looks_like_auth_error(text: str) -> bool:
    t = (text or "").lower()
    return any(marker in t for marker in AUTH_ERROR_MARKERS)


async def push_claude_key(ssh, machine: dict, token: str) -> None:
    """Write the subscription OAuth token to a 600 file in the remote home so
    every run picks it up via the env file, without ever putting it in argv.

    Exported as CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`), which is
    billed to the Claude subscription, not the usage-based API.
    """
    await ssh.run(machine, "mkdir -p ~/.claude && chmod 700 ~/.claude", timeout=15)
    sftp = await ssh.sftp(machine)
    content = f"CLAUDE_CODE_OAUTH_TOKEN='{token}'\n"
    async with sftp.open(KEY_FILE, "w") as f:
        await f.write(content)
    await ssh.run(machine, f"chmod 600 ~/{KEY_FILE}", timeout=15)


async def remove_claude_key(ssh, machine: dict) -> None:
    await ssh.run(machine, f"rm -f ~/{KEY_FILE}", timeout=15)


_INSTALL_SCRIPT = r"""
_fail() { echo "ERROR:$*"; exit 1; }

# 1. Already installed?
if command -v claude >/dev/null 2>&1; then
    echo "ALREADY:$(claude --version 2>&1 | tail -1)"
    exit 0
fi

# 2. Check Node.js >= 18
HAS_NODE=0
if command -v node >/dev/null 2>&1; then
    MAJ=$(node -e 'console.log(process.version.split(".")[0].slice(1))' 2>/dev/null || echo 0)
    [ "${MAJ:-0}" -ge 18 ] 2>/dev/null && HAS_NODE=1
fi

if [ "$HAS_NODE" = "0" ]; then
    echo "INFO:Node.js отсутствует или устарел, устанавливаю..."
    command -v curl >/dev/null 2>&1 || _fail "curl не найден — установи Node.js >= 18 вручную"

    # Strategy 1: nvm (may already be installed)
    if [ -s "${HOME}/.nvm/nvm.sh" ]; then
        echo "INFO:nvm найден, использую его..."
        . "${HOME}/.nvm/nvm.sh"
        nvm install 22 2>&1 | tail -3 || true
        nvm use 22 2>/dev/null || true
    fi

    # Strategy 2: fnm
    if ! command -v node >/dev/null 2>&1; then
        FNM_DIR="${HOME}/.fnm"
        if [ ! -x "${FNM_DIR}/fnm" ]; then
            echo "INFO:Скачиваю fnm..."
            # Try official installer first
            curl -fsSL https://fnm.vercel.app/install | bash -s -- --skip-shell 2>&1 | tail -3 || true
            # If that didn't work, try direct GitHub zip
            if [ ! -x "${FNM_DIR}/fnm" ]; then
                echo "INFO:Пробую прямую загрузку fnm с GitHub..."
                TMPF=$(mktemp /tmp/fnm_XXXXXX.zip)
                curl -fsSL "https://github.com/Schniz/fnm/releases/latest/download/fnm-linux.zip" \
                    -o "$TMPF" 2>&1 || _fail "не удалось скачать fnm (нет доступа к github.com?)"
                mkdir -p "${FNM_DIR}"
                command -v unzip >/dev/null 2>&1 || _fail "unzip не найден — установи Node.js >= 18 вручную"
                unzip -o "$TMPF" fnm -d "${FNM_DIR}" 2>&1 | tail -2
                chmod +x "${FNM_DIR}/fnm"
                rm -f "$TMPF"
            fi
        fi
        if [ -x "${FNM_DIR}/fnm" ]; then
            export PATH="${FNM_DIR}:${PATH}"
            echo "INFO:Скачиваю Node.js 22..."
            FNM_OUT=$("${FNM_DIR}/fnm" install 22 2>&1)
            FNM_RC=$?
            [ $FNM_RC -eq 0 ] || _fail "fnm install 22 (exit ${FNM_RC}): $(echo "$FNM_OUT" | tail -2 | tr '\n' ' ')"
            "${FNM_DIR}/fnm" use 22 2>/dev/null || true
            eval "$("${FNM_DIR}/fnm" env --shell bash 2>/dev/null)" 2>/dev/null || true
            # direct path fallback
            NODE_BIN=$(find "${FNM_DIR}" -name "node" -type f 2>/dev/null | grep -v npm | head -1)
            [ -n "$NODE_BIN" ] && export PATH="$(dirname "${NODE_BIN}"):${PATH}"
            # persist to .profile (guarded)
            grep -qF 'FNM_DIR' "${HOME}/.profile" 2>/dev/null \
                || printf '\n[ -x "$HOME/.fnm/fnm" ] && export FNM_DIR="$HOME/.fnm" && export PATH="$FNM_DIR:$PATH" && eval "$($FNM_DIR/fnm env)" 2>/dev/null || true\n' \
                    >> "${HOME}/.profile"
        fi
    fi

    command -v node >/dev/null 2>&1 \
        || _fail "node не найден. Установи Node.js >= 18 вручную: https://nodejs.org/en/download"
    echo "INFO:Node.js $(node --version) установлен"
else
    echo "INFO:Node.js $(node --version) найден"
fi

# 3. Configure user-local npm prefix (no root needed)
echo "INFO:Настраиваю npm prefix..."
NPM_PREFIX="${HOME}/.npm-global"
mkdir -p "${NPM_PREFIX}/bin"
npm config set prefix "${NPM_PREFIX}" 2>&1 || true
grep -qF '.npm-global' "${HOME}/.profile" 2>/dev/null \
    || printf '\nexport PATH="$HOME/.npm-global/bin:$PATH"\n' >> "${HOME}/.profile"
export PATH="${NPM_PREFIX}/bin:${PATH}"

# 4. Install
echo "INFO:Устанавливаю @anthropic-ai/claude-code..."
NPM_LOG=$(npm install -g @anthropic-ai/claude-code 2>&1)
NPM_RC=$?
if [ $NPM_RC -ne 0 ]; then
    _fail "npm error: $(echo "$NPM_LOG" | grep -i 'error\|err!' | tail -3 | tr '\n' ' ')"
fi
ADDED=$(echo "$NPM_LOG" | grep -E 'added [0-9]' | head -1)
[ -n "$ADDED" ] && echo "INFO:$ADDED"

# 5. Verify
command -v claude >/dev/null 2>&1 \
    && { echo "DONE:$(claude --version 2>&1 | tail -1)"; exit 0; }

_fail "claude не найден после установки (npm prefix: ${NPM_PREFIX})"
"""


async def install_claude(ssh, machine: dict, on_progress) -> str:
    """Install Claude Code CLI on the remote machine.

    Calls on_progress(text) for each status line.
    Returns the version string on success, raises RuntimeError on failure.
    """
    conn = await ssh.connect(machine)
    cmd = login_shell(_INSTALL_SCRIPT)
    proc = await conn.create_process(cmd, encoding="utf-8", errors="replace")

    stderr_lines: list[str] = []

    async def drain_stderr():
        try:
            async for line in proc.stderr:
                s = line.strip()
                if s:
                    stderr_lines.append(s)
        except Exception:
            pass

    stderr_task = asyncio.create_task(drain_stderr())
    result: str | None = None
    error: str | None = None
    try:
        async for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("INFO:"):
                await on_progress(line[5:])
            elif line.startswith("ALREADY:"):
                result = line[8:].strip() or "уже установлен"
                await on_progress(f"уже установлен: {result}")
            elif line.startswith("DONE:"):
                result = line[5:].strip() or "установлен"
            elif line.startswith("ERROR:"):
                error = line[6:].strip()
    finally:
        stderr_task.cancel()
        try:
            await stderr_task
        except (asyncio.CancelledError, Exception):
            pass

    if error:
        detail = "\n".join(stderr_lines[-5:])
        raise RuntimeError(f"{error}" + (f"\n{detail}" if detail else ""))
    if result is None:
        stderr_tail = "\n".join(stderr_lines[-8:])
        raise RuntimeError("установка завершилась без подтверждения"
                           + (f"\nstderr:\n{stderr_tail}" if stderr_tail else ""))
    return result


def _parse_lines(data: bytes, skip_first_partial: bool = False) -> list[dict]:
    objs = []
    lines = data.split(b"\n")
    if skip_first_partial and lines:
        lines = lines[1:]
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            objs.append(json.loads(line))
        except ValueError:
            continue
    return objs


def _text_of_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text") or ""
    return ""


def _is_service_text(text: str) -> bool:
    t = text.lstrip()
    return not t or t.startswith("<") or t.startswith("Caveat:")


def _extract_meta(objs: list[dict]) -> dict:
    """Pull cwd and a human-readable title out of session jsonl lines."""
    cwd = None
    summary = None
    first_user = None
    for obj in objs:
        if cwd is None and obj.get("cwd"):
            cwd = obj["cwd"]
        t = obj.get("type")
        if t == "summary" and obj.get("summary") and summary is None:
            summary = obj["summary"]
        elif t == "user" and first_user is None:
            text = _text_of_content((obj.get("message") or {}).get("content")).strip()
            if not _is_service_text(text):
                first_user = text
    return {"cwd": cwd, "title": summary or first_user or "(без названия)"}


async def _read_head(sftp: asyncssh.SFTPClient, path: str) -> bytes:
    try:
        async with sftp.open(path, "rb") as f:
            return await f.read(HEAD_BYTES)
    except Exception:
        return b""


async def list_projects(ssh, machine: dict, limit: int = 15) -> list[dict]:
    """Project directories under ~/.claude/projects, newest first."""
    sftp = await ssh.sftp(machine)
    try:
        entries = await sftp.readdir(PROJECTS_DIR)
    except (asyncssh.SFTPError, OSError):
        return []

    dirs = [e for e in entries if e.filename not in (".", "..")]
    dirs.sort(key=lambda e: e.attrs.mtime or 0, reverse=True)

    projects = []
    for entry in dirs:
        if len(projects) >= limit:
            break
        dir_path = f"{PROJECTS_DIR}/{entry.filename}"
        try:
            files = await sftp.readdir(dir_path)
        except (asyncssh.SFTPError, OSError):
            continue
        jsonls = [f for f in files if f.filename.endswith(".jsonl")]
        if not jsonls:
            continue
        newest = max(jsonls, key=lambda f: f.attrs.mtime or 0)
        head = await _read_head(sftp, f"{dir_path}/{newest.filename}")
        meta = _extract_meta(_parse_lines(head))
        projects.append({
            "dir": entry.filename,
            "cwd": meta["cwd"] or entry.filename,
            "count": len(jsonls),
            "mtime": newest.attrs.mtime or 0,
        })
    return projects


async def list_sessions(ssh, machine: dict, project_dir: str, limit: int = 10) -> list[dict]:
    """Sessions inside one project directory, newest first."""
    sftp = await ssh.sftp(machine)
    dir_path = f"{PROJECTS_DIR}/{project_dir}"
    try:
        files = await sftp.readdir(dir_path)
    except (asyncssh.SFTPError, OSError):
        return []

    jsonls = [f for f in files if f.filename.endswith(".jsonl")]
    jsonls.sort(key=lambda f: f.attrs.mtime or 0, reverse=True)

    sessions = []
    for entry in jsonls[:limit]:
        head = await _read_head(sftp, f"{dir_path}/{entry.filename}")
        meta = _extract_meta(_parse_lines(head))
        sessions.append({
            "id": entry.filename[:-len(".jsonl")],
            "cwd": meta["cwd"],
            "title": meta["title"],
            "mtime": entry.attrs.mtime or 0,
        })
    return sessions


async def session_tail(ssh, machine: dict, project_dir: str, session_id: str,
                       max_messages: int = 6) -> list[tuple[str, str]]:
    """Last user/assistant text messages of a session, for the chat recap.

    Returns a list of ("user" | "assistant", text) tuples, oldest first.
    """
    sftp = await ssh.sftp(machine)
    path = f"{PROJECTS_DIR}/{project_dir}/{session_id}.jsonl"
    try:
        attrs = await sftp.stat(path)
        size = attrs.size or 0
        offset = max(0, size - TAIL_BYTES)
        async with sftp.open(path, "rb") as f:
            if offset:
                await f.seek(offset)
            data = await f.read(TAIL_BYTES + 1024)
    except Exception:
        return []

    messages: list[tuple[str, str]] = []
    for obj in _parse_lines(data, skip_first_partial=offset > 0):
        t = obj.get("type")
        if t not in ("user", "assistant"):
            continue
        if obj.get("isMeta"):
            continue
        text = _text_of_content((obj.get("message") or {}).get("content")).strip()
        if t == "user" and _is_service_text(text):
            continue
        if not text:
            continue
        messages.append((t, text))
    return messages[-max_messages:]


class ClaudeRun:
    """One non-interactive claude invocation, launched *detached* on the remote.

    The process is started under setsid with stdout/stderr redirected to files in
    ~/.claude/tg-runs/, so it survives the SSH channel closing (i.e. a bot
    restart). The bot follows the output file by polling, which means an
    interrupted run can be re-attached after a restart from the same run_id.
    """

    def __init__(self, machine: dict, cwd: str, prompt: str = "",
                 resume_id: str | None = None, new_session_id: str | None = None,
                 model: str | None = None, run_id: str | None = None,
                 permission_mode: str = "bypassPermissions"):
        self.machine = machine
        self.cwd = cwd
        self.prompt = prompt
        self.resume_id = resume_id
        self.new_session_id = new_session_id
        self.model = model
        self.run_id = run_id or uuid.uuid4().hex
        self.permission_mode = permission_mode
        self.session_id = resume_id or new_session_id
        self.stopped = False
        self.stderr = ""
        self.result: dict | None = None

    def _runner_script(self) -> str:
        parts = [
            "claude", "-p",
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", self.permission_mode,
        ]
        if self.model:
            parts += ["--model", self.model]
        if self.resume_id:
            parts += ["--resume", self.resume_id]
        elif self.new_session_id:
            parts += ["--session-id", self.new_session_id]
        claude_cmd = " ".join(shlex.quote(p) for p in parts)
        rd = "$HOME/" + RUNS_DIR
        rid = self.run_id
        return (
            "#!/bin/bash\n"
            f'if [ -f "$HOME/{KEY_FILE}" ]; then set -a; . "$HOME/{KEY_FILE}"; set +a; fi\n'
            f"cd {shlex.quote(self.cwd)} || exit 1\n"
            f'{claude_cmd} < "{rd}/{rid}.prompt" > "{rd}/{rid}.out" 2> "{rd}/{rid}.err"\n'
            f'echo $? > "{rd}/{rid}.rc"\n'
        )

    async def launch(self, ssh) -> None:
        """Write prompt + runner to the remote and start it detached."""
        await ssh.run(self.machine, f"mkdir -p ~/{RUNS_DIR}", timeout=15)
        # Write prompt + runner, retrying once if the connection blips mid-write
        # (a single SFTP drop must not kill the whole run). Idempotent: both files
        # are overwritten, and the detached process is only spawned after.
        for attempt in (1, 2):
            try:
                sftp = await ssh.sftp(self.machine)
                async with sftp.open(f"{RUNS_DIR}/{self.run_id}.prompt", "w") as f:
                    await f.write(self.prompt)
                async with sftp.open(f"{RUNS_DIR}/{self.run_id}.sh", "w") as f:
                    await f.write(self._runner_script())
                break
            except (OSError, asyncssh.Error):
                ssh.drop(self.machine["id"])
                if attempt == 2:
                    raise
        launch = (
            f"setsid bash ~/{RUNS_DIR}/{self.run_id}.sh </dev/null >/dev/null 2>&1 & "
            f"echo $! > ~/{RUNS_DIR}/{self.run_id}.pid"
        )
        await ssh.run(self.machine, login_shell(launch), timeout=20)

    @staticmethod
    async def _exists(sftp, path: str) -> bool:
        try:
            await sftp.stat(path)
            return True
        except Exception:
            return False

    async def _alive(self, ssh) -> bool:
        try:
            r = await ssh.run(
                self.machine,
                f"kill -0 $(cat ~/{RUNS_DIR}/{self.run_id}.pid 2>/dev/null) 2>/dev/null "
                "&& echo y || echo n",
                timeout=10,
            )
            return "y" in (r.stdout or "")
        except Exception:
            return True  # uncertain: don't abort the follow prematurely

    async def follow(self, ssh, on_event) -> dict | None:
        """Tail the run's output file, feeding stream-json events to on_event
        until the process writes its return-code file (or dies). Idempotent:
        re-attaching reads from the start and re-emits events."""
        out_path = f"{RUNS_DIR}/{self.run_id}.out"
        rc_path = f"{RUNS_DIR}/{self.run_id}.rc"
        err_path = f"{RUNS_DIR}/{self.run_id}.err"
        offset = 0
        buf = ""
        result = None
        dead_checks = 0
        polls = 0
        max_polls = 1800  # ~36 min hard cap so a wedged run can't hang forever
        # Reuse ONE SFTP channel for the whole follow loop. Opening a fresh one
        # every poll leaks channels and exhausts the server's MaxSessions
        # (→ reconnect churn and intermittent "Connection closed").
        sftp = None
        while True:
            polls += 1
            if polls > max_polls:
                log.warning("follow: hard cap reached for run %s", self.run_id)
                break
            if sftp is None:
                try:
                    sftp = await ssh.sftp(self.machine)
                except Exception:
                    await asyncio.sleep(1.2)
                    continue
            chunk = b""
            try:
                async with sftp.open(out_path, "rb") as f:
                    await f.seek(offset)
                    chunk = await f.read()
            except asyncssh.SFTPError:
                chunk = b""           # file not there yet — client still fine
            except Exception:
                sftp = None           # connection/channel died — reopen next poll
            if chunk:
                offset += len(chunk)
                buf += chunk.decode("utf-8", errors="replace")
                lines = buf.split("\n")
                buf = lines.pop()
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("type") == "system" and event.get("subtype") == "init":
                        self.session_id = event.get("session_id") or self.session_id
                    if event.get("type") == "result":
                        result = event
                    try:
                        await on_event(event)
                    except Exception:
                        log.exception("on_event failed")

            rc_done = await self._exists(sftp, rc_path) if sftp is not None else False
            if rc_done and not chunk:
                break
            if not rc_done:
                if self.stopped:
                    break
                if not await self._alive(ssh):
                    dead_checks += 1
                    if dead_checks >= 2:
                        break
                else:
                    dead_checks = 0
            await asyncio.sleep(1.2)

        try:
            if sftp is None:
                sftp = await ssh.sftp(self.machine)
            async with sftp.open(err_path, "rb") as f:
                self.stderr = (await f.read(8192)).decode("utf-8", errors="replace")
        except Exception:
            self.stderr = ""
        finally:
            if sftp is not None:
                try:
                    sftp.exit()
                except Exception:
                    pass
        self.result = result
        return result

    async def execute(self, ssh, on_event) -> dict | None:
        await self.launch(ssh)
        return await self.follow(ssh, on_event)

    async def cleanup(self, ssh) -> None:
        try:
            await ssh.run(self.machine, f"rm -f ~/{RUNS_DIR}/{self.run_id}.*", timeout=10)
        except Exception:
            pass

    async def stop(self, ssh) -> None:
        self.stopped = True
        try:
            await ssh.run(
                self.machine,
                f"kill -TERM -- -$(cat ~/{RUNS_DIR}/{self.run_id}.pid 2>/dev/null) 2>/dev/null; "
                f"pkill -f {shlex.quote(self.run_id)} 2>/dev/null || true",
                timeout=10,
            )
        except Exception:
            pass
