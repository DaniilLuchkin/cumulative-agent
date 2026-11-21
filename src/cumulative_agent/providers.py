from typing import Protocol
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import json

import httpx

from .domain import Completion, Model
from .routing import SYSTEM_PROMPT


class Provider(Protocol):
    async def complete(self, model: Model, prompt: str, output_limit: int, timeout: float) -> Completion: ...


class ProviderFailure(Exception):
    """Sanitized provider failure: never include response body or credentials."""
    def __init__(self,message,retry_after=0):
        super().__init__(message)
        self.retry_after=retry_after


def retry_delay(value):
    try: delay=float(value)
    except (TypeError,ValueError):
        try: delay=(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds()
        except (TypeError,ValueError,OverflowError): delay=60
    return max(1,min(86400,delay))


class OpenRouter:
    def __init__(self, api_key: str, client: httpx.AsyncClient):
        self.api_key, self.client = api_key, client

    def _body(self, model: Model, messages: list[dict], output_limit: int, temperature: float) -> dict:
        return {"model": model.id, "messages": messages, "max_tokens": output_limit, "temperature": temperature,
                "provider": {"allow_fallbacks": False, "require_parameters": True,
                             "max_price": {"prompt": model.input_price, "completion": model.output_price}}}

    def _usage(self, model: Model, usage: dict) -> tuple[int, int, float]:
        input_tokens, output_tokens = int(usage["prompt_tokens"]), int(usage["completion_tokens"])
        cost = usage.get("cost")
        cost = float(cost) if cost is not None else (input_tokens * model.input_price + output_tokens * model.output_price) / 1_000_000
        return input_tokens, output_tokens, cost

    async def complete(self, model: Model, prompt: str, output_limit: int, timeout: float, *, messages: list[dict] | None = None, on_delta=None) -> Completion:
        """Single completion. With on_delta, streams SSE and reports accumulated text."""
        if messages is None:
            messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
        if on_delta is not None:
            return await self._stream(model, messages, output_limit, timeout, on_delta)
        try:
            response = await self.client.post("https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
                json=self._body(model, messages, output_limit, 0.2), timeout=timeout)
        except httpx.HTTPError as exc:
            raise ProviderFailure("OpenRouter transport failure (usage unknown)") from None
        if response.status_code != 200:
            raise ProviderFailure(f"OpenRouter HTTP {response.status_code} (usage unknown)",retry_delay(response.headers.get('Retry-After')) if response.status_code in (429,503) else 0)
        try:
            data = response.json()
            input_tokens, output_tokens, cost = self._usage(model, data["usage"])
            text = data["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("empty completion")
            return Completion(text=text, input_tokens=input_tokens, output_tokens=output_tokens, cost=cost)
        except (KeyError, TypeError, ValueError, IndexError):
            raise ProviderFailure("OpenRouter returned invalid completion/usage (usage unknown)") from None

    async def _stream(self, model: Model, messages: list[dict], output_limit: int, timeout: float, on_delta) -> Completion:
        body = self._body(model, messages, output_limit, 0.5)
        body.update(stream=True, usage={"include": True})
        parts: list[str] = []
        usage = None
        try:
            async with self.client.stream("POST", "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
                    json=body, timeout=timeout) as response:
                if response.status_code != 200:
                    raise ProviderFailure(f"OpenRouter HTTP {response.status_code} (usage unknown)",
                                          retry_delay(response.headers.get('Retry-After')) if response.status_code in (429, 503) else 0)
                async for line in response.aiter_lines():
                    # SSE comments (": OPENROUTER PROCESSING") keep the connection alive.
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        raise ProviderFailure("OpenRouter stream error (usage unknown)")
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if isinstance(delta, str) and delta:
                            parts.append(delta)
                            on_delta("".join(parts))
        except httpx.HTTPError:
            raise ProviderFailure("OpenRouter transport failure (usage unknown)") from None
        text = "".join(parts)
        try:
            if not text.strip():
                raise ValueError("empty completion")
            input_tokens, output_tokens, cost = self._usage(model, usage)
        except (KeyError, TypeError, ValueError):
            raise ProviderFailure("OpenRouter returned invalid completion/usage (usage unknown)") from None
        return Completion(text=text, input_tokens=input_tokens, output_tokens=output_tokens, cost=cost)
