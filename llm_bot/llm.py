"""OpenRouter chat completions with SSE streaming."""
import json
from collections.abc import AsyncIterator

import httpx


class LLMError(Exception):
    pass


class OpenRouter:
    url = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, model: str, client: httpx.AsyncClient, max_tokens: int = 4096, timeout: float = 300):
        self.api_key, self.model, self.client = api_key, model, client
        self.max_tokens, self.timeout = max_tokens, timeout

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        """Yield the accumulated answer text after every received delta."""
        body = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens, "stream": True}
        headers = {"Authorization": "Bearer " + self.api_key, "X-Title": "Telegram LLM bot"}
        text = ""
        try:
            async with self.client.stream("POST", self.url, json=body, headers=headers, timeout=self.timeout) as response:
                if response.status_code != 200:
                    raw = await response.aread()
                    try:
                        detail = json.loads(raw)["error"]["message"]
                    except (ValueError, KeyError, TypeError):
                        detail = ""
                    raise LLMError(f"OpenRouter HTTP {response.status_code} {detail}".strip()[:300])
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue  # SSE comments such as ": OPENROUTER PROCESSING"
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        raise LLMError(str(chunk["error"].get("message", "stream error"))[:300])
                    for choice in chunk.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if isinstance(delta, str) and delta:
                            text += delta
                            yield text
        except httpx.HTTPError as exc:
            raise LLMError("OpenRouter: " + type(exc).__name__) from None
        if not text.strip():
            raise LLMError("Модель вернула пустой ответ")
