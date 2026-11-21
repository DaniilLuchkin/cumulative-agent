import asyncio
import json
import time
import unittest
from zoneinfo import ZoneInfo

import httpx

from llm_bot.agent import next_occurrence
from llm_bot.bot import Bot, Config, split
from llm_bot.llm import LLMError, OpenRouter, Result
from llm_bot.store import Store
from llm_bot.telegram import TelegramError


class FakeApi:
    def __init__(self, topics=True):
        self.calls, self.fail, self.topics, self.next_id = [], set(), topics, 1000
        self.files = {}

    async def call(self, method, http_timeout=30, **params):
        self.calls.append((method, params))
        if method in self.fail:
            raise TelegramError(method, "Bad Request")
        if method == "getMe":
            return {"id": 1, "username": "test_bot", "has_topics_enabled": self.topics}
        if method == "createForumTopic":
            return {"message_thread_id": 77, "name": params["name"]}
        if method == "getFile":
            return {"file_path": params["file_id"]}
        if method == "answerGuestQuery":
            return {"inline_message_id": "guest-1"}
        if method in ("sendMessage", "sendRichMessage"):
            self.next_id += 1
            return {"message_id": self.next_id}
        return True

    async def download(self, path):
        return self.files[path]

    def sent(self, method):
        return [p for m, p in self.calls if m == method]


