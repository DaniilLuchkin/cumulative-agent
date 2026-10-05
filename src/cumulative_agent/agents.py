import asyncio
import json
import os
import re
import hashlib
from pathlib import Path
from typing import Protocol

from .config import Settings
from .domain import BackgroundYield, Blocked, Task, TaskCreate, now
from .metrics import Metrics
from .repository import Repository
from .tasks import ProjectRegistry


class Agent(Protocol):
    async def run(self, task: Task) -> str: ...


EXCLUDED = {".git", ".env", ".aws", ".ssh", ".codex", ".agents", "node_modules", ".venv", "venv", "__pycache__", "data", ".cache", "vendor"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".db"}
TEXT_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".md", ".toml", ".yaml", ".yml", ".go", ".rs", ".sh"}


class LocalAgents:
    def __init__(self, repo: Repository, settings: Settings, projects: ProjectRegistry):
        self.repo, self.settings, self.projects = repo, settings, projects
        self.should_yield=lambda task:False

    async def run(self, task: Task) -> str:
        if task.handler == 'resource_sync':
            return await self.observer.refresh()
        if task.handler == "system_report":
            return json.dumps({"metrics": Metrics(self.repo).snapshot(), "recommendations": ["Enable only verified models and configured budgets", "Register projects explicitly for useful local maintenance"], "mode": "local; no external data transfer"}, ensure_ascii=False)
        if task.handler == "usage_review":
            rows = self.repo.all("SELECT model,COUNT(*) runs,AVG(cost) average_cost,AVG(success) success_rate,AVG(latency) latency FROM usage WHERE model IS NOT NULL GROUP BY model")
            findings = []
            for row in rows:
                if row["success_rate"] < 0.8:
                    findings.append({"model": row["model"], "proposal": "Review recurring failures before further background use"})
                if row["average_cost"] > self.settings.limits.task_cost_limit / 2:
                    findings.append({"model": row["model"], "proposal": "Evaluate cheaper permitted model on human-rated representative tasks"})
            context=self.repo.one("SELECT AVG(json_extract(decision,'$.routing.input_bound')) n FROM task_runs WHERE model IS NOT NULL")['n']
            if context and context>self.settings.limits.max_tokens*.65:
                findings.append({'model':None,'proposal':'Reduce context size: input consumes most of the token budget','input_bound':context})
            backlog=self.repo.one("SELECT COUNT(*) n FROM tasks WHERE type!='user' AND status IN ('inbox','ready','running','blocked')")['n']
            room=max(0,self.settings.limits.max_background_backlog-backlog)
            from .tasks import TaskManager
            created=[]
            if self.settings.limits.minimum_expected_value<=2:
                for finding in findings[:room]:
                    fingerprint=hashlib.sha256((str(finding['model'])+':'+finding['proposal']).encode()).hexdigest()
                    key='improvement:'+fingerprint
                    previous=self.repo.one('SELECT created_at FROM tasks WHERE idempotency_key LIKE ? ORDER BY created_at DESC LIMIT 1',(key+':%',))
                    from datetime import datetime,timezone
                    if previous and (datetime.now(timezone.utc)-datetime.fromisoformat(previous['created_at'])).total_seconds()<self.settings.limits.planner_cooldown_seconds:continue
                    proposal=TaskManager(self.repo).create(TaskCreate(title=finding['proposal'][:500],description=json.dumps(finding),type='optimization',status='inbox',parent_task=task.id,estimated_value=2,
                        idempotency_key=key+':'+now()[:10]))
                    created.append(proposal.id)
            return json.dumps({"model_statistics": rows, "improvement_proposals": findings, "proposal_tasks":created, "automatic_configuration_changes": False}, ensure_ascii=False)
        if task.handler not in ("file_index", "todo_scan"):
            raise Blocked("Unknown local agent")
        project = self.projects.get(task.project) if task.project else None
        if not project or task.handler not in project.allowed_actions:
            raise Blocked("Local action not allowed for project")
        root = Path(project.path).resolve(strict=True)
        allowed_roots = [Path(p).resolve() for p in self.settings.project_roots]
        if not any(root == p or p in root.parents for p in allowed_roots):
            raise Blocked("Project root no longer allowed")
        checkpoint=self.repo.one('SELECT payload FROM workflow_checkpoints WHERE task_id=?',(task.id,))
        saved=json.loads(checkpoint['payload']) if checkpoint else {}
        visited=saved.get('visited',{})
        indexed, findings, batch, walked = len(visited), saved.get('findings',[]), [], 0
        cap = self.settings.limits.max_index_files
        # No symlinks, no secrets, capped traversed entries; yield for cancellation.
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not d.startswith(".") and not (Path(directory) / d).is_symlink())
            walked += len(dirs) + len(files)
            if walked > cap * 5:
                break
            for name in sorted(files):
                path = Path(directory) / name
                if name.startswith(".") or "secret" in name.lower() or "credential" in name.lower() or path.suffix.lower() in SECRET_SUFFIXES or path.is_symlink():
                    continue
                try:
                    stat = path.stat()
                    if not path.is_file() or root not in path.resolve().parents:
                        continue
                    relative = str(path.relative_to(root))
                    if visited.get(relative)==[stat.st_size,stat.st_mtime]:continue
                    is_new = relative not in visited
                    visited[relative]=[stat.st_size,stat.st_mtime]
                    findings = [finding for finding in findings if finding['path'] != relative]
                    batch.append((project.id, relative, stat.st_size, stat.st_mtime, path.suffix.lower(), now()))
                    if task.handler == "todo_scan" and path.suffix.lower() in TEXT_SUFFIXES and stat.st_size <= 128_000:
                        # O_NOFOLLOW prevents a final-path symlink substitution race.
                        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                        with os.fdopen(descriptor, "r", encoding="utf-8", errors="replace") as stream:
                            content = stream.read(128_000)
                        for line_no, line in enumerate(content.splitlines(), 1):
                            if re.search(r"\b(TODO|FIXME)\b", line) and len(findings) < 200:
                                # Report locations only; do not persist potential secrets in code lines.
                                findings.append({"path": relative, "line": line_no})
                except OSError:
                    continue
                indexed += int(is_new)
                if indexed >= cap:
                    break
                if indexed % 20 == 0:
                    await asyncio.sleep(0)
                    if self.should_yield(task):
                        with self.repo.transaction() as db:
                            db.executemany("INSERT INTO file_index VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,path) DO UPDATE SET size=excluded.size,mtime=excluded.mtime,category=excluded.category,indexed_at=excluded.indexed_at",batch)
                            db.execute('INSERT INTO workflow_checkpoints VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',(task.id,json.dumps({'kind':'file_scan','visited':visited,'findings':findings}),now()))
                        raise BackgroundYield()
            if indexed >= cap:
                break
            await asyncio.sleep(0)
        with self.repo.transaction() as db:
            db.executemany("INSERT INTO file_index VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,path) DO UPDATE SET size=excluded.size,mtime=excluded.mtime,category=excluded.category,indexed_at=excluded.indexed_at", batch)
            project.last_analysis, project.health_status = now(), "needs_review" if findings else "analyzed"
            db.execute("UPDATE projects SET payload=? WHERE id=?", (project.model_dump_json(), project.id))
            db.execute('DELETE FROM workflow_checkpoints WHERE task_id=?',(task.id,))
        return json.dumps({"project": project.id, "files_indexed": indexed, "scan_capped": indexed >= cap or walked > cap * 5, "todo_locations": findings, "files_deleted": 0}, ensure_ascii=False)
