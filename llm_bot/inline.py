"""Inline mode (@bot query in any chat) and Guest Mode (@bot mention in any chat).

Both surfaces post a placeholder inline message first, then stream the answer into it
with editMessageText(inline_message_id), as drafts are available in private chats only.
"""
import asyncio
import logging
import secrets
import time

from .llm import SERVER_TOOLS, LLMError, chat_var, purpose_var
from .telegram import TelegramError

log = logging.getLogger("llm_bot")
EDIT_INTERVAL = 1.5  # seconds between edits of an inline message
INLINE_PROMPT = ("You answer a question asked from another Telegram chat; your reply is posted into that chat as is. "
                 "Answer in the language of the question, concisely, in Markdown. Use web search for current facts.")


class Surfaces:
    def __init__(self, bot):
        self.bot = bot

    def keyboard(self) -> dict:
        # An inline keyboard is what makes the sent inline message editable.
        return {"inline_keyboard": [[{"text": "Открыть бота", "url": f"https://t.me/{self.bot.username}"}]]}

    def placeholder(self, result_id: str, question: str) -> dict:
        return {"type": "article", "id": result_id, "title": "Спросить: " + question[:60],
                "description": "Ответ появится в сообщении", "reply_markup": self.keyboard(),
                "input_message_content": {"message_text": "⏳ " + question[:3500]}}

    async def on_inline_query(self, query: dict):
        question = (query.get("query") or "").strip()
        results = []
        if question and self.bot.allowed(query.get("from", {}).get("id")):
            result_id = secrets.token_hex(8)
            self.bot.store.set("inline:" + result_id, question)
            results = [self.placeholder(result_id, question)]
        await self.bot.api.call("answerInlineQuery", inline_query_id=query["id"], results=results, cache_time=0, is_personal=True)

    async def on_chosen(self, chosen: dict):
        inline_id = chosen.get("inline_message_id")
        question = self.bot.store.get("inline:" + chosen.get("result_id", "")) or chosen.get("query", "")
        if inline_id and question and self.bot.allowed(chosen.get("from", {}).get("id")):
            await self.stream(inline_id, question, None)

    async def on_guest(self, message: dict):
        caller = message.get("from", {})
        if not self.bot.allowed(caller.get("id")):
            return
        text = (message.get("text") or message.get("caption") or "").replace("@" + (self.bot.username or ""), "").strip()
        replied = message.get("reply_to_message") or {}
        context = (replied.get("text") or replied.get("caption") or "").strip() or None
        question = text or ("Ответь на это сообщение" if context else "")
        if not question:
            return
        sent = await self.bot.api.call("answerGuestQuery", guest_query_id=message["guest_query_id"],
                                       result=self.placeholder(secrets.token_hex(8), question))
        await self.stream(sent["inline_message_id"], question, context)

    async def stream(self, inline_id: str, question: str, context: str | None):
        chat_var.set(self.bot.store.get("owner"))
        purpose_var.set("inline")
        prompt = f"Message being discussed:\n> {context}\n\n{question}" if context else question
        messages = [{"role": "system", "content": INLINE_PROMPT}, {"role": "user", "content": prompt}]
        header = "> " + question[:300].replace("\n", " ") + "\n\n"
        state = {"text": "", "shown": "", "last": 0.0, "rich": True}

        async def edit(text: str, final: bool = False):
            params = {"inline_message_id": inline_id, "reply_markup": self.keyboard()}
            try:
                if state["rich"]:
                    try:
                        await self.bot.api.call("editMessageText", rich_message={"markdown": (header + text)[:32000]}, **params)
                        return
                    except TelegramError as exc:
                        if exc.retry_after and not final:
                            return
                        state["rich"] = False
                await self.bot.api.call("editMessageText", text=(header + text)[:4096], **params)
            except TelegramError as exc:
                if "not modified" not in exc.description:
                    log.warning("inline edit failed: %s", exc.description)

        def on_text(text: str):
            state["text"] = text

        async def pump():
            if state["text"] != state["shown"] and time.monotonic() - state["last"] >= EDIT_INTERVAL:
                state["shown"], state["last"] = state["text"], time.monotonic()
                await edit(state["text"] + " …")

        task = self.bot.spawn(self.bot.llm.run(messages, self.bot.current_model(None), tools=SERVER_TOOLS, on_text=on_text))
        while not task.done():
            await pump()
            await asyncio.sleep(0.3)
        try:
            answer = task.result().text.strip() or "Пустой ответ."
        except LLMError as exc:
            answer = "Не удалось получить ответ: " + str(exc)
        await edit(answer, final=True)
