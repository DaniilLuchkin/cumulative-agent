"""Mini App: a panel with the model, tasks, spending and memory search, served by the bot itself.

Listens on 127.0.0.1 (WEB_PORT, default 8000) behind the HTTPS nginx site that the previous
cumulative-agent installation created. Every API call carries Telegram's signed initData, checked
with the bot token (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
"""
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

log = logging.getLogger("llm_bot")
PAGE = (Path(__file__).with_name("miniapp.html")).read_text()
MAX_AGE = 24 * 3600
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline' https://telegram.org; style-src 'unsafe-inline'; "
       "connect-src 'self'; img-src 'self' data:; frame-ancestors https://web.telegram.org https://*.telegram.org")


def check_init_data(init_data: str, bot_token: str, now: float | None = None) -> dict | None:
    """Returns the user dict when the signature is valid and fresh, else None."""
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received = fields.pop("hash", "")
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not received or not hmac.compare_digest(expected, received):
        return None
    try:
        if (now or time.time()) - int(fields.get("auth_date", "0")) > MAX_AGE:
            return None
        user = json.loads(fields.get("user", "{}"))
    except ValueError:
        return None
    return user if isinstance(user.get("id"), int) else None


class Panel:
    def __init__(self, bot):
        self.bot = bot
        self.runner = None

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self.security_headers], client_max_size=64 * 1024)
        app.router.add_get("/", self.page)
        app.router.add_get("/app", self.page)
        app.router.add_get("/api/state", self.api(self.state))
        app.router.add_post("/api/model", self.api(self.set_model))
        app.router.add_post("/api/reasoning", self.api(self.set_reasoning))
        app.router.add_get("/api/tasks", self.api(self.tasks))
        app.router.add_post("/api/cancel", self.api(self.cancel))
        app.router.add_get("/api/usage", self.api(self.usage))
        app.router.add_get("/api/search", self.api(self.search))
        return app

    async def start(self, port: int):
        self.runner = web.AppRunner(self.app(), access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", port).start()
        log.info("mini app listening on 127.0.0.1:%s", port)

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()

    @web.middleware
    async def security_headers(self, request, handler):
        response = await handler(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    async def page(self, request):
        return web.Response(text=PAGE, content_type="text/html")

    def api(self, handler):
        async def wrapped(request):
            init_data = request.headers.get("Authorization", "").removeprefix("tma ").strip()
            user = check_init_data(init_data, self.bot.config.telegram_token)
            if not user or not self.bot.allowed(user["id"]):
                return web.json_response({"error": "unauthorized"}, status=401)
            try:
                body = await request.json() if request.method == "POST" else {}
                return web.json_response(await handler(user["id"], body if isinstance(body, dict) else {}, request))
            except (ValueError, KeyError, TypeError) as exc:
                return web.json_response({"error": str(exc)[:200]}, status=400)
        return wrapped

    # --- endpoints ---------------------------------------------------------------------

    async def state(self, user_id, body, request):
        bot = self.bot
        place = bot.location(user_id) if hasattr(bot, "location") else None
        return {"model": bot.current_model(user_id), "reasoning": bot.reasoning_level(user_id),
                "models": [{"id": m, "price": (bot.catalog.get(m) or {}).get("price")} for m in bot.models],
                "timezone": bot.zone_label(user_id), "location": bool(place), "username": bot.username,
                "widgets": bot.outputs.widgets, "secretary": bot.secretary.enabled}

    async def set_model(self, user_id, body, request):
        if body.get("model") not in self.bot.models:
            raise ValueError("unknown model")
        self.bot.store.set(f"model:{user_id}", body["model"])
        return await self.state(user_id, body, request)

    async def set_reasoning(self, user_id, body, request):
        if body.get("level") not in ("auto", "low", "high"):
            raise ValueError("unknown level")
        self.bot.store.set(f"reasoning:{user_id}", body["level"])
        return await self.state(user_id, body, request)

    async def tasks(self, user_id, body, request):
        return self.bot.agent.scheduled(user_id)

    async def cancel(self, user_id, body, request):
        item = str(body.get("id", ""))
        cancelled = self.bot.agent.cancel(user_id, item)
        if cancelled and item.startswith("w"):
            await self.bot.agent.refresh_status(int(item[1:]))
        return {"cancelled": cancelled, **self.bot.agent.scheduled(user_id)}

    async def usage(self, user_id, body, request):
        store, now = self.bot.store, time.time()
        days = [{"label": label, **dict(store.one("SELECT COUNT(*) calls, COALESCE(SUM(cost),0) cost FROM usage WHERE ts>=?", (since,)))}
                for label, since in (("Сегодня", now - now % 86400), ("7 дней", now - 7 * 86400), ("30 дней", now - 30 * 86400))]
        purposes = [dict(r) for r in store.all("SELECT purpose, COALESCE(SUM(cost),0) cost FROM usage WHERE ts>=? GROUP BY purpose ORDER BY cost DESC",
                                               (now - 30 * 86400,))]
        return {"periods": days, "purposes": purposes}

    async def search(self, user_id, body, request):
        query = request.query.get("q", "").strip()[:200]
        if not query:
            raise ValueError("empty query")
        return json.loads(await self.bot.memory.search(user_id, query, -1))
