"""Agent tools: reminders and background watches, delivered proactively by the bot."""
import json
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import outputs
from .llm import SERVER_TOOLS, LLMError, chat_var, purpose_var
from .telegram import TelegramError

log = logging.getLogger("llm_bot")

MAX_WATCHES = 10
WATCH_PROMPT = ("You run a scheduled background check for the user and report in Telegram. Current time: {now}. "
                "Use web search when the task needs current information. Write a short report in Markdown in the user's language, "
                "with sources as links. If nothing important changed compared to the previous report, reply exactly NO_UPDATE.")

FUNCTIONS = [
    {"type": "function", "function": {
        "name": "set_reminder",
        "description": "Schedule a reminder message to the user. Use it whenever the user asks to remind, notify or ping them later.",
        "parameters": {"type": "object", "required": ["text", "time"], "properties": {
            "text": {"type": "string", "description": "What to remind about, in the user's language"},
            "time": {"type": "string", "description": "Local time 'YYYY-MM-DD HH:MM' in the user's timezone"},
            "repeat": {"type": "string", "enum": ["none", "daily", "weekdays", "weekly"], "description": "Repetition, default none"}}}}},
    {"type": "function", "function": {
        "name": "start_watch",
        "description": ("Start a recurring background task that checks something (news, prices, releases, a web page) and reports "
                        "changes in its own topic. Use it when the user asks to follow, monitor or regularly check something."),
        "parameters": {"type": "object", "required": ["title", "instructions", "every_hours"], "properties": {
            "title": {"type": "string", "description": "2-5 word topic title"},
            "instructions": {"type": "string", "description": "What to check and what counts as worth reporting"},
            "every_hours": {"type": "number", "description": "Check interval in hours, 1 to 168"}}}}},
    {"type": "function", "function": {
        "name": "list_scheduled",
        "description": "List the user's active reminders and background watches with their ids.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "cancel_scheduled",
        "description": "Cancel a reminder (id like r3) or a background watch (id like w2).",
        "parameters": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}}},
]


def tg_time(ts: float, label: str, fmt: str = "wDT") -> str:
    """Telegram renders this in the reader's own timezone and language."""
    return f"![{label}](tg://time?unix={int(ts)}&format={fmt})"


def next_occurrence(due: float, repeat: str, zone: ZoneInfo) -> float | None:
    if repeat == "none":
        return None
    local = datetime.fromtimestamp(due, zone)
    step = {"daily": 1, "weekdays": 1, "weekly": 7}[repeat]
    while True:
        # Add calendar days in local time so the wall-clock hour survives DST changes.
        local = (local.replace(tzinfo=None) + timedelta(days=step)).replace(tzinfo=zone)
        if repeat != "weekdays" or local.weekday() < 5:
            if local.timestamp() > time.time():
                return local.timestamp()


