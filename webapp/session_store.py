"""SQLite-backed session store.

Provides a dict-like (MutableMapping) interface so it's a drop-in
replacement for the plain in-memory `dict` webapp.main previously used --
callers do `SESSIONS[token]`, `SESSIONS[token] = session`, `del
SESSIONS[token]`, `token in SESSIONS`, `len(SESSIONS)`, etc. exactly as
before. The difference that matters to callers: `__getitem__` returns a
freshly deserialized copy on every access (there's no single shared object
living in memory anymore), so any in-place mutation of a fetched session
dict must be followed by writing it back (`SESSIONS[token] = session`)
for the change to actually persist -- see webapp/main.py's routes.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import MutableMapping
from pathlib import Path


def _serialize(session: dict) -> str:
    payload = dict(session)
    # JSON object keys must be strings; manual/qty overrides are keyed by
    # (BLItemNo, ElementId) tuples, so store them as [[key_list, value], ...]
    # instead and rebuild the tuple keys on read.
    for overrides_key in ("manual_overrides", "qty_overrides"):
        if overrides_key in payload:
            payload[overrides_key] = [[list(k), v] for k, v in payload[overrides_key].items()]
    return json.dumps(payload)


def _deserialize(raw: str) -> dict:
    payload = json.loads(raw)
    for overrides_key in ("manual_overrides", "qty_overrides"):
        if overrides_key in payload:
            payload[overrides_key] = {tuple(k): v for k, v in payload[overrides_key]}
    return payload


class SQLiteSessionStore(MutableMapping):
    """Persists sessions to a SQLite file so they survive a process restart
    (container redeploy, crash, etc.) instead of vanishing with the
    in-memory dict they replace.

    Opens a fresh connection per operation rather than holding one open --
    simplest way to be safe across FastAPI's threadpool-executed sync routes
    without needing an explicit lock, and cheap enough at this app's scale
    (a handful of trusted users, infrequent requests).
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "token TEXT PRIMARY KEY, data TEXT NOT NULL, last_accessed REAL NOT NULL"
                ")"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def __getitem__(self, token: str) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM sessions WHERE token = ?", (token,)).fetchone()
        if row is None:
            raise KeyError(token)
        return _deserialize(row[0])

    def __setitem__(self, token: str, session: dict) -> None:
        data = _serialize(session)
        last_accessed = session.get("last_accessed", time.time())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token, data, last_accessed) VALUES (?, ?, ?) "
                "ON CONFLICT(token) DO UPDATE SET data = excluded.data, last_accessed = excluded.last_accessed",
                (token, data, last_accessed),
            )

    def __delitem__(self, token: str) -> None:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            if cur.rowcount == 0:
                raise KeyError(token)

    def __iter__(self):
        with self._connect() as conn:
            rows = conn.execute("SELECT token FROM sessions").fetchall()
        return iter(row[0] for row in rows)

    def __len__(self) -> int:
        with self._connect() as conn:
            (count,) = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        return count

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions")

    def evict(self, ttl_seconds: float, max_sessions: int) -> None:
        """Drop sessions older than `ttl_seconds`, then (if still over
        `max_sessions`) drop the oldest-accessed ones until back at the cap.
        Pure SQL on the indexed `last_accessed` column -- no need to
        deserialize every session's JSON blob just to check a timestamp.
        """
        now = time.time()
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE (? - last_accessed) > ?", (now, ttl_seconds))
            (count,) = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
            if count > max_sessions:
                conn.execute(
                    "DELETE FROM sessions WHERE token IN ("
                    "SELECT token FROM sessions ORDER BY last_accessed ASC LIMIT ?"
                    ")",
                    (count - max_sessions,),
                )
