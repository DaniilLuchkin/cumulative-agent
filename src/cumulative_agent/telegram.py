import asyncio
import re
import json
import time
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import httpx

from .config import Settings
from .domain import TaskCreate
from .metrics import Metrics
from .ownership import claim_telegram_owner, telegram_owner_ids
from .intake import parse_task, describe
from .repository import Repository
from .resources import ResourceManager
from .tasks import ProjectRegistry, TaskManager


RICH_LIMIT = 32000  # sendRichMessage accepts up to 32768 characters.
TEXT_LIMIT = 4000   # sendMessage/sendMessageDraft accept up to 4096 characters.
DRAFT_INTERVAL = 0.8  # seconds between streamed draft updates
DRAFT_KEEPALIVE = 20  # drafts are 30-second previews; refresh before they expire
BLOCKED_GRACE = 4     # a chat turn waiting for resources is reported, not queued forever
FINISHED = ("done", "failed", "cancelled")
COMMANDS = [
    ("new", "Новый диалог (отдельный топик)"),
    ("reset", "Очистить контекст текущего диалога"),
    ("task", "Создать задачу: /task текст"),
    ("app", "Открыть список задач"),
    ("tasks", "Последние задачи"),
    ("status", "Очередь и расходы"),
    ("resources", "Модели и квоты"),
    ("pause", "Остановить новые запуски"),
    ("resume", "Возобновить запуски"),
    ("help", "Справка"),
]
TASK_HINT = re.compile(r"^\s*(напомни|напоминание|remind|задача\s*:)", re.IGNORECASE)


@dataclass
class Generation:
    owner: int
    thread: int | None
    task_id: str
    draft_id: int
    reply_to: int | None
    text: str = ""
    stopped: bool = False
    drafts: bool = True
    rich: bool = True  # Rich Markdown can reject unusual model output; fall back per answer.
    changed: asyncio.Event = field(default_factory=asyncio.Event)


