import json
from pathlib import Path
from uuid import uuid4

from .config import Settings
from .domain import Conflict, Project, Task, TaskCreate, TaskPatch, now
from .repository import Repository


class TaskManager:
    def __init__(self, repo: Repository, wake=lambda: None):
        self.repo, self.wake = repo, wake
        self.cancel_running = lambda task_id: None

    def create(self, spec: TaskCreate) -> Task:
        with self.repo.transaction() as db:
            if spec.idempotency_key:
                existing = db.execute("SELECT * FROM tasks WHERE idempotency_key=?", (spec.idempotency_key,)).fetchone()
                if existing:
                    return self.repo.decode_task(dict(existing))
            ids = list(spec.dependencies) + ([spec.parent_task] if spec.parent_task else [])
            for task_id in ids:
                if not db.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone():
                    raise ValueError("Unknown dependency or parent task")
            if spec.project and not db.execute("SELECT id FROM projects WHERE id=?", (spec.project,)).fetchone():
                raise ValueError("Unknown project")
            task = Task(**spec.model_dump())
            if task.proposal_action:
                task.status, task.blocked_reason = "blocked", "approval_required"
            db.execute("INSERT INTO tasks(id,payload,status,type,priority,created_at,updated_at,idempotency_key,blocked_reason) VALUES(?,?,?,?,?,?,?,?,?)",
                       (task.id, task.model_dump_json(), task.status, task.type, task.priority, task.created_at, task.updated_at, task.idempotency_key, task.blocked_reason))
            for dep in set(task.dependencies):
                db.execute("INSERT INTO dependencies VALUES(?,?)", (task.id, dep))
            if task.proposal_action:
                db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,NULL)", (str(uuid4()), task.id, task.proposal_action, "pending", now()))
            self.repo.event("task_created", task.id, type=task.type, handler=task.handler)
        self.wake()
        return task

    def control(self, task_id: str, action: str, reason: str = "operator") -> Task:
        if action == "retry":
            previous = self.repo.task(task_id)
            if not previous:
                raise KeyError(task_id)
            if previous.status not in ("failed", "cancelled", "done") or previous.proposal_action:
                raise ValueError("Retry requires a finished safe task")
            values = {key: getattr(previous, key) for key in TaskCreate.model_fields}
            values.update(status="ready", parent_task=task_id, recurring_origin=None, recurrence=None,
                          idempotency_key=f"explicit-retry:{task_id}:{previous.updated_at}")
            result = self.create(TaskCreate(**values))
            self.repo.event("task_retried", task_id, new_task=result.id)
            return result
        if action == "cancel":
            with self.repo.transaction() as db:
                task = self.repo.task(task_id)
                if not task:
                    raise KeyError(task_id)
                if task.status in ("done", "cancelled"):
                    raise ValueError("Task is already finished")
                if task.status == "running":
                    self.repo.set_setting("cancel_requested:" + task_id, True)
                else:
                    db.execute("UPDATE tasks SET status='cancelled',updated_at=?,next_run_at=NULL WHERE id=?", (now(), task_id))
                self.repo.event("task_cancel_requested", task_id)
            if task.status == "running":
                self.cancel_running(task_id)
            self.wake()
            return self.repo.task(task_id)
        with self.repo.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            task = self.repo.decode_task(dict(row))
            if task.status == "cancelled":
                raise ValueError("Cancelled tasks require an explicit new retry")
            if action == "complete" and task.status == "done":
                return task
            if task.status == "running":
                raise ValueError("Running or completed task cannot be changed")
            if action in ("complete", "reopen"):
                if task.proposal_action:
                    raise ValueError("Dangerous proposals cannot be completed or reopened through the task list")
                if action == "reopen" and (task.status != "done" or not task.completed_manually):
                    raise ValueError("Only manually completed tasks can be reopened")
                if action == "complete":
                    task.manual_previous_status, task.manual_previous_reason = task.status, task.blocked_reason
                    status, blocked = "done", None
                else:
                    status, blocked = task.manual_previous_status or "inbox", task.manual_previous_reason
                    task.manual_previous_status, task.manual_previous_reason = None, None
                task.completed_manually = action == "complete"
                db.execute("UPDATE tasks SET payload=? WHERE id=?", (task.model_dump_json(), task_id))
            elif task.status == "done":
                raise ValueError("Running or completed task cannot be changed")
            elif action == "block":
                status, blocked = "blocked", "manual:" + reason[:500]
            elif action == "ready":
                if task.proposal_action:
                    raise ValueError("Dangerous proposals cannot execute in MVP; approval records review only")
                if task.attempts >= task.max_attempts:
                    raise ValueError("Attempt limit reached; create a new explicit task")
                status, blocked = "ready", None
            else:
                raise ValueError("Unknown action")
            db.execute("UPDATE tasks SET status=?,blocked_reason=?,next_run_at=NULL,updated_at=? WHERE id=?", (status, blocked, now(), task_id))
            if action in ("ready", "reopen"):
                self.repo.set_setting("telegram_notified:" + task_id, False)
            self.repo.event("task_control", task_id, action=action)
        self.wake()
        return self.repo.task(task_id)

    def update(self, task_id: str, patch: TaskPatch) -> Task:
        changes = patch.model_dump(exclude_unset=True)
        expected = changes.pop("expected_updated_at", None)
        if not changes:
            raise ValueError("No changes supplied")
        with self.repo.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(task_id)
            task = self.repo.decode_task(dict(row))
            if expected is not None and task.updated_at != expected:
                raise Conflict("Задача изменилась. Откройте её заново перед сохранением.")
            if task.status in ("running", "done") and any(field in changes and changes[field] != getattr(task, field) for field in ("title", "description", "project")):
                raise Conflict("Содержимое выполняемой или завершённой задачи менять нельзя. Дедлайн и приоритет доступны.")
            if changes.get("project") and not db.execute("SELECT id FROM projects WHERE id=?", (changes["project"],)).fetchone():
                raise ValueError("Unknown project")
            updated = Task.model_validate({**task.model_dump(), **changes, "updated_at": now()})
            if any(getattr(updated, field) != getattr(task, field) for field in ('deadline','recurrence','task_timezone')):
                from zoneinfo import ZoneInfo
                updated.recurrence_anchor_day = updated.deadline.astimezone(ZoneInfo(updated.task_timezone)).day if updated.recurrence == 'monthly' else None
            db.execute("UPDATE tasks SET payload=?,priority=?,updated_at=? WHERE id=?", (updated.model_dump_json(), updated.priority, updated.updated_at, task_id))
            before = task.model_dump(mode="json"); after = updated.model_dump(mode="json")
            self.repo.event("task_updated", task_id, fields=sorted(changes), changes={k:{"before":before[k], "after":after[k]} for k in changes if k != "description"})
        self.wake()
        return updated

    def add_attachment(self, task_id: str, name: str, uri: str):
        if not self.repo.task(task_id):
            raise KeyError(task_id)
        self.repo.execute("INSERT INTO attachments VALUES(?,?,?,?,?)", (str(uuid4()), task_id, name, uri, now()))

    def decide_approval(self, approval_id: str, approved: bool):
        with self.repo.transaction() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if not row:
                raise KeyError(approval_id)
            if row["status"] != "pending":
                raise ValueError("Approval already decided")
            db.execute("UPDATE approvals SET status=?,decided_at=? WHERE id=?", ("approved" if approved else "rejected", now(), approval_id))
            self.repo.event("approval_reviewed", row["task_id"], approved=approved, execution="not_supported")


