"""OpenRouter chat completions: SSE streaming, tool calls, transcription."""
import base64
import json
import logging
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("llm_bot")
API = "https://openrouter.ai/api/v1"
# Server tools run on OpenRouter; the model decides when to call them.
SERVER_TOOLS = [{"type": "openrouter:web_search", "parameters": {"max_results": 5}}, {"type": "openrouter:web_fetch"}]
# PDFs go natively to models that read files; otherwise OpenRouter converts them for free.
FILE_PLUGIN = [{"id": "file-parser", "pdf": {"engine": "cloudflare-ai"}}]


class LLMError(Exception):
    pass


@dataclass
class Result:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    model: str | None = None


def _detail(raw: bytes) -> str:
    try:
        return json.loads(raw)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return ""


class OpenRouter:
    def __init__(self, api_key: str, model: str, client: httpx.AsyncClient, max_tokens: int = 4096, timeout: float = 300):
        self.api_key, self.model, self.client = api_key, model, client
        self.max_tokens, self.timeout = max_tokens, timeout
        self.last_model = None  # model that actually served the last answer (differs for openrouter/auto)
        self.server_tools = True  # switched off if OpenRouter rejects them for this account or API

    @property
    def headers(self) -> dict:
        return {"Authorization": "Bearer " + self.api_key, "X-Title": "Telegram LLM bot"}

    async def catalog(self) -> dict[str, dict]:
        """Public catalog: id -> {"price": (prompt, completion) USD per million tokens, "input": modalities}."""
        response = await self.client.get(API + "/models", timeout=30)
        result = {}
        for item in response.json()["data"]:
            try:
                price = (float(item["pricing"]["prompt"]) * 1e6, float(item["pricing"]["completion"]) * 1e6)
            except (KeyError, TypeError, ValueError):
                price = None
            result[item["id"]] = {"price": price, "input": (item.get("architecture") or {}).get("input_modalities") or ["text"]}
        return result

    async def complete(self, messages: list[dict], model: str | None = None, max_tokens: int = 64) -> str:
        """Short non-streamed completion: titles, suggestions."""
        body = {"model": model or self.model, "messages": messages, "max_tokens": max_tokens}
        try:
            response = await self.client.post(API + "/chat/completions", json=body, headers=self.headers, timeout=60)
            text = response.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            raise LLMError("OpenRouter: request failed") from None
        if response.status_code != 200 or not isinstance(text, str) or not text.strip():
            raise LLMError("OpenRouter: empty completion")
        return text

    async def run(self, messages: list[dict], model: str | None = None, tools: list[dict] | None = None,
                  plugins: list[dict] | None = None, on_text=None) -> Result:
        """One streamed model call. on_text(accumulated) is called for every text delta.

        A request rejected with HTTP 400 is retried without server tools, then without tools,
        so an unsupported tool never breaks plain answers."""
        variants = [tools] if tools else [None]
        if tools:
            own = [t for t in tools if t.get("type") == "function"]
            variants = ([tools] if self.server_tools else []) + ([own] if own and own != tools else []) + [None]
        for index, variant in enumerate(variants):
            try:
                return await self._run(messages, model, variant, plugins, on_text)
            except LLMError as exc:
                if not str(exc).startswith("OpenRouter HTTP 400") or index == len(variants) - 1:
                    raise
                if variant and any(t.get("type") != "function" for t in variant):
                    self.server_tools = False
                log.warning("retrying without %s: %s", "server tools" if variant == tools else "tools", exc)
        raise LLMError("unreachable")

    async def _run(self, messages, model, tools, plugins, on_text) -> Result:
        body = {"model": model or self.model, "messages": messages, "max_tokens": self.max_tokens, "stream": True}
        if tools:
            body["tools"] = tools
        if plugins:
            body["plugins"] = plugins
        result, calls = Result(), {}
        try:
            async with self.client.stream("POST", API + "/chat/completions", json=body, headers=self.headers, timeout=self.timeout) as response:
                if response.status_code != 200:
                    detail = _detail(await response.aread())
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
                    if chunk.get("model"):
                        result.model = self.last_model = chunk["model"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if isinstance(delta.get("content"), str) and delta["content"]:
                            result.text += delta["content"]
                            if on_text:
                                on_text(result.text)
                        for part in delta.get("tool_calls") or []:
                            call = calls.setdefault(part.get("index", 0), {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                            call["id"] = part.get("id") or call["id"]
                            function = part.get("function") or {}
                            call["function"]["name"] += function.get("name") or ""
                            call["function"]["arguments"] += function.get("arguments") or ""
        except httpx.HTTPError as exc:
            raise LLMError("OpenRouter: " + type(exc).__name__) from None
        result.tool_calls = [calls[i] for i in sorted(calls) if calls[i]["function"]["name"]]
        if not result.text.strip() and not result.tool_calls:
            raise LLMError("Модель вернула пустой ответ")
        return result

    async def transcribe(self, audio: bytes, fmt: str, model: str) -> str:
        body = {"model": model, "input_audio": {"data": base64.b64encode(audio).decode(), "format": fmt}}
        try:
            response = await self.client.post(API + "/audio/transcriptions", json=body, headers=self.headers, timeout=120)
            if response.status_code != 200:
                raise LLMError(f"Распознавание речи: HTTP {response.status_code} {_detail(response.content)}".strip()[:300])
            text = response.json()["text"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise LLMError("Распознавание речи не удалось") from None
        if not isinstance(text, str) or not text.strip():
            raise LLMError("Не удалось разобрать речь")
        return text.strip()
