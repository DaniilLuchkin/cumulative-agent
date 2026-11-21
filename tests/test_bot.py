import asyncio
import json
import unittest

import httpx

from llm_bot.bot import Bot, Config, split
from llm_bot.llm import LLMError, OpenRouter
from llm_bot.store import Store
from llm_bot.telegram import TelegramError


class FakeApi:
    def __init__(self, topics=True):
        self.calls, self.fail, self.topics = [], set(), topics

    async def call(self, method, http_timeout=30, **params):
        self.calls.append((method, params))
        if method in self.fail:
            raise TelegramError(method, "Bad Request")
        if method == "getMe":
            return {"id": 1, "username": "test_bot", "has_topics_enabled": self.topics}
        if method == "createForumTopic":
            return {"message_thread_id": 77, "name": params["name"]}
        return True

    def sent(self, method):
        return [p for m, p in self.calls if m == method]


class FakeLLM:
    def __init__(self, chunks=("Привет", ", **мир**", "!"), pause=0.02, error=None):
        self.chunks, self.pause, self.error, self.requests = chunks, pause, error, []

    async def stream(self, messages):
        self.requests.append(messages)
        if self.error:
            raise LLMError(self.error)
        text = ""
        for chunk in self.chunks:
            await asyncio.sleep(self.pause)
            text += chunk
            yield text


