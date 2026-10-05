import json
import unittest
from datetime import datetime, timedelta, timezone

import httpx

from cumulative_agent.domain import TaskCreate
from cumulative_agent.providers import OpenRouter, ProviderFailure
from cumulative_agent.service import Service
from test_system import settings, MockProvider


class ResourceObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cfg=settings(openrouter_api_key='synthetic-key-never-store')
        cfg.models[0].provider='openrouter'
        self.price='.0000001'
        self.fail=False
        async def handle(request):
            if self.fail:return httpx.Response(503,json={'error':'synthetic-key-never-store'})
            if request.url.path.endswith('/key'):
                return httpx.Response(200,json={'data':{'limit_remaining':.01,'limit':1,'label':'synthetic-key-never-store','rate_limit':{'requests':20,'interval':'1m'}}})
            return httpx.Response(200,json={'data':[{'id':'mock/cheap','pricing':{'prompt':self.price,'completion':'.0000002'},'context_length':8192,'top_provider':{'max_completion_tokens':512}},
                {'id':'unapproved/new','pricing':{'prompt':'0','completion':'0'},'context_length':4096}]})
        self.client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.s=Service(cfg,providers={'openrouter':MockProvider()},client=self.client)
    async def asyncTearDown(self):await self.s.stop()

    async def test_observed_metadata_does_not_enable_new_models_or_store_credentials(self):
        result=await self.s.observer.refresh()
        self.assertNotIn('synthetic-key-never-store',result);self.assertNotIn('unapproved/new',json.loads(result)['models'])
        self.assertEqual(len(self.s.repo.models()),1)
        task=self.s.tasks.create(TaskCreate(title='Allowed'))
        selection=self.s.scheduler.next();self.assertIsNotNone(selection);self.assertLessEqual(selection.decision.output_limit,512)

    async def test_price_change_blocks_calls_and_failed_refresh_keeps_prior_observation(self):
        self.price='.0000003';await self.s.observer.refresh()
        task=self.s.tasks.create(TaskCreate(title='Changed price'))
        self.assertIsNone(self.s.scheduler.next());self.assertEqual(self.s.repo.task(task.id).status,'blocked')
        prior=self.s.resources.snapshot()['observations']['openrouter']['observed_at']
        self.fail=True;result=await self.s.observer.refresh()
        self.assertNotIn('synthetic-key-never-store',result)
        self.assertEqual(self.s.resources.snapshot()['observations']['openrouter']['observed_at'],prior)

    async def test_429_honors_provider_cooldown_and_retry_after(self):
        async def limited(request):return httpx.Response(429,headers={'Retry-After':'120'},json={'error':'synthetic-key-never-store'})
        client=httpx.AsyncClient(transport=httpx.MockTransport(limited))
        self.s.providers['openrouter']=OpenRouter('synthetic-key-never-store',client)
        task=self.s.tasks.create(TaskCreate(title='Rate limited'))
        selection=self.s.scheduler.next();run=self.s.worker.claim(selection);await self.s.worker.execute(selection,run)
        retry=datetime.fromisoformat(self.s.repo.task(task.id).next_run_at)
        self.assertGreater(retry,datetime.now(timezone.utc)+timedelta(seconds=110))
        self.s.tasks.create(TaskCreate(title='Another same provider'))
        self.assertIsNone(self.s.scheduler.next())
        self.assertNotIn('synthetic-key-never-store',self.s.repo.one('SELECT error FROM task_runs WHERE id=?',(run,))['error'])
        await client.aclose()
