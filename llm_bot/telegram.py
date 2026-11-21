"""Thin Telegram Bot API client."""
import httpx


class TelegramError(Exception):
    def __init__(self, method: str, description: str, retry_after: float = 0):
        super().__init__(f"{method}: {description}")
        self.description, self.retry_after = description, retry_after


class Api:
    def __init__(self, token: str, client: httpx.AsyncClient):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.files = f"https://api.telegram.org/file/bot{token}/"
        self.client = client

    async def download(self, file_path: str) -> bytes:
        try:
            response = await self.client.get(self.files + file_path, timeout=120)
            response.raise_for_status()
            return response.content
        except httpx.HTTPError as exc:
            raise TelegramError("download", type(exc).__name__) from None

    async def call(self, method: str, http_timeout: float = 30, **params):
        try:
            response = await self.client.post(self.base + method, json=params, timeout=http_timeout)
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Never include the URL: it contains the bot token.
            raise TelegramError(method, type(exc).__name__) from None
        if not data.get("ok"):
            retry = (data.get("parameters") or {}).get("retry_after", 0)
            raise TelegramError(method, str(data.get("description", response.status_code)), retry)
        return data["result"]
