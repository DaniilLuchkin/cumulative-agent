import argparse
import json
import logging
import os
from datetime import datetime, timezone

import uvicorn

from .config import load_settings
from .repository import Repository


class StructuredFormatter(logging.Formatter):
    def format(self, record):
        data = {"timestamp": datetime.now(timezone.utc).isoformat(), "level": record.levelname, "event": record.getMessage()}
        for key in ("task_id", "run_id", "status", "cost", "error_type"):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        return json.dumps(data, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description="Cumulative Agent")
    parser.add_argument("command", choices=("serve", "init-db", "backup"), nargs="?", default="serve")
    parser.add_argument("--config")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--output", help="Backup SQLite destination")
    args = parser.parse_args()
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    os.umask(0o077)
    settings = load_settings(args.config)
    if args.command == "serve":
        from .api import create_app
        if len(settings.api_token) < 24:
            parser.error("AGENT_API_TOKEN must contain at least 24 characters")
        if args.host not in ("127.0.0.1", "::1") and os.environ.get("AGENT_ALLOW_REMOTE") != "1":
            parser.error("Remote binding requires AGENT_ALLOW_REMOTE=1 and a TLS/authenticated reverse proxy")
        uvicorn.run(create_app(settings), host=args.host, port=args.port, workers=1, access_log=False, log_config=None)
    else:
        repo = Repository(settings.database)
        if args.command == "backup":
            import sqlite3
            from pathlib import Path
            if not args.output:
                parser.error("--output is required for backup")
            destination = Path(args.output).resolve()
            if destination.exists() or destination == Path(settings.database).resolve():
                parser.error("Backup destination must be a new file")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(destination) as backup:
                repo.conn.backup(backup)
        repo.close()


if __name__ == "__main__":
    main()
