from dataclasses import dataclass
from datetime import datetime, timezone

from .domain import Blocked, Decision, Task
from .repository import Repository
from .resources import ResourceManager
from .routing import Router, build_prompt, expiry_bonus
from .tasks import Memory, ProjectRegistry


@dataclass
class Selection:
    task: Task
    decision: Decision | None
    prompt: str
    score: float
    prepared: dict | None = None


class Scheduler:
    def __init__(self, repo: Repository, resources: ResourceManager, router: Router, memory: Memory, projects: ProjectRegistry):
        self.repo, self.resources, self.router, self.memory, self.projects = repo, resources, router, memory, projects
        self.coding = None
        self.chat = None

    def score(self, task: Task, decision: Decision | None) -> float:
        current = datetime.now(timezone.utc)
        age = min(24, max(0, (current - datetime.fromisoformat(task.created_at)).total_seconds() / 3600))
        urgency = 0 if not task.deadline else min(100, max(0, 100 - (task.deadline - current).total_seconds() / 3600))
        expiry = 0
        if decision and task.type != "user":
            model = next(m for m in self.repo.models() if m.id == decision.model)
            expiry = expiry_bonus(model, decision.input_bound + decision.output_limit)
        return task.priority * 10 + urgency + 5 * min(20, task.estimated_value) - 2 * min(20, task.estimated_cost or 0) + age + expiry

    def next(self, exclude: set[str] | None = None) -> Selection | None:
        if self.repo.get_setting("paused", False) or not self.resources.machine()["healthy"]:
            return None
        excluded = exclude or set()
        rows = self.repo.all("SELECT * FROM tasks WHERE (status='ready' OR (status='blocked' AND blocked_reason LIKE 'wait:%')) AND (next_run_at IS NULL OR next_run_at<=?) ORDER BY CASE type WHEN 'user' THEN 0 ELSE 1 END,priority DESC,created_at", (datetime.now(timezone.utc).isoformat(),))
        candidates = []
        for row in rows:
            task = self.repo.decode_task(row)
            if task.start_after and task.start_after > datetime.now(timezone.utc):
                continue
            if task.type != "user" and not self.repo.get_setting("background_enabled", True):
                continue
            if task.id in excluded:
                continue
            waiting = self.repo.one("SELECT COUNT(*) n FROM dependencies d JOIN tasks t ON d.depends_on=t.id WHERE d.task_id=? AND t.status!='done'", (task.id,))["n"]
            if waiting:
                self._wait(task, "dependencies")
                continue
            try:
                prepared = None
                if task.handler == "coding":
                    checkpoint=self.repo.one('SELECT payload FROM workflow_checkpoints WHERE task_id=?',(task.id,))
                    if checkpoint:
                        import json
                        self.coding.project(task)
                        prepared={'checkpoint':json.loads(checkpoint['payload'])};prompt='';decision=None
                    else:
                        prompt, prepared = self.coding.prepare(task)
                        decision = self.router.choose(task, prompt)
                elif task.handler == "chat":
                    prompt = self.chat.prompt(task)
                    decision = self.router.choose(task, prompt)
                elif task.handler == "llm":
                    prompt = build_prompt(task, self.memory, self.projects)
                    decision = self.router.choose(task, prompt)
                else:
                    if task.handler == "project_tests":
                        self.coding.project(task, llm=False)
                    elif task.handler in ("file_index", "todo_scan"):
                        project = self.projects.get(task.project) if task.project else None
                        if not project or task.handler not in project.allowed_actions:
                            raise Blocked("Project has not allowed this local agent")
                        if self.repo.one('SELECT task_id FROM workflow_checkpoints WHERE task_id=?',(task.id,)):
                            prepared={'resumed':True}
                    prompt, decision = "", None
                candidates.append(Selection(task, decision, prompt, self.score(task, decision), prepared))
            except Blocked as exc:
                self._wait(task, str(exc))
        users = [c for c in candidates if c.task.type == "user"]
        pool = users or candidates
        return max(pool, key=lambda c: (c.score, c.task.created_at, c.task.id)) if pool else None

    def _wait(self, task: Task, reason: str):
        text = "wait:" + reason
        if task.status != "blocked" or task.blocked_reason != text:
            self.repo.execute("UPDATE tasks SET status='blocked',blocked_reason=?,updated_at=? WHERE id=? AND status!='running'", (text, datetime.now(timezone.utc).isoformat(), task.id))
            self.repo.event("task_waiting", task.id, reason=reason)
