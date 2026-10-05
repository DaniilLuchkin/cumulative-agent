import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .domain import Model, now
from .ownership import telegram_owner_ids
from .repository import Repository


class ResourceManager:
    def __init__(self, repo: Repository, settings: Settings):
        self.repo, self.settings = repo, settings
        self.sync_models()

    def sync_models(self):
        """Apply changed config but preserve consumed quota across unchanged restarts."""
        configured = {m.id for m in self.settings.models}
        with self.repo.transaction() as db:
            for provider_id, resource in self.settings.providers.items():
                old = db.execute("SELECT payload FROM providers WHERE id=?", (provider_id,)).fetchone()
                previous = self.repo.get_setting("provider_config:" + provider_id)
                payload = resource.model_dump()
                if old and previous == payload:
                    payload["balance"] = json.loads(old["payload"])["balance"]
                self.repo.set_setting("provider_config:" + provider_id, resource.model_dump())
                db.execute("INSERT INTO providers VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (provider_id, json.dumps(payload)))
            for model in self.settings.models:
                old_config = self.repo.get_setting("model_config:" + model.id)
                old = db.execute("SELECT payload FROM models WHERE id=?", (model.id,)).fetchone()
                current = model.model_copy(deep=True)
                if old and old_config == model.model_dump(mode="json"):
                    stored = Model.model_validate_json(old["payload"])
                    current.quota_remaining = stored.quota_remaining
                    current.resets_at = stored.resets_at
                self.repo.set_setting("model_config:" + model.id, model.model_dump(mode="json"))
                db.execute("INSERT INTO models VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (model.id, current.model_dump_json()))
                db.execute("INSERT OR IGNORE INTO providers VALUES(?,?)", (model.provider, json.dumps({"tracking": "manual/config", "balance": None, "requests_per_minute": 20})))
            for stored in self.repo.models():
                if stored.id not in configured:
                    stored.enabled = False
                    db.execute("UPDATE models SET payload=? WHERE id=?", (stored.model_dump_json(), stored.id))

    def refresh(self):
        current = datetime.now(timezone.utc)
        with self.repo.transaction() as db:
            for model in self.repo.models():
                if model.resets_at and model.resets_at <= current and model.reset_period_seconds and model.quota_limit is not None:
                    periods = int((current - model.resets_at).total_seconds() // model.reset_period_seconds) + 1
                    model.resets_at += timedelta(seconds=periods * model.reset_period_seconds)
                    # Deduct in-flight reservations; a reset must not free unaccounted calls.
                    active = db.execute("SELECT COALESCE(SUM(reserved_tokens),0) n FROM task_runs WHERE status='running' AND model=?", (model.id,)).fetchone()["n"]
                    model.quota_remaining = max(0, model.quota_limit - active)
                    db.execute("UPDATE models SET payload=? WHERE id=?", (model.model_dump_json(), model.id))
                    self.repo.event("quota_reset", model=model.id)

    def manual_quota(self, model_id: str, remaining: int, resets_at: datetime | None = None):
        with self.repo.transaction() as db:
            row = db.execute("SELECT payload FROM models WHERE id=?", (model_id,)).fetchone()
            if not row:
                raise KeyError(model_id)
            model = Model.model_validate_json(row["payload"])
            model.quota_remaining, model.resets_at = remaining, resets_at
            db.execute("UPDATE models SET payload=? WHERE id=?", (model.model_dump_json(), model_id))
            self.repo.event("manual_quota_update", model=model_id, remaining=remaining)

    def machine(self) -> dict:
        disk = shutil.disk_usage(Path(self.settings.database).parent if self.settings.database != ":memory:" else ".")
        memory = {}
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                key, value = line.split(":", 1)
                if key in ("MemTotal", "MemAvailable"):
                    memory[key] = int(value.strip().split()[0]) * 1024
        except OSError:
            pass
        load = os.getloadavg()[0]
        cpu = os.cpu_count() or 1
        limits = self.settings.limits
        healthy = disk.free >= limits.min_free_disk_bytes and memory.get("MemAvailable", 0) >= limits.min_available_memory_bytes and load / cpu <= limits.max_load_per_cpu
        return {"cpu_count": cpu, "load_1m": load, "memory": memory, "disk_free": disk.free, "healthy": healthy}

    def snapshot(self, executable_providers: set[str] | None = None) -> dict:
        supported = executable_providers if executable_providers is not None else ({"openrouter"} if self.settings.openrouter_api_key else set())
        providers = [{"id": p["id"], **json.loads(p["payload"]), "adapter_available": p["id"] in supported}
                     for p in self.repo.all("SELECT * FROM providers ORDER BY id")]
        current = datetime.now(timezone.utc)
        day = current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        # Match worker budgeting: include all active reservations across midnight.
        usage = self.repo.one("SELECT COALESCE(SUM(COALESCE(charged_cost,reserved_cost)),0) used,COALESCE(SUM(CASE WHEN status='running' THEN reserved_cost ELSE 0 END),0) reserved FROM task_runs WHERE started_at>=? OR status='running'", (day,))
        background = self.repo.one("SELECT COALESCE(SUM(COALESCE(charged_cost,reserved_cost)),0) used,COALESCE(SUM(CASE WHEN status='running' THEN reserved_cost ELSE 0 END),0) reserved FROM task_runs WHERE background=1 AND (started_at>=? OR status='running')", (day,))
        limits = self.settings.limits
        budgets = {"daily": {**usage, "limit": limits.daily_cost_limit, "remaining": max(0, limits.daily_cost_limit - usage["used"])},
                   "background": {**background, "limit": limits.background_cost_limit, "remaining": max(0, limits.background_cost_limit - background["used"])}}
        return {"local": self.machine(), "models": [m.model_dump(mode="json") for m in self.repo.models()],
                "observations": {row['provider']:json.loads(row['payload']) for row in self.repo.all('SELECT * FROM resource_observations')},
                "providers": providers, "budgets": budgets,
                "execution_limits": {"task_cost_limit": limits.task_cost_limit, "max_tokens": limits.max_tokens,
                                     "timeout_seconds": limits.timeout_seconds, "max_concurrent_agents": limits.max_concurrent_agents},
                "credential_readiness": {"openrouter": bool(self.settings.openrouter_api_key), "telegram": bool(self.settings.telegram_bot_token and telegram_owner_ids(self.repo, self.settings))},
                "telegram_ownership": {"awaiting_first_user": bool(self.settings.telegram_bot_token and self.settings.telegram_claim_first_user and not telegram_owner_ids(self.repo, self.settings))}, "observed_at": now()}
