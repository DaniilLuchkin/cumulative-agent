from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path

from .domain import Model, Task, now


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS tasks(
 id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
 type TEXT NOT NULL, priority INTEGER NOT NULL, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 blocked_reason TEXT, next_run_at TEXT, idempotency_key TEXT UNIQUE);
CREATE INDEX IF NOT EXISTS task_queue ON tasks(status,type,priority);
CREATE TABLE IF NOT EXISTS dependencies(
 task_id TEXT REFERENCES tasks(id), depends_on TEXT REFERENCES tasks(id),
 PRIMARY KEY(task_id,depends_on));
CREATE TABLE IF NOT EXISTS task_runs(
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
 status TEXT NOT NULL, model TEXT, provider TEXT, started_at TEXT NOT NULL,
 finished_at TEXT, lease_until TEXT, decision TEXT NOT NULL,
 reserved_cost REAL NOT NULL DEFAULT 0, charged_cost REAL,
 reserved_tokens INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER,
 output_tokens INTEGER, latency REAL, quality_score REAL, result TEXT, error TEXT,
 background INTEGER NOT NULL, remote_started INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS runs_task ON task_runs(task_id,started_at);
CREATE INDEX IF NOT EXISTS runs_model_rate ON task_runs(model,started_at);
CREATE INDEX IF NOT EXISTS runs_provider_rate ON task_runs(provider,started_at);
CREATE TABLE IF NOT EXISTS usage(
 run_id TEXT PRIMARY KEY REFERENCES task_runs(id), task_type TEXT, model TEXT,
 provider TEXT,input_tokens INTEGER,output_tokens INTEGER,cost REAL,latency REAL,
 success INTEGER,quality_score REAL,created_at TEXT,usage_known INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS usage_routing ON usage(model,task_type);
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS models(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS providers(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS discoveries(
 id TEXT PRIMARY KEY,provider TEXT,resource TEXT,conditions TEXT,limits TEXT,
 estimated_value REAL,source TEXT,discovered_at TEXT,verified_at TEXT);
CREATE TABLE IF NOT EXISTS system_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT,kind TEXT,task_id TEXT,metadata TEXT);
CREATE TABLE IF NOT EXISTS memory(
 id TEXT PRIMARY KEY,scope TEXT NOT NULL,scope_id TEXT,content TEXT,
 tags TEXT,share_with_llm INTEGER NOT NULL DEFAULT 0,created_at TEXT);
CREATE TABLE IF NOT EXISTS attachments(
 id TEXT PRIMARY KEY,task_id TEXT REFERENCES tasks(id),name TEXT,uri TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS approvals(
 id TEXT PRIMARY KEY,task_id TEXT REFERENCES tasks(id),action TEXT,status TEXT,
 created_at TEXT,decided_at TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_stages(
 id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT REFERENCES task_runs(id),
 name TEXT,status TEXT,created_at TEXT,metadata TEXT);
CREATE TABLE IF NOT EXISTS artifacts(
 id TEXT PRIMARY KEY,task_id TEXT REFERENCES tasks(id),run_id TEXT REFERENCES task_runs(id),
 kind TEXT,path TEXT,metadata TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS workflow_checkpoints(task_id TEXT PRIMARY KEY,payload TEXT,updated_at TEXT);
CREATE TABLE IF NOT EXISTS notifications(
 id TEXT PRIMARY KEY,task_id TEXT REFERENCES tasks(id),owner_id INTEGER,kind TEXT,
 payload TEXT,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
 created_at TEXT,sent_at TEXT);
CREATE TABLE IF NOT EXISTS resource_observations(provider TEXT PRIMARY KEY,payload TEXT,observed_at TEXT);
CREATE TABLE IF NOT EXISTS run_evaluations(
 run_id TEXT REFERENCES task_runs(id),source TEXT,score REAL,metadata TEXT,created_at TEXT,
 PRIMARY KEY(run_id,source));
CREATE TABLE IF NOT EXISTS file_index(
 project_id TEXT,path TEXT,size INTEGER,mtime REAL,category TEXT,indexed_at TEXT,
 PRIMARY KEY(project_id,path));
CREATE TABLE IF NOT EXISTS chat_messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER NOT NULL,thread_id INTEGER NOT NULL DEFAULT 0,
 role TEXT NOT NULL,content TEXT NOT NULL,task_id TEXT,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chat_thread ON chat_messages(owner_id,thread_id,id);
"""


class Repository:
    """SQLite persistence boundary. Short synchronous transactions; no network inside."""

    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, timeout=10, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("casefold", 1, lambda value: (value or "").casefold(), deterministic=True)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        if self.one("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"):
            version = self.one("SELECT MAX(version) v FROM schema_version")["v"]
            if version != 1:
                self.conn.close()
                raise RuntimeError("Unsupported schema version")
        self.conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def all(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def execute(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    def get_setting(self, key: str, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value):
        self.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def event(self, kind: str, task_id: str | None = None, **metadata):
        self.execute("INSERT INTO system_events(created_at,kind,task_id,metadata) VALUES(?,?,?,?)", (now(), kind, task_id, json.dumps(metadata)))

    @staticmethod
    def decode_task(row: dict) -> Task:
        data = json.loads(row["payload"])
        for k in ("id", "status", "created_at", "updated_at", "attempts", "blocked_reason", "next_run_at"):
            data[k] = row[k]
        return Task.model_validate(data)

    def task(self, task_id: str) -> Task | None:
        row = self.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        return self.decode_task(row) if row else None

    def tasks(self, status: str | None = None, limit: int = 200) -> list[Task]:
        rows = self.all("SELECT * FROM tasks " + ("WHERE status=? " if status else "") + "ORDER BY created_at DESC LIMIT ?", (status, limit) if status else (limit,))
        return [self.decode_task(r) for r in rows]

    def models(self) -> list[Model]:
        return [Model.model_validate_json(r["payload"]) for r in self.all("SELECT payload FROM models ORDER BY id")]

    def task_page(self, *, status: str, kind: str, project: str | None, search: str, sort: str, due: str, day_start: datetime, day_end: datetime, limit: int, offset: int) -> dict:
        # Chat turns are conversation messages, not to-do items.
        clauses, args = ["COALESCE(json_extract(payload,'$.handler'),'')!='chat'"], []
        if status == "active":
            clauses.append("status NOT IN ('done','cancelled')")
        elif status != "all":
            clauses.append("status=?")
            args.append(status)
        if kind != "all":
            clauses.append("type='user'" if kind == "user" else "type!='user'")
        if project:
            clauses.append("json_extract(payload,'$.project')=?")
            args.append(project)
        if search:
            clauses.append("(casefold(json_extract(payload,'$.title')) LIKE ? ESCAPE '\\' OR casefold(json_extract(payload,'$.description')) LIKE ? ESCAPE '\\')")
            escaped = search.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.extend(["%" + escaped + "%"] * 2)
        deadline = "julianday(json_extract(payload,'$.deadline'))"
        if due == "overdue":
            clauses.extend(["status NOT IN ('done','cancelled')", f"{deadline}<julianday(?)"])
            args.append(datetime.now(timezone.utc).isoformat())
        elif due == "today":
            clauses.extend(["status NOT IN ('done','cancelled')", f"{deadline}>=julianday(?)", f"{deadline}<julianday(?)"])
            args.extend([day_start.isoformat(), day_end.isoformat()])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        order = {"priority": f"priority DESC,{deadline} IS NULL,{deadline},created_at,id",
                 "deadline": f"{deadline} IS NULL,{deadline},priority DESC,created_at,id",
                 "created": "created_at DESC,id", "updated": "updated_at DESC,id"}[sort]
        total = self.one("SELECT COUNT(*) n FROM tasks" + where, args)["n"]
        rows = self.all("SELECT * FROM tasks" + where + " ORDER BY " + order + " LIMIT ? OFFSET ?", (*args, limit, offset))
        summary = self.one(f"SELECT COALESCE(SUM(type='user' AND status NOT IN ('done','cancelled')),0) active,COALESCE(SUM(type='user' AND status='done'),0) done,COALESCE(SUM(type!='user' AND status NOT IN ('done','cancelled')),0) background,COALESCE(SUM(type='user' AND status NOT IN ('done','cancelled') AND {deadline}<julianday(?)),0) overdue,COALESCE(SUM(type='user' AND status NOT IN ('done','cancelled') AND {deadline}>=julianday(?) AND {deadline}<julianday(?)),0) today FROM tasks WHERE COALESCE(json_extract(payload,'$.handler'),'')!='chat'", (datetime.now(timezone.utc).isoformat(), day_start.isoformat(), day_end.isoformat()))
        return {"items": [self.decode_task(row) for row in rows], "total": total, "offset": offset, "limit": limit,
                "next_offset": offset + limit if offset + limit < total else None, "summary": summary}

    def close(self):
        self.conn.close()
