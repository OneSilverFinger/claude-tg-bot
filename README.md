# Claude Code in Telegram

*Read this in [Русский](README.ru.md).*

A bridge between Telegram and Claude Code running on your own servers. Sessions
are shared with VS Code: what you start in the editor over Remote SSH you can
continue in Telegram, and vice versa. The bot keeps no separate copy of the
context — it works on top of the same `~/.claude/projects/*.jsonl` files on the
server.

**How it works:** setup happens in a private chat with the bot, while the
actual conversation with Claude happens in a dedicated work group, where each
session opens as its own forum topic with its own history. The private chat
never talks to Claude — it is only the control panel.

## Features

- Multiple users, each with their own list of machines (SSH access).
- Add a machine right in the chat; SSH keys are encrypted (Fernet) and verified
  on add.
- **Install Claude Code on a server straight from the bot** — checks for
  Node.js (installs it via nvm/fnm if missing), then installs
  `@anthropic-ai/claude-code` into a user-local npm prefix (no root needed),
  with live progress.
- Connect a work group in one tap (deep-link `?startgroup`) with auto-binding:
  the bot links the group as soon as it is made admin in a supergroup with
  topics enabled. `/bindgroup` remains as a manual fallback.
- Each session = its own forum topic. The bot creates topics itself.
- Resume existing sessions (`claude --resume`) with a recap of the last
  messages posted into the topic on open. Create new sessions.
- Live streaming of Claude's work (`--output-format stream-json`): tool calls
  and text appear as they are generated, with an elapsed-time heartbeat during
  long silent steps.
- Pick a model (opus / sonnet / haiku) per topic with `/model`.
- Send files and photos: uploaded to the server, the path is passed to Claude.
- "Stop" button and `/stop` command.
- Restore Claude's authorization on the server through the bot (see below).
- Telegram-ID whitelist.

## Why a group with topics

Telegram does not let a bot rewrite the history of a private chat to match a
chosen session (old messages cannot be swapped retroactively). So "separate
history per session" is done with forum topics: open a session and the bot
creates a topic, pulls in the recap, and binds it to that session permanently.
Switching sessions is just moving between topics.

> The bot **cannot create a group itself** — the Bot API does not allow it, only
> user accounts create groups. Everything else is automated: a button to add the
> bot to a group plus auto-binding when it is granted rights. You create the
> group by hand once; after that topics appear on their own.

## Server requirements

Each machine needs the Claude Code CLI. You can install it from the bot (see
Features), or check an existing install:

```bash
claude --version   # should respond
```

The bot invokes `claude` through `bash -lc`, so the PATH from
`~/.bashrc`/`~/.profile`/nvm is picked up. Permission mode is
`bypassPermissions` (like Bypass permissions in VS Code).

### Claude authorization and restoring it through the bot

Claude on the server must be logged in via subscription (`claude auth`). If the
server "loses" the login (logged out, expired token, different account), there
is no need to SSH in by hand:

1. On a machine with a browser run `claude setup-token` — it issues a
   long-lived (~1 year) subscription token.
2. In the private chat with the bot: `/machines` → the 🔑/🔓 icon next to the
   machine → "Log in via subscription" → send the token.
3. The bot encrypts the token, writes it to the server as
   `~/.claude/.tg-anthropic-key` with `600` permissions (over SFTP, not the
   command line — the token never shows up in `ps`), and exports it as
   `CLAUDE_CODE_OAUTH_TOKEN`. Every Claude run picks it up without touching the
   regular `claude auth`.
- "Remove authorization" deletes the file from the server and the record from
  the database.

This is **subscription** auth (billed to Pro/Max), not per-token API billing. If
a run fails with an auth error, the bot prompts you to restore the login. The
message containing the token is deleted from the chat immediately after
processing.

## Running

You need Docker with the compose plugin. The bot uses long polling: no inbound
ports are required, only outbound access to `api.telegram.org`.