class ProjectRegistry:
    def __init__(self, repo: Repository, settings: Settings):
        self.repo, self.settings = repo, settings

    def register(self, project: Project) -> Project:
        path = Path(project.path).resolve(strict=True)
        roots = [Path(r).resolve(strict=True) for r in self.settings.project_roots]
        if not path.is_dir() or not any(path == root or root in path.parents for root in roots):
            raise ValueError("Project path must be inside a configured project_roots directory")
        project.path = str(path)
        if project.test_runtime:
            runtime=Path(project.test_runtime).resolve(strict=True)
            runtime_roots=[Path(p).resolve(strict=True) for p in self.settings.test_runtime_roots]
            if not runtime.is_dir() or not any(runtime==p or p in runtime.parents for p in runtime_roots):
                raise ValueError('Test runtime must be inside explicit test_runtime_roots')
            project.test_runtime=str(runtime)
        self.repo.execute("INSERT INTO projects VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (project.id, project.model_dump_json()))
        self.repo.event("project_registered", project_id=project.id)
        return project

    def get(self, project_id: str) -> Project | None:
        row = self.repo.one("SELECT payload FROM projects WHERE id=?", (project_id,))
        return Project.model_validate_json(row["payload"]) if row else None

    def list(self) -> list[Project]:
        return [Project.model_validate_json(r["payload"]) for r in self.repo.all("SELECT payload FROM projects")]


class Memory:
    def __init__(self, repo: Repository):
        self.repo = repo

    def add(self, scope: str, scope_id: str | None, content: str, tags: list[str], share: bool = False) -> str:
        if scope not in ("global", "project", "task"):
            raise ValueError("Invalid memory scope")
        if scope != "global" and not scope_id:
            raise ValueError("Scope id is required")
        mid = str(uuid4())
        self.repo.execute("INSERT INTO memory VALUES(?,?,?,?,?,?,?)", (mid, scope, scope_id, content, json.dumps(tags), int(share), now()))
        return mid

    def retrieve(self, task: Task, max_bytes: int = 3000) -> list[str]:
        rows = self.repo.all("SELECT content,tags FROM memory WHERE share_with_llm=1 AND (scope='global' OR (scope='task' AND scope_id=?) OR (scope='project' AND scope_id=?)) ORDER BY created_at DESC LIMIT 100", (task.id, task.project))
        words = set((task.title + " " + task.description).lower().split())
        rows.sort(key=lambda r: len(words & set((r["content"] + " " + r["tags"]).lower().split())), reverse=True)
        results, used = [], 0
        for r in rows:
            content = r["content"]
            if used + len(content.encode()) <= max_bytes:
                results.append(content)
                used += len(content.encode())
            if len(results) >= 5:
                break
        return results
