from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskCreate(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    status: Literal["inbox", "ready"] = "ready"
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=40000)
    type: Literal["user", "maintenance", "research", "resource", "optimization"] = "user"
    priority: int = Field(default=0, ge=-1000, le=1000)
    project: str | None = None
    deadline: datetime | None = None
    estimated_cost: float | None = Field(default=None, ge=0)
    estimated_value: float = Field(default=1, ge=0, le=100)
    required_capabilities: list[str] = Field(default_factory=lambda: ["general"], max_length=20)
    preferred_model: str | None = None
    max_cost: float | None = Field(default=None, ge=0)
    parent_task: str | None = None
    dependencies: list[str] = Field(default_factory=list, max_length=100)
    max_steps: int = Field(default=1, ge=1, le=20)
    max_tokens: int = Field(default=4096, ge=128, le=200000)
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    max_attempts: int = Field(default=3, ge=1, le=10)
    complexity: Literal["simple", "normal", "complex"] = "normal"
    handler: Literal["llm", "coding", "project_tests", "resource_sync", "system_report", "usage_review", "file_index", "todo_scan"] = "llm"
    code_paths: list[str] = Field(default_factory=list, max_length=20)
    start_after: datetime | None = None
    recurrence: Literal["daily", "weekly", "monthly"] | None = None
    task_timezone: str = "UTC"
    notify_before_minutes: int = Field(default=15, ge=0, le=10080)
    telegram_owner_id: int | None = Field(default=None, gt=0)
    recurring_origin: str | None = None
    recurrence_anchor_day: int | None = Field(default=None, ge=1, le=31)
    idempotency_key: str | None = Field(default=None, max_length=200)
    proposal_action: Literal["delete", "firewall", "database_delete", "deploy", "merge", "message", "payment", "account"] | None = None

    @model_validator(mode="after")
    def aware_deadline(self):
        if self.deadline and self.deadline.tzinfo is None:
            raise ValueError("deadline must include timezone")
        if self.start_after and self.start_after.tzinfo is None:
            raise ValueError("start_after must include timezone")
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(self.task_timezone)
        except (KeyError, ValueError):
            raise ValueError("Unknown task timezone") from None
        if self.recurrence and not self.deadline:
            raise ValueError("Recurring tasks require a deadline")
        return self


class Task(TaskCreate):
    id: str = Field(default_factory=lambda: str(uuid4()))
    status: Literal["inbox", "ready", "running", "blocked", "done", "failed", "cancelled"] = "ready"
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    attempts: int = 0
    blocked_reason: str | None = None
    next_run_at: str | None = None
    completed_manually: bool = False
    manual_previous_status: Literal["inbox", "ready", "blocked", "failed"] | None = None
    manual_previous_reason: str | None = None


class TaskPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=40000)
    priority: int | None = Field(default=None, ge=-1000, le=1000)
    deadline: datetime | None = None
    project: str | None = None
    expected_updated_at: str | None = Field(default=None, max_length=100)
    recurrence: Literal["daily", "weekly", "monthly"] | None = None
    task_timezone: str | None = None
    notify_before_minutes: int | None = Field(default=None, ge=0, le=10080)

    @model_validator(mode="after")
    def valid_changes(self):
        for field in ("title", "description", "priority", "expected_updated_at"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        if self.title is not None and not self.title.strip():
            raise ValueError("title cannot be blank")
        if self.deadline and self.deadline.tzinfo is None:
            raise ValueError("deadline must include timezone")
        return self


class Model(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    id: str
    provider: str = "openrouter"
    groups: list[str] = Field(default_factory=lambda: ["general"])
    capabilities: list[str] = Field(default_factory=lambda: ["general"])
    enabled: bool = False
    input_price: float = Field(default=0, ge=0)  # USD per million tokens
    output_price: float = Field(default=0, ge=0)
    context_size: int = Field(default=8192, ge=128)
    max_output_tokens: int = Field(default=2048, ge=1)
    latency: float = Field(default=5, ge=0)
    reliability: float = Field(default=0.9, ge=0, le=1)
    quality: float = Field(default=0.7, ge=0, le=1)
    quota_remaining: int | None = Field(default=None, ge=0)  # conservative token units
    quota_limit: int | None = Field(default=None, ge=0)
    resets_at: datetime | None = None
    reset_period_seconds: int | None = Field(default=None, ge=60)
    requests_per_minute: int = Field(default=10, ge=1)
    tokens_per_minute: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def aware_reset(self):
        if self.resets_at and self.resets_at.tzinfo is None:
            raise ValueError("resets_at must include timezone")
        return self


class Project(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    path: str
    git_repo: str | None = None
    description: str = ""
    technologies: list[str] = Field(default_factory=list)
    instructions: str = Field(default="", max_length=8000)
    allowed_actions: list[Literal["file_index", "todo_scan", "worktree", "read_code", "code_patch", "run_tests"]] = Field(default_factory=list)
    share_code_with_llm: bool = False
    context_paths: list[str] = Field(default_factory=list, max_length=50)
    editable_paths: list[str] = Field(default_factory=lambda: ["src/**", "tests/**"], max_length=50)
    test_commands: list[list[str]] = Field(default_factory=list, max_length=10)
    test_runtime: str | None = None # optional curated venv, mounted read-only
    maintenance_interval_hours: int = Field(default=24, ge=1, le=720)
    last_analysis: str | None = None
    health_status: str = "unknown"


class Completion(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    text: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost: float = Field(ge=0)


class Decision(BaseModel):
    model: str
    provider: str
    input_bound: int
    output_limit: int
    reserved_cost: float
    score: float
    reasons: list[str]
    evidence: dict = Field(default_factory=dict)


class Blocked(Exception):
    """A task cannot safely start under the current configuration."""


class Conflict(Exception):
    """A concurrent change or task state prevents this edit."""


class BackgroundYield(Exception):
    """A durable checkpoint exists; release resources for a user task."""
