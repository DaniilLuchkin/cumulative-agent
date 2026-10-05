import asyncio
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from cumulative_agent.api import create_app
from cumulative_agent.config import Limits, Settings
from cumulative_agent.domain import Blocked, Completion, Model, Project, TaskCreate
from cumulative_agent.providers import OpenRouter, ProviderFailure
from cumulative_agent.repository import Repository
from cumulative_agent.routing import Router
from cumulative_agent.service import Service


class MockProvider:
    def __init__(self, error=False, delay=0):
        self.calls = 0
        self.error, self.delay = error, delay

    async def complete(self, model, prompt, output_limit, timeout):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise ProviderFailure("Test transport failure")
        return Completion(text="Verified mock report", input_tokens=50, output_tokens=25, cost=(50 * model.input_price + 25 * model.output_price) / 1_000_000)


def settings(database=":memory:", **overrides):
    base = dict(database=database, api_token="test-token-that-is-long-enough-123", limits=Limits(daily_cost_limit=1, background_cost_limit=.5, task_cost_limit=.1, min_free_disk_bytes=0, min_available_memory_bytes=0, max_load_per_cpu=1000, max_background_backlog=0),
                models=[Model(id="mock/cheap", provider="mock", enabled=True, input_price=.1, output_price=.2, capabilities=["general", "coding"], groups=["cheap", "general", "coding"], quota_remaining=100000, quota_limit=100000)])
    base.update(overrides)
    return Settings(**base)


class DomainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = MockProvider()
        self.s = Service(settings(), providers={"mock": self.provider})

    async def asyncTearDown(self):
        await self.s.stop()

    def task(self, **kwargs):
        return self.s.tasks.create(TaskCreate(title="Inspect code", **kwargs))

    async def execute_next(self):
        selected = self.s.scheduler.next()
        self.assertIsNotNone(selected)
        run_id = self.s.worker.claim(selected)
        self.assertIsNotNone(run_id)
        await self.s.worker.execute(selected, run_id)
        return run_id

    async def test_user_strictly_precedes_background(self):
        self.task(type="maintenance", priority=1000)
        user = self.task(priority=-1000)
        self.assertEqual(self.s.scheduler.next().task.id, user.id)

    async def test_background_claim_yields_to_new_user(self):
        self.task(type="maintenance", handler="system_report", required_capabilities=[])
        selected = self.s.scheduler.next()
        self.task(handler="system_report", required_capabilities=[])
        with self.assertRaises(Blocked):
            self.s.worker.claim(selected)

    async def test_blocked_paid_user_allows_useful_free_local_background(self):
        self.s.settings.limits.daily_cost_limit = 0
        user = self.task()
        background = self.task(type="maintenance", handler="system_report", required_capabilities=[])
        selected = self.s.scheduler.next()
        self.assertEqual(selected.task.id, background.id)
        run_id = self.s.worker.claim(selected)
        self.assertIsNotNone(run_id)
        await self.s.worker.execute(selected, run_id)
        self.assertEqual(self.s.repo.task(user.id).status, "blocked")

    async def test_provider_balance_reserved_refunded_and_shared_rate_enforced(self):
        self.s.repo.execute("UPDATE providers SET payload=json_set(payload,'$.balance',0.1,'$.requests_per_minute',1)")
        self.task()
        run_id = await self.execute_next()
        provider = json.loads(self.s.repo.one("SELECT payload FROM providers WHERE id='mock'")["payload"])
        self.assertAlmostEqual(provider["balance"], .1 - .00001)
        self.task()
        self.assertIsNone(self.s.scheduler.next())

    async def test_quota_reset_deducts_active_reservations_and_does_not_repeat(self):
        task = self.task()
        selected = self.s.scheduler.next()
        run_id = self.s.worker.claim(selected)
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.s.repo.execute("UPDATE models SET payload=json_set(payload,'$.resets_at',?,'$.reset_period_seconds',3600)", (past,))
        self.s.resources.refresh()
        model = self.s.repo.models()[0]
        self.assertEqual(model.quota_remaining, model.quota_limit - selected.decision.input_bound - selected.decision.output_limit)
        reset = model.resets_at
        self.s.resources.refresh()
        self.assertEqual(self.s.repo.models()[0].resets_at, reset)
        self.s.worker.finish(run_id, None, "No remote call sent", None, 0)
        self.assertEqual(self.s.repo.models()[0].quota_remaining, model.quota_limit)

    async def test_provider_cost_overrun_pauses_scheduler_and_records_true_charge(self):
        class Overrun:
            async def complete(self, model, prompt, output_limit, timeout):
                return Completion(text="report", input_tokens=10, output_tokens=10, cost=.5)
        self.s.providers["mock"] = Overrun()
        self.task(max_attempts=1)
        await self.execute_next()
        self.assertTrue(self.s.repo.get_setting("paused"))
        self.assertEqual(self.s.metrics.snapshot()["cost"], .5)
        self.assertEqual(self.s.metrics.snapshot()["tasks_failed"], 1)

    async def test_deadline_affects_order_within_user_tier(self):
        self.task()
        due = self.task(deadline=datetime.now(timezone.utc) + timedelta(minutes=1))
        self.assertEqual(self.s.scheduler.next().task.id, due.id)

    async def test_shutdown_settles_inflight_call(self):
        self.provider.delay = 100
        task = self.task(max_attempts=1)
        self.s.worker.start()
        for _ in range(100):
            if self.provider.calls:
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.provider.calls, 1)
        await self.s.worker.stop()
        self.assertEqual(self.s.repo.task(task.id).status, "failed")
        self.assertEqual(self.s.metrics.snapshot()["usage_unknown_runs"], 1)

    async def test_dependency_unblocks_and_subtask_metadata_survives(self):
        first = self.task(handler="system_report", required_capabilities=[])
        second = self.task(dependencies=[first.id], parent_task=first.id, priority=100)
        self.assertEqual(self.s.scheduler.next().task.id, first.id)
        self.assertEqual(self.s.repo.task(second.id).blocked_reason, "wait:dependencies")
        await self.execute_next()
        self.assertEqual(self.s.scheduler.next().task.id, second.id)
        self.assertEqual(self.s.repo.task(second.id).parent_task, first.id)

    async def test_dependency_rejects_unknown_and_creation_is_idempotent(self):
        with self.assertRaises(ValueError):
            self.task(dependencies=["missing"])
        a = self.task(idempotency_key="input:1")
        b = self.task(idempotency_key="input:1")
        self.assertEqual(a.id, b.id)
        self.assertEqual(len(self.s.repo.tasks()), 1)

    async def test_inbox_requires_operator_ready(self):
        task = self.task(status="inbox", handler="system_report", required_capabilities=[])
        self.assertIsNone(self.s.scheduler.next())
        self.s.tasks.control(task.id, "ready")
        self.assertEqual(self.s.scheduler.next().task.id, task.id)

    async def test_success_records_cost_usage_decisions_and_refunds_quota(self):
        task = self.task()
        before = self.s.repo.models()[0].quota_remaining
        run_id = await self.execute_next()
        self.assertEqual(self.s.repo.task(task.id).status, "done")
        run = self.s.repo.one("SELECT * FROM task_runs WHERE id=?", (run_id,))
        self.assertIn("routing", json.loads(run["decision"]))
        self.assertEqual(self.s.repo.models()[0].quota_remaining, before - 75)
        self.assertEqual(self.s.metrics.snapshot()["tasks_completed"], 1)
        self.assertAlmostEqual(self.s.metrics.snapshot()["cost"], .00001)

    async def test_unknown_remote_usage_charges_reservation_and_retry_budget(self):
        self.provider.error = True
        task = self.task(max_attempts=2)
        run_id = await self.execute_next()
        run = self.s.repo.one("SELECT * FROM task_runs WHERE id=?", (run_id,))
        self.assertEqual(run["charged_cost"], run["reserved_cost"])
        self.assertEqual(self.s.repo.task(task.id).status, "ready")
        self.assertEqual(self.s.metrics.snapshot()["usage_unknown_runs"], 1)
        self.assertIsNone(self.s.scheduler.next())  # backoff enforced
        self.s.repo.execute("UPDATE tasks SET next_run_at=NULL WHERE id=?", (task.id,))
        await self.execute_next()
        self.assertEqual(self.s.repo.task(task.id).status, "failed")
        with self.assertRaises(ValueError):
            self.s.tasks.control(task.id, "ready")

    async def test_timeout_is_bounded_and_charged(self):
        self.provider.delay = 2
        task = self.task(timeout_seconds=1, max_attempts=1)
        run_id = await self.execute_next()
        run = self.s.repo.one("SELECT * FROM task_runs WHERE id=?", (run_id,))
        self.assertEqual(self.s.repo.task(task.id).status, "failed")
        self.assertIn("timeout", run["error"])
        self.assertLess(run["latency"], 1.5)
        self.assertEqual(run["charged_cost"], run["reserved_cost"])

    async def test_daily_budget_reservation_prevents_second_claim(self):
        self.s.settings.limits.max_concurrent_agents = 2
        self.task()
        self.task()
        selected = self.s.scheduler.next()
        other = self.s.scheduler.next({selected.task.id})
        self.s.settings.limits.daily_cost_limit = selected.decision.reserved_cost * 1.1
        self.s.worker.claim(selected)
        with self.assertRaises(Blocked):
            self.s.worker.claim(other)

    async def test_task_budget_accumulates_across_attempts(self):
        self.provider.error = True
        task = self.task()
        selected = self.s.scheduler.next()
        self.s.settings.limits.task_cost_limit = selected.decision.reserved_cost * 1.1
        run_id = self.s.worker.claim(selected)
        await self.s.worker.execute(selected, run_id)
        self.s.repo.execute("UPDATE tasks SET next_run_at=NULL WHERE id=?", (task.id,))
        with self.assertRaises(Blocked):
            self.s.worker.claim(selected)
        self.assertIsNone(self.s.scheduler.next())

    async def test_manual_quota_context_and_capabilities_fail_closed(self):
        self.task(required_capabilities=["vision"])
        self.assertIsNone(self.s.scheduler.next())
        self.task(description="x" * 10000)
        self.assertIsNone(self.s.scheduler.next())
        self.task(preferred_model="not-allowed")
        self.assertIsNone(self.s.scheduler.next())
        self.task()
        self.s.resources.manual_quota("mock/cheap", 0)
        self.assertIsNone(self.s.scheduler.next())

    async def test_rate_limit_is_enforced_before_remote_request(self):
        self.s.settings.limits.max_concurrent_agents = 2
        self.s.repo.execute("UPDATE models SET payload=json_set(payload,'$.requests_per_minute',1)")
        self.task()
        self.task()
        selected = self.s.scheduler.next()
        other = self.s.scheduler.next({selected.task.id})
        self.s.worker.claim(selected)
        with self.assertRaises(Blocked):
            self.s.worker.claim(other)

    async def test_lease_recovery_conservatively_charges_unknown_call(self):
        task = self.task(max_attempts=1)
        run_id = self.s.worker.claim(self.s.scheduler.next())
        self.s.repo.execute("UPDATE task_runs SET lease_until=?,remote_started=1 WHERE id=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), run_id))
        self.s.worker.recover()
        self.assertEqual(self.s.repo.task(task.id).status, "failed")
        self.assertEqual(self.s.metrics.snapshot()["usage_unknown_runs"], 1)
        self.s.worker.recover()
        self.assertEqual(self.s.metrics.snapshot()["runs"], 1)

    async def test_pause_background_toggle_and_manual_block(self):
        user = self.task(handler="system_report", required_capabilities=[])
        self.s.tasks.control(user.id, "block")
        bg = self.task(type="maintenance", handler="system_report", required_capabilities=[])
        self.assertEqual(self.s.scheduler.next().task.id, bg.id)
        self.s.repo.set_setting("background_enabled", False)
        self.assertIsNone(self.s.scheduler.next())
        self.s.tasks.control(user.id, "ready")
        self.s.repo.set_setting("paused", True)
        self.assertIsNone(self.s.scheduler.next())

    async def test_approval_never_executes_dangerous_proposal(self):
        task = self.task(proposal_action="merge")
        approval = self.s.repo.one("SELECT id FROM approvals")["id"]
        self.s.tasks.decide_approval(approval, True)
        self.assertIsNone(self.s.scheduler.next())
        with self.assertRaises(ValueError):
            self.s.tasks.control(task.id, "ready")

    async def test_planner_dedup_cooldown_and_backlog(self):
        self.s.settings.limits.max_background_backlog = 2
        self.assertEqual(self.s.planner.inspect(), 2)
        self.assertEqual(self.s.planner.inspect(), 0)
        self.assertEqual(len(self.s.repo.tasks()), 2)
        for _ in range(2):
            await self.execute_next()
        self.assertEqual(self.s.planner.inspect(), 0)

    async def test_memory_is_scoped_opt_in_and_bounded(self):
        task = self.task()
        self.s.memory.add("global", None, "private secret", [], False)
        self.s.memory.add("task", "another-task", "irrelevant", [], True)
        self.s.memory.add("task", task.id, "relevant", [], True)
        self.assertEqual(self.s.memory.retrieve(task), ["relevant"])
        self.assertEqual(self.s.memory.retrieve(task, max_bytes=2), [])

    async def test_local_index_excludes_secrets_symlinks_and_never_changes_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("# TODO improve\nprint('hello')\n")
            (root / ".env").write_text("SECRET=never-read")
            (root / "key.pem").write_text("private-key")
            (root / "linked.py").symlink_to(root / "app.py")
            self.s.settings.project_roots = [tmp]
            self.s.projects.register(Project(id="demo", path=tmp, allowed_actions=["file_index", "todo_scan"]))
            self.task(project="demo", handler="todo_scan", required_capabilities=[])
            run_id = await self.execute_next()
            report = json.loads(self.s.repo.one("SELECT result FROM task_runs WHERE id=?", (run_id,))["result"])
            self.assertEqual(report["files_indexed"], 1)
            self.assertEqual(report["todo_locations"], [{"path": "app.py", "line": 1}])
            self.assertEqual((root / ".env").read_text(), "SECRET=never-read")
            self.assertEqual((root / "app.py").read_text(), "# TODO improve\nprint('hello')\n")

    async def test_project_root_requires_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self.s.projects.register(Project(id="unsafe", path=tmp))

    async def test_telegram_acl_and_idempotent_conversational_task(self):
        self.s.settings.telegram_owner_ids = [123]
        sent = []
        async def send(chat, text):
            sent.append((chat, text))
        self.s.telegram.send = send
        def update(owner, kind="private"):
            return {"update_id": 99, "message": {"from": {"id": owner}, "chat": {"id": owner, "type": kind}, "text": "Проверь проект priority=10"}}
        self.assertFalse(await self.s.telegram.handle(update(999)))
        self.assertFalse(await self.s.telegram.handle(update(123, "group")))
        await self.s.telegram.handle(update(123))
        await self.s.telegram.handle(update(123))
        self.assertEqual(len(self.s.repo.tasks()), 1)
        self.assertEqual(self.s.repo.tasks()[0].priority, 10)
        self.assertEqual(len(sent), 2)

    async def test_openrouter_request_parameters_and_bad_usage(self):
        observed = []
        def handler(request):
            observed.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 50, "completion_tokens": 10, "cost": .001}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OpenRouter("fake-key", client)
            completion = await adapter.complete(self.s.repo.models()[0], "task", 100, 1)
        self.assertEqual(completion.output_tokens, 10)
        self.assertEqual(observed[0]["max_tokens"], 100)
        self.assertFalse(observed[0]["provider"]["allow_fallbacks"])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"secret": "do-not-echo"}))) as client:
            with self.assertRaises(ProviderFailure) as error:
                await OpenRouter("fake-key", client).complete(self.s.repo.models()[0], "task", 100, 1)
            self.assertNotIn("secret", str(error.exception))

    async def test_human_quality_changes_routing(self):
        other = self.s.repo.models()[0].model_copy(update={"id": "mock/other", "quality": .8})
        self.s.repo.execute("INSERT INTO models VALUES(?,?)", (other.id, other.model_dump_json()))
        self.task()
        self.assertEqual(self.s.scheduler.next().decision.model, "mock/other")
        run_id = await self.execute_next()
        self.s.repo.execute("UPDATE usage SET quality_score=0.1 WHERE run_id=?", (run_id,))
        self.task()
        self.assertEqual(self.s.scheduler.next().decision.model, "mock/cheap")

    async def test_event_driven_worker_wakes_from_another_thread(self):
        await self.s.start()
        thread = threading.Thread(target=lambda: self.task(handler="system_report", required_capabilities=[]))
        thread.start()
        thread.join()
        for _ in range(100):
            if self.s.metrics.snapshot()["tasks_completed"]:
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.s.metrics.snapshot()["tasks_completed"], 1)