def call(name, **args):
    return {"id": "call_" + name, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class FakeLLM:
    """Plays scripted results; a plain string means a streamed text answer."""

    def __init__(self, script=None, pause=0.02):
        self.script, self.pause, self.requests, self.models, self.error = list(script or []), pause, [], [], None
        self.completions = ["«Поездка в Казань.»"]

    async def catalog(self):
        return {"deepseek/deepseek-v4.1-flash": {"price": (0.05, 0.2), "input": ["text", "image"]},
                "openrouter/auto": {"price": None, "input": ["text"]},
                "google/gemini-4-hydrogen": {"price": (1.5, 7.5), "input": ["text", "image", "audio", "file"]}}

    async def complete(self, messages, model=None, max_tokens=64):
        self.last_complete = messages
        if "JSON array" in messages[0]["content"]:
            return '["Почему так?", "Пример", "Короче"]'
        return self.completions[0]

    async def transcribe(self, audio, fmt, model):
        self.transcribed = (audio, fmt, model)
        return "Какая завтра погода"

    async def run(self, messages, model=None, tools=None, plugins=None, on_text=None):
        self.requests.append(json.loads(json.dumps(messages)))
        self.models.append(model)
        self.plugins = plugins
        if self.error:
            raise LLMError(self.error)
        step = self.script.pop(0) if self.script else ("Привет", ", **мир**", "!")
        if isinstance(step, Result):
            return step
        text = ""
        for chunk in step:
            await asyncio.sleep(self.pause)
            text += chunk
            if on_text:
                on_text(text)
        return Result(text=text)


def message(text, message_id=10, thread=0, user=123, **extra):
    body = {"message_id": message_id, "from": {"id": user, "is_bot": False}, "chat": {"id": user, "type": "private"}, **extra}
    if text is not None:
        body["text"] = text
    if thread:
        body.update(message_thread_id=thread, is_topic_message=True)
    return body


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api, self.llm = FakeApi(), FakeLLM()
        config = Config(telegram_token="t", openrouter_key="k", database=":memory:", draft_interval=0.01, suggestions=False)
        self.bot = Bot(config, self.api, self.llm, Store(":memory:"))
        await self.bot.configure()

    async def asyncTearDown(self):
        await self.bot.stop()
        self.bot.store.close()

    async def settle(self):
        for _ in range(100):
            await asyncio.sleep(0.01)
            if not [t for t in self.bot.tasks if not t.done()]:
                return

    def roles(self, index=-1):
        return [m["role"] for m in self.llm.requests[index]]

    # --- basics -------------------------------------------------------------------

    async def test_commands_and_topics_detected(self):
        self.assertTrue(self.bot.topics_enabled)
        commands = self.api.sent("setMyCommands")[0]
        self.assertEqual([c["command"] for c in commands["commands"]], ["new", "tasks", "reset", "settings", "timezone", "info", "help"])

    async def test_streams_thinking_then_drafts_then_rich_answer_with_reactions(self):
        await self.bot.on_message(message("Как дела?"))
        drafts = self.api.sent("sendMessageDraft")
        self.assertEqual(drafts[0]["text"], "")  # "Thinking…"
        self.assertTrue(all(d["can_stop"] and d["draft_id"] == 10 for d in self.api.sent("sendRichMessageDraft")))
        final = self.api.sent("sendRichMessage")[-1]
        self.assertEqual(final["rich_message"]["markdown"], "Привет, **мир**!")
        self.assertEqual(final["reply_parameters"]["message_id"], 10)
        self.assertEqual(final["reply_markup"]["inline_keyboard"][-1][0]["text"], "🔄 Перегенерировать")
        reactions = [r["reaction"] for r in self.api.sent("setMessageReaction")]
        self.assertEqual(reactions, [[{"type": "emoji", "emoji": "👀"}], []])
        self.assertIn("Current local time of the user", self.llm.requests[-1][0]["content"])

    async def test_history_is_kept_per_conversation_and_reset(self):
        await self.bot.on_message(message("Первый", 10))
        await self.bot.on_message(message("Второй", 11))
        self.assertEqual(self.roles(), ["system", "user", "assistant", "user"])
        await self.bot.on_message(message("В топике", 12, thread=5))
        self.assertEqual(self.roles(), ["system", "user"])
        await self.bot.on_message(message("/reset", 13))
        await self.bot.on_message(message("Третий", 14))
        self.assertEqual(self.roles(), ["system", "user"])

    async def test_stop_keeps_partial_answer(self):
        self.llm.script = [("Начало. ",) + ("ещё ",) * 100]
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

    async def test_plain_fallback_when_rich_rejected(self):
        self.api.fail |= {"sendRichMessage", "sendRichMessageDraft"}
        self.llm.script = [("x" * 5000,)]
        await self.bot.on_message(message("Длинно"))
        plain = self.api.sent("sendMessage")
        self.assertEqual([len(p["text"]) for p in plain], [4000, 1000])
        self.assertIn("reply_markup", plain[-1])

    async def test_llm_error_is_reported(self):
        self.llm.error = "OpenRouter HTTP 402 Insufficient credits"
        await self.bot.on_message(message("Привет"))
        self.assertIn("402", self.api.sent("sendMessage")[-1]["text"])

    async def test_first_user_owns_bot_others_ignored(self):
        await self.bot.on_message(message("Привет", user=123))
        await self.bot.on_message(message("Привет", 11, user=999))
        self.assertEqual(len(self.llm.requests), 1)

    # --- topics -------------------------------------------------------------------

    async def test_new_creates_topic_and_names_it(self):
        await self.bot.on_message(message("/new"))
        self.assertEqual(self.api.sent("createForumTopic")[0]["name"], "Новый чат")
        await self.bot.on_message(message("План поездки в Казань", 11, thread=77))
        self.assertEqual(self.api.sent("editForumTopic")[0]["name"], "Поездка в Казань")

    async def test_user_created_topic_is_renamed_service_messages_ignored(self):
        service = message(None, 30, thread=88, forum_topic_created={"name": "New chat", "is_name_implicit": True})
        await self.bot.on_message(service)
        self.assertFalse(self.api.sent("sendMessage"))
        await self.bot.on_message(message("Как сварить борщ", 31, thread=88))
        await self.bot.on_message(message("А без свёклы?", 32, thread=88))
        self.assertEqual(len(self.api.sent("editForumTopic")), 1)

    # --- agent tools: reminders -------------------------------------------------------

    async def test_reminder_tool_schedules_reacts_and_delivers(self):
        self.bot.store.set("timezone:123", "UTC")
        when = __import__("datetime").datetime.fromtimestamp(time.time() + 7200, ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M")
        self.llm.script = [Result(tool_calls=[call("set_reminder", text="Позвонить врачу", time=when)]), ("Напомню ", "в срок.")]
        await self.bot.on_message(message("Напомни через 2 часа позвонить врачу"))
        tool_reply = json.loads(self.llm.requests[-1][-1]["content"])
        self.assertTrue(tool_reply["scheduled_for"].startswith("!["))
        self.assertIn("tg://time?unix=", tool_reply["scheduled_for"])
        self.assertEqual(self.api.sent("setMessageReaction")[-1]["reaction"], [{"type": "emoji", "emoji": "🫡"}])
        self.assertTrue(any(d.get("rich_message", {}).get("html", "").startswith("<tg-thinking>Ставлю") for d in self.api.sent("sendRichMessageDraft")))
        # Deliver it when due.
        self.bot.store.run("UPDATE reminders SET due_at=?", (time.time() - 1,))
        await self.bot.agent.tick()
        delivered = self.api.sent("sendRichMessage")[-1]
        self.assertEqual(delivered["rich_message"]["markdown"], "⏰ Позвонить врачу")
        self.assertEqual(delivered["reply_markup"]["inline_keyboard"][0][1]["callback_data"], "snooze:1:3600")
        await self.bot.agent.tick()
        self.assertEqual(len([p for p in self.api.sent("sendRichMessage") if p["rich_message"]["markdown"].startswith("⏰")]), 1)
        # Snooze button creates a new one-shot reminder.
        await self.bot.on_callback({"id": "q", "from": {"id": 123}, "data": "snooze:1:3600", "message": {"message_id": 5, "chat": {"id": 123}}})
        self.assertEqual(self.api.sent("answerCallbackQuery")[-1]["text"], "Напомню через час")
        self.assertEqual(len(self.bot.agent.scheduled(123)["reminders"]), 1)

    async def test_reminder_in_the_past_is_rejected_to_the_model(self):
        self.llm.script = [Result(tool_calls=[call("set_reminder", text="x", time="2020-01-01 09:00")]), ("Это время прошло.",)]
        await self.bot.on_message(message("Напомни вчера"))
        self.assertIn("already passed", self.llm.requests[-1][-1]["content"])
        self.assertEqual(self.bot.agent.scheduled(123)["reminders"], [])

    def test_repeat_keeps_wall_clock_and_skips_weekends(self):
        zone = ZoneInfo("Europe/Moscow")
        friday = __import__("datetime").datetime(2030, 1, 4, 9, 0, tzinfo=zone).timestamp()  # a Friday
        following = __import__("datetime").datetime.fromtimestamp(next_occurrence(friday, "weekdays", zone), zone)
        self.assertEqual((following.weekday(), following.hour), (0, 9))
        self.assertIsNone(next_occurrence(friday, "none", zone))

    # --- agent tools: watches ---------------------------------------------------------

    async def test_watch_gets_topic_pinned_status_and_reports(self):
        self.llm.script = [Result(tool_calls=[call("start_watch", title="Цены на билеты", instructions="Билеты Москва–Казань дешевле 3000", every_hours=6)]),
                           ("Запустил наблюдение.",), ("Нашёл билет за 2500 ₽ [^1]",)]
        await self.bot.on_message(message("Следи за билетами в Казань"))
        self.assertEqual(self.api.sent("createForumTopic")[-1]["name"], "👁 Цены на билеты")
        status = [p for p in self.api.sent("sendRichMessage") if p["rich_message"]["markdown"].startswith("👁")][0]
        self.assertEqual(status["message_thread_id"], 77)
        self.assertEqual([b["callback_data"] for b in status["reply_markup"]["inline_keyboard"][0]], ["watch:run:1", "watch:stop:1"])
        self.assertEqual(len(self.api.sent("pinChatMessage")), 1)
        self.assertIn("https://t.me/test_bot/77", self.llm.requests[1][-1]["content"])
        await self.bot.agent.tick()
        await self.settle()
        report = self.api.sent("sendRichMessage")[-1]
        self.assertEqual((report["message_thread_id"], report["rich_message"]["markdown"]), (77, "Нашёл билет за 2500 ₽ [^1]"))
        self.assertIn("Последняя проверка", self.api.sent("editMessageText")[-1]["rich_message"]["markdown"])
        # Nothing new: no message, only the status refresh.
        self.llm.script = [("NO_UPDATE",)]
        before = len(self.api.sent("sendRichMessage"))
        await self.bot.agent.run_watch(1)
        self.assertEqual(len(self.api.sent("sendRichMessage")), before)
        # Stop button.
        await self.bot.on_callback({"id": "q", "from": {"id": 123}, "data": "watch:stop:1", "message": {"message_id": 5, "chat": {"id": 123}}})
        self.assertIn("Остановлено", self.api.sent("editMessageText")[-1]["rich_message"]["markdown"])
        self.assertEqual(self.bot.agent.scheduled(123)["watches"], [])

    async def test_tasks_command_lists_and_cancels(self):
        self.bot.store.run("INSERT INTO reminders(chat_id,thread_id,text,due_at,repeat) VALUES(123,0,'Купить хлеб',?, 'daily')", (time.time() + 600,))
        await self.bot.on_message(message("/tasks"))
        listing = self.api.sent("sendMessage")[-1]
        self.assertIn("Купить хлеб", listing["text"])
        self.assertEqual(listing["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "cancel:r1")
        await self.bot.on_callback({"id": "q", "from": {"id": 123}, "data": "cancel:r1", "message": {"message_id": 5, "chat": {"id": 123}}})
        self.assertIn("Нет напоминаний", self.api.sent("editMessageText")[-1]["text"])

    # --- buttons, regeneration, edits ---------------------------------------------------

    async def test_suggestions_attach_and_ask(self):
        self.bot.config.suggestions = True
        await self.bot.on_message(message("Что такое SQLite?"))
        await self.settle()
        markup = self.api.sent("editMessageReplyMarkup")[-1]["reply_markup"]["inline_keyboard"]
        self.assertEqual([row[0]["text"] for row in markup], ["💬 Почему так?", "💬 Пример", "💬 Короче", "🔄 Перегенерировать"])
        await self.bot.on_callback({"id": "q", "from": {"id": 123}, "data": markup[1][0]["callback_data"], "message": {"message_id": 5, "chat": {"id": 123}}})
        await self.settle()
        self.assertEqual(self.llm.requests[-1][-1]["content"], "Пример")
        self.assertTrue(self.api.sent("sendRichMessage")[-1]["rich_message"]["markdown"].startswith("> Пример"))

    async def test_regenerate_button_replaces_last_answer_only(self):
        await self.bot.on_message(message("Первый", 10))
        await self.bot.on_message(message("Второй", 11))
        await self.bot.on_callback({"id": "q", "from": {"id": 123}, "data": "regen:1", "message": {"message_id": 5, "chat": {"id": 123}}})
        self.assertIn("только последний", self.api.sent("answerCallbackQuery")[-1]["text"])
        old = self.api.sent("sendRichMessage")[-1]
        await self.bot.on_callback({"id": "q", "from": {"id": 123}, "data": "regen:2", "message": {"message_id": 5, "chat": {"id": 123}}})
        await self.settle()
        self.assertEqual(self.api.sent("deleteMessage")[-1]["message_id"], 1002)
        self.assertEqual(self.roles(), ["system", "user", "assistant", "user"])  # no duplicated question
        self.assertIsNot(self.api.sent("sendRichMessage")[-1], old)

    async def test_editing_last_question_regenerates(self):
        await self.bot.on_message(message("Столица Франции?", 10))
        await self.bot.on_edit(message("Столица Германии?", 10))
        self.assertEqual(self.llm.requests[-1][-1]["content"], "Столица Германии?")
        self.assertEqual(self.roles(), ["system", "user"])
        self.assertEqual(len(self.api.sent("deleteMessage")), 1)

    # --- settings -------------------------------------------------------------------

    async def test_settings_has_two_buttons_and_a_model_view(self):
        self.assertEqual(self.bot.models, ["openrouter/auto", "deepseek/deepseek-v4.1-flash", "openai/gpt-6.2-sol"])
        await self.bot.on_message(message("/settings"))
        sent = self.api.sent("sendMessage")[-1]
        self.assertEqual([row[0]["callback_data"] for row in sent["reply_markup"]["inline_keyboard"]], ["settings:model", "settings:timezone"])
        self.assertIn("Часовой пояс: UTC", sent["text"])
        press = lambda data: self.bot.on_callback({"id": "q", "from": {"id": 123}, "data": data, "message": {"message_id": 5, "chat": {"id": 123}}})
        await press("settings:model")
        view = self.api.sent("editMessageText")[-1]
        rows = view["reply_markup"]["inline_keyboard"]
        flash = self.bot.models.index("deepseek/deepseek-v4.1-flash")
        self.assertIn("$0.05/$0.20", rows[flash][0]["text"])
        self.assertEqual(rows[-1][0]["callback_data"], "settings:main")
        await press(f"model:{flash}")
        main = self.api.sent("editMessageText")[-1]
        self.assertIn("Модель: deepseek/deepseek-v4.1-flash", main["text"])
        self.assertEqual(main["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "settings:model")
        await press("settings:timezone")
        self.assertTrue(self.api.sent("sendMessage")[-1]["reply_markup"]["keyboard"][0][0]["request_location"])
        await self.bot.on_message(message("Привет", 11))
        self.assertEqual(self.llm.models[-1], "deepseek/deepseek-v4.1-flash")

    async def test_locations_saved_by_an_older_version_are_deleted(self):
        self.bot.store.set("location:123", {"latitude": 1, "longitude": 2})
        await self.bot.configure()
        self.assertIsNone(self.bot.store.get("location:123"))

    async def test_default_timezone_is_utc_and_model_is_told_it_is_unset(self):
        self.assertEqual(self.bot.zone(123).key, "UTC")
        await self.bot.on_message(message("Привет"))
        self.assertIn("time zone is not set", self.llm.requests[-1][0]["content"])

    async def test_location_sets_timezone_and_is_not_kept(self):
        async def lookup(client, lat, lon):
            self.looked_up = (lat, lon)
            return "Asia/Novosibirsk", True
        original, __import__("llm_bot.geo").geo.timezone_at = __import__("llm_bot.geo").geo.timezone_at, lookup
        try:
            await self.bot.on_message(message("/timezone"))
            button = self.api.sent("sendMessage")[-1]["reply_markup"]["keyboard"][0][0]
            self.assertTrue(button["request_location"])
            await self.bot.on_message(message(None, 11, location={"latitude": 55.03, "longitude": 82.92}))
        finally:
            __import__("llm_bot.geo").geo.timezone_at = original
        self.assertEqual(self.bot.zone(123).key, "Asia/Novosibirsk")
        reply = self.api.sent("sendMessage")[-1]
        self.assertEqual(reply["text"], "🕒 Часовой пояс: Asia/Novosibirsk (UTC+7).")
        stored = [tuple(row) for row in self.bot.store.all("SELECT key, value FROM settings")]
        self.assertFalse([row for row in stored if "55.03" in row[1] or "82.92" in row[1]])
        await self.bot.on_message(message("/settings", 13))
        settings = self.api.sent("sendMessage")[-1]
        self.assertEqual(settings["reply_markup"]["inline_keyboard"][-1][0]["callback_data"], "settings:utc")
        await self.bot.on_callback({"id": "q", "from": {"id": 123}, "data": "settings:utc", "message": {"message_id": 5, "chat": {"id": 123}}})
        self.assertEqual(self.bot.zone(123).key, "UTC")
        self.bot.store.set("timezone:123", "Asia/Novosibirsk")
        self.assertEqual(reply["reply_markup"], {"remove_keyboard": True})
        self.assertEqual(self.llm.requests, [])  # a location is not a question
        await self.bot.on_message(message("Привет", 14))
        self.assertNotIn("time zone is not set", self.llm.requests[-1][0]["content"])

    async def test_strangers_cannot_press_buttons(self):
        await self.bot.on_message(message("Привет"))
        await self.bot.on_callback({"id": "q", "from": {"id": 999}, "data": "model:1", "message": {"message_id": 5, "chat": {"id": 999}}})
        self.assertIsNone(self.bot.store.get("model:999"))

    # --- media ----------------------------------------------------------------------

    async def test_photo_goes_to_a_vision_model(self):
        self.api.files["photo-big"] = b"\xff\xd8jpeg"
        await self.bot.on_message(message(None, caption="Что это?", photo=[{"file_id": "photo-small"}, {"file_id": "photo-big", "file_size": 10}]))
        parts = self.llm.requests[-1][-1]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "Что это?"})
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(self.llm.models[-1], "deepseek/deepseek-v4.1-flash")  # openrouter/auto is text-only in the catalog
        await self.bot.on_message(message("А цвет?", 11))
        self.assertEqual(self.llm.requests[-1][1]["content"], "[фото] Что это?")  # history keeps a text placeholder

    async def test_voice_is_transcribed_and_quoted(self):
        self.api.files["voice-1"] = b"OggS"
        await self.bot.on_message(message(None, voice={"file_id": "voice-1", "mime_type": "audio/ogg", "file_size": 4}))
        self.assertEqual(self.llm.transcribed[1:], ("ogg", "openai/whisper-large-v3-turbo"))
        self.assertEqual(self.llm.requests[-1][-1]["content"], "Какая завтра погода")
        self.assertTrue(self.api.sent("sendRichMessage")[-1]["rich_message"]["markdown"].startswith("> 🎙 Какая завтра погода"))

    async def test_pdf_and_text_documents(self):
        self.api.files["doc-pdf"] = b"%PDF-1.7"
        await self.bot.on_message(message(None, document={"file_id": "doc-pdf", "file_name": "a.pdf", "mime_type": "application/pdf"}))
        self.assertEqual(self.llm.requests[-1][-1]["content"][1]["type"], "file")
        self.assertEqual(self.llm.plugins[0]["id"], "file-parser")
        self.api.files["doc-txt"] = "строка".encode()
        await self.bot.on_message(message(None, 11, caption="Проверь", document={"file_id": "doc-txt", "file_name": "notes.md", "mime_type": "text/markdown"}))
        self.assertIn("строка", self.llm.requests[-1][-1]["content"])
        await self.bot.on_message(message(None, 12, document={"file_id": "x", "file_name": "a.exe", "mime_type": "application/octet-stream"}))
        self.assertIn("Не умею читать", self.api.sent("sendMessage")[-1]["text"])
        self.assertNotIn({"file_id": "x"}, self.api.sent("getFile"))  # rejected before downloading

    # --- inline and guest -----------------------------------------------------------------

    async def test_inline_query_then_streamed_edit(self):
        await self.bot.on_message(message("Привет"))  # claim owner
        await self.bot.dispatch({"update_id": 1, "inline_query": {"id": "iq", "from": {"id": 123}, "query": "курс евро"}})
        result = self.api.sent("answerInlineQuery")[-1]["results"][0]
        self.assertEqual(result["input_message_content"]["message_text"], "⏳ курс евро")
        self.assertTrue(result["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("test_bot"))
        await self.bot.dispatch({"update_id": 2, "chosen_inline_result": {"result_id": result["id"], "from": {"id": 123}, "query": "курс евро", "inline_message_id": "im-1"}})
        final = self.api.sent("editMessageText")[-1]
        self.assertEqual(final["inline_message_id"], "im-1")
        self.assertEqual(final["rich_message"]["markdown"], "> курс евро\n\nПривет, **мир**!")
        # Strangers get nothing.
        await self.bot.dispatch({"update_id": 3, "inline_query": {"id": "iq2", "from": {"id": 999}, "query": "x"}})
        self.assertEqual(self.api.sent("answerInlineQuery")[-1]["results"], [])

    async def test_guest_mention_answers_with_context(self):
        await self.bot.on_message(message("Привет"))  # claim owner
        guest = {"message_id": 5, "guest_query_id": "gq", "from": {"id": 123}, "chat": {"id": -100, "type": "supergroup"},
                 "text": "@test_bot переведи", "reply_to_message": {"message_id": 4, "text": "Bonjour à tous"}}
        await self.bot.dispatch({"update_id": 4, "guest_message": guest})
        self.assertEqual(self.api.sent("answerGuestQuery")[-1]["guest_query_id"], "gq")
        self.assertIn("Bonjour à tous", self.llm.requests[-1][-1]["content"])
        self.assertTrue(self.llm.requests[-1][-1]["content"].endswith("переведи"))
        self.assertEqual(self.api.sent("editMessageText")[-1]["inline_message_id"], "guest-1")

    async def test_geo_lookup_rounds_coordinates_and_falls_back(self):
        from llm_bot import geo
        seen = []

        def handler(request):
            seen.append(dict(request.url.params))
            return httpx.Response(200, json={"timezone": "Europe/Moscow"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(await geo.timezone_at(client, 55.7558, 37.6173), ("Europe/Moscow", True))
        self.assertEqual((seen[0]["latitude"], seen[0]["longitude"]), ("55.8", "37.6"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
            self.assertEqual(await geo.timezone_at(client, 55.7, 37.6), ("Etc/GMT-3", False))
        self.assertEqual(geo.by_longitude(-74), "Etc/GMT+5")
        self.assertEqual(geo.by_longitude(1), "UTC")

    async def test_info_is_a_rich_guide_with_working_buttons(self):
        await self.bot.on_message(message("/info"))
        sent = self.api.sent("sendRichMessage")[-1]
        text = sent["rich_message"]["markdown"]
        self.assertTrue(text.startswith("# Что умеет бот"))
        for section in ("## 💬 Разговор", "## 📎 Файлы и голос", "## ⏰ Напоминания", "## 👁 Фоновые задачи", "## 🌍 В других чатах", "<details>", "@test_bot"):
            self.assertIn(section, text)
        self.assertIn("часовой пояс **UTC**", text)
        self.assertEqual(self.llm.requests, [])
        buttons = [b["callback_data"] for row in sent["reply_markup"]["inline_keyboard"] for b in row]
        self.assertEqual(buttons, ["cmd:settings", "cmd:timezone", "cmd:tasks", "cmd:new"])
        await self.bot.on_callback({"id": "q", "from": {"id": 123}, "data": "cmd:timezone", "message": {"message_id": 5, "chat": {"id": 123}}})
        await self.settle()
        self.assertTrue(self.api.sent("sendMessage")[-1]["reply_markup"]["keyboard"][0][0]["request_location"])
        await self.bot.on_callback({"id": "q", "from": {"id": 123}, "data": "cmd:reset", "message": {"message_id": 5, "chat": {"id": 123}}})
        self.assertEqual(len(self.api.sent("sendMessage")), 1)  # only whitelisted commands run from buttons

    def test_split_prefers_paragraphs(self):
        self.assertEqual(split("a" * 30 + "\n\n" + "b" * 30, 40), ["a" * 30, "b" * 30])
        self.assertEqual(split("x" * 90, 40), ["x" * 40, "x" * 40, "x" * 10])


class OpenRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_text_and_tool_call_deltas(self):
        chunks = [{"model": "deepseek/x", "choices": [{"delta": {"content": "Hel"}}]},
                  {"choices": [{"delta": {"content": "lo"}}]},
                  {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "set_rem", "arguments": '{"te'}}]}}]},
                  {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "inder", "arguments": 'xt":"a"}'}}]}}]}]
        body = "\n".join([": OPENROUTER PROCESSING", ""] + [f"data: {json.dumps(c)}\n" for c in chunks] + ["data: [DONE]", ""])
        seen, bodies = [], []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, text=body)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await OpenRouter("k", "m", client).run([{"role": "user", "content": "hi"}], tools=[{"type": "openrouter:web_search"}], on_text=seen.append)
        self.assertEqual((result.text, result.model), ("Hello", "deepseek/x"))
        self.assertEqual(result.tool_calls, [{"id": "c1", "type": "function", "function": {"name": "set_reminder", "arguments": '{"text":"a"}'}}])
        self.assertEqual(seen, ["Hel", "Hello"])
        self.assertTrue(bodies[0]["stream"])
        self.assertEqual(bodies[0]["tools"], [{"type": "openrouter:web_search"}])

    async def test_http_error_message(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(402, json={"error": {"message": "Insufficient credits"}}))) as client:
            with self.assertRaisesRegex(LLMError, "402 Insufficient credits"):
                await OpenRouter("k", "m", client).run([])

    async def test_rejected_server_tools_fall_back_to_own_tools_then_none(self):
        bodies = []

        def handler(request):
            body = json.loads(request.content)
            bodies.append(body)
            if any(t["type"] != "function" for t in body.get("tools", [])) or (body.get("tools") and len(bodies) < 3):
                return httpx.Response(400, json={"error": {"message": "unsupported tool"}})
            return httpx.Response(200, text="data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]}) + "\n\ndata: [DONE]\n")
        tools = [{"type": "openrouter:web_search"}, {"type": "function", "function": {"name": "f", "parameters": {}}}]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            router = OpenRouter("k", "m", client)
            result = await router.run([], tools=tools)
            self.assertEqual(result.text, "ok")
            self.assertEqual([len(b.get("tools", [])) for b in bodies], [2, 1, 0])
            self.assertFalse(router.server_tools)
            await router.run([], tools=tools)  # server tools are not offered again
            self.assertEqual(len(bodies[3]["tools"]), 1)

    async def test_transcription_request(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"text": " привет "})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            text = await OpenRouter("k", "m", client).transcribe(b"OggS", "ogg", "openai/whisper-large-v3-turbo")
        self.assertEqual(text, "привет")
        self.assertEqual(bodies[0]["input_audio"]["format"], "ogg")
        self.assertTrue(str(bodies[0]["input_audio"]["data"]).startswith("T2dnUw"))


if __name__ == "__main__":
    unittest.main()
