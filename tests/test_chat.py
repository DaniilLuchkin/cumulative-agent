import asyncio
import json
import unittest

import httpx

from cumulative_agent.config import Limits, Settings
from cumulative_agent.domain import Completion, Model
from cumulative_agent.providers import OpenRouter
from cumulative_agent.service import Service
from cumulative_agent import telegram as telegram_module


class StreamingProvider:
    def __init__(self, chunks=("Привет", ", **мир**", "!"), pause=0.0):
        self.chunks, self.pause = chunks, pause
        self.calls = []

    async def complete(self, model, prompt, output_limit, timeout, *, messages=None, on_delta=None):
        self.calls.append(messages)
        text = ""
        for chunk in self.chunks:
            text += chunk
            if on_delta:
                on_delta(text)
            await asyncio.sleep(self.pause)
        return Completion(text=text, input_tokens=40, output_tokens=10, cost=(40 * model.input_price + 10 * model.output_price) / 1_000_000)


def settings(**overrides):
    base = dict(database=":memory:", api_token="test-token-that-is-long-enough-123", telegram_bot_token="1:test", telegram_owner_ids=[123],
                limits=Limits(daily_cost_limit=1, background_cost_limit=0, task_cost_limit=.1, min_free_disk_bytes=0,
                              min_available_memory_bytes=0, max_load_per_cpu=1000, max_background_backlog=0, poll_seconds=60),
                models=[Model(id="mock/chat", provider="mock", enabled=True, input_price=.1, output_price=.2, context_size=64000, max_output_tokens=2048)])
    base.update(overrides)
    return Settings(**base)


def message(text, update_id=10, thread=None, owner=123):
    body = {"message_id": update_id * 10, "from": {"id": owner}, "chat": {"id": owner, "type": "private"}, "text": text}
    if thread:
        body.update(message_thread_id=thread, is_topic_message=True)
    return {"update_id": update_id, "message": body}


class ChatModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        telegram_module.DRAFT_INTERVAL = 0.01
        telegram_module.BLOCKED_GRACE = 0.05
        self.provider = StreamingProvider(pause=0.03)
        self.service = Service(settings(), providers={"mock": self.provider})
        self.calls = []
        self.fail = set()

        async def call(method, payload):
            self.calls.append((method, payload))
            if method in self.fail:
                raise RuntimeError("Telegram rejected request")
            if method == "createForumTopic":
                return {"message_thread_id": 77, "name": payload["name"]}
            if method == "getMe":
                return {"id": 1, "is_bot": True, "has_topics_enabled": True}
            return True
        self.service.telegram.call = call
        await self.service.start()

    async def asyncTearDown(self):
        await self.service.stop()

    async def settle(self):
        for _ in range(300):
            await asyncio.sleep(0.01)
            if not self.service.telegram._streams:
                return
        self.fail_test = True
        raise AssertionError("stream did not finish")

    def methods(self, name):
        return [payload for method, payload in self.calls if method == name]

    async def test_streams_drafts_then_persists_rich_answer_with_history(self):
        await self.service.telegram.handle(message("Как дела?"))
        await self.settle()
        drafts = self.methods("sendMessageDraft")
        self.assertEqual(drafts[0]["text"], "")  # "Thinking…" placeholder first
        self.assertTrue(all(d["can_stop"] for d in drafts))
        rich_drafts = self.methods("sendRichMessageDraft")
        self.assertTrue(rich_drafts)
        self.assertEqual(rich_drafts[0]["draft_id"], 10)
        final = self.methods("sendRichMessage")[-1]
        self.assertEqual(final["rich_message"]["markdown"], "Привет, **мир**!")
        self.assertEqual(final["reply_parameters"]["message_id"], 100)
        # Second turn carries the first exchange as context.
        await self.service.telegram.handle(message("А подробнее?", 11))
        await self.settle()
        roles = [m["role"] for m in self.provider.calls[-1]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertEqual(self.provider.calls[-1][-1]["content"], "А подробнее?")
        # Chat turns do not clutter the task list or trigger generic notifications.
        await self.service.telegram.notify_results()
        self.assertFalse([p for p in self.methods("sendMessage") if "Задача" in p["text"]])
        page = self.service.repo.task_page(status="all", kind="all", project=None, search="", sort="created", due="all",
                                           day_start=__import__("datetime").datetime.now(), day_end=__import__("datetime").datetime.now(), limit=10, offset=0)
        self.assertEqual(page["total"], 0)

    async def test_redelivered_update_is_answered_once(self):
        await self.service.telegram.handle(message("Привет"))
        await self.service.telegram.handle(message("Привет"))
        await self.settle()
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(self.methods("sendRichMessage")), 1)

    async def test_stop_button_cancels_and_keeps_partial(self):
        self.provider.chunks = ("Часть 1. ",) + ("ещё ",) * 50
        self.provider.pause = 0.02
        await self.service.telegram.handle(message("Длинный ответ"))
        for _ in range(100):
            await asyncio.sleep(0.01)
            generation = self.service.telegram.generations.get(10)
            if generation and generation.text:
                break
        handled = await self.service.telegram.handle({"update_id": 11, "stopped_message_generation": {"chat": {"id": 123, "type": "private"}, "draft_id": 10}})
        self.assertTrue(handled)
        await self.settle()
        task = self.service.repo.tasks()[0]
        self.assertEqual(task.status, "cancelled")
        final = self.methods("sendRichMessage")[-1]["rich_message"]["markdown"]
        self.assertTrue(final.startswith("Часть 1."))
        self.assertIn("Генерация остановлена", final)
        # Foreign chats cannot stop the owner's generation.
        self.assertFalse(self.service.telegram.handle_stop({"chat": {"id": 999}, "draft_id": 10}))

    async def test_topics_keep_separate_contexts_and_autoname(self):
        await self.service.telegram.configure()
        self.assertTrue(self.service.telegram.topics_enabled)
        commands = self.methods("setMyCommands")[0]
        self.assertIn("new", [c["command"] for c in commands["commands"]])
        await self.service.telegram.handle(message("/new"))
        self.assertEqual(self.methods("createForumTopic")[0]["name"], "Новый чат")
        self.assertEqual(self.methods("sendMessage")[-1]["message_thread_id"], 77)
        await self.service.telegram.handle(message("Вопрос в основном чате", 20))
        await self.settle()
        await self.service.telegram.handle(message("План поездки в Казань", 21, thread=77))
        await self.settle()
        self.assertEqual([m["role"] for m in self.provider.calls[-1]], ["system", "user"])
        self.assertEqual(self.methods("sendRichMessage")[-1]["message_thread_id"], 77)
        self.assertEqual(self.methods("editForumTopic")[0]["name"], "План поездки в Казань")

    async def test_reset_clears_context(self):
        await self.service.telegram.handle(message("Первый"))
        await self.settle()
        await self.service.telegram.handle(message("/reset", 11))
        await self.service.telegram.handle(message("Второй", 12))
        await self.settle()
        self.assertEqual([m["role"] for m in self.provider.calls[-1]], ["system", "user"])

    async def test_budget_block_is_reported_not_queued(self):
        self.service.settings.limits.daily_cost_limit = 0
        await self.service.telegram.handle(message("Привет"))
        await self.settle()
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.service.repo.tasks()[0].status, "cancelled")
        text = self.methods("sendRichMessage")[-1]["rich_message"]["markdown"]
        self.assertIn("Сейчас не могу ответить", text)

    async def test_plain_fallback_when_rich_messages_rejected(self):
        self.fail |= {"sendRichMessage", "sendRichMessageDraft"}
        self.provider.chunks = ("x" * 5000,)
        await self.service.telegram.handle(message("Длинно"))
        await self.settle()
        plain = [p for p in self.methods("sendMessage")]
        self.assertEqual([len(p["text"]) for p in plain], [4000, 1000])
        self.assertTrue(all(len(d["text"]) <= 4096 for d in self.methods("sendMessageDraft")))

    async def test_task_hint_and_command_still_create_tasks(self):
        await self.service.telegram.handle(message("Напомни купить хлеб"))
        await self.service.telegram.handle(message("/task Подготовить отчёт", 11))
        handlers = sorted(t.handler for t in self.service.repo.tasks())
        self.assertEqual(handlers, ["llm", "llm"])
        self.assertEqual(self.provider.calls, [])

    async def test_without_enabled_model_falls_back_to_task_intake(self):
        self.service.repo.execute("UPDATE models SET payload=json_set(payload,'$.enabled',json('false'))")
        await self.service.telegram.handle(message("Обычный текст"))
        self.assertEqual(self.service.repo.tasks()[0].handler, "llm")


class OpenRouterStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_sse_stream_parses_deltas_and_usage(self):
        lines = [": OPENROUTER PROCESSING", "",
                 "data: " + json.dumps({"choices": [{"delta": {"content": "Hel"}}]}), "",
                 "data: " + json.dumps({"choices": [{"delta": {"content": "lo"}}]}), "",
                 "data: " + json.dumps({"choices": [{"delta": {}}], "usage": {"prompt_tokens": 12, "completion_tokens": 2, "cost": 0.0001}}), "",
                 "data: [DONE]", ""]
        seen, bodies = [], []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, text="\n".join(lines), headers={"Content-Type": "text/event-stream"})
        model = Model(id="m", provider="openrouter", enabled=True, input_price=1, output_price=1)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            completion = await OpenRouter("fake", client).complete(model, "{}", 100, 5, messages=[{"role": "user", "content": "hi"}], on_delta=seen.append)
        self.assertEqual(completion.text, "Hello")
        self.assertEqual((completion.input_tokens, completion.output_tokens, completion.cost), (12, 2, 0.0001))
        self.assertEqual(seen, ["Hel", "Hello"])
        self.assertTrue(bodies[0]["stream"])
        self.assertFalse(bodies[0]["provider"]["allow_fallbacks"])

    async def test_stream_without_usage_is_rejected(self):
        body = "data: " + json.dumps({"choices": [{"delta": {"content": "x"}}]}) + "\n\ndata: [DONE]\n\n"
        model = Model(id="m", provider="openrouter", enabled=True)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body))) as client:
            with self.assertRaises(Exception):
                await OpenRouter("fake", client).complete(model, "{}", 100, 5, messages=[], on_delta=lambda t: None)


if __name__ == "__main__":
    unittest.main()
