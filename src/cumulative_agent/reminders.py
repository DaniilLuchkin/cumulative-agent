import calendar
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .domain import TaskCreate, now
from .ownership import telegram_owner_ids


def next_deadline(task, current):
    zone=ZoneInfo(task.task_timezone)
    original=task.deadline.astimezone(zone); local=current.astimezone(zone)
    if task.recurrence in ("daily","weekly"):
        days=1 if task.recurrence=="daily" else 7
        count=max(1, (local.date()-original.date()).days//days)
        target=original+timedelta(days=days*count)
        if target<=local: target+=timedelta(days=days)
    else:
        count=max(1,(local.year-original.year)*12+local.month-original.month)
        index=original.year*12+original.month-1+count
        year,month=index//12,index%12+1
        anchor=task.recurrence_anchor_day or original.day
        target=original.replace(year=year,month=month,day=min(anchor,calendar.monthrange(year,month)[1]))
        if target<=local:
            index+=1;year,month=index//12,index%12+1
            target=original.replace(year=year,month=month,day=min(anchor,calendar.monthrange(year,month)[1]))
    # DST gap policy: normalize forward; preserve wall-clock time otherwise.
    return target.astimezone(timezone.utc).astimezone(zone)


class Reminders:
    def __init__(self,repo,settings,tasks): self.repo,self.settings,self.tasks=repo,settings,tasks

    def tick(self,current=None):
        current=current or datetime.now(timezone.utc)
        owners=telegram_owner_ids(self.repo,self.settings)
        rows=self.repo.all("""SELECT * FROM tasks WHERE type='user' AND json_extract(payload,'$.deadline') IS NOT NULL
            AND (status NOT IN ('done','cancelled') OR (status='done' AND json_extract(payload,'$.recurrence') IS NOT NULL
            AND NOT EXISTS(SELECT 1 FROM settings s WHERE s.key='recurrence_created:' || tasks.id)))""")
        for row in rows:
            task=self.repo.decode_task(row)
            if task.status=='done' and task.recurrence:
                spec={key:getattr(task,key) for key in TaskCreate.model_fields}
                deadline=next_deadline(task,current)
                spec.update(deadline=deadline,status='inbox' if task.completed_manually or task.manual_previous_status=='inbox' else 'ready',
                            parent_task=task.id,recurring_origin=task.recurring_origin or task.id,
                            recurrence_anchor_day=(task.recurrence_anchor_day or task.deadline.astimezone(ZoneInfo(task.task_timezone)).day) if task.recurrence=='monthly' else None,
                            start_after=current if task.completed_manually else max(current,task.deadline),
                            idempotency_key='recurrence:'+task.id)
                if not self.repo.get_setting('recurrence_created:'+task.id,False):
                    child=self.tasks.create(TaskCreate(**spec));self.repo.set_setting('recurrence_created:'+task.id,child.id)
                    self.repo.event('recurrence_created',task.id,next_task=child.id,deadline=deadline.isoformat())
            if task.status in ('done','cancelled'): continue
            owner=task.telegram_owner_id or (next(iter(owners)) if len(owners)==1 else None)
            if owner not in owners: continue
            seconds=(task.deadline-current).total_seconds()
            kind='overdue' if seconds<=0 else 'due_soon' if seconds<=task.notify_before_minutes*60 else None
            if not kind: continue
            identifier=f'{kind}:{task.id}:{task.deadline.isoformat()}'
            text=('Срок наступил' if kind=='overdue' else 'Скоро дедлайн')+f': {task.title}\n'+task.deadline.astimezone(ZoneInfo(task.task_timezone)).strftime('%d.%m.%Y %H:%M')+' '+task.task_timezone
            self.repo.execute("INSERT OR IGNORE INTO notifications(id,task_id,owner_id,kind,payload,created_at) VALUES(?,?,?,?,?,?)",
                              (identifier,task.id,owner,kind,json.dumps({'text':text}),now()))

    async def deliver(self,telegram):
        owners=telegram_owner_ids(self.repo,self.settings)
        for notification in self.repo.all("SELECT * FROM notifications WHERE status='pending' AND attempts<3 ORDER BY created_at LIMIT 20"):
            task=self.repo.task(notification['task_id'])
            if notification['owner_id'] not in owners or task.status in ('done','cancelled') or not task.deadline or not notification['id'].endswith(':'+task.deadline.isoformat()):
                self.repo.execute("UPDATE notifications SET status='cancelled' WHERE id=?",(notification['id'],));continue
            self.repo.execute("UPDATE notifications SET attempts=attempts+1 WHERE id=?",(notification['id'],))
            try:
                await telegram.send(notification['owner_id'],json.loads(notification['payload'])['text'])
                self.repo.execute("UPDATE notifications SET status='sent',sent_at=? WHERE id=?",(now(),notification['id']))
                self.repo.event('reminder_sent',task.id,notification_kind=notification['kind'])
            except RuntimeError:
                self.repo.event('reminder_delivery_failed',task.id)
                if notification['attempts']>=2:
                    self.repo.execute("UPDATE notifications SET status='failed' WHERE id=?",(notification['id'],))
