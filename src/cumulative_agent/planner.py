from datetime import datetime, timezone

from .config import Settings
from .domain import TaskCreate
from .repository import Repository
from .tasks import ProjectRegistry, TaskManager


class Planner:
    def __init__(self, repo: Repository, settings: Settings, tasks: TaskManager, projects: ProjectRegistry):
        self.repo, self.settings, self.tasks, self.projects = repo, settings, tasks, projects

    def inspect(self) -> int:
        limits = self.settings.limits
        if self.repo.get_setting("paused", False) or not self.repo.get_setting("background_enabled", True):
            return 0
        # Inspect only during idle. User and existing runnable background work comes first.
        if self.repo.one("SELECT COUNT(*) n FROM tasks WHERE status IN ('ready','running')")["n"]:
            return 0
        stamp = datetime.now(timezone.utc).timestamp()
        previous = self.repo.get_setting("planner_last", 0)
        if stamp - previous < limits.planner_cooldown_seconds:
            return 0
        backlog = self.repo.one("SELECT COUNT(*) n FROM tasks WHERE type!='user' AND status IN ('inbox','ready','running','blocked')")["n"]
        room = max(0, limits.max_background_backlog - backlog)
        if not room:
            return 0
        candidates = [("System health and queue report", "system_report", None, 1),
                      ("Review routing cost and recurring failures", "usage_review", None, 2)]
        for project in self.projects.list():
            for action in ("todo_scan", "file_index"):
                if action in project.allowed_actions:
                    candidates.append((f"{project.id}: {action}", action, project.id, 3))
            if {'run_tests','read_code','worktree'}<=set(project.allowed_actions) and project.test_commands:
                candidates.append((f'{project.id}: committed snapshot tests','project_tests',project.id,5))
        created = 0
        for title, handler, project, value in candidates:
            if value < limits.minimum_expected_value or created >= room:
                continue
            duplicate = self.repo.one("SELECT id,created_at,status FROM tasks WHERE json_extract(payload,'$.handler')=? AND COALESCE(json_extract(payload,'$.project'),'')=? ORDER BY created_at DESC LIMIT 1", (handler, project or ""))
            project_settings=self.projects.get(project) if project else None
            cooldown=max(limits.planner_cooldown_seconds,project_settings.maintenance_interval_hours*3600) if project_settings else limits.planner_cooldown_seconds
            if duplicate and (duplicate["status"] in ("inbox", "ready", "running", "blocked") or stamp - datetime.fromisoformat(duplicate["created_at"]).timestamp() < cooldown):
                continue
            self.tasks.create(TaskCreate(title=title, type="optimization" if handler == "usage_review" else "maintenance", handler=handler, project=project,
                                        estimated_value=value, max_cost=0, required_capabilities=[], max_attempts=1,max_steps=8 if handler=='project_tests' else 1,
                                        idempotency_key=f"planner:{handler}:{project}:{int(stamp // limits.planner_cooldown_seconds)}"))
            created += 1
        self.repo.set_setting("planner_last", stamp)
        self.repo.event("planner_inspected", created=created)
        return created
