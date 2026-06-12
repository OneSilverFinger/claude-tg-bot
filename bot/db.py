import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS machines(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  host TEXT NOT NULL,
  port INTEGER NOT NULL DEFAULT 22,
  username TEXT NOT NULL,
  auth_type TEXT NOT NULL DEFAULT 'key',
  secret_enc TEXT NOT NULL,
  passphrase_enc TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Binding: what a given Telegram chat (and forum topic) is connected to.
-- Private chat -> (chat_id=user_id, thread_id=0), switchable.
-- Forum topic  -> (chat_id=group_id, thread_id=topic_id), one session per topic.
CREATE TABLE IF NOT EXISTS bindings(
  chat_id INTEGER NOT NULL,
  thread_id INTEGER NOT NULL DEFAULT 0,
  user_id INTEGER NOT NULL,
  machine_id INTEGER,
  cwd TEXT,
  session_id TEXT,
  model TEXT,
  pending_files TEXT NOT NULL DEFAULT '[]',
  title TEXT,
  PRIMARY KEY (chat_id, thread_id)
);

CREATE TABLE IF NOT EXISTS user_prefs(
  user_id INTEGER PRIMARY KEY,
  forum_chat_id INTEGER
);
"""

BINDING_FIELDS = {"machine_id", "cwd", "session_id", "model", "pending_files", "title", "user_id"}


class Database:
    def __init__(self, path: str):
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        """Additive migrations for DBs created before a column existed."""
        cur = await self._db.execute("PRAGMA table_info(machines)")
        cols = {r["name"] for r in await cur.fetchall()}
        if "claude_key_enc" not in cols:
            await self._db.execute("ALTER TABLE machines ADD COLUMN claude_key_enc TEXT")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # ---- machines ----

    async def add_machine(self, user_id: int, name: str, host: str, port: int,
                          username: str, auth_type: str, secret_enc: str,
                          passphrase_enc: str | None) -> int:
        cur = await self._db.execute(
            "INSERT INTO machines(user_id, name, host, port, username, auth_type, secret_enc, passphrase_enc) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (user_id, name, host, port, username, auth_type, secret_enc, passphrase_enc),
        )
        await self._db.commit()
        return cur.lastrowid

    async def machines(self, user_id: int) -> list[dict]:
        cur = await self._db.execute(
            "SELECT * FROM machines WHERE user_id=? ORDER BY id", (user_id,)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def machine(self, machine_id: int, user_id: int) -> dict | None:
        cur = await self._db.execute(
            "SELECT * FROM machines WHERE id=? AND user_id=?", (machine_id, user_id)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_claude_key(self, machine_id: int, user_id: int, key_enc: str | None) -> None:
        await self._db.execute(
            "UPDATE machines SET claude_key_enc=? WHERE id=? AND user_id=?",
            (key_enc, machine_id, user_id),
        )
        await self._db.commit()

    async def delete_machine(self, machine_id: int, user_id: int) -> None:
        await self._db.execute(
            "DELETE FROM machines WHERE id=? AND user_id=?", (machine_id, user_id)
        )
        await self._db.execute(
            "UPDATE bindings SET machine_id=NULL, cwd=NULL, session_id=NULL "
            "WHERE machine_id=? AND user_id=?",
            (machine_id, user_id),
        )
        await self._db.commit()

    # ---- bindings ----

    async def get_binding(self, chat_id: int, thread_id: int) -> dict | None:
        cur = await self._db.execute(
            "SELECT * FROM bindings WHERE chat_id=? AND thread_id=?", (chat_id, thread_id)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_binding(self, chat_id: int, thread_id: int, user_id: int, **fields) -> None:
        bad = set(fields) - BINDING_FIELDS
        if bad:
            raise ValueError(f"unknown binding fields: {bad}")
        await self._db.execute(
            "INSERT OR IGNORE INTO bindings(chat_id, thread_id, user_id) VALUES(?,?,?)",
            (chat_id, thread_id, user_id),
        )
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            await self._db.execute(
                f"UPDATE bindings SET {cols} WHERE chat_id=? AND thread_id=?",
                (*fields.values(), chat_id, thread_id),
            )
        await self._db.commit()

    async def delete_binding(self, chat_id: int, thread_id: int) -> None:
        await self._db.execute(
            "DELETE FROM bindings WHERE chat_id=? AND thread_id=?", (chat_id, thread_id)
        )
        await self._db.commit()

    # ---- user prefs ----

    async def set_forum_chat(self, user_id: int, forum_chat_id: int | None) -> None:
        await self._db.execute(
            "INSERT INTO user_prefs(user_id, forum_chat_id) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET forum_chat_id=excluded.forum_chat_id",
            (user_id, forum_chat_id),
        )
        await self._db.commit()

    async def get_forum_chat(self, user_id: int) -> int | None:
        cur = await self._db.execute(
            "SELECT forum_chat_id FROM user_prefs WHERE user_id=?", (user_id,)
        )
        row = await cur.fetchone()
        return row["forum_chat_id"] if row else None
