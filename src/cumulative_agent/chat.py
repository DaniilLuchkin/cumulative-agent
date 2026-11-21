"""Conversation history for Telegram chat mode: one context per private-chat topic."""
import json

from .domain import Task, now
from .repository import Repository
from .routing import CHAT_SYSTEM_PROMPT


class ChatStore:
    def __init__(self, repo: Repository, history_bytes: int = 24000):
        self.repo, self.history_bytes = repo, history_bytes

    @staticmethod
    def _thread(thread_id: int | None) -> int:
        return thread_id or 0

    def add(self, owner: int, thread_id: int | None, role: str, content: str, task_id: str | None = None) -> int:
        if role not in ("user", "assistant"):
            raise ValueError("role")
        with self.repo.transaction() as db:
            if task_id and db.execute("SELECT 1 FROM chat_messages WHERE task_id=? AND role=?", (task_id, role)).fetchone():
                return 0  # Idempotent on Telegram update redelivery.
            cursor = db.execute("INSERT INTO chat_messages(owner_id,thread_id,role,content,task_id,created_at) VALUES(?,?,?,?,?,?)",
                                (owner, self._thread(thread_id), role, content[:40000], task_id, now()))
            return cursor.lastrowid

    def reset(self, owner: int, thread_id: int | None):
        row = self.repo.one("SELECT COALESCE(MAX(id),0) n FROM chat_messages WHERE owner_id=? AND thread_id=?", (owner, self._thread(thread_id)))
        self.repo.set_setting(f"chat_reset:{owner}:{self._thread(thread_id)}", row["n"])

    def count(self, owner: int, thread_id: int | None) -> int:
        start = self.repo.get_setting(f"chat_reset:{owner}:{self._thread(thread_id)}", 0)
        return self.repo.one("SELECT COUNT(*) n FROM chat_messages WHERE owner_id=? AND thread_id=? AND id>?", (owner, self._thread(thread_id), start))["n"]

    def messages(self, task: Task) -> list[dict]:
        """System prompt, bounded recent history before this turn, then the turn itself."""
        owner, thread = task.telegram_owner_id, self._thread(task.telegram_thread_id)
        current = self.repo.one("SELECT id,content FROM chat_messages WHERE task_id=? AND role='user'", (task.id,))
        limit_id = current["id"] if current else 1 << 62
        start = self.repo.get_setting(f"chat_reset:{owner}:{thread}", 0)
        rows = self.repo.all("SELECT role,content FROM chat_messages WHERE owner_id=? AND thread_id=? AND id>? AND id<? ORDER BY id DESC LIMIT 200",
                             (owner, thread, start, limit_id))
        history, used = [], 0
        for row in rows:
            size = len(row["content"].encode("utf-8"))
            if used + size > self.history_bytes:
                break
            used += size
            history.append({"role": row["role"], "content": row["content"]})
        history.reverse()
        # Providers expect alternating turns; drop a leading assistant message.
        while history and history[0]["role"] == "assistant":
            history.pop(0)
        turn = current["content"] if current else task.description or task.title
        return [{"role": "system", "content": CHAT_SYSTEM_PROMPT}, *history, {"role": "user", "content": turn}]

    def prompt(self, task: Task) -> str:
        return json.dumps(self.messages(task), ensure_ascii=False)