```bash
git clone https://github.com/OneSilverFinger/claude-tg-bot.git
cd claude-tg-bot
cp .env.example .env
# fill in BOT_TOKEN, ALLOWED_USER_IDS, MASTER_KEY (generator inside the file)
docker compose up -d --build
docker compose logs -f
```

### Where to get the .env values

- `BOT_TOKEN` — from @BotFather (`/newbot`). After creating it, enable group
  access: @BotFather → bot → Group Privacy → Turn off (otherwise it won't see
  messages in topics).
- `ALLOWED_USER_IDS` — find your Telegram ID via @userinfobot. Comma-separated
  if there are several users.
- `MASTER_KEY` — generate once and never change it:
  ```bash
  python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
  ```

Data (SQLite with machines and bindings) lives in the `bot-data` docker volume
and survives rebuilds. To update: `git pull && docker compose up -d --build`.

## First use

1. Send the bot `/start` in a private chat.
2. "Machines" → "Add machine": name, host, port, login, key/password. Send the
   key as a file or text — the message is deleted right after processing, the
   key is encrypted. The bot immediately tests the connection and checks for
   `claude` (and offers to install it if missing).
   - If Claude is not authorized on the server, set the subscription token (🔑
     icon → "Log in via subscription", see the auth section above).
3. "Connect work group" → add the bot to a group (or create a new one in the
   same flow), enable "Topics" in it and make the bot an admin. The bot binds
   the group automatically and says so.
4. "Projects and sessions" → pick a project → pick a session (or "New session").
   The bot opens it as its own topic in the group.
5. Go to that topic and talk to Claude. Drop files and photos straight into the
   topic.

### Commands inside a topic

- `/sessions` — switch the project/session for this topic.
- `/model` — choose the model for this topic.
- `/status` — what is currently bound to the topic.
- `/stop` — stop the current Claude run.
- `/unbind` — detach the topic from its session (the session on the server is
  left untouched).

## Security

- Access only from the whitelist (`ALLOWED_USER_IDS`). Messages from others are
  ignored; the person is shown their ID so they can request access.
- SSH secrets are encrypted with a Fernet key derived from `MASTER_KEY`; only
  ciphertext is stored in the database. Losing `MASTER_KEY` means re-adding
  machines.
- Messages containing keys/passwords are deleted from the chat right after
  processing.
- `bypassPermissions` means Claude runs commands on the server without
  confirmations. Only give bot access to trusted people and use dedicated SSH
  accounts with appropriate privilege levels.

## Architecture

```
bot/
  config.py            load and validate .env
  crypto.py            encrypt SSH secrets (Fernet)
  db.py                SQLite: machines, chat→session bindings, prefs
  access.py            whitelist middleware
  ssh.py               SSH connection pool (asyncssh), machine test
  claude.py            run claude, parse stream-json, session lists, recap, install
  render.py            stream-json → messages, live-edit, markdown→HTML, chunking
  keyboards.py         inline keyboards
  handlers_menu.py     /start, /menu, model, status, /bindgroup
  handlers_machines.py add/select/delete machines (FSM), install Claude
  handlers_sessions.py projects, sessions, recap, opening a topic
  handlers_chat.py     main dialog, files, /stop
  main.py              app assembly and polling
```

## Known limitations

- The bot does not create the group itself (Bot API limitation) — you create the
  group by hand once, after that topics are automatic.
- One active Claude run per topic at a time (guards against in-session races).
- Do not write to the same session from VS Code and the bot simultaneously.
- The recap shows the last few messages, not the whole history (which can be
  huge). Claude still has the full context via `--resume`.
- Group auto-binding triggers on the bot's rights event. If "Topics" were enabled
  after rights were granted, the event does not repeat — just send `/bindgroup`
  in the group.
- In-chat permission buttons (Allow/Deny) are a v2 candidate; the current mode is
  bypass.

## License

MIT — see [LICENSE](LICENSE).
