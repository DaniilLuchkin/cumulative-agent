import asyncio
import re
import json
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


class Telegram:
    """Private owner chats only. No model-generated tool or command execution."""

    def __init__(self, repo: Repository, settings: Settings, tasks: TaskManager, resources: ResourceManager, projects: ProjectRegistry, client: httpx.AsyncClient):
        self.repo, self.settings, self.tasks, self.resources, self.projects, self.client = repo, settings, tasks, resources, projects, client
        self.enabled = False
        self._timer = None
        self._poll_task = None
        self.reminders = None

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

    async def send(self, chat_id: int, text: str, reply_markup: dict | None = None):
        if chat_id not in telegram_owner_ids(self.repo, self.settings):
            raise ValueError("Unauthorized recipient")
        payload = {"chat_id": chat_id, "text": text[:4000]}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        await self.call("sendMessage", payload)

    async def poll(self):
        delay = 1
        try:
            updates = await self.call("getUpdates", {"offset": self.repo.get_setting("telegram_offset", 0), "timeout": 25, "allowed_updates": ["message", "callback_query"], "limit": 100})
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
        elif command == "/tasks":
            items = self.repo.tasks(limit=20)
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
            reply = "Напишите задачу обычным языком: «Напомни завтра в 18:00, высокий приоритет». /app — список с дедлайнами и приоритетами. /tasks /status /resources /projects /background [on|off] /pause /resume /cancel ID /retry ID /timezone Europe/Moscow."
            if self.settings.telegram_webapp_url:
                markup = {"inline_keyboard": [[{"text": "Открыть мои задачи", "web_app": {"url": self.settings.telegram_webapp_url}}]]}
        elif command.startswith("/"):
            reply = "Неизвестная команда. /help"
        else:
            zone=self.repo.get_setting('timezone:'+str(owner),self.settings.timezone)
            spec,confirm,warning=parse_task(text,self.projects.list(),zone,owner,update['update_id'])
            if confirm:
                draft_id=str(update['update_id'])
                self.repo.set_setting('intake:'+draft_id,{'owner':owner,'spec':spec.model_dump(mode='json')})
                reply='Проверьте параметры перед созданием:\n'+describe(spec)+(('\n'+warning) if warning else '')+'\nДля исправления напишите задачу заново.'
                markup={'inline_keyboard':[[{'text':'Создать с этими параметрами','callback_data':f'intake:{draft_id}:confirm'}, {'text':'Только напоминание','callback_data':f'intake:{draft_id}:reminder'}]]}
            else:
                task=self.tasks.create(spec);reply=f'Задача {task.id} добавлена.\n'+describe(spec)
        # Task creation remains durable even if acknowledgement fails.
        if claimed:
            reply = f"Вы назначены владельцем бота. Ваш Telegram ID: {owner}. Привязка сохранена; другим пользователям доступ закрыт.\n\n" + reply
        try:
            if markup:
                await self.send(owner, reply, markup)
            else:
                await self.send(owner, reply)
        except RuntimeError:
            self.repo.event("telegram_reply_failed", update_id=update["update_id"])
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
            content = "Отмечена выполненной владельцем." if task.completed_manually else ((run["result"] or run["error"] or "") if run else "")
            text = f"Задача {row['id']} [{row['status']}]\n" + content
            await self.send(owner, text)
            self.repo.set_setting("telegram_notified:" + row["id"], True)

    async def stop(self):
        self.enabled = False
        if self._timer:
            self._timer.cancel()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
