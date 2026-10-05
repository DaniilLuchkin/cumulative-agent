import json
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field, model_validator

from .config import Settings, load_settings
from .domain import Conflict, Project, TaskCreate, TaskPatch, now
from .service import Service
from .web_auth import InvalidTelegramData, verify_init_data


class Control(BaseModel):
    action: Literal["block", "ready", "complete", "reopen", "cancel", "retry"]
    reason: str = Field(default="operator", max_length=500)


class Priority(BaseModel):
    priority: int = Field(ge=-1000, le=1000)


class Toggle(BaseModel):
    enabled: bool


class Attachment(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    uri: str = Field(min_length=1, max_length=2000)


class Rating(BaseModel):
    quality_score: float = Field(ge=0, le=1, allow_inf_nan=False)


class Quota(BaseModel):
    remaining: int = Field(ge=0)
    resets_at: datetime | None = None

    @model_validator(mode="after")
    def aware(self):
        if self.resets_at and self.resets_at.tzinfo is None:
            raise ValueError("resets_at requires timezone")
        return self


class MemoryCreate(BaseModel):
    scope: Literal["global", "project", "task"]
    scope_id: str | None = None
    content: str = Field(min_length=1, max_length=8000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    share_with_llm: bool = False


class Discovery(BaseModel):
    provider: str = Field(max_length=200)
    resource: str = Field(max_length=500)
    conditions: str = Field(max_length=8000)
    limits: str = Field(max_length=8000)
    estimated_value: float = Field(ge=0, allow_inf_nan=False)
    source: str = Field(pattern=r"^https://", max_length=2000)
    verified_at: datetime | None = None


def create_app(settings: Settings | None = None, service: Service | None = None, start_worker: bool = True) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.service = service or Service(settings)
        if start_worker:
            await app.state.service.start()
        try:
            yield
        finally:
            await app.state.service.stop()

    app = FastAPI(title="Cumulative Agent", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    async def svc(request: Request) -> Service:
        return request.app.state.service

    async def authorize(request: Request, authorization: str | None = Header(default=None)):
        if (authorization or "").startswith("Telegram "):
            if not settings.telegram_bot_token:
                raise HTTPException(503, "Telegram authentication is not configured")
            try:
                owner = verify_init_data(authorization[len("Telegram "):], settings.telegram_bot_token, settings.telegram_init_data_max_age_seconds)
            except InvalidTelegramData:
                raise HTTPException(401, "Сессия Telegram недействительна или истекла. Откройте приложение заново из бота.") from None
            from .ownership import telegram_owner_ids
            if owner not in telegram_owner_ids(request.app.state.service.repo, settings):
                raise HTTPException(403, "Этот Telegram-пользователь не имеет доступа.")
            request.state.owner_id = owner
            request.state.auth_method = "telegram"
            return
        if len(settings.api_token) < 24:
            raise HTTPException(503, "Configure AGENT_API_TOKEN with at least 24 characters")
        supplied = (authorization or "").removeprefix("Bearer ")
        if not (authorization or "").startswith("Bearer ") or not secrets.compare_digest(supplied.encode(), settings.api_token.encode()):
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})
        request.state.auth_method = "api_token"

    @app.middleware("http")
    async def protect(request, call_next):
        if request.method in ("POST", "PUT", "PATCH"):
            # Enforce for chunked requests too, before JSON parsing.
            total = 0
            chunks = []
            async for chunk in request.stream():
                total += len(chunk)
                if total > 256_000:
                    return JSONResponse({"detail": "Request body too large"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        mini_app = request.url.path in ("/", "/app", "/app/") or request.url.path.startswith("/app/assets/")
        if mini_app:
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors https://web.telegram.org https://webk.telegram.org https://webz.telegram.org; base-uri 'none'; form-action 'self'"
        else:
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "Not found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    @app.get("/app", response_class=HTMLResponse)
    @app.get("/app/", response_class=HTMLResponse)
    async def mini_app():
        return Path(__file__).with_name("web").joinpath("index.html").read_text()

    asset_types = {"app.js": "text/javascript", "task-utils.mjs": "text/javascript", "resources.mjs": "text/javascript", "execution.mjs": "text/javascript", "app.css": "text/css"}
    web_assets = {name: Path(__file__).with_name("web").joinpath(name).read_bytes() for name in asset_types}

    @app.get("/app/assets/{filename}")
    async def asset(filename: str):
        if filename not in web_assets:
            raise HTTPException(404, "Not found")
        return Response(web_assets[filename], media_type=asset_types[filename])

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard():
        return Path(__file__).with_name("dashboard.html").read_text()

    auth = [Depends(authorize)]

    @app.get("/api/auth/me", dependencies=auth)
    async def identity(request: Request):
        return {"method": request.state.auth_method, "owner_id": getattr(request.state, "owner_id", None)}

    @app.get("/api/task-list", dependencies=auth)
    async def task_list(status: Literal["all", "active", "inbox", "ready", "running", "blocked", "done", "failed", "cancelled"] = "active",
                        kind: Literal["all", "user", "background"] = "user", project: str | None = None,
                        search: str = Query(default="", max_length=500), sort: Literal["priority", "deadline", "created", "updated"] = "priority",
                        due: Literal["all", "today", "overdue"] = "all", day_start: datetime | None = None, day_end: datetime | None = None,
                        limit: int = Query(default=100, ge=1, le=200), offset: int = Query(default=0, ge=0), s: Service = Depends(svc)):
        start = day_start or datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        end = day_end or start + timedelta(days=1)
        if start.tzinfo is None or end.tzinfo is None or not timedelta(hours=20) <= end - start <= timedelta(hours=28):
            raise HTTPException(422, "day_start/day_end must be timezone-aware bounds of a local day")
        return s.repo.task_page(status=status, kind=kind, project=project, search=search, sort=sort, due=due,
                                day_start=start, day_end=end, limit=limit, offset=offset)

    @app.get("/api/tasks", dependencies=auth)
    async def tasks(status: str | None = None, limit: int = 200, s: Service = Depends(svc)):
        return s.repo.tasks(status, min(max(1, limit), 1000))

    @app.post("/api/tasks", dependencies=auth, status_code=201)
    async def create(spec: TaskCreate, request: Request, s: Service = Depends(svc)):
        if getattr(request.state,"owner_id",None):
            spec.telegram_owner_id=request.state.owner_id
        return s.tasks.create(spec)

    @app.get("/api/tasks/{task_id}", dependencies=auth)
    async def detail(task_id: str, s: Service = Depends(svc)):
        task = s.repo.task(task_id)
        if not task:
            raise KeyError(task_id)
        runs = s.repo.all("SELECT * FROM task_runs WHERE task_id=? ORDER BY started_at", (task_id,))
        for run in runs:
            run["decision"] = json.loads(run["decision"])
        stages = s.repo.all("SELECT rs.* FROM run_stages rs JOIN task_runs r ON r.id=rs.run_id WHERE r.task_id=? ORDER BY rs.id", (task_id,))
        for stage in stages: stage["metadata"] = json.loads(stage["metadata"])
        artifacts = s.repo.all("SELECT id,run_id,kind,metadata,created_at FROM artifacts WHERE task_id=?", (task_id,))
        for artifact in artifacts: artifact["metadata"] = json.loads(artifact["metadata"])
        events = s.repo.all("SELECT * FROM system_events WHERE task_id=? ORDER BY id DESC LIMIT 50", (task_id,))
        for event in events: event["metadata"] = json.loads(event["metadata"])
        return {"task": task, "runs": runs, "stages":stages, "artifacts":artifacts, "evaluations":s.repo.all('SELECT e.* FROM run_evaluations e JOIN task_runs r ON r.id=e.run_id WHERE r.task_id=?',(task_id,)), "attachments": s.repo.all("SELECT * FROM attachments WHERE task_id=?", (task_id,)), "events": events, "subtasks": [s.repo.decode_task(t) for t in s.repo.all("SELECT * FROM tasks WHERE json_extract(payload,'$.parent_task')=?", (task_id,))]}

    @app.get("/api/artifacts/{artifact_id}", dependencies=auth)
    async def artifact(artifact_id: str, s: Service = Depends(svc)):
        row = s.repo.one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
        if not row: raise KeyError(artifact_id)
        path = Path(row["path"]).resolve()
        if s.coding.root.resolve() not in path.parents or not path.is_file():
            raise HTTPException(404, "Artifact unavailable")
        return FileResponse(path, media_type="application/octet-stream", filename=path.name)

    @app.patch("/api/tasks/{task_id}", dependencies=auth)
    async def update(task_id: str, patch: TaskPatch, s: Service = Depends(svc)):
        return s.tasks.update(task_id, patch)

    @app.post("/api/tasks/{task_id}/control", dependencies=auth)
    async def control(task_id: str, command: Control, s: Service = Depends(svc)):
        return s.tasks.control(task_id, command.action, command.reason)

    @app.patch("/api/tasks/{task_id}/priority", dependencies=auth)
    async def priority(task_id: str, data: Priority, s: Service = Depends(svc)):
        return s.tasks.update(task_id, TaskPatch(priority=data.priority))

    @app.post("/api/tasks/{task_id}/attachments", dependencies=auth)
    async def attach(task_id: str, data: Attachment, s: Service = Depends(svc)):
        s.tasks.add_attachment(task_id, data.name, data.uri)
        return {"stored": True, "content_fetched": False}

    @app.get("/api/status", dependencies=auth)
    async def status(s: Service = Depends(svc)):
        return {"paused": s.repo.get_setting("paused", False), "background_enabled": s.repo.get_setting("background_enabled", True), "limits": settings.limits, "metrics": s.metrics.snapshot()}

    @app.post("/api/pause", dependencies=auth)
    async def pause(data: Toggle, s: Service = Depends(svc)):
        s.repo.set_setting("paused", data.enabled)
        s.repo.event("operator_pause", paused=data.enabled, source="api")
        s.worker.wake()
        return {"paused": data.enabled}

    @app.post("/api/background", dependencies=auth)
    async def background(data: Toggle, s: Service = Depends(svc)):
        s.repo.set_setting("background_enabled", data.enabled)
        s.worker.wake()
        return {"enabled": data.enabled}

    @app.get("/api/projects", dependencies=auth)
    async def projects(s: Service = Depends(svc)):
        return s.projects.list()

    @app.post("/api/projects", dependencies=auth, status_code=201)
    async def project(data: Project, s: Service = Depends(svc)):
        return s.projects.register(data)

    @app.get("/api/models", dependencies=auth)
    async def models(s: Service = Depends(svc)):
        return s.repo.models()

    @app.get("/api/resources", dependencies=auth)
    async def resources(s: Service = Depends(svc)):
        return s.resources.snapshot(set(s.providers))

    @app.post("/api/resources/quota/{model_id:path}", dependencies=auth)
    async def quota(model_id: str, data: Quota, s: Service = Depends(svc)):
        s.resources.manual_quota(model_id, data.remaining, data.resets_at)
        s.worker.wake()
        return {"updated": True}

    @app.post('/api/resources/sync',dependencies=auth,status_code=201)
    async def sync_resources(s:Service=Depends(svc)):
        return s.tasks.create(TaskCreate(title='Refresh OpenRouter resource observations',handler='resource_sync',required_capabilities=[],max_cost=0,max_attempts=1))

    @app.get("/api/metrics", dependencies=auth)
    async def metrics(s: Service = Depends(svc)):
        return s.metrics.snapshot()

    @app.get("/api/usage", dependencies=auth)
    async def usage(s: Service = Depends(svc)):
        return s.repo.all("SELECT * FROM usage ORDER BY created_at DESC LIMIT 500")

    @app.post("/api/runs/{run_id}/rating", dependencies=auth)
    async def rate(run_id: str, rating: Rating, s: Service = Depends(svc)):
        with s.repo.transaction() as db:
            if not db.execute("SELECT run_id FROM usage WHERE run_id=?", (run_id,)).fetchone():
                raise KeyError(run_id)
            db.execute("UPDATE usage SET quality_score=? WHERE run_id=?", (rating.quality_score, run_id))
            db.execute("UPDATE task_runs SET quality_score=? WHERE id=?", (rating.quality_score, run_id))
            db.execute("INSERT INTO run_evaluations VALUES(?,?,?,?,?) ON CONFLICT(run_id,source) DO UPDATE SET score=excluded.score,created_at=excluded.created_at",(run_id,'human',rating.quality_score,'{}',now()))
            s.repo.event("run_rated", run_id=run_id, quality=rating.quality_score)
        return {"rated": True}

    @app.get("/api/events", dependencies=auth)
    async def events(s: Service = Depends(svc)):
        rows = s.repo.all("SELECT * FROM system_events ORDER BY id DESC LIMIT 100")
        for row in rows:
            row["metadata"] = json.loads(row["metadata"])
        return rows

    @app.post("/api/memory", dependencies=auth, status_code=201)
    async def memory(data: MemoryCreate, s: Service = Depends(svc)):
        if data.scope == "task" and not s.repo.task(data.scope_id or ""):
            raise KeyError(data.scope_id)
        if data.scope == "project" and not s.projects.get(data.scope_id or ""):
            raise KeyError(data.scope_id)
        return {"id": s.memory.add(data.scope, data.scope_id, data.content, data.tags, data.share_with_llm)}

    @app.get("/api/discoveries", dependencies=auth)
    async def discoveries(s: Service = Depends(svc)):
        return s.repo.all("SELECT * FROM discoveries ORDER BY discovered_at DESC LIMIT 200")

    @app.post("/api/discoveries", dependencies=auth, status_code=201)
    async def discovery(data: Discovery, s: Service = Depends(svc)):
        did = str(uuid4())
        s.repo.execute("INSERT INTO discoveries VALUES(?,?,?,?,?,?,?,?,?)", (did, data.provider, data.resource, data.conditions, data.limits, data.estimated_value, data.source, now(), data.verified_at.isoformat() if data.verified_at else None))
        task = s.tasks.create(TaskCreate(title=f"Evaluate resource: {data.resource}"[:500], description=f"Provider: {data.provider}\nConditions: {data.conditions}\nLimits: {data.limits}\nSource: {data.source}", type="resource", estimated_value=min(100, data.estimated_value), idempotency_key="discovery:" + did))
        return {"id": did, "task_id": task.id, "accounts_created": False}

    @app.get("/api/approvals", dependencies=auth)
    async def approvals(s: Service = Depends(svc)):
        return s.repo.all("SELECT * FROM approvals ORDER BY created_at DESC LIMIT 200")

    @app.post("/api/approvals/{approval_id}", dependencies=auth)
    async def approval(approval_id: str, data: Toggle, s: Service = Depends(svc)):
        s.tasks.decide_approval(approval_id, data.enabled)
        return {"reviewed": True, "executed": False}

    return app
