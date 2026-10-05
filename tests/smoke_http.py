"""Real TCP/CLI/backup/restart acceptance. No paid/provider requests."""
import json
import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as tmp:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        token = secrets.token_urlsafe(32)
        config = Path(tmp) / "config.yaml"
        config.write_text(yaml.safe_dump({"database": str(Path(tmp) / "agent.sqlite3"), "models": [], "limits": {"max_background_backlog": 0, "poll_seconds": .1, "min_free_disk_bytes": 0, "min_available_memory_bytes": 0, "max_load_per_cpu": 1000}}))
        env = dict(os.environ, PYTHONPATH=str(root / "src"), AGENT_API_TOKEN=token, AGENT_CONFIG=str(config), OPENROUTER_API_KEY="", TELEGRAM_BOT_TOKEN="", TELEGRAM_OWNER_IDS="")
        env.pop("AGENT_DATABASE", None)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback only
        base = f"http://127.0.0.1:{port}"

        def request(path, data=None, auth=True):
            headers = {"Content-Type": "application/json"}
            if auth:
                headers["Authorization"] = "Bearer " + token
            req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
            with opener.open(req, timeout=3) as response:
                return json.load(response)

        def start(log):
            process = subprocess.Popen([sys.executable, "-m", "cumulative_agent.cli", "serve", "--port", str(port)], cwd=root, env=env, stdout=log, stderr=log)
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Service exited before readiness")
                try:
                    request("/health", auth=False)
                    return process
                except (OSError, urllib.error.URLError):
                    time.sleep(.05)
            process.terminate()
            process.wait(timeout=10)
            raise TimeoutError("Service readiness timeout")

        def stop(process):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            # Uvicorn re-raises SIGTERM after completing graceful lifespan shutdown.
            assert process.returncode in (0, -signal.SIGTERM), process.returncode

        with (Path(tmp) / "server.log").open("w+") as log:
            process = start(log)
            try:
                try:
                    request("/api/tasks", auth=False)
                    raise AssertionError("API accepted missing token")
                except urllib.error.HTTPError as exc:
                    assert exc.code == 401
                request("/api/pause", {"enabled": True})
                background = request("/api/tasks", {"title": "Background report", "handler": "system_report", "required_capabilities": [], "type": "maintenance", "priority": 1000})
                user = request("/api/tasks", {"title": "User report", "handler": "system_report", "required_capabilities": [], "priority": -1000})
                request("/api/pause", {"enabled": False})
                for _ in range(100):
                    rows = request("/api/tasks")
                    if all(t["status"] == "done" for t in rows):
                        break
                    time.sleep(.05)
                assert all(t["status"] == "done" for t in rows)
                usage = request("/api/usage")
                assert sorted(usage, key=lambda r: r["created_at"])[0]["task_type"] == "user"
                assert request("/api/tasks/" + user["id"])["runs"][0]["result"]
                metrics = request("/api/metrics")
                assert metrics["tasks_completed"] == 2 and metrics["cost"] == 0
                assert metrics["background_tasks_completed"] == 1
                backup = Path(tmp) / "backup.sqlite3"
                subprocess.run([sys.executable, "-m", "cumulative_agent.cli", "backup", "--output", str(backup)], cwd=root, env=env, check=True)
                with sqlite3.connect(backup) as conn:
                    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0] == 2
            finally:
                stop(process)
            process = start(log)
            try:
                assert request("/api/tasks/" + background["id"])["task"]["status"] == "done"
                assert request("/api/metrics")["runs"] == 2
            finally:
                stop(process)
            log.seek(0)
            logs = log.read()
            assert token not in logs
            assert logs.count("Application shutdown complete.") == 2
        print(json.dumps({"http": "passed", "user_priority": "passed", "backup_integrity": "passed", "restart_persistence": "passed", "tasks_completed": 2, "cost": 0, "secrets_in_logs": False}))


if __name__ == "__main__":
    main()
