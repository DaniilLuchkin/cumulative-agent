"""SQLite storage: conversation history per chat topic and small settings."""
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
 role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(chat_id, thread_id, id);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def add(self, chat_id: int, thread_id: int, role: str, content: str):
        self.db.execute("INSERT INTO messages(chat_id,thread_id,role,content) VALUES(?,?,?,?)", (chat_id, thread_id, role, content))

    def history(self, chat_id: int, thread_id: int, max_chars: int) -> list[dict]:
        rows = self.db.execute("SELECT role,content FROM messages WHERE chat_id=? AND thread_id=? ORDER BY id DESC LIMIT 200",
                               (chat_id, thread_id)).fetchall()
        result, used = [], 0
        for role, content in rows:
            if result and used + len(content) > max_chars:
                break
            used += len(content)
            result.append({"role": role, "content": content})
        result.reverse()
        while result and result[0]["role"] != "user":
            result.pop(0)
        return result

    def clear(self, chat_id: int, thread_id: int):
        self.db.execute("DELETE FROM messages WHERE chat_id=? AND thread_id=?", (chat_id, thread_id))

    def first_user_message(self, chat_id: int, thread_id: int) -> str | None:
        row = self.db.execute("SELECT content FROM messages WHERE chat_id=? AND thread_id=? AND role='user' ORDER BY id LIMIT 1",
                              (chat_id, thread_id)).fetchone()
        return row[0] if row else None

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value):
        self.db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def claim_owner(self, user_id: int) -> bool:
        """The first private user becomes the only owner when no allowlist is configured."""
        cursor = self.db.execute("INSERT OR IGNORE INTO settings VALUES('owner',?)", (json.dumps(user_id),))
        return cursor.rowcount == 1
