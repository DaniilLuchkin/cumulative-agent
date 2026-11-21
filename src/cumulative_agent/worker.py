from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .agents import LocalAgents
from .config import Settings
from .domain import BackgroundYield, Blocked, Completion, Model, now
from .planner import Planner
from .providers import Provider, ProviderFailure
from .repository import Repository
from .scheduler import Scheduler, Selection
from .observations import provider_budget_remaining


logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, repo: Repository, settings: Settings, scheduler: Scheduler, planner: Planner, agents: LocalAgents, providers: dict[str, Provider]):
        self.repo, self.settings, self.scheduler, self.planner, self.agents, self.providers = repo, settings, scheduler, planner, agents, providers
        self.running: dict[str, asyncio.Task] = {}
        self.enabled = False
        self._timer = None
        self._tick_task = None
        self._loop = None
        self.coding = None
        self.reminders = None
        self.observer = None
        # task_id -> callback(accumulated_text); registered by interactive front-ends.
        self.stream_sinks: dict = {}

    def should_yield(self,task):
        if task.type=='user':return False
        if self.repo.get_setting('paused',False) or not self.repo.get_setting('background_enabled',True):return True
        selection=self.scheduler.next()
        return bool(selection and selection.task.type=='user')

    def start(self):
        self._loop = asyncio.get_running_loop()
        self.enabled = True
        self.wake()

    def wake(self):
        # FastAPI sync endpoints execute in a thread pool.
        if self.enabled and self._loop:
            self._loop.call_soon_threadsafe(self._wake_on_loop)

    def _wake_on_loop(self):
        if self.enabled:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            if not self._tick_task or self._tick_task.done():
                self._tick_task = asyncio.create_task(self._tick())

    async def _tick(self):
        try:
            self.reminders.tick()
            self.observer.schedule(self.planner.tasks)
            self.scheduler.resources.refresh()
            self.recover()
            excluded = set()
            for _ in range(self.settings.limits.max_concurrent_agents):
                if len(self.running) >= self.settings.limits.max_concurrent_agents:
                    break
                selection = self.scheduler.next(excluded)
                if not selection:
                    break
                excluded.add(selection.task.id)
                try:
                    run_id = self.claim(selection)
                except Blocked as exc:
                    self.scheduler._wait(selection.task, str(exc))
                    # Recheck other user tasks/background within a bounded queue scan.
                    for _ in range(100):
                        selection = self.scheduler.next(excluded)
                        if not selection:
                            break
                        excluded.add(selection.task.id)
                        try:
                            run_id = self.claim(selection)
                            break
                        except Blocked as other:
                            self.scheduler._wait(selection.task, str(other))
                    else:
                        selection = None
                    if not selection:
                        break
                if run_id:
                    execution = asyncio.create_task(self.execute(selection, run_id))
                    self.running[run_id] = execution
                    execution.add_done_callback(lambda done, rid=run_id: self._completed(rid, done))
            if not self.running and not self.scheduler.next():
                self.planner.inspect()
        except Exception as exc:
            # No exception messages: third-party errors can contain request data.
            logger.error("worker_tick_failed", extra={"error_type": type(exc).__name__})
            self.repo.event("worker_tick_failed", error_type=type(exc).__name__)
        finally:
            if self.enabled:
                self._timer = asyncio.get_running_loop().call_later(self.settings.limits.poll_seconds, self.wake)

    def _completed(self, run_id: str, task: asyncio.Task):
        self.running.pop(run_id, None)
        if task.cancelled():
            self.finish(run_id, None, "Run cancelled before execution", None, 0)
        if not task.cancelled() and task.exception():
            logger.error("worker_execution_failed", extra={"run_id": run_id, "error_type": type(task.exception()).__name__})
        self.wake()

    def request_cancel(self, task_id: str):
        def cancel():
            for run_id, execution in list(self.running.items()):
                row = self.repo.one("SELECT task_id FROM task_runs WHERE id=?", (run_id,))
                if row and row["task_id"] == task_id:
                    execution.cancel()
        if self._loop:
            self._loop.call_soon_threadsafe(cancel)

    def claim(self, selection: Selection) -> str | None:
        task, decision = selection.task, selection.decision
        limits = self.settings.limits
        current = datetime.now(timezone.utc)
        reserved_cost = decision.reserved_cost if decision else 0
        tokens = decision.input_bound + decision.output_limit if decision else 0
        with self.repo.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task.id,)).fetchone()
            if not row or row["status"] not in ("ready", "blocked"):
                return None
            if row["status"] == "blocked" and not (row["blocked_reason"] or "").startswith("wait:"):
                return None
            if self.repo.get_setting("paused", False):
                return None
            if task.type != "user" and not self.repo.get_setting("background_enabled", True):
                return None
            if task.type != "user":
                current_choice = self.scheduler.next()
                if current_choice and current_choice.task.type == "user":
                    raise Blocked("yield_to_user_task")
            if db.execute("SELECT COUNT(*) FROM task_runs WHERE status='running'").fetchone()[0] >= limits.max_concurrent_agents:
                return None
            if row["attempts"] >= task.max_attempts:
                raise Blocked("attempt_limit")
            if row["next_run_at"] and row["next_run_at"] > current.isoformat():
                return None
            if db.execute("SELECT COUNT(*) FROM dependencies d JOIN tasks t ON t.id=d.depends_on WHERE d.task_id=? AND t.status!='done'", (task.id,)).fetchone()[0]:
                raise Blocked("dependencies")
            day = current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            # Include all active reservations, even across midnight, to avoid day rollover races.
            total = db.execute("SELECT COALESCE(SUM(COALESCE(charged_cost,reserved_cost)),0) FROM task_runs WHERE started_at>=? OR status='running'", (day,)).fetchone()[0]
            background = db.execute("SELECT COALESCE(SUM(COALESCE(charged_cost,reserved_cost)),0) FROM task_runs WHERE background=1 AND (started_at>=? OR status='running')", (day,)).fetchone()[0]
            task_spent = db.execute("SELECT COALESCE(SUM(COALESCE(charged_cost,reserved_cost)),0) FROM task_runs WHERE task_id=?", (task.id,)).fetchone()[0]
            task_cap = min(task.max_cost if task.max_cost is not None else limits.task_cost_limit, limits.task_cost_limit)
            if reserved_cost > 0:
                if total + reserved_cost > limits.daily_cost_limit + 1e-12:
                    raise Blocked("daily_budget")
                if task.type != "user" and background + reserved_cost > limits.background_cost_limit + 1e-12:
                    raise Blocked("background_budget")
                if task_spent + reserved_cost > task_cap + 1e-12:
                    raise Blocked("task_budget_across_retries")
            if decision:
                cooldown=self.repo.get_setting('provider_cooldown:'+decision.provider)
                if cooldown and cooldown>current.isoformat():raise Blocked('provider_cooldown')
                live_remaining=provider_budget_remaining(self.repo,decision.provider)
                if live_remaining is not None and reserved_cost>live_remaining+1e-12:raise Blocked('observed_key_budget')
                stored = db.execute("SELECT payload FROM models WHERE id=?", (decision.model,)).fetchone()
                model = Model.model_validate_json(stored["payload"])
                observation=self.repo.one('SELECT payload FROM resource_observations WHERE provider=?',(model.provider,))
                if observation:
                    data=json.loads(observation['payload']);live=data.get('models',{}).get(model.id)
                    if data.get('catalog_checked') and not live:raise Blocked('model_missing_from_catalog')
                    if live and (abs(live['input_price']-model.input_price)>1e-9 or abs(live['output_price']-model.output_price)>1e-9):raise Blocked('provider_price_changed')
                    if live and (tokens>live['context_size'] or decision.output_limit>(live.get('max_output_tokens') or model.max_output_tokens)):raise Blocked('provider_context_changed')
                if not model.enabled or model.provider not in self.providers:
                    raise Blocked("model_unavailable")
                if model.quota_remaining is not None and model.quota_remaining < tokens:
                    raise Blocked("quota_exhausted")
                minute = (current - timedelta(seconds=60)).isoformat()
                provider_row = db.execute("SELECT payload FROM providers WHERE id=?", (model.provider,)).fetchone()
                provider = json.loads(provider_row["payload"])
                provider_count = db.execute("SELECT COUNT(*) FROM task_runs WHERE provider=? AND started_at>?", (model.provider, minute)).fetchone()[0]
                if provider_count >= provider.get("requests_per_minute", 20):
                    raise Blocked("provider_rate_limit")
                if provider.get("balance") is not None:
                    if provider["balance"] + 1e-12 < reserved_cost:
                        raise Blocked("provider_balance")
                    provider["balance"] -= reserved_cost
                    db.execute("UPDATE providers SET payload=? WHERE id=?", (json.dumps(provider), model.provider))
                rate = db.execute("SELECT COUNT(*) n,COALESCE(SUM(reserved_tokens),0) tokens FROM task_runs WHERE model=? AND started_at>?", (model.id, minute)).fetchone()
                if rate["n"] >= model.requests_per_minute or (model.tokens_per_minute is not None and rate["tokens"] + tokens > model.tokens_per_minute):
                    raise Blocked("rate_limit")
                if model.quota_remaining is not None:
                    model.quota_remaining -= tokens
                    db.execute("UPDATE models SET payload=? WHERE id=?", (model.model_dump_json(), model.id))
            run_id = str(uuid4())
            lease = (current + timedelta(seconds=limits.lease_seconds)).isoformat()
            db.execute("UPDATE tasks SET status='running',attempts=attempts+1,updated_at=?,blocked_reason=NULL WHERE id=?", (now(), task.id))
            db.execute("INSERT INTO task_runs(id,task_id,status,model,provider,started_at,lease_until,decision,reserved_cost,reserved_tokens,background) VALUES(?,?,'running',?,?,?,?,?,?,?,?)",
                       (run_id, task.id, decision.model if decision else None, decision.provider if decision else None, now(), lease,
                        json.dumps({"routing": decision.model_dump() if decision else {"handler": task.handler}, "scheduler_score": selection.score, "max_steps": min(task.max_steps, limits.max_steps)}), reserved_cost, tokens, int(task.type != "user")))
            self.repo.event("run_claimed", task.id, run_id=run_id, model=decision.model if decision else None, reserved_cost=reserved_cost)
            return run_id

    async def execute(self, selection: Selection, run_id: str):
        started = time.monotonic()
        heartbeat = None
        limits = self.settings.limits
        timeout = min(selection.task.timeout_seconds, limits.timeout_seconds)
        if selection.prepared and (selection.prepared.get('checkpoint') or selection.prepared.get('resumed')):
            used=self.repo.one('SELECT COALESCE(SUM(latency),0) n FROM task_runs WHERE task_id=?',(selection.task.id,))['n']
            timeout=max(.001,timeout-used)
        loop = asyncio.get_running_loop()

        def beat():
            nonlocal heartbeat
            lease = (datetime.now(timezone.utc) + timedelta(seconds=limits.lease_seconds)).isoformat()
            self.repo.execute("UPDATE task_runs SET lease_until=? WHERE id=? AND status='running'", (lease, run_id))
            heartbeat = loop.call_later(limits.lease_seconds / 3, beat)

        beat()
        completion = None
        retry_after = 0
        yielded=False
        result, error = None, None
        try:
            async with asyncio.timeout(timeout):
                if self.repo.get_setting("cancel_requested:" + selection.task.id, False):
                    raise asyncio.CancelledError()
                tree = None
                if selection.prepared and selection.prepared.get('checkpoint'):
                    result=await self.coding.resume(selection.task,run_id,selection.prepared['checkpoint'])
                elif selection.task.handler in ("coding", "project_tests"):
                    tree = self.coding.create_tree(selection.task, run_id, selection.prepared)
                if selection.decision:
                    model = next(m for m in self.repo.models() if m.id == selection.decision.model)
                    self.repo.execute("UPDATE task_runs SET remote_started=1 WHERE id=?", (run_id,))
                    self.coding.stage(run_id, "model", "running", model=model.id)
                    if selection.task.handler == "chat":
                        completion = await self.providers[model.provider].complete(model, selection.prompt, selection.decision.output_limit, timeout,
                                                                                    messages=json.loads(selection.prompt), on_delta=self.stream_sinks.get(selection.task.id))
                    else:
                        completion = await self.providers[model.provider].complete(model, selection.prompt, selection.decision.output_limit, timeout)
                    result = completion.text[:100000]
                    if completion.input_tokens + completion.output_tokens > selection.decision.input_bound + selection.decision.output_limit or completion.output_tokens > selection.decision.output_limit:
                        error = "Provider exceeded requested token bound"
                        self.repo.set_setting("paused", True)
                        self.repo.event("provider_token_overrun", selection.task.id, model=model.id)
                    if completion.cost > selection.decision.reserved_cost + 1e-9:
                        error = "Provider exceeded reserved cost; verify provider pricing and API-key cap"
                        # Stop further calls until operator review, preserving local queue work.
                        self.repo.set_setting("paused", True)
                        self.repo.event("provider_cost_overrun", selection.task.id, model=model.id, charged=completion.cost)
                    if tree and not error:
                        result = await self.coding.verify(selection.task, run_id, tree, completion)
                elif tree:
                    result = await self.coding.verify(selection.task, run_id, tree)
                elif not result:
                    self.coding.stage(run_id, "local", "running", handler=selection.task.handler)
                    result = (await self.agents.run(selection.task))[:100000]
                    if selection.task.handler=='resource_sync' and json.loads(result).get('status')=='refresh_failed':
                        error='Provider metadata refresh failed; prior observations retained'
        except BackgroundYield:
            yielded=True
            result=json.dumps({'yielded':True,'summary':'Background workflow checkpoint saved; user tasks take priority'})
        except TimeoutError:
            error = "Run timeout; remote usage may be unknown"
        except asyncio.CancelledError:
            error = "Run interrupted during shutdown"
        except (ProviderFailure, Blocked) as exc:
            error = str(exc)
            if isinstance(exc,ProviderFailure) and exc.retry_after and selection.decision:
                retry_after = exc.retry_after
                until=(datetime.now(timezone.utc)+timedelta(seconds=retry_after)).isoformat()
                key='provider_cooldown:'+selection.decision.provider
                self.repo.set_setting(key,max(until,self.repo.get_setting(key,until)))
                self.repo.event('provider_cooldown',selection.task.id,provider=selection.decision.provider,retry_after=retry_after)
        except Exception as exc:
            error = "Run failed: " + type(exc).__name__
        finally:
            if heartbeat:
                heartbeat.cancel()
            self.finish(run_id, result, error, completion, time.monotonic() - started,retry_after,yielded)

    def finish(self, run_id: str, result: str | None, error: str | None, completion: Completion | None, latency: float,retry_after=0,yielded=False):
        with self.repo.transaction() as db:
            run = db.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if not run or run["status"] != "running":
                return
            task = self.repo.task(run["task_id"])
            remote_unknown = bool(run["remote_started"] and not completion)
            charged = completion.cost if completion else (run["reserved_cost"] if remote_unknown else 0)
            input_tokens = completion.input_tokens if completion else (run["reserved_tokens"] if remote_unknown else 0)
            output_tokens = completion.output_tokens if completion else 0
            known = int(not remote_unknown)
            if run["model"] and not remote_unknown:
                provider_row = db.execute("SELECT payload FROM providers WHERE id=?", (run["provider"],)).fetchone()
                provider = json.loads(provider_row["payload"])
                if provider.get("balance") is not None:
                    provider["balance"] = max(0, provider["balance"] + run["reserved_cost"] - charged)
                    db.execute("UPDATE providers SET payload=? WHERE id=?", (json.dumps(provider), run["provider"]))
                stored = db.execute("SELECT payload FROM models WHERE id=?", (run["model"],)).fetchone()
                model = Model.model_validate_json(stored["payload"])
                if model.quota_remaining is not None:
                    model.quota_remaining = max(0, model.quota_remaining + run["reserved_tokens"] - input_tokens - output_tokens)
                    if model.quota_limit is not None:
                        model.quota_remaining = min(model.quota_remaining, model.quota_limit)
                    db.execute("UPDATE models SET payload=? WHERE id=?", (model.model_dump_json(), model.id))
            cancelled = self.repo.get_setting("cancel_requested:" + task.id, False)
            run_status = "cancelled" if cancelled else "yielded" if yielded else "failed" if error else "done"
            db.execute("UPDATE task_runs SET status=?,finished_at=?,lease_until=NULL,charged_cost=?,input_tokens=?,output_tokens=?,latency=?,result=?,error=? WHERE id=?",
                       (run_status, now(), charged, input_tokens, output_tokens, latency, result, error, run_id))
            next_run = None
            if cancelled:
                status = "cancelled"
            elif yielded:
                status='ready'
                db.execute('UPDATE tasks SET attempts=MAX(0,attempts-1) WHERE id=?',(task.id,))
            elif not error:
                status = "done"
            elif task.attempts < task.max_attempts:
                status = "ready"
                next_run = (datetime.now(timezone.utc) + timedelta(seconds=max(retry_after,min(300, 5 * 2 ** (task.attempts - 1))))).isoformat()
            else:
                status = "failed"
            db.execute("UPDATE tasks SET status=?,updated_at=?,next_run_at=?,blocked_reason=NULL WHERE id=?", (status, now(), next_run, task.id))
            db.execute("INSERT INTO usage VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, task.type, run["model"], run["provider"], input_tokens, output_tokens, charged, latency, int(not error), None, now(), known))
            if task.handler=='coding' and result and not error:
                report=json.loads(result)
                if report.get('changed_files') and (report.get('tests_passed') or any(t.get('status')=='failed' for t in report.get('tests',[]))):
                    quality=.8 if report['tests_passed'] else .2
                    evaluated=report.get('model_run_id') or run_id
                    db.execute('INSERT INTO run_evaluations VALUES(?,?,?,?,?) ON CONFLICT(run_id,source) DO UPDATE SET score=excluded.score,metadata=excluded.metadata',(evaluated,'tests',quality,json.dumps({'provisional':True,'tests_passed':report['tests_passed']}),now()))
                    human=db.execute("SELECT score FROM run_evaluations WHERE run_id=? AND source='human'",(evaluated,)).fetchone()
                    score=human['score'] if human else quality
                    db.execute('UPDATE usage SET quality_score=? WHERE run_id=?',(score,evaluated));db.execute('UPDATE task_runs SET quality_score=? WHERE id=?',(score,evaluated))
            self.repo.event("run_finished", task.id, run_id=run_id, status=run_status, cost=charged, usage_known=bool(known), error=error)
            self.coding.stage(run_id, "finished", run_status, cost=charged)
            logger.info("run_finished", extra={"task_id": task.id, "run_id": run_id, "status": run_status, "cost": charged})

    def recover(self):
        for run in self.repo.all("SELECT id FROM task_runs WHERE status='running' AND lease_until<?", (now(),)):
            self.finish(run["id"], None, "Lease expired; recovered interrupted run", None, 0)

    async def stop(self):
        self.enabled = False
        if self._timer:
            self._timer.cancel()
        if self._tick_task and not self._tick_task.done():
            await self._tick_task
        ids = list(self.running)
        active = list(self.running.values())
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        for run_id in ids:
            self.finish(run_id, None, "Run interrupted before execution during shutdown", None, 0)
