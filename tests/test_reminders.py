import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from cumulative_agent.domain import Task, TaskCreate, TaskPatch
from cumulative_agent.intake import parse_task
from cumulative_agent.reminders import next_deadline
from cumulative_agent.service import Service
from test_system import settings


class IntakeTests(unittest.TestCase):
    def test_russian_deadline_priority_and_timezone_are_explicit(self):
        current=datetime(2026,10,2,10,tzinfo=timezone.utc)
        spec,confirm,warning=parse_task('Напомни завтра в 18:30, высокий приоритет',[], 'Europe/Moscow',123,7,current)
        self.assertTrue(confirm);self.assertIsNone(warning);self.assertEqual(spec.priority,20);self.assertEqual(spec.status,'inbox')
        self.assertEqual(spec.deadline.isoformat(),'2026-10-03T18:30:00+03:00')

    def test_invalid_clock_and_dst_gap_are_not_silently_shifted(self):
        for text,zone in [('Напомни завтра в 25:99','UTC'),('Напомни 2026-03-29 в 02:30','Europe/Berlin')]:
            spec,confirm,warning=parse_task(text,[],zone)
            self.assertTrue(confirm);self.assertIsNone(spec.deadline);self.assertIsNotNone(warning)

    def test_month_end_anchor_and_dst_wall_clock_are_preserved(self):
        task=Task(title='Monthly',deadline=datetime(2026,1,31,18,tzinfo=timezone.utc),recurrence='monthly',recurrence_anchor_day=31)
        self.assertEqual(next_deadline(task,datetime(2026,2,1,tzinfo=timezone.utc)).day,28)
        task.deadline=next_deadline(task,datetime(2026,2,1,tzinfo=timezone.utc))
        self.assertEqual(next_deadline(task,datetime(2026,3,1,tzinfo=timezone.utc)).day,31)
        daily=Task(title='Daily',deadline=datetime(2026,3,28,9,tzinfo=ZoneInfo('Europe/Berlin')),recurrence='daily',task_timezone='Europe/Berlin')
        date=next_deadline(daily,datetime(2026,3,28,10,tzinfo=ZoneInfo('Europe/Berlin')))
        self.assertEqual(date.hour,9);self.assertEqual(date.utcoffset(),timedelta(hours=2))


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s=Service(settings(telegram_owner_ids=[123]),providers={})
    async def asyncTearDown(self):await self.s.stop()

    async def test_outbox_deduplicates_deadline_and_skips_removed_owner_or_completed_task(self):
        current=datetime.now(timezone.utc)
        task=self.s.tasks.create(TaskCreate(title='Deadline',status='inbox',deadline=current+timedelta(minutes=5),telegram_owner_id=123))
        self.s.reminders.tick(current);self.s.reminders.tick(current)
        self.assertEqual(len(self.s.repo.all('SELECT * FROM notifications')),1)
        self.s.tasks.control(task.id,'complete')
        calls=[]
        async def send(*args):calls.append(args)
        self.s.telegram.send=send
        await self.s.reminders.deliver(self.s.telegram)
        self.assertFalse(calls);self.assertEqual(self.s.repo.one('SELECT status FROM notifications')['status'],'cancelled')

    async def test_one_future_recurrence_is_created_after_manual_completion(self):
        current=datetime.now(timezone.utc)
        task=self.s.tasks.create(TaskCreate(title='Daily',status='inbox',deadline=current-timedelta(days=400),recurrence='daily'))
        self.s.tasks.control(task.id,'complete')
        self.s.reminders.tick(current);self.s.reminders.tick(current)
        children=self.s.repo.all("SELECT * FROM tasks WHERE json_extract(payload,'$.parent_task')=?",(task.id,))
        self.assertEqual(len(children),1)
        child=self.s.repo.decode_task(children[0]);self.assertEqual(child.status,'inbox');self.assertGreater(child.deadline,current)

    async def test_telegram_confirmation_only_owner_and_idempotent(self):
        calls=[]
        async def call(method,payload):calls.append((method,payload));return True
        self.s.telegram.call=call
        await self.s.telegram.handle({'update_id':44,'message':{'from':{'id':123},'chat':{'id':123,'type':'private'},'text':'Напомни завтра в 18:00'}})
        self.assertEqual(len(self.s.repo.tasks()),0)
        query={'id':'callback','from':{'id':999},'message':{'chat':{'id':999,'type':'private'}},'data':'intake:44:confirm'}
        self.assertFalse(await self.s.telegram.handle_confirmation(query))
        query['from']['id']=123;query['message']['chat']['id']=123
        await self.s.telegram.handle_confirmation(query);await self.s.telegram.handle_confirmation(query)
        self.assertEqual(len(self.s.repo.tasks()),1)

    async def test_monthly_anchor_uses_local_day_when_deadline_is_utc(self):
        task=self.s.tasks.create(TaskCreate(title='Monthly',status='inbox',deadline=datetime(2026,1,30,21,30,tzinfo=timezone.utc),recurrence='monthly',task_timezone='Europe/Moscow'))
        self.s.tasks.control(task.id,'complete')
        self.s.reminders.tick(datetime(2026,2,1,tzinfo=timezone.utc))
        row=self.s.repo.one("SELECT * FROM tasks WHERE json_extract(payload,'$.parent_task')=?",(task.id,))
        child=self.s.repo.decode_task(row)
        self.assertEqual(child.recurrence_anchor_day,31)
        self.assertEqual(child.deadline.astimezone(ZoneInfo('Europe/Moscow')).day,28)
        self.assertEqual(next_deadline(child,datetime(2026,3,1,tzinfo=timezone.utc)).day,31)
        unchanged=self.s.tasks.update(child.id,TaskPatch(priority=20,deadline=child.deadline,recurrence='monthly',task_timezone='Europe/Moscow'))
        self.assertEqual(unchanged.recurrence_anchor_day,31)
        edited=self.s.tasks.update(child.id,TaskPatch(deadline=datetime(2026,2,9,21,30,tzinfo=timezone.utc)))
        self.assertEqual(edited.recurrence_anchor_day,10)
