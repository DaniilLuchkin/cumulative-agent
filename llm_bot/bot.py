"""LLM chat bot following Telegram's recommendations for AI bots.

* Streaming replies: sendMessageDraft / sendRichMessageDraft, an empty draft shows
  "Thinking…", can_stop shows a Stop button (update stopped_message_generation).
* Final answer persisted with sendRichMessage (Markdown), plain-text fallback.
* Topics in private chats: one conversation per topic, /new creates a topic.
"""
import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

from .llm import LLMError
from .store import Store
from .telegram import Api, TelegramError

log = logging.getLogger("llm_bot")

RICH_LIMIT = 32000  # sendRichMessage: up to 32768 characters
TEXT_LIMIT = 4000   # sendMessage / sendMessageDraft: up to 4096 characters
DEFAULT_PROMPT = ("You are a helpful assistant in a Telegram chat. Reply in the language of the user's last message. "
                  "Be concise unless asked for detail. Use Markdown: headings, lists, tables, fenced code blocks.")
TITLE_PROMPT = ("Name this conversation in 2-5 words in the language of the user's message. "
                "Reply with the title only, no quotes or trailing period.")
SERVICE_KEYS = ("forum_topic_created", "forum_topic_edited", "forum_topic_closed", "forum_topic_reopened",
                "general_forum_topic_hidden", "general_forum_topic_unhidden", "pinned_message")
COMMANDS = [("new", "Новый диалог"), ("reset", "Очистить контекст"), ("help", "Справка")]


@dataclass
class Config:
    telegram_token: str
    openrouter_key: str
    model: str = "openrouter/auto"
    system_prompt: str = DEFAULT_PROMPT
    allowed_ids: set[int] = field(default_factory=set)
    database: str = "data/bot.sqlite3"
    max_tokens: int = 4096
    history_chars: int = 24000
    draft_interval: float = 0.8   # seconds between streamed draft updates
    draft_keepalive: float = 20   # drafts are 30-second previews

    @classmethod
    def from_env(cls) -> "Config":
        missing = [name for name in ("TELEGRAM_BOT_TOKEN", "OPENROUTER_API_KEY") if not os.environ.get(name)]
        if missing:
            raise SystemExit("Missing environment variables: " + ", ".join(missing))
        ids = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
        return cls(telegram_token=os.environ["TELEGRAM_BOT_TOKEN"], openrouter_key=os.environ["OPENROUTER_API_KEY"],
                   model=os.environ.get("LLM_MODEL") or cls.model, system_prompt=os.environ.get("SYSTEM_PROMPT") or DEFAULT_PROMPT,
                   allowed_ids=ids, database=os.environ.get("BOT_DB", cls.database),
                   max_tokens=int(os.environ.get("MAX_OUTPUT_TOKENS", cls.max_tokens)),
                   history_chars=int(os.environ.get("HISTORY_CHARS", cls.history_chars)))


class Draft:
    """Keeps a streamed draft visible: pushes changes and refreshes before expiry."""

    def __init__(self, bot: "Bot", chat_id: int, thread_id: int, draft_id: int):
        self.bot, self.chat_id, self.thread_id, self.draft_id = bot, chat_id, thread_id, draft_id
        self.text, self.shown, self.rich, self.enabled = "", None, True, True
        self.last = 0.0

    async def push(self):
        if not self.enabled:
            return
        params = {"chat_id": self.chat_id, "draft_id": self.draft_id, "can_stop": True}
        if self.thread_id:
            params["message_thread_id"] = self.thread_id
        text, self.shown, self.last = self.text, self.text, time.monotonic()
        try:
            if text and self.rich:
                try:
                    await self.bot.api.call("sendRichMessageDraft", rich_message={"markdown": text[:RICH_LIMIT]}, **params)
                    return
                except TelegramError as exc:
                    if exc.retry_after:
                        return
                    self.rich = False  # e.g. markup the rich parser rejects; plain drafts still work
            visible = text if len(text) <= TEXT_LIMIT else "…" + text[-(TEXT_LIMIT - 1):]
            await self.bot.api.call("sendMessageDraft", text=visible, **params)
        except TelegramError as exc:
            if not exc.retry_after:
                self.enabled = False
                log.warning("drafts disabled for this answer: %s", exc.description)

    async def run(self):
        await self.push()  # empty text = "Thinking…" placeholder
        while True:
            await asyncio.sleep(self.bot.config.draft_interval)
            idle = time.monotonic() - self.last
            if self.text != self.shown or idle >= self.bot.config.draft_keepalive:
                await self.push()


