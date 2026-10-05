"""Cheap, deterministic Russian/English intake. Ambiguous dates stay user text."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .domain import TaskCreate


def parse_task(text, projects, timezone="UTC", owner=None, update_id=None, current=None):
    local = (current or datetime.now(ZoneInfo(timezone))).astimezone(ZoneInfo(timezone))
    lowered = text.lower()
    priority = 10
    explicit = re.search(r"\bpriority=(-?\d{1,4})\b", text)
    if explicit: priority = max(-1000, min(1000, int(explicit.group(1))))
    elif re.search(r"\b(срочно|срочный|urgent)\b", lowered): priority = 30
    elif re.search(r"\b(высокий|высокая|важно|high)\b", lowered): priority = 20
    elif re.search(r"\b(низкий|низкая|low)\b", lowered): priority = 0
    reminder = bool(re.search(r"\b(напомни|напоминание|remind)\b", lowered))
    recurrence = next((kind for pattern, kind in [(r"каждый день|ежедневно", "daily"), (r"каждую неделю|еженедельно", "weekly"), (r"каждый месяц|ежемесячно", "monthly")] if re.search(pattern, lowered)), None)
    deadline = None
    day = re.search(r"\b(сегодня|завтра)\b", lowered)
    clock = re.search(r"\b(?:в|at)\s*(\d{1,2}):(\d{2})\b", lowered)
    date = re.search(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}(?:\.\d{4})?)\b", lowered)
    needs_confirmation = bool(day or clock or date or recurrence)
    warning = None
    try:
        if day or date or clock or recurrence:
            target = local
            if date:
                raw = date.group(1)
                if "-" in raw: target = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=local.tzinfo)
                else:
                    fields = [int(x) for x in raw.split('.')]
                    target = local.replace(year=fields[2] if len(fields)==3 else local.year, month=fields[1], day=fields[0])
            elif day and day.group(1)=="завтра": target += timedelta(days=1)
            hour, minute = (int(clock.group(1)),int(clock.group(2))) if clock else (18,0)
            target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
            deadline = target
            if not day and not date and target <= local: deadline += timedelta(days=1)
            # Ask for an explicit correction instead of silently shifting DST gaps.
            if deadline.replace(tzinfo=None) != deadline.astimezone(ZoneInfo("UTC")).astimezone(local.tzinfo).replace(tzinfo=None):
                raise ValueError()
    except ValueError:
        deadline=None;recurrence=None;warning="Дата или время не распознаны. Уточните их новым сообщением."
    hits = [p for p in projects if p.id.lower() in lowered]
    project = hits[0] if len(hits)==1 else None
    coding = project and {"read_code","worktree","code_patch"} <= set(project.allowed_actions) and project.share_code_with_llm and re.search(r"проверь|исправь|баг|bug|fix|review", lowered)
    spec = TaskCreate(title=text[:200], description=text[:40000], status="inbox" if reminder else "ready", priority=priority,
                      project=project.id if project else None, deadline=deadline, recurrence=recurrence,
                      recurrence_anchor_day=deadline.day if recurrence else None, task_timezone=timezone,
                      telegram_owner_id=owner, handler="coding" if coding else "llm", max_steps=8 if coding else 1,
                      max_tokens=8192 if coding else 4096, required_capabilities=["coding"] if coding else ["general"],
                      idempotency_key=f"telegram:{owner}:{update_id}" if update_id is not None else None)
    return spec, needs_confirmation or bool(coding), warning


def describe(spec):
    deadline = spec.deadline.strftime("%d.%m.%Y %H:%M") + " " + spec.task_timezone if spec.deadline else "без срока"
    repeat = {"daily":"ежедневно","weekly":"еженедельно","monthly":"ежемесячно"}.get(spec.recurrence,"нет")
    return f"{spec.title}\nПроект: {spec.project or 'не выбран'}\nПриоритет: {spec.priority}\nДедлайн: {deadline}\nПовтор: {repeat}"
