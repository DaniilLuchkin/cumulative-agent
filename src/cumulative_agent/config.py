import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from urllib.parse import urlsplit

from .domain import Model


class Limits(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    daily_cost_limit: float = Field(default=0, ge=0)
    background_cost_limit: float = Field(default=0, ge=0)
    task_cost_limit: float = Field(default=0.10, ge=0)
    max_concurrent_agents: int = Field(default=1, ge=1, le=16)
    max_tokens: int = Field(default=8192, ge=128, le=200000)
    max_steps: int = Field(default=8, ge=1, le=20)
    timeout_seconds: int = Field(default=180, ge=1, le=3600)
    poll_seconds: float = Field(default=10, ge=0.1, le=300)
    lease_seconds: int = Field(default=60, ge=5)
    planner_cooldown_seconds: int = Field(default=3600, ge=1)
    max_background_backlog: int = Field(default=8, ge=0, le=1000)
    minimum_expected_value: float = Field(default=1, ge=0)
    min_free_disk_bytes: int = Field(default=100_000_000, ge=0)
    min_available_memory_bytes: int = Field(default=100_000_000, ge=0)
    max_load_per_cpu: float = Field(default=2, gt=0)
    max_index_files: int = Field(default=2000, ge=1, le=100000)
    max_context_bytes: int = Field(default=5000, ge=128, le=100000)
    max_snapshot_bytes: int = Field(default=20_000_000, ge=1000, le=500_000_000)
    max_patch_bytes: int = Field(default=100000, ge=1000, le=1_000_000)
    test_timeout_seconds: int = Field(default=60, ge=1, le=600)


class ProviderResource(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    balance: float | None = Field(default=None, ge=0)
    requests_per_minute: int = Field(default=20, ge=1)
    tracking: str = "manual/config"


class Settings(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    database: str = "data/agent.sqlite3"
    limits: Limits = Field(default_factory=Limits)
    models: list[Model] = Field(default_factory=list)
    providers: dict[str, ProviderResource] = Field(default_factory=dict)
    project_roots: list[str] = Field(default_factory=list)
    test_runtime_roots: list[str] = Field(default_factory=list)
    api_token: str = Field(default="", exclude=True)
    openrouter_api_key: str = Field(default="", exclude=True)
    telegram_bot_token: str = Field(default="", exclude=True)
    telegram_owner_ids: list[int] = Field(default_factory=list)
    telegram_claim_first_user: bool = False
    telegram_webapp_url: str = ""
    telegram_init_data_max_age_seconds: int = Field(default=3600, ge=60, le=86400)
    timezone: str = "UTC"
    resource_sync_seconds: int = Field(default=3600, ge=60, le=86400)
    resource_auto_sync: bool = False

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls,value):
        from zoneinfo import ZoneInfo
        try: ZoneInfo(value)
        except (KeyError,ValueError): raise ValueError("Unknown timezone") from None
        return value

    @field_validator("telegram_webapp_url")
    @classmethod
    def https_url(cls, value: str):
        if value:
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("telegram_webapp_url must be a public HTTPS URL without credentials/query/fragment")
        return value


def load_settings(path: str | None = None) -> Settings:
    selected = path or os.environ.get("AGENT_CONFIG", "config/default.yaml")
    data = yaml.safe_load(Path(selected).read_text()) if Path(selected).exists() else {}
    data = data or {}
    forbidden = {"api_token", "openrouter_api_key", "telegram_bot_token"} & data.keys()
    if forbidden:
        raise ValueError("Secrets must be supplied through environment variables")
    data.update(api_token=os.environ.get("AGENT_API_TOKEN", ""),
                openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
                telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    if "TELEGRAM_OWNER_IDS" in os.environ:
        data["telegram_owner_ids"] = [int(x.strip()) for x in os.environ["TELEGRAM_OWNER_IDS"].split(",") if x.strip()]
    if "TELEGRAM_CLAIM_FIRST_USER" in os.environ:
        value = os.environ["TELEGRAM_CLAIM_FIRST_USER"].strip().lower()
        if value not in ("true", "false", "1", "0"):
            raise ValueError("TELEGRAM_CLAIM_FIRST_USER must be true/false or 1/0")
        data["telegram_claim_first_user"] = value in ("true", "1")
    if "AGENT_DATABASE" in os.environ:
        data["database"] = os.environ["AGENT_DATABASE"]
    if "TELEGRAM_WEBAPP_URL" in os.environ:
        data["telegram_webapp_url"] = os.environ["TELEGRAM_WEBAPP_URL"]
    return Settings.model_validate(data)
