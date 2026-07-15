"""Per-machine status: connectivity, Claude install/auth, host resources, and
consumed Claude usage aggregated from the local session logs.

There is no CLI to read the remaining subscription quota, so "usage" here is
the *consumed* side — token volume per time window parsed from
~/.claude/projects/*.jsonl (the 5h window is the one that matters for Max
limits). Everything runs on the machine the caller owns; the handler resolves
the machine by (machine_id, user_id) so a user only ever sees their own.
"""

import hashlib
import json
import logging
import re

from .ssh import login_shell

log = logging.getLogger(__name__)


def token_fingerprint(crypto, enc: str | None) -> str | None:
    """Short, non-secret fingerprint of a machine's Claude token, so tokens can
    be compared across machines (same fp = same token) without exposing them."""
    if not enc:
        return None
    try:
        val = crypto.decrypt(enc)
    except Exception:
        return None
    m = re.search(r"oat[0-9]+-[A-Za-z0-9_\-]+", val) or re.search(r"sk-ant-[A-Za-z0-9_\-]+", val)
    tok = m.group(0) if m else val.strip()
    return hashlib.sha256(tok.encode()).hexdigest()[:10]


def token_line(crypto, machine: dict, all_machines: list[dict]) -> str:
    """One line describing the machine's token fingerprint and which other
    machines share the same token."""
    fp = token_fingerprint(crypto, machine.get("claude_key_enc"))
    if not fp:
        return "🔑 Токен: не задан"
    shared = [mm["name"] for mm in all_machines
              if mm.get("id") != machine.get("id")
              and token_fingerprint(crypto, mm.get("claude_key_enc")) == fp]
    tail = ("общий с: " + ", ".join(shared)) if shared else "уникальный"
    return f"🔑 Токен: <code>{fp}</code> ({tail})"

_HEALTH = login_shell(
    'echo "CLAUDE:$(command -v claude >/dev/null 2>&1 && claude --version 2>/dev/null | tail -1 || echo none)"; '
    'A=""; [ -f ~/.claude/.credentials.json ] && A="${A}creds "; '
    '[ -f ~/.claude/.tg-anthropic-key ] && A="${A}token"; echo "AUTH:${A:-нет}"; '
    'echo "DISK:$(df -h ~ 2>/dev/null | tail -1 | awk \'{print $4" свободно, "$5" занято"}\')"; '
    'echo "LOAD:$(cut -d\' \' -f1-3 /proc/loadavg 2>/dev/null)"; '
    'echo "RAM:$(free -m 2>/dev/null | awk \'/Mem:/{print $3"/"$2" МБ"}\')"; '
    'echo "UP:$(uptime -p 2>/dev/null || true)"'
)

# Reads stdin as a script; aggregates usage from session logs into JSON.
_USAGE_PY = r"""
import json, glob, os, datetime
now = datetime.datetime.now(datetime.timezone.utc)
agg = {w: {"msgs": 0, "in": 0, "out": 0, "cache": 0}
       for w in ("5h", "today", "total")}

def parse_ts(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
    try:
        fh = open(f, encoding="utf-8", errors="replace")
    except Exception:
        continue
    with fh:
        for line in fh:
            if '"usage"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            u = (o.get("message") or {}).get("usage") or {}
            if not u:
                continue
            inp = u.get("input_tokens", 0) or 0
            out = u.get("output_tokens", 0) or 0
            cache = (u.get("cache_read_input_tokens", 0) or 0) + \
                    (u.get("cache_creation_input_tokens", 0) or 0)
            ts = parse_ts(o.get("timestamp", "") or "")
            windows = ["total"]
            if ts:
                if (now - ts).total_seconds() <= 5 * 3600:
                    windows.append("5h")
                if ts.date() == now.date():
                    windows.append("today")
            for w in windows:
                agg[w]["msgs"] += 1
                agg[w]["in"] += inp
                agg[w]["out"] += out
                agg[w]["cache"] += cache
print(json.dumps(agg))
"""


def _h(n: int) -> str:
    """Humanize a token count."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


async def collect(ssh, machine: dict) -> dict:
    """Gather health + usage for a machine. Returns a dict of parsed fields;
    'ok' is False if the machine is unreachable."""
    out: dict = {"ok": False, "health": {}, "usage": None}
    try:
        res = await ssh.run(machine, _HEALTH, timeout=25)
    except Exception as e:
        out["error"] = str(e) or type(e).__name__
        return out
    out["ok"] = True
    for line in (res.stdout or "").splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out["health"][k.strip()] = v.strip()

    try:
        res = await ssh.run(machine, login_shell("python3 -"), input=_USAGE_PY, timeout=40)
        out["usage"] = json.loads((res.stdout or "").strip().splitlines()[-1])
    except Exception:
        log.exception("usage aggregation failed")
        out["usage"] = None
    return out


def render(machine: dict, data: dict, tok_line: str | None = None) -> str:
    import html
    name = html.escape(machine["name"])
    if not data.get("ok"):
        err = html.escape(str(data.get("error", "недоступна"))[:200])
        return f"📊 <b>{name}</b>\n\n❌ Не отвечает: <code>{err}</code>"

    h = data.get("health", {})
    claude = h.get("CLAUDE", "?")
    claude_line = "не установлен" if claude == "none" else html.escape(claude)
    lines = [
        f"📊 <b>Статус: {name}</b>",
        "🔌 Связь: OK",
        f"🤖 Claude: {claude_line} · авторизация: {html.escape(h.get('AUTH', '?'))}",
    ]
    if tok_line:
        lines.append(tok_line)
    lines += [
        f"💾 Диск: {html.escape(h.get('DISK', '?'))}",
        f"⚙️ Load: {html.escape(h.get('LOAD', '?'))} · RAM: {html.escape(h.get('RAM', '?'))}",
    ]
    if h.get("UP"):
        lines.append(f"⏱ {html.escape(h['UP'])}")

    u = data.get("usage")
    if u:
        lines.append("\n📈 <b>Расход Claude</b> (из логов сессий):")
        labels = [("5h", "за 5ч (окно лимита Max)"), ("today", "сегодня"), ("total", "всего")]
        for key, label in labels:
            b = u.get(key, {})
            lines.append(
                f"  • {label}: {b.get('msgs', 0)} сообщ. · "
                f"in {_h(b.get('in', 0))} / out {_h(b.get('out', 0))} / cache {_h(b.get('cache', 0))}"
            )
        lines.append(
            "\n<i>Остаток квоты подписки CLI не отдаёт — показан фактический расход. "
            "Подписка не тарифицируется по токенам, токены — индикатор нагрузки.</i>"
        )
    else:
        lines.append("\n📈 Расход: логи сессий не прочитались.")
    return "\n".join(lines)
