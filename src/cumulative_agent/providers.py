from typing import Protocol
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

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

    async def complete(self, model: Model, prompt: str, output_limit: int, timeout: float) -> Completion:
        try:
            response = await self.client.post("https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"},
                json={"model": model.id, "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
                      "max_tokens": output_limit, "temperature": 0.2,
                      "provider": {"allow_fallbacks": False, "require_parameters": True,
                                   "max_price": {"prompt": model.input_price, "completion": model.output_price}}},
                timeout=timeout)
        except httpx.HTTPError as exc:
            raise ProviderFailure("OpenRouter transport failure (usage unknown)") from None
        if response.status_code != 200:
            raise ProviderFailure(f"OpenRouter HTTP {response.status_code} (usage unknown)",retry_delay(response.headers.get('Retry-After')) if response.status_code in (429,503) else 0)
        try:
            data = response.json()
            usage = data["usage"]
            input_tokens, output_tokens = int(usage["prompt_tokens"]), int(usage["completion_tokens"])
            calculated = (input_tokens * model.input_price + output_tokens * model.output_price) / 1_000_000
            cost = usage.get("cost")
            cost = float(cost) if cost is not None else calculated
            text = data["choices"][0]["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("empty completion")
            return Completion(text=text, input_tokens=input_tokens, output_tokens=output_tokens, cost=cost)
        except (KeyError, TypeError, ValueError, IndexError):
            raise ProviderFailure("OpenRouter returned invalid completion/usage (usage unknown)") from None
