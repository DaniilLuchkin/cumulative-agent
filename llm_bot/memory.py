"""Semantic search over all past conversations with OpenRouter embeddings."""
import asyncio
import json
import logging
import math
from array import array

from .llm import LLMError, purpose_var

log = logging.getLogger("llm_bot")
BATCH = 64
SNIPPET = 400

FUNCTION = {"type": "function", "function": {
    "name": "search_history",
    "description": ("Search all earlier conversations with the user, in every topic, by meaning. Use it when the user refers to "
                    "something discussed before that is not in the current context («как мы решили…», «что я говорил про…»)."),
    "parameters": {"type": "object", "required": ["query"], "properties": {
        "query": {"type": "string", "description": "What to look for, as a short phrase"}}}}}


def rank(rows: list[tuple[int, bytes]], target: array) -> list[tuple[float, int]]:
    scored = []
    for message_id, blob in rows:
        vector = unpack(blob)
        if len(vector) == len(target):
            scored.append((sum(a * b for a, b in zip(vector, target)), message_id))
    return sorted(scored, reverse=True)


def pack(vector: list[float]) -> bytes:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return array("f", (x / norm for x in vector)).tobytes()


def unpack(blob: bytes) -> array:
    vector = array("f")
    vector.frombytes(blob)
    return vector


class Memory:
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store

    async def index(self, limit: int = 2000):
        """Embed messages that have no vector yet (new ones and, on first start, the backlog)."""
        purpose = purpose_var.set("memory")
        try:
            for _ in range(max(1, limit // BATCH)):
                rows = self.store.all("SELECT m.id, m.chat_id, m.content FROM messages m LEFT JOIN embeddings e ON e.message_id=m.id "
                                      "WHERE e.message_id IS NULL ORDER BY m.id LIMIT ?", (BATCH,))
                if not rows:
                    return
                vectors = await self.bot.llm.embed([row["content"][:4000] or " " for row in rows], self.bot.config.embedding_model)
                for row, vector in zip(rows, vectors):
                    self.store.run("INSERT OR REPLACE INTO embeddings VALUES(?,?,?)", (row["id"], row["chat_id"], pack(vector)))
        except LLMError as exc:
            log.warning("memory indexing paused: %s", exc)
        finally:
            purpose_var.reset(purpose)

    def in_context(self, chat_id: int, thread_id: int) -> set[int]:
        """Ids of the current topic's messages that the history window already sends to the model."""
        ids, used = set(), 0
        for row in self.store.all("SELECT id, content FROM messages WHERE chat_id=? AND thread_id=? ORDER BY id DESC LIMIT 200", (chat_id, thread_id)):
            used += len(row["content"])
            if ids and used > self.bot.config.history_chars:
                break
            ids.add(row["id"])
        return ids

    async def search(self, chat_id: int, query: str, current_thread: int, k: int = 5) -> str:
        if not query.strip():
            raise ValueError("query is empty")
        await self.index(200)
        purpose = purpose_var.set("memory")
        try:
            target = unpack(pack((await self.bot.llm.embed([query], self.bot.config.embedding_model))[0]))
        finally:
            purpose_var.reset(purpose)
        visible = self.in_context(chat_id, current_thread)
        rows = [(row["message_id"], row["vector"]) for row in self.store.all("SELECT message_id, vector FROM embeddings WHERE chat_id=?", (chat_id,))
                if row["message_id"] not in visible]  # messages in the model's context are skipped
        scored = await asyncio.to_thread(rank, rows, target)  # pure Python math: keep the bot responsive
        hits = []
        for score, message_id in scored[:k]:
            row = self.store.one("SELECT thread_id, role, content, created_at FROM messages WHERE id=?", (message_id,))
            if row and score > 0.2:
                link = f"https://t.me/{self.bot.username}/{row['thread_id']}" if row["thread_id"] and self.bot.username else None
                hits.append({"when": row["created_at"][:16], "who": row["role"], "topic_link": link,
                             "same_topic": row["thread_id"] == current_thread, "score": round(score, 2), "text": row["content"][:SNIPPET]})
        return json.dumps({"results": hits, "note": "Quote what helps; link a topic with topic_link when it is another topic."}, ensure_ascii=False)