class Agent:
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store

    def tools(self) -> list[dict]:
        return FUNCTIONS + outputs.FUNCTIONS + SERVER_TOOLS

    # --- tool calls -----------------------------------------------------------------

    async def call(self, call: dict, ctx: dict) -> str:
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except ValueError:
            return json.dumps({"error": "arguments are not valid JSON"})
        if name in outputs.NAMES:
            return await self.bot.outputs.call(name, args, ctx)
        if name == "search_history":
            try:
                return await self.bot.memory.search(ctx["chat_id"], str(args.get("query", "")), ctx["thread_id"])
            except (LLMError, ValueError) as exc:
                return json.dumps({"error": str(exc)}, ensure_ascii=False)
        try:
            if name == "set_reminder":
                return self.set_reminder(ctx, str(args.get("text", "")).strip(), str(args.get("time", "")), args.get("repeat") or "none")
            if name == "start_watch":
                return await self.start_watch(ctx, str(args.get("title", "")).strip(), str(args.get("instructions", "")).strip(), args.get("every_hours", 24))
            if name == "list_scheduled":
                return json.dumps(self.scheduled(ctx["chat_id"]), ensure_ascii=False)
            if name == "cancel_scheduled":
                return json.dumps({"cancelled": self.cancel(ctx["chat_id"], str(args.get("id", "")))})
        except (ValueError, TypeError) as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        return json.dumps({"error": f"unknown tool {name}"})

    def set_reminder(self, ctx: dict, text: str, when: str, repeat: str) -> str:
        if not text:
            raise ValueError("text is empty")
        if repeat not in ("none", "daily", "weekdays", "weekly"):
            raise ValueError("repeat must be none, daily, weekdays or weekly")
        zone = ctx["zone"]
        try:
            local = datetime.fromisoformat(when.strip().replace("T", " "))
        except ValueError:
            raise ValueError("time must look like 'YYYY-MM-DD HH:MM'") from None
        due = (local if local.tzinfo else local.replace(tzinfo=zone)).timestamp()
        if due <= time.time():
            raise ValueError("this time has already passed")
        reminder = self.store.run("INSERT INTO reminders(chat_id,thread_id,text,due_at,repeat) VALUES(?,?,?,?,?)",
                                  (ctx["chat_id"], ctx["thread_id"], text, due, repeat))
        ctx["reaction"] = "🫡"
        log.info("reminder r%s at %s", reminder, datetime.fromtimestamp(due, zone).isoformat())
        return json.dumps({"id": f"r{reminder}", "scheduled_for": tg_time(due, datetime.fromtimestamp(due, zone).strftime("%d.%m %H:%M")),
                           "note": "Mention the time to the user by copying scheduled_for verbatim: Telegram renders it in their timezone."},
                          ensure_ascii=False)

    async def start_watch(self, ctx: dict, title: str, instructions: str, every_hours) -> str:
        every = float(every_hours)
        if not title or not instructions:
            raise ValueError("title and instructions are required")
        if not 1 <= every <= 168:
            raise ValueError("every_hours must be between 1 and 168")
        if self.store.one("SELECT COUNT(*) n FROM watches WHERE chat_id=? AND active=1", (ctx["chat_id"],))["n"] >= MAX_WATCHES:
            raise ValueError(f"at most {MAX_WATCHES} active watches; cancel one first")
        chat_id, thread_id = ctx["chat_id"], ctx["thread_id"]
        if self.bot.topics_enabled:
            topic = await self.bot.api.call("createForumTopic", chat_id=chat_id, name=("👁 " + title)[:128])
            thread_id = topic["message_thread_id"]
        watch = self.store.run("INSERT INTO watches(chat_id,thread_id,title,instructions,every_hours,next_at) VALUES(?,?,?,?,?,?)",
                               (chat_id, thread_id, title, instructions, every, time.time()))
        row = self.watch(watch)
        sent = await self.bot.send_markdown(chat_id, thread_id, self.status_text(row), None, self.watch_keyboard(row))
        if sent:
            self.store.run("UPDATE watches SET status_message_id=? WHERE id=?", (sent[-1], watch))
            try:
                await self.bot.api.call("pinChatMessage", chat_id=chat_id, message_id=sent[-1], disable_notification=True)
            except TelegramError as exc:
                log.warning("pin failed: %s", exc.description)
        ctx["reaction"] = "🫡"
        link = f"https://t.me/{self.bot.username}/{thread_id}" if self.bot.topics_enabled and self.bot.username else None
        return json.dumps({"id": f"w{watch}", "topic_link": link, "first_check": "now",
                           "note": "Tell the user reports will appear in the topic; include topic_link if present."}, ensure_ascii=False)

    def scheduled(self, chat_id: int) -> dict:
        zone = self.bot.zone(chat_id)
        reminders = [{"id": f"r{r['id']}", "text": r["text"], "time": datetime.fromtimestamp(r["due_at"], zone).strftime("%Y-%m-%d %H:%M"), "repeat": r["repeat"]}
                     for r in self.store.all("SELECT * FROM reminders WHERE chat_id=? AND active=1 ORDER BY due_at", (chat_id,))]
        watches = [{"id": f"w{w['id']}", "title": w["title"], "every_hours": w["every_hours"]}
                   for w in self.store.all("SELECT * FROM watches WHERE chat_id=? AND active=1 ORDER BY id", (chat_id,))]
        return {"reminders": reminders, "watches": watches}

    def cancel(self, chat_id: int, item: str) -> bool:
        table = {"r": "reminders", "w": "watches"}.get(item[:1])
        if not table or not item[1:].isdigit():
            raise ValueError("id must look like r3 or w2")
        return self.store.db.execute(f"UPDATE {table} SET active=0 WHERE id=? AND chat_id=? AND active=1", (int(item[1:]), chat_id)).rowcount == 1

    # --- proactive delivery ---------------------------------------------------------

    async def tick(self):
        now = time.time()
        for row in self.store.all("SELECT * FROM reminders WHERE active=1 AND due_at<=? ORDER BY due_at LIMIT 20", (now,)):
            await self.deliver_reminder(row)
        for row in self.store.all("SELECT * FROM watches WHERE active=1 AND next_at<=? ORDER BY next_at LIMIT 3", (now,)):
            # Move the schedule first so a failing check is not retried in a tight loop.
            self.store.run("UPDATE watches SET next_at=? WHERE id=?", (now + row["every_hours"] * 3600, row["id"]))
            self.bot.spawn(self.run_watch(row["id"]))

    async def deliver_reminder(self, row):
        zone = self.bot.zone(row["chat_id"])
        following = next_occurrence(row["due_at"], row["repeat"], zone)
        if following:
            self.store.run("UPDATE reminders SET due_at=? WHERE id=?", (following, row["id"]))
        else:
            self.store.run("UPDATE reminders SET active=0 WHERE id=?", (row["id"],))
        keyboard = {"inline_keyboard": [[{"text": "+15 мин", "callback_data": f"snooze:{row['id']}:900"},
                                         {"text": "+1 час", "callback_data": f"snooze:{row['id']}:3600"},
                                         {"text": "Завтра", "callback_data": f"snooze:{row['id']}:86400"}]]}
        text = "⏰ " + row["text"]
        if following:
            text += "\n\nСледующий раз: " + tg_time(following, datetime.fromtimestamp(following, zone).strftime("%d.%m %H:%M"))
        await self.bot.send_markdown(row["chat_id"], row["thread_id"], text, None, keyboard)

    async def run_watch(self, watch_id: int):
        row = self.watch(watch_id)
        if not row or not row["active"]:
            return
        chat_var.set(row["chat_id"])
        purpose_var.set("watch")
        zone = self.bot.zone(row["chat_id"])
        messages = [{"role": "system", "content": WATCH_PROMPT.format(now=datetime.now(zone).strftime("%Y-%m-%d %H:%M %Z"))},
                    {"role": "user", "content": f"Task: {row['instructions']}\n\nPrevious report:\n{row['last_report'] or '(none yet)'}"}]
        try:
            result = await self.bot.llm.run(messages, self.bot.current_model(row["chat_id"]), tools=SERVER_TOOLS)
            report = result.text.strip()
        except LLMError as exc:
            log.warning("watch w%s failed: %s", watch_id, exc)
            report = None
        self.store.run("UPDATE watches SET last_run=? WHERE id=?", (time.time(), watch_id))
        if report and not report.startswith("NO_UPDATE"):
            self.store.run("UPDATE watches SET last_report=? WHERE id=?", (report[:8000], watch_id))
            await self.bot.send_markdown(row["chat_id"], row["thread_id"], report, None)
        await self.refresh_status(watch_id)

    async def refresh_status(self, watch_id: int):
        row = self.watch(watch_id)
        if not row or not row["status_message_id"]:
            return
        try:
            await self.bot.api.call("editMessageText", chat_id=row["chat_id"], message_id=row["status_message_id"],
                                    rich_message={"markdown": self.status_text(row)}, reply_markup=self.watch_keyboard(row))
        except TelegramError as exc:
            if "not modified" not in exc.description:
                log.warning("watch status edit failed: %s", exc.description)

    def watch(self, watch_id: int):
        return self.store.one("SELECT * FROM watches WHERE id=?", (watch_id,))

    def status_text(self, row) -> str:
        hours = f"{row['every_hours']:g}"
        lines = [f"👁 **{row['title']}**", "", row["instructions"], "", f"Проверка каждые {hours} ч."]
        if not row["active"]:
            lines.append("**Остановлено.**")
        else:
            if row["last_run"]:
                lines.append("Последняя проверка: " + tg_time(row["last_run"], "недавно", "r"))
            lines.append("Следующая: " + tg_time(row["next_at"], "скоро", "wDT"))
        return "\n".join(lines)

    def watch_keyboard(self, row) -> dict:
        if not row["active"]:
            return {"inline_keyboard": []}
        return {"inline_keyboard": [[{"text": "Проверить сейчас", "callback_data": f"watch:run:{row['id']}", "style": "primary"},
                                     {"text": "Остановить", "callback_data": f"watch:stop:{row['id']}", "style": "danger"}]]}

    # --- buttons ---------------------------------------------------------------------

    async def on_callback(self, query: dict, data: str) -> str | None:
        """Handle agent buttons; returns the toast text, or None when the data is not ours."""
        chat_id = (query.get("message") or {}).get("chat", {}).get("id") or query["from"]["id"]
        kind, _, rest = data.partition(":")
        if kind == "snooze":
            reminder, seconds = (int(x) for x in rest.split(":"))
            row = self.store.one("SELECT * FROM reminders WHERE id=? AND chat_id=?", (reminder, chat_id))
            if not row:
                return "Напоминание не найдено"
            self.store.run("INSERT INTO reminders(chat_id,thread_id,text,due_at,repeat) VALUES(?,?,?,?,'none')",
                           (chat_id, row["thread_id"], row["text"], time.time() + seconds))
            return {900: "Напомню через 15 минут", 3600: "Напомню через час", 86400: "Напомню завтра"}.get(seconds, "Отложено")
        if kind == "watch":
            action, watch = rest.split(":")
            row = self.store.one("SELECT * FROM watches WHERE id=? AND chat_id=?", (int(watch), chat_id))
            if not row:
                return "Задача не найдена"
            if action == "stop":
                self.store.run("UPDATE watches SET active=0 WHERE id=?", (row["id"],))
                await self.refresh_status(row["id"])
                return "Остановлено"
            if action == "run" and row["active"]:
                self.bot.spawn(self.run_watch(row["id"]))
                return "Проверяю…"
            return "Задача остановлена"
        if kind == "cancel":
            cancelled = self.cancel(chat_id, rest)
            if rest.startswith("w"):
                await self.refresh_status(int(rest[1:]))
            await self.edit_list(query)
            return "Отменено" if cancelled else "Уже отменено"
        return None

    def list_markup(self, chat_id: int) -> tuple[str, dict]:
        items = self.scheduled(chat_id)
        rows, lines = [], []
        for reminder in items["reminders"]:
            repeat = {"daily": ", каждый день", "weekdays": ", по будням", "weekly": ", каждую неделю"}.get(reminder["repeat"], "")
            lines.append(f"⏰ {reminder['time']}{repeat} — {reminder['text']}")
            rows.append([{"text": "✖ " + reminder["text"][:40], "callback_data": "cancel:" + reminder["id"]}])
        for watch in items["watches"]:
            lines.append(f"👁 {watch['title']} — каждые {watch['every_hours']:g} ч")
            rows.append([{"text": "✖ " + watch["title"][:40], "callback_data": "cancel:" + watch["id"]}])
        text = "\n".join(lines) if lines else "Нет напоминаний и фоновых задач. Попросите, например: «напомни завтра в 9 позвонить врачу» или «следи за новостями о …»."
        return text, {"inline_keyboard": rows}

    async def edit_list(self, query: dict):
        message = query.get("message") or {}
        if not message.get("message_id"):
            return
        text, markup = self.list_markup(message["chat"]["id"])
        try:
            await self.bot.api.call("editMessageText", chat_id=message["chat"]["id"], message_id=message["message_id"], text=text, reply_markup=markup)
        except TelegramError as exc:
            if "not modified" not in exc.description:
                log.warning("list edit failed: %s", exc.description)
