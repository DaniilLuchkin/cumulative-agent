"""SQLite storage: conversations, answered turns, reminders, watches, settings."""
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
 role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(chat_id, thread_id, id);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turns(
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
 user_message_id INTEGER, user_row INTEGER NOT NULL, assistant_row INTEGER, bot_message_ids TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS turns_thread ON turns(chat_id, thread_id, id);
CREATE TABLE IF NOT EXISTS suggestions(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reminders(
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
 text TEXT NOT NULL, due_at REAL NOT NULL, repeat TEXT NOT NULL DEFAULT 'none', active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS widgets(
 chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL, message_id INTEGER NOT NULL, caption TEXT, PRIMARY KEY(chat_id, message_id));
CREATE TABLE IF NOT EXISTS watches(
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, thread_id INTEGER NOT NULL,
 title TEXT NOT NULL, instructions TEXT NOT NULL, every_hours REAL NOT NULL, next_at REAL NOT NULL,
 last_run REAL, last_report TEXT, status_message_id INTEGER, active INTEGER NOT NULL DEFAULT 1);
"""


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def all(self, sql: str, args=()) -> list[sqlite3.Row]:
        return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, args=()) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    def run(self, sql: str, args=()) -> int:
        return self.db.execute(sql, args).lastrowid

    # --- conversation ---------------------------------------------------------------

    def add(self, chat_id: int, thread_id: int, role: str, content: str) -> int:
        return self.run("INSERT INTO messages(chat_id,thread_id,role,content) VALUES(?,?,?,?)", (chat_id, thread_id, role, content))

    def history(self, chat_id: int, thread_id: int, max_chars: int) -> list[dict]:
        rows = self.all("SELECT role,content FROM messages WHERE chat_id=? AND thread_id=? ORDER BY id DESC LIMIT 200", (chat_id, thread_id))
        result, used = [], 0
        for row in rows:
            if result and used + len(row["content"]) > max_chars:
                break
            used += len(row["content"])
            result.append({"role": row["role"], "content": row["content"]})
        result.reverse()
        while result and result[0]["role"] != "user":
            result.pop(0)
        return result

    def clear(self, chat_id: int, thread_id: int):
        self.run("DELETE FROM messages WHERE chat_id=? AND thread_id=?", (chat_id, thread_id))
        self.run("DELETE FROM turns WHERE chat_id=? AND thread_id=?", (chat_id, thread_id))

    def first_user_message(self, chat_id: int, thread_id: int) -> str | None:
        row = self.one("SELECT content FROM messages WHERE chat_id=? AND thread_id=? AND role='user' ORDER BY id LIMIT 1", (chat_id, thread_id))
        return row["content"] if row else None

    # --- turns: what a regeneration or an edit has to replace -----------------------

    def add_turn(self, chat_id, thread_id, user_message_id, user_row, assistant_row, bot_message_ids) -> int:
        return self.run("INSERT INTO turns(chat_id,thread_id,user_message_id,user_row,assistant_row,bot_message_ids) VALUES(?,?,?,?,?,?)",
                        (chat_id, thread_id, user_message_id, user_row, assistant_row, json.dumps(bot_message_ids)))

    def turn(self, turn_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM turns WHERE id=?", (turn_id,))

    def turn_by_message(self, chat_id: int, user_message_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM turns WHERE chat_id=? AND user_message_id=? ORDER BY id DESC LIMIT 1", (chat_id, user_message_id))

    def is_latest(self, turn: sqlite3.Row) -> bool:
        row = self.one("SELECT MAX(id) n FROM turns WHERE chat_id=? AND thread_id=?", (turn["chat_id"], turn["thread_id"]))
        return row["n"] == turn["id"]

    def drop_answer(self, turn: sqlite3.Row):
        """Forget the answer of a turn; the user message stays in the history."""
        if turn["assistant_row"]:
            self.run("DELETE FROM messages WHERE id=?", (turn["assistant_row"],))
        self.run("DELETE FROM turns WHERE id=?", (turn["id"],))

    # --- settings -------------------------------------------------------------------

    def get(self, key: str, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value):
        self.run("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def claim_owner(self, user_id: int) -> bool:
        """The first private user becomes the only owner when no allowlist is configured."""
        cursor = self.db.execute("INSERT OR IGNORE INTO settings VALUES('owner',?)", (json.dumps(user_id),))
        return cursor.rowcount == 1
