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
        self.last_model = None  # model that actually served the last answer (differs for openrouter/auto)

    async def complete(self, messages: list[dict], max_tokens: int = 64) -> str:
        """Short non-streamed completion, e.g. a topic title."""
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        try:
            response = await self.client.post(self.url, json=body, headers={"Authorization": "Bearer " + self.api_key}, timeout=60)
            text = response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            raise LLMError("OpenRouter: title request failed") from None
        if response.status_code != 200 or not isinstance(text, str) or not text.strip():
            raise LLMError("OpenRouter: empty title")
        return text

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
                    if chunk.get("model"):
                        self.last_model = chunk["model"]
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