def message(text, message_id=10, thread=0, user=123):
    body = {"message_id": message_id, "from": {"id": user, "is_bot": False}, "chat": {"id": user, "type": "private"}, "text": text}
    if thread:
        body.update(message_thread_id=thread, is_topic_message=True)
    return body


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api, self.llm = FakeApi(), FakeLLM()
        config = Config(telegram_token="t", openrouter_key="k", database=":memory:", draft_interval=0.01)
        self.bot = Bot(config, self.api, self.llm, Store(":memory:"))
        await self.bot.configure()

    async def test_commands_and_topics_detected(self):
        self.assertTrue(self.bot.topics_enabled)
        commands = self.api.sent("setMyCommands")[0]
        self.assertEqual([c["command"] for c in commands["commands"]], ["new", "reset", "help"])
        self.assertEqual(commands["scope"], {"type": "all_private_chats"})

    async def test_streams_thinking_then_drafts_then_rich_answer(self):
        await self.bot.on_message(message("Как дела?"))
        drafts = self.api.sent("sendMessageDraft")
        self.assertEqual(drafts[0]["text"], "")  # "Thinking…"
        self.assertTrue(all(d["can_stop"] and d["draft_id"] == 10 for d in drafts))
        self.assertTrue(self.api.sent("sendRichMessageDraft"))
        final = self.api.sent("sendRichMessage")[-1]
        self.assertEqual(final["rich_message"]["markdown"], "Привет, **мир**!")
        self.assertEqual(final["reply_parameters"]["message_id"], 10)

    async def test_history_is_kept_per_conversation_and_reset(self):
        await self.bot.on_message(message("Первый", 10))
        await self.bot.on_message(message("Второй", 11))
        self.assertEqual([m["role"] for m in self.llm.requests[-1]], ["system", "user", "assistant", "user"])
        await self.bot.on_message(message("В топике", 12, thread=5))
        self.assertEqual([m["role"] for m in self.llm.requests[-1]], ["system", "user"])
        await self.bot.on_message(message("/reset", 13))
        await self.bot.on_message(message("Третий", 14))
        self.assertEqual([m["role"] for m in self.llm.requests[-1]], ["system", "user"])

    async def test_stop_keeps_partial_answer(self):
        self.llm.chunks = ("Начало. ",) + ("ещё ",) * 100
        task = asyncio.create_task(self.bot.on_message(message("Длинно")))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if self.bot.generations and self.api.sent("sendRichMessageDraft"):
                break
        self.bot.on_stop({"chat": {"id": 123}, "draft_id": 10})
        await task
        final = self.api.sent("sendRichMessage")[-1]["rich_message"]["markdown"]
        self.assertTrue(final.startswith("Начало."))
        self.assertIn("Генерация остановлена", final)
        self.assertLess(len(final), 200)

    async def test_new_creates_topic_and_names_it_after_first_question(self):
        await self.bot.on_message(message("/new"))
        self.assertEqual(self.api.sent("createForumTopic")[0]["name"], "Новый чат")
        self.assertEqual(self.api.sent("sendMessage")[-1]["message_thread_id"], 77)
        await self.bot.on_message(message("План поездки в Казань", 11, thread=77))
        self.assertEqual(self.api.sent("editForumTopic")[0]["name"], "План поездки в Казань")
        self.assertEqual(self.api.sent("sendRichMessage")[-1]["message_thread_id"], 77)

    async def test_new_without_topics_resets(self):
        self.api.topics = False
        await self.bot.configure()
        await self.bot.on_message(message("Первый"))
        await self.bot.on_message(message("/new", 11))
        self.assertFalse(self.api.sent("createForumTopic"))
        await self.bot.on_message(message("Второй", 12))
        self.assertEqual(len(self.llm.requests[-1]), 2)

    async def test_plain_fallback_when_rich_rejected(self):
        self.api.fail |= {"sendRichMessage", "sendRichMessageDraft"}
        self.llm.chunks = ("x" * 5000,)
        await self.bot.on_message(message("Длинно"))
        self.assertEqual([len(p["text"]) for p in self.api.sent("sendMessage")], [4000, 1000])
        self.assertTrue(all(len(d["text"]) <= 4096 for d in self.api.sent("sendMessageDraft")))

    async def test_llm_error_is_reported(self):
        self.llm.error = "OpenRouter HTTP 402 Insufficient credits"
        await self.bot.on_message(message("Привет"))
        self.assertIn("402", self.api.sent("sendMessage")[-1]["text"])

    async def test_first_user_owns_bot_others_ignored(self):
        await self.bot.on_message(message("Привет", user=123))
        await self.bot.on_message(message("Привет", 11, user=999))
        self.assertEqual(len(self.llm.requests), 1)
        self.bot.config.allowed_ids = {999}
        await self.bot.on_message(message("Привет", 12, user=999))
        self.assertEqual(len(self.llm.requests), 2)

    async def test_groups_and_non_text_ignored_or_explained(self):
        group = message("Привет")
        group["chat"]["type"] = "group"
        await self.bot.on_message(group)
        self.assertEqual(self.api.calls[-1][0], "setMyCommands")
        photo = message("")
        await self.bot.on_message(photo)
        self.assertIn("текстовые", self.api.sent("sendMessage")[-1]["text"])

    def test_split_prefers_paragraphs(self):
        text = "a" * 30 + "\n\n" + "b" * 30
        self.assertEqual(split(text, 40), ["a" * 30, "b" * 30])
        self.assertEqual(split("x" * 90, 40), ["x" * 40, "x" * 40, "x" * 10])


class OpenRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_sse_stream(self):
        lines = [": OPENROUTER PROCESSING", "", "data: " + json.dumps({"choices": [{"delta": {"content": "Hel"}}]}), "",
                 "data: " + json.dumps({"choices": [{"delta": {"content": "lo"}}]}), "", "data: [DONE]", ""]
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, text="\n".join(lines))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            seen = [t async for t in OpenRouter("k", "m", client).stream([{"role": "user", "content": "hi"}])]
        self.assertEqual(seen, ["Hel", "Hello"])
        self.assertTrue(bodies[0]["stream"])

    async def test_http_error_message(self):
        def handler(request):
            return httpx.Response(402, json={"error": {"message": "Insufficient credits"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(LLMError, "402 Insufficient credits"):
                [t async for t in OpenRouter("k", "m", client).stream([])]


if __name__ == "__main__":
    unittest.main()
