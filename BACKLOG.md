# Backlog / Roadmap

Deferred ideas — **not scheduled**. The bot is already used by real people, so
stability beats features. The two items below would require reworking the core
run loop (high regression risk), so they're parked until there's a concrete need.

## 1. Interactive option questions in Telegram
When Claude asks a question with predefined options (the AskUserQuestion-style
prompts you see in the CLI / VS Code, and permission prompts), present those
options as inline buttons in the topic and feed the chosen answer back into the
running session.

## 2. Mid-task message injection (steering)
While a task is running, a new message in the topic should be delivered to Claude
on a *following step of the same task* — instead of being rejected with
"previous request still running". Lets you add context / correct course mid-run.

## Shared foundation & why deferred
Both need a **live two-way session**, not the current one-shot `claude -p`. The
enabler is `claude --input-format stream-json --output-format stream-json`
(realtime streaming input — confirmed available in the CLI).

**Tension with current design:** streaming input needs an open stdin attached to
the bot, which conflicts with today's *detached, file-based* runs that survive bot
restarts (`bot/claude.py` `ClaudeRun` + `bot/recovery.py`).

**Proposed approach (when picked up):**
- Run claude reading stdin from a **FIFO** on the remote: `claude … < fifo > out`.
  Preserves the detached / restart-survival property **and** lets the bot write
  extra user messages / answers into the FIFO.
- Feature 2: bot appends the new user message to the FIFO → picked up next step.
- Feature 1: on a question/permission event the step pauses; the bot renders the
  options as buttons and writes the choice back (use the permission-prompt-tool
  mechanism for permissions).
- Make it an **opt-in mode** (like `/confirm`); keep the current simple one-shot
  as the default/fallback.
- Build both together (same foundation); start with #2 (simpler, lower risk).

**Decision (2026-06-15):** parked. The bot is in production with real users and
this rework touches the just-hardened run/recovery core — the regression risk
isn't worth it without a concrete need.

---

## 3. Localization (EN + KO)
All user-facing text is currently hardcoded Russian. Add English and Korean while
keeping Russian as the default (the production bot must stay RU, byte-identical).

Scope: ~250-300 user-facing strings across all `handlers_*`, `keyboards.py`,
`machine_status.py`, `access.py`. Agent-facing prompts (the QA orchestration in
`bot/qa.py`, internal instructions in `bot/claude.py`) stay RU — Claude reads
them, not the user.

**Proposed approach (prod-safe by design):**
- `bot/i18n.py`: `T[key] = {"ru":…, "en":…, "ko":…}` + `t(key, lang)` that falls
  back to RU. RU values = exact current strings.
- Default lang `ru`; per-user `user_prefs.lang` (migration), a `/lang ru|en|ko`
  command (+ menu button); resolve lang via middleware from `from_user` (in group
  topics: the binding owner's lang).
- Replace inline literals with `t('key', lang)`. EN/KO are additive; any missing
  key falls back to RU — so RU stays identical at every step.
- Do it on a branch, in batches (menu → sessions → machines → chat/statuses → QA),
  verifying RU is unchanged after each; merge only when complete.

**Open questions:** Korean needs a native-speaker review for production quality
(my translation is a first pass).

**Decision (2026-06-17):** parked at user's request. Big multi-file refactor;
no urgency yet (only RU users active). Safe to pick up anytime — the RU-fallback
design means it can't regress the production bot.

