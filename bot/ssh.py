import asyncio
import logging
import shlex

import asyncssh

log = logging.getLogger(__name__)

CONNECT_TIMEOUT = 20


def login_shell(cmd: str) -> str:
    """Wrap a command in a login shell so PATH (nvm, ~/.local/bin) is set up."""
    return "bash -lc " + shlex.quote(cmd)


class SSHManager:
    """Keeps one cached SSH connection per machine, reconnects on failure."""

    def __init__(self, crypto):
        self._crypto = crypto
        self._conns: dict[int, asyncssh.SSHClientConnection] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def _auth_options(self, machine: dict) -> dict:
        opts: dict = {}
        secret = self._crypto.decrypt(machine["secret_enc"])
        if machine["auth_type"] == "key":
            passphrase = (
                self._crypto.decrypt(machine["passphrase_enc"])
                if machine.get("passphrase_enc")
                else None
            )
            opts["client_keys"] = [asyncssh.import_private_key(secret, passphrase)]
        else:
            opts["password"] = secret
        return opts

    async def _open(self, machine: dict) -> asyncssh.SSHClientConnection:
        return await asyncio.wait_for(
            asyncssh.connect(
                machine["host"],
                port=machine.get("port") or 22,
                username=machine["username"],
                known_hosts=None,
                keepalive_interval=30,
                **self._auth_options(machine),
            ),
            CONNECT_TIMEOUT,
        )

    async def connect(self, machine: dict) -> asyncssh.SSHClientConnection:
        mid = machine["id"]
        lock = self._locks.setdefault(mid, asyncio.Lock())
        async with lock:
            conn = self._conns.get(mid)
            if conn is not None:
                return conn
            conn = await self._open(machine)
            self._conns[mid] = conn
            return conn

    def drop(self, machine_id: int) -> None:
        conn = self._conns.pop(machine_id, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    async def run(self, machine: dict, command: str, timeout: int = 30,
                  input: str | None = None):
        """Run a command, transparently reconnecting once if the cached
        connection turned out to be dead."""
        last_exc: Exception | None = None
        for attempt in (1, 2):
            conn = await self.connect(machine)
            try:
                return await asyncio.wait_for(conn.run(command, input=input), timeout)
            except (OSError, asyncssh.Error) as e:
                last_exc = e
                self.drop(machine["id"])
                if attempt == 2:
                    raise
        raise last_exc  # unreachable, keeps type checkers happy

    async def sftp(self, machine: dict) -> asyncssh.SFTPClient:
        last_exc: Exception | None = None
        for attempt in (1, 2):
            conn = await self.connect(machine)
            try:
                return await conn.start_sftp_client()
            except (OSError, asyncssh.Error) as e:
                last_exc = e
                self.drop(machine["id"])
                if attempt == 2:
                    raise
        raise last_exc

    async def test(self, machine: dict) -> tuple[bool, str]:
        """Fresh connection test for the add-machine flow.

        Returns (ok, info): on success info is the claude version or empty
        string when claude is missing; on failure it is the error text.
        """
        try:
            conn = await self._open(machine)
        except Exception as e:
            return False, str(e) or type(e).__name__
        try:
            result = await asyncio.wait_for(
                conn.run(login_shell(
                    "command -v claude >/dev/null 2>&1 && claude --version || echo __NO_CLAUDE__"
                )),
                30,
            )
            out = (result.stdout or "").strip()
            if "__NO_CLAUDE__" in out or not out:
                return True, ""
            return True, out.splitlines()[-1]
        except Exception as e:
            log.warning("claude check failed: %s", e)
            return True, ""
        finally:
            conn.close()