class Bot:
    def __init__(self, config: Config, api: Api, llm, store: Store):
        self.config, self.api, self.llm, self.store = config, api, llm, store
        self.topics_enabled = False
        self.locks: dict[tuple[int, int], asyncio.Lock] = {}
        self.generations: dict[tuple[int, int], asyncio.Task] = {}
        self.stopped: set[tuple[int, int]] = set()
        self.tasks: set[asyncio.Task] = set()

    # --- setup and polling ---------------------------------------------------------

    async def refresh_topics(self) -> bool:
        """Topic mode is switched in @BotFather; re-read it without a restart."""
        me = await self.api.call("getMe")
        self.topics_enabled = bool(me.get("has_topics_enabled"))
        return self.topics_enabled

    async def configure(self):
        me = await self.api.call("getMe")
        self.topics_enabled = bool(me.get("has_topics_enabled"))
        await self.api.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS],
                            scope={"type": "all_private_chats"})
        log.info("started as @%s, topics=%s, model=%s", me.get("username"), self.topics_enabled, self.config.model)

    async def run(self):
        while True:
            try:
                await self.configure()
                break
            except TelegramError as exc:
                log.error("Telegram setup failed: %s", exc.description)
                await asyncio.sleep(exc.retry_after or 10)
        offset = self.store.get("offset", 0)
        while True:
            try:
                updates = await self.api.call("getUpdates", http_timeout=40, offset=offset, timeout=30, limit=100,
                                              allowed_updates=["message", "stopped_message_generation"])
            except TelegramError as exc:
                log.warning("getUpdates failed: %s", exc.description)
                await asyncio.sleep(exc.retry_after or 5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                self.spawn(self.dispatch(update))
            self.store.set("offset", offset)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def dispatch(self, update: dict):
        try:
            if "stopped_message_generation" in update:
                self.on_stop(update["stopped_message_generation"])
            elif "message" in update:
                await self.on_message(update["message"])
        except Exception:
            log.exception("update %s failed", update.get("update_id"))

    # --- access --------------------------------------------------------------------

    def allowed(self, user_id: int) -> bool:
        if self.config.allowed_ids:
            return user_id in self.config.allowed_ids
        self.store.claim_owner(user_id)
        return self.store.get("owner") == user_id

    # --- messages ------------------------------------------------------------------

    async def on_message(self, message: dict):
        chat, user = message.get("chat", {}), message.get("from", {})
        if chat.get("type") != "private" or user.get("is_bot") or not self.allowed(user.get("id")):
            return
        chat_id = chat["id"]
        thread_id = message.get("message_thread_id", 0) if message.get("is_topic_message") else 0
        if thread_id:
            self.topics_enabled = True
            # A topic the user opened in the client gets a placeholder name; the bot renames it.
            created = message.get("forum_topic_created") or (message.get("reply_to_message") or {}).get("forum_topic_created") or {}
            if created.get("is_name_implicit") and not self.store.first_user_message(chat_id, thread_id):
                self.store.set(f"autoname:{chat_id}:{thread_id}", True)
        if any(key in message for key in SERVICE_KEYS):
            return
        text = (message.get("text") or "").strip()
        if not text:
            await self.reply(chat_id, thread_id, "Пока понимаю только текстовые сообщения.")
            return
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else None
        if command in ("/start", "/help"):
            topics = "/new — новый диалог в отдельном топике.\n" if self.topics_enabled else "/new — начать диалог заново.\n"
            await self.reply(chat_id, thread_id, "Напишите вопрос — ответ появляется по мере генерации, его можно остановить кнопкой «Стоп».\n"
                             + topics + "/reset — очистить контекст этого диалога.")
        elif command == "/new":
            await self.new_topic(chat_id, thread_id, text.partition(" ")[2].strip())
        elif command == "/reset":
            self.store.clear(chat_id, thread_id)
            await self.reply(chat_id, thread_id, "Контекст очищен.")
        else:
            await self.answer(chat_id, thread_id, text, message["message_id"])

    async def new_topic(self, chat_id: int, thread_id: int, name: str):
        if not self.topics_enabled and not await self.refresh_topics():
            self.store.clear(chat_id, thread_id)
            await self.reply(chat_id, thread_id, "Начинаем заново — контекст очищен.\n"
                             "Чтобы каждый диалог был в отдельном топике, включите для бота режим топиков в @BotFather.")
            return
        try:
            topic = await self.api.call("createForumTopic", chat_id=chat_id, name=(name or "Новый чат")[:128])
        except TelegramError as exc:
            await self.reply(chat_id, thread_id, "Не удалось создать топик: " + exc.description)
            return
        new_thread = topic["message_thread_id"]
        if not name:
            self.store.set(f"autoname:{chat_id}:{new_thread}", True)
        await self.reply(chat_id, new_thread, "Новый диалог. Задайте вопрос.")

    async def answer(self, chat_id: int, thread_id: int, text: str, message_id: int):
        # One answer at a time per conversation; later messages wait their turn.
        async with self.locks.setdefault((chat_id, thread_id), asyncio.Lock()):
            self.store.add(chat_id, thread_id, "user", text)
            messages = [{"role": "system", "content": self.config.system_prompt}, *self.store.history(chat_id, thread_id, self.config.history_chars)]
            key = (chat_id, message_id)
            draft = Draft(self, chat_id, thread_id, message_id)
            pusher = asyncio.create_task(draft.run())
            generation = asyncio.create_task(self.generate(messages, draft))
            self.generations[key] = generation
            stopped, error = False, None
            try:
                await generation
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise  # shutdown
                stopped = True
            except LLMError as exc:
                error = str(exc)
            finally:
                self.generations.pop(key, None)
                self.stopped.discard(key)
                pusher.cancel()
            if error:
                await self.reply(chat_id, thread_id, "Не удалось получить ответ: " + error, message_id)
                return
            result = draft.text.strip()
            if result:
                self.store.add(chat_id, thread_id, "assistant", result)
            if stopped:
                result = (result + "\n\n_Генерация остановлена._") if result else "Генерация остановлена."
            await self.send_markdown(chat_id, thread_id, result, message_id)
            await self.autoname(chat_id, thread_id)

    async def generate(self, messages: list[dict], draft: Draft):
        async for text in self.llm.stream(messages):
            draft.text = text

    def on_stop(self, event: dict):
        key = (event.get("chat", {}).get("id"), event.get("draft_id"))
        generation = self.generations.get(key)
        if generation:
            self.stopped.add(key)
            generation.cancel()

    async def autoname(self, chat_id: int, thread_id: int):
        key = f"autoname:{chat_id}:{thread_id}"
        if thread_id and self.store.get(key):
            self.store.set(key, False)
            first = " ".join((self.store.first_user_message(chat_id, thread_id) or "").split())
            if not first:
                return
            name = first[:60]
            try:
                title = await self.llm.complete([{"role": "system", "content": TITLE_PROMPT}, {"role": "user", "content": first[:2000]}], max_tokens=30)
                title = " ".join(title.split()).strip("\"'«».")
                name = title[:128] or name
            except (LLMError, AttributeError):
                pass  # keep the first message as the name
            try:
                await self.api.call("editForumTopic", chat_id=chat_id, message_thread_id=thread_id, name=name)
            except TelegramError as exc:
                log.warning("topic rename failed: %s", exc.description)

    # --- sending -------------------------------------------------------------------

    def _target(self, chat_id: int, thread_id: int, reply_to: int | None) -> dict:
        params = {"chat_id": chat_id}
        if thread_id:
            params["message_thread_id"] = thread_id
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return params

    async def reply(self, chat_id: int, thread_id: int, text: str, reply_to: int | None = None):
        await self.api.call("sendMessage", text=text[:TEXT_LIMIT], **self._target(chat_id, thread_id, reply_to))

    async def send_markdown(self, chat_id: int, thread_id: int, text: str, reply_to: int | None):
        for index, part in enumerate(split(text, RICH_LIMIT)):
            target = self._target(chat_id, thread_id, reply_to if index == 0 else None)
            try:
                await self.api.call("sendRichMessage", rich_message={"markdown": part}, **target)
            except TelegramError as exc:
                log.warning("rich message rejected, sending plain text: %s", exc.description)
                for number, chunk in enumerate(split(part, TEXT_LIMIT)):
                    await self.api.call("sendMessage", text=chunk, **(target if number == 0 else self._target(chat_id, thread_id, None)))

    async def stop(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def split(text: str, limit: int) -> list[str]:
    """Split on paragraph or line boundaries where possible."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        cut = cut if cut > limit // 2 else text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts + [text] if text else parts