class Telegram:
    """Private owner chats only. No model-generated tool or command execution.

    Chat mode follows the Bot API guidance for AI bots: streamed drafts
    (sendMessageDraft / sendRichMessageDraft) with a Stop button, one context per
    private-chat topic, and the final answer persisted with sendRichMessage.
    """

    def __init__(self, repo: Repository, settings: Settings, tasks: TaskManager, resources: ResourceManager, projects: ProjectRegistry, client: httpx.AsyncClient):
        self.repo, self.settings, self.tasks, self.resources, self.projects, self.client = repo, settings, tasks, resources, projects, client
        self.enabled = False
        self._timer = None
        self._poll_task = None
        self.reminders = None
        self.chat = None
        self.worker = None
        self.topics_enabled = False
        self.rich_supported = True
        self._configured = False
        self.generations: dict[int, Generation] = {}
        self._streams: set[asyncio.Task] = set()

    def start(self):
        if self.settings.telegram_bot_token and (telegram_owner_ids(self.repo, self.settings) or self.settings.telegram_claim_first_user):
            self.enabled = True
            self._schedule(0)

    def _schedule(self, delay: float):
        if self.enabled:
            self._timer = asyncio.get_running_loop().call_later(delay, self._launch)

    def _launch(self):
        self._poll_task = asyncio.create_task(self.poll())

    async def call(self, method: str, payload: dict):
        try:
            response = await self.client.post(f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/{method}", json=payload, timeout=35)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError("Telegram rejected request")
            return data["result"]
        except (httpx.HTTPError, ValueError, KeyError):
            raise RuntimeError("Telegram transport/protocol failure") from None

    async def send(self, chat_id: int, text: str, reply_markup: dict | None = None, thread_id: int | None = None):
        if chat_id not in telegram_owner_ids(self.repo, self.settings):
            raise ValueError("Unauthorized recipient")
        payload = {"chat_id": chat_id, "text": text[:4000]}
        if thread_id:
            payload["message_thread_id"] = thread_id
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self.call("sendMessage", payload)

    async def _reply(self, owner: int, thread: int | None, text: str, markup: dict | None = None):
        if thread:
            await self.send(owner, text, markup, thread)
        elif markup:
            await self.send(owner, text, markup)
        else:
            await self.send(owner, text)

    async def configure(self):
        """Advertise commands and detect private-chat topics once per start."""
        self._configured = True
        try:
            me = await self.call("getMe", {})
            self.topics_enabled = bool(me.get("has_topics_enabled"))
        except RuntimeError:
            self.repo.event("telegram_getme_failed")
        try:
            await self.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS],
                                              "scope": {"type": "all_private_chats"}})
        except RuntimeError:
            self.repo.event("telegram_commands_setup_failed")

    def chat_available(self) -> bool:
        if self.settings.telegram_mode != "chat" or not self.chat or not self.worker:
            return False
        return any(m.enabled and m.provider in self.worker.providers for m in self.repo.models())

    async def poll(self):
        delay = 1
        try:
            if not self._configured:
                await self.configure()
            updates = await self.call("getUpdates", {"offset": self.repo.get_setting("telegram_offset", 0), "timeout": 25, "allowed_updates": ["message", "callback_query", "stopped_message_generation"], "limit": 100})
            for update in updates:
                await self.handle(update)
                self.repo.set_setting("telegram_offset", int(update["update_id"]) + 1)
            await self.notify_results()
            if self.reminders:
                self.reminders.tick()
                await self.reminders.deliver(self)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            delay = 15
            self.repo.event("telegram_poll_failed", error_type=type(exc).__name__)
        finally:
            self._schedule(delay)

    async def handle(self, update: dict) -> bool:
        if update.get("callback_query"):
            return await self.handle_confirmation(update['callback_query'])
        if update.get("stopped_message_generation"):
            return self.handle_stop(update["stopped_message_generation"])
        message = update.get("message", {})
        chat, sender = message.get("chat", {}), message.get("from", {})
        owner = sender.get("id")
        if chat.get("type") != "private" or type(owner) is not int or owner <= 0 or chat.get("id") != owner or sender.get("is_bot"):
            return False
        text = message.get("text", "")
        if not isinstance(text, str):
            return False
        text = text.strip()
        if not text:
            return False
        claimed = claim_telegram_owner(self.repo, self.settings, owner)
        if owner not in telegram_owner_ids(self.repo, self.settings):
            return False
        thread = message.get("message_thread_id") if message.get("is_topic_message") else None
        thread = thread if type(thread) is int and thread > 0 else None
        command = text.split()[0].split("@")[0].lower()
        natural = {"покажи задачи":"/tasks", "список задач":"/tasks", "поставь агента на паузу":"/pause", "возобнови работу":"/resume", "покажи ресурсы":"/resources"}
        command = natural.get(text.lower(),command)
        markup = None
        if command in ("/pause", "/resume"):
            self.repo.set_setting("paused", command == "/pause")
            self.repo.event("operator_pause", paused=command == "/pause", source="telegram")
            self.tasks.wake()
            reply = "Пауза: новые запуски остановлены; текущие завершатся в своих лимитах." if command == "/pause" else "Выполнение возобновлено."
        elif command == "/timezone":
            parts=text.split(maxsplit=1)
            try:
                zone=parts[1].strip();ZoneInfo(zone)
                self.repo.set_setting('timezone:'+str(owner),zone)
                reply='Часовой пояс: '+zone
            except (IndexError,KeyError,ValueError): reply='Укажите часовой пояс, например /timezone Europe/Moscow'
        elif command in ("/cancel", "/retry"):
            parts=text.split(maxsplit=1)
            matches=self.repo.all('SELECT id FROM tasks WHERE id LIKE ? LIMIT 2',((parts[1].strip() if len(parts)>1 else '')+'%',))
            if len(matches)!=1 or len(parts)!=2 or not re.fullmatch(r'[a-f0-9-]{8,36}',parts[1].strip()):
                reply='Укажите уникальный ID задачи после команды.'
            else:
                try:
                    task=self.tasks.control(matches[0]['id'],command[1:]);reply=f'Задача {task.id}: {task.status}'
                except ValueError as error: reply=str(error)
        elif command == "/new":
            parts = text.split(maxsplit=1)
            name = parts[1].strip()[:128] if len(parts) > 1 else ""
            if self.topics_enabled:
                try:
                    topic = await self.call("createForumTopic", {"chat_id": owner, "name": name or "Новый чат"})
                    thread = int(topic["message_thread_id"])
                    if not name:
                        self.repo.set_setting(f"chat_autoname:{owner}:{thread}", True)
                    reply = "Новый диалог. Контекст пустой — задайте вопрос."
                except (RuntimeError, KeyError, TypeError, ValueError):
                    self.chat and self.chat.reset(owner, thread)
                    reply = "Не удалось создать топик; контекст этого диалога очищен."
            else:
                self.chat and self.chat.reset(owner, thread)
                reply = "Контекст очищен. Чтобы вести диалоги в отдельных топиках, включите для бота режим топиков в @BotFather."
        elif command == "/reset":
            self.chat and self.chat.reset(owner, thread)
            reply = "Контекст этого диалога очищен."
        elif command == "/task":
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                reply = "Напишите задачу после команды: /task Напомни завтра в 18:00 позвонить"
            else:
                reply, markup = self.intake(owner, parts[1].strip(), update["update_id"])
        elif command == "/tasks":
            items = [t for t in self.repo.tasks(limit=100) if t.handler != "chat"][:20]
            reply = "\n".join(f"{t.id[:8]} [{t.status}] p={t.priority} {t.title}" for t in items) or "Очередь пуста."
        elif command == "/status":
            m = Metrics(self.repo).snapshot()
            reply = f"Пауза: {self.repo.get_setting('paused', False)}\nОчередь: {m['queue']}\nВыполнено: {m['tasks_completed']}\nСтоимость: ${m['cost']:.6f}"
        elif command == "/resources":
            reply = "\n".join(f"{m.id}: enabled={m.enabled}, quota={m.quota_remaining}, reset={m.resets_at}" for m in self.repo.models()) or "Модели не настроены."
            reply += f"\nLocal healthy: {self.resources.machine()['healthy']}"
        elif command == "/projects":
            reply = "\n".join(f"{p.id}: {p.description}" for p in self.projects.list()) or "Проекты пока не зарегистрированы."
        elif command == "/background":
            parts = text.lower().split()
            if len(parts) == 2 and parts[1] in ("on", "off"):
                self.repo.set_setting("background_enabled", parts[1] == "on")
                self.tasks.wake()
            items = [t for t in self.repo.tasks(limit=100) if t.type != "user"][:15]
            reply = f"Фоновые запуски: {self.repo.get_setting('background_enabled', True)}\n" + "\n".join(f"[{t.status}] {t.title}" for t in items)
        elif command == "/app":
            if self.settings.telegram_webapp_url:
                markup = {"inline_keyboard": [[{"text": "Открыть мои задачи", "web_app": {"url": self.settings.telegram_webapp_url}}]]}
                reply = "Ваш список задач: дедлайны, приоритеты, проекты и результаты."
                try:
                    await self.call("setChatMenuButton", {"chat_id": owner, "menu_button": {"type": "web_app", "text": "Мои задачи", "web_app": {"url": self.settings.telegram_webapp_url}}})
                except RuntimeError:
                    self.repo.event("telegram_menu_setup_failed", owner_id=owner)
            else:
                reply = "Mini App готово, но публичный HTTPS-адрес ещё не настроен. Укажите TELEGRAM_WEBAPP_URL для сервиса."
        elif command in ("/start", "/help"):
            if self.chat_available():
                reply = ("Пишите как в обычном чате — ответ появляется по мере генерации, его можно остановить кнопкой «Стоп». "
                         "/new — новый диалог" + (" в отдельном топике" if self.topics_enabled else "") + ", /reset — очистить контекст.\n"
                         "Задачи: /task текст или сообщение, начинающееся с «Напомни…». /app — список задач. "
                         "/tasks /status /resources /projects /background [on|off] /pause /resume /cancel ID /retry ID /timezone Europe/Moscow.")
            else:
                reply = "Напишите задачу обычным языком: «Напомни завтра в 18:00, высокий приоритет». /app — список с дедлайнами и приоритетами. /tasks /status /resources /projects /background [on|off] /pause /resume /cancel ID /retry ID /timezone Europe/Moscow."
            if self.settings.telegram_webapp_url:
                markup = {"inline_keyboard": [[{"text": "Открыть мои задачи", "web_app": {"url": self.settings.telegram_webapp_url}}]]}
        elif command.startswith("/"):
            reply = "Неизвестная команда. /help"
        elif self.chat_available() and not TASK_HINT.match(text):
            if claimed:
                await self._safe_reply(owner, thread, f"Вы назначены владельцем бота. Ваш Telegram ID: {owner}. Привязка сохранена; другим пользователям доступ закрыт.", update)
            await self.start_generation(owner, thread, text, update["update_id"], message.get("message_id"))
            return True
        else:
            reply, markup = self.intake(owner, text, update["update_id"])
        # Task creation remains durable even if acknowledgement fails.
        if claimed:
            reply = f"Вы назначены владельцем бота. Ваш Telegram ID: {owner}. Привязка сохранена; другим пользователям доступ закрыт.\n\n" + reply
        await self._safe_reply(owner, thread, reply, update, markup)
        return True

    async def _safe_reply(self, owner, thread, reply, update, markup=None):
        try:
            await self._reply(owner, thread, reply, markup)
        except RuntimeError:
            self.repo.event("telegram_reply_failed", update_id=update["update_id"])

    def intake(self, owner: int, text: str, update_id: int):
        zone=self.repo.get_setting('timezone:'+str(owner),self.settings.timezone)
        spec,confirm,warning=parse_task(text,self.projects.list(),zone,owner,update_id)
        if confirm:
            draft_id=str(update_id)
            self.repo.set_setting('intake:'+draft_id,{'owner':owner,'spec':spec.model_dump(mode='json')})
            reply='Проверьте параметры перед созданием:\n'+describe(spec)+(('\n'+warning) if warning else '')+'\nДля исправления напишите задачу заново.'
            markup={'inline_keyboard':[[{'text':'Создать с этими параметрами','callback_data':f'intake:{draft_id}:confirm'}, {'text':'Только напоминание','callback_data':f'intake:{draft_id}:reminder'}]]}
            return reply, markup
        task=self.tasks.create(spec)
        return f'Задача {task.id} добавлена.\n'+describe(spec), None

    # --- Chat mode -----------------------------------------------------------------

    async def start_generation(self, owner: int, thread: int | None, text: str, update_id: int, message_id: int | None):
        key = f"telegram:{owner}:{update_id}"
        if self.repo.one("SELECT id FROM tasks WHERE idempotency_key=?", (key,)):
            return  # Redelivered update: the turn already exists.
        zone = self.repo.get_setting('timezone:' + str(owner), self.settings.timezone)
        spec = TaskCreate(title=text[:200], description=text[:40000], priority=100, handler="chat", telegram_owner_id=owner,
                          telegram_thread_id=thread, idempotency_key=key, max_attempts=1, max_tokens=self.settings.chat_max_tokens,
                          timeout_seconds=self.settings.limits.timeout_seconds, task_timezone=zone)
        task = self.tasks.create(spec)
        # Both run before the worker tick that tasks.create scheduled on this loop.
        self.chat.add(owner, thread, "user", text, task.id)
        generation = Generation(owner, thread, task.id, update_id, message_id if type(message_id) is int else None)
        self.generations[generation.draft_id] = generation

        def sink(accumulated: str):
            generation.text = accumulated
            generation.changed.set()
        self.worker.stream_sinks[task.id] = sink
        stream = asyncio.create_task(self.stream(generation))
        self._streams.add(stream)
        stream.add_done_callback(self._streams.discard)

    async def draft(self, generation: Generation, text: str):
        if not generation.drafts:
            try:
                await self.call("sendChatAction", {"chat_id": generation.owner, "action": "typing", **({"message_thread_id": generation.thread} if generation.thread else {})})
            except RuntimeError:
                pass
            return
        base = {"chat_id": generation.owner, "draft_id": generation.draft_id, "can_stop": True}
        if generation.thread:
            base["message_thread_id"] = generation.thread
        if text and self.rich_supported and generation.rich:
            try:
                await self.call("sendRichMessageDraft", {**base, "rich_message": {"markdown": text[:RICH_LIMIT]}})
                return
            except RuntimeError:
                generation.rich = False
                self.repo.event("telegram_rich_draft_failed")
        try:
            visible = text if len(text) <= TEXT_LIMIT else "…" + text[-(TEXT_LIMIT - 1):]
            await self.call("sendMessageDraft", {**base, "text": visible})
        except RuntimeError:
            generation.drafts = False
            self.repo.event("telegram_draft_failed")

    async def stream(self, generation: Generation):
        """Mirror a chat task into drafts, then persist the final answer."""
        started = time.monotonic()
        limit = self.settings.limits.timeout_seconds + 60
        shown, last_sent, blocked_since = None, 0.0, None
        try:
            await self.draft(generation, "")
            last_sent = time.monotonic()
            while True:
                try:
                    await asyncio.wait_for(generation.changed.wait(), timeout=DRAFT_INTERVAL)
                except TimeoutError:
                    pass
                generation.changed.clear()
                task = self.repo.task(generation.task_id)
                if not task or task.status in FINISHED:
                    break
                current = time.monotonic()
                if task.status == "blocked" and (task.blocked_reason or "").startswith("wait:"):
                    blocked_since = blocked_since or current
                    if current - blocked_since >= BLOCKED_GRACE:
                        try:
                            self.tasks.control(task.id, "cancel", "chat_blocked")
                        except (KeyError, ValueError):
                            pass
                        generation.text = ""
                        reason = task.blocked_reason.removeprefix("wait:")
                        await self.finish_generation(generation, f"Сейчас не могу ответить: {reason}. Проверьте /resources и лимиты бюджета.", store=False)
                        return
                else:
                    blocked_since = None
                if current - started > limit:
                    break
                if generation.text != shown and current - last_sent >= DRAFT_INTERVAL:
                    shown = generation.text
                    await self.draft(generation, shown)
                    last_sent = current
                elif current - last_sent >= DRAFT_KEEPALIVE:
                    await self.draft(generation, generation.text)
                    last_sent = current
            await self.deliver(generation)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.repo.event("telegram_stream_failed", generation.task_id, error_type=type(exc).__name__)
        finally:
            self.worker.stream_sinks.pop(generation.task_id, None)
            self.generations.pop(generation.draft_id, None)

    async def deliver(self, generation: Generation):
        task = self.repo.task(generation.task_id)
        run = self.repo.one("SELECT status,result,error FROM task_runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (generation.task_id,))
        if task and task.status == "done" and run and run["result"]:
            await self.finish_generation(generation, run["result"])
        elif generation.stopped or (task and task.status == "cancelled"):
            partial = generation.text.strip()
            if partial:
                await self.finish_generation(generation, partial + "\n\n_Генерация остановлена._", stored_text=partial)
            else:
                await self.finish_generation(generation, "Генерация остановлена.", store=False)
        else:
            error = (run["error"] if run else None) or "ответ не получен"
            await self.finish_generation(generation, f"Не удалось получить ответ: {error}", store=False)

    async def finish_generation(self, generation: Generation, text: str, store: bool = True, stored_text: str | None = None):
        if store:
            self.chat.add(generation.owner, generation.thread, "assistant", stored_text or text, generation.task_id)
        self.repo.set_setting("telegram_notified:" + generation.task_id, True)
        try:
            await self.send_markdown(generation.owner, generation.thread, text, generation.reply_to)
        except RuntimeError:
            self.repo.event("telegram_reply_failed", generation.task_id)
        await self.autoname(generation)

    async def autoname(self, generation: Generation):
        key = f"chat_autoname:{generation.owner}:{generation.thread}"
        if not generation.thread or not self.repo.get_setting(key, False):
            return
        self.repo.set_setting(key, False)
        first = self.repo.one("SELECT content FROM chat_messages WHERE owner_id=? AND thread_id=? AND role='user' ORDER BY id LIMIT 1", (generation.owner, generation.thread))
        if first:
            name = " ".join(first["content"].split())[:60] or "Чат"
            try:
                await self.call("editForumTopic", {"chat_id": generation.owner, "message_thread_id": generation.thread, "name": name})
            except RuntimeError:
                pass

    async def send_markdown(self, owner: int, thread: int | None, text: str, reply_to: int | None = None):
        """Final answer: rich Markdown when supported, plain 4096-character chunks otherwise."""
        if owner not in telegram_owner_ids(self.repo, self.settings):
            raise ValueError("Unauthorized recipient")
        base = {"chat_id": owner}
        if thread:
            base["message_thread_id"] = thread
        reply = {"reply_parameters": {"message_id": reply_to, "allow_sending_without_reply": True}} if reply_to else {}
        if self.rich_supported and len(text) <= RICH_LIMIT:
            try:
                await self.call("sendRichMessage", {**base, **reply, "rich_message": {"markdown": text}})
                return
            except RuntimeError:
                self.repo.event("telegram_rich_message_failed")
        for index, start in enumerate(range(0, len(text), TEXT_LIMIT)):
            await self.call("sendMessage", {**base, **(reply if index == 0 else {}), "text": text[start:start + TEXT_LIMIT]})

    def handle_stop(self, stopped: dict) -> bool:
        owner = stopped.get("chat", {}).get("id")
        generation = self.generations.get(stopped.get("draft_id"))
        if not generation or owner != generation.owner or owner not in telegram_owner_ids(self.repo, self.settings):
            return False
        generation.stopped = True
        try:
            self.tasks.control(generation.task_id, "cancel", "user_stopped_generation")
        except (KeyError, ValueError):
            pass
        generation.changed.set()
        return True

    async def handle_confirmation(self, query):
        owner=query.get('from',{}).get('id');message=query.get('message',{});chat=message.get('chat',{})
        if owner not in telegram_owner_ids(self.repo,self.settings) or chat.get('type')!='private' or chat.get('id')!=owner:
            return False
        match=re.fullmatch(r'intake:(\d+):(confirm|reminder)',query.get('data',''))
        if not match:return False
        draft=self.repo.get_setting('intake:'+match[1])
        if not draft or draft['owner']!=owner:return False
        spec=TaskCreate.model_validate(draft['spec'])
        if match[2]=='reminder':spec.status='inbox'
        task=self.tasks.create(spec)
        await self.call('answerCallbackQuery',{'callback_query_id':query['id'],'text':'Создано'})
        await self.send(owner,f'Задача {task.id} создана.\n'+describe(task)+'\nПараметры можно исправить в /app.')
        return True

    async def notify_results(self):
        rows = self.repo.all("SELECT id,idempotency_key,status FROM tasks WHERE idempotency_key LIKE 'telegram:%' AND status IN ('done','failed') ORDER BY updated_at DESC LIMIT 100")
        for row in rows:
            if self.repo.get_setting("telegram_notified:" + row["id"], False):
                continue
            owner = int(row["idempotency_key"].split(":")[1])
            if owner not in telegram_owner_ids(self.repo, self.settings):
                continue
            run = self.repo.one("SELECT result,error FROM task_runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (row["id"],))
            task = self.repo.task(row["id"])
            if task.handler == "chat":
                if task.id in self.worker_sinks():
                    continue  # Streaming in progress; the stream delivers it.
                # Recovered after a restart: deliver the stored answer without drafts.
                generation = Generation(owner, task.telegram_thread_id, task.id, 1, None)
                if task.status == "done" and run and run["result"]:
                    await self.finish_generation(generation, run["result"])
                else:
                    self.repo.set_setting("telegram_notified:" + task.id, True)
                continue
            content = "Отмечена выполненной владельцем." if task.completed_manually else ((run["result"] or run["error"] or "") if run else "")
            text = f"Задача {row['id']} [{row['status']}]\n" + content
            await self.send(owner, text)
            self.repo.set_setting("telegram_notified:" + row["id"], True)

    def worker_sinks(self) -> dict:
        return self.worker.stream_sinks if self.worker else {}

    async def stop(self):
        self.enabled = False
        for stream in list(self._streams):
            stream.cancel()
        await asyncio.gather(*self._streams, return_exceptions=True)
        if self._timer:
            self._timer.cancel()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
