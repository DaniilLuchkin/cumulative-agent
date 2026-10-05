import asyncio
import unittest

from cumulative_agent.domain import TaskCreate
from cumulative_agent.providers import ProviderFailure
from cumulative_agent.service import Service
from test_system import settings


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_remote_run_settles_reservation_and_never_retries(self):
        entered=asyncio.Event()
        class Slow:
            async def complete(self,*args):
                entered.set();await asyncio.Event().wait()
        service=Service(settings(),providers={'mock':Slow()})
        try:
            task=service.tasks.create(TaskCreate(title='Long task'))
            service.worker.start();await asyncio.wait_for(entered.wait(),2)
            running=list(service.worker.running.values())
            service.tasks.control(task.id,'cancel')
            await asyncio.wait_for(asyncio.gather(*running),2)
            run=service.repo.one('SELECT * FROM task_runs WHERE task_id=?',(task.id,))
            self.assertEqual(run['status'],'cancelled');self.assertEqual(service.repo.task(task.id).status,'cancelled')
            self.assertEqual(run['charged_cost'],run['reserved_cost']);self.assertIsNone(service.repo.task(task.id).next_run_at)
        finally:await service.stop()

    async def test_explicit_retry_creates_one_new_task_without_resetting_old_attempts(self):
        service=Service(settings(),providers={})
        try:
            task=service.tasks.create(TaskCreate(title='Failed'))
            service.repo.execute("UPDATE tasks SET status='failed',attempts=3 WHERE id=?",(task.id,))
            first=service.tasks.control(task.id,'retry');second=service.tasks.control(task.id,'retry')
            self.assertEqual(first.id,second.id);self.assertNotEqual(task.id,first.id)
            self.assertEqual(first.parent_task,task.id);self.assertEqual(first.attempts,0);self.assertEqual(service.repo.task(task.id).attempts,3)
        finally:await service.stop()