class PersistenceTests(unittest.TestCase):
    def test_sqlite_reservation_race_and_quota_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "agent.db")
            cfg = settings(db)
            first = Service(cfg, providers={"mock": MockProvider()})
            task = first.tasks.create(TaskCreate(title="persistent"))
            selected = first.scheduler.next()
            second = Service(cfg, providers={"mock": MockProvider()})
            barrier = threading.Barrier(2)
            results = []
            def claim(service):
                barrier.wait()
                results.append(service.worker.claim(selected))
            threads = [threading.Thread(target=claim, args=(s,)) for s in (first, second)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sum(r is not None for r in results), 1)
            quota = first.repo.models()[0].quota_remaining
            third = Service(cfg, providers={"mock": MockProvider()})
            self.assertEqual(third.repo.models()[0].quota_remaining, quota)
            self.assertEqual(third.repo.task(task.id).attempts, 1)
            for s in (first, second, third):
                asyncio.run(s.stop())


class APITests(unittest.IsolatedAsyncioTestCase):
    async def test_api_auth_dashboard_task_result_and_quality(self):
        cfg = settings()
        service = Service(cfg, providers={"mock": MockProvider()})
        app = create_app(cfg, service=service, start_worker=False)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers = {"Authorization": "Bearer " + cfg.api_token}
            self.assertEqual((await client.get("/health")).status_code, 200)
            self.assertEqual((await client.get("/api/tasks")).status_code, 401)
            self.assertEqual((await client.get("/api/tasks", headers={"Authorization": "Bearer wrong"})).status_code, 401)
            self.assertEqual((await client.get("/")).status_code, 200)
            response = await client.post("/api/tasks", headers=headers, json={"title": "report", "handler": "system_report", "required_capabilities": []})
            self.assertEqual(response.status_code, 201, response.text)
            task_id = response.json()["id"]
            detail = await client.get("/api/tasks/" + task_id, headers=headers)
            self.assertEqual(detail.json()["task"]["status"], "ready")
            result = await client.patch("/api/tasks/" + task_id + "/priority", headers=headers, json={"priority": 15})
            self.assertEqual(result.json()["priority"], 15)
            self.assertEqual((await client.post("/api/tasks/" + task_id + "/control", headers=headers, json={"action": "block"})).status_code, 200)
            self.assertEqual((await client.get("/api/tasks/missing", headers=headers)).status_code, 404)
            self.assertEqual((await client.post("/api/tasks", headers=headers, json={"title": "bad", "deadline": "2026-10-02T01:00:00"})).status_code, 422)
            self.assertEqual((await client.post("/api/tasks", headers=headers, content=b"x" * 256001)).status_code, 413)
            self.assertNotIn(cfg.api_token, (await client.get("/api/resources", headers=headers)).text)

    async def test_missing_token_fails_closed(self):
        cfg = settings(api_token="")
        service = Service(cfg, providers={})
        app = create_app(cfg, service=service, start_worker=False)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            self.assertEqual((await client.get("/api/tasks")).status_code, 503)

    async def test_api_worker_processes_enqueued_task(self):
        cfg = settings()
        service = Service(cfg, providers={"mock": MockProvider()})
        app = create_app(cfg, service=service)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers = {"Authorization": "Bearer " + cfg.api_token}
            response = await client.post("/api/tasks", headers=headers, json={"title": "thread wake", "handler": "system_report", "required_capabilities": []})
            self.assertEqual(response.status_code, 201, response.text)
            task_id = response.json()["id"]
            for _ in range(100):
                status = (await client.get("/api/tasks/" + task_id, headers=headers)).json()["task"]["status"]
                if status == "done":
                    break
                await asyncio.sleep(.01)
            self.assertEqual(status, "done")


if __name__ == "__main__":
    unittest.main()
