"""OpenRouter: streamed chat with tools and reasoning, images, speech, transcription, embeddings, usage."""
import base64
import contextvars
import json
import logging
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("llm_bot")
API = "https://openrouter.ai/api/v1"
# Server tools run on OpenRouter; the model decides when to call them.
SERVER_TOOLS = [{"type": "openrouter:web_search", "parameters": {"max_results": 5}}, {"type": "openrouter:web_fetch"}]
# PDFs go natively to models that read files; otherwise OpenRouter converts them for free.
BASIC_SERVER_TOOLS = {"openrouter:web_search", "openrouter:web_fetch"}
FILE_PLUGIN = [{"id": "file-parser", "pdf": {"engine": "cloudflare-ai"}}]

# Who a call is for and why: set by the bot, read when usage is recorded.
chat_var = contextvars.ContextVar("chat", default=None)
purpose_var = contextvars.ContextVar("purpose", default="chat")


class LLMError(Exception):
    pass


@dataclass
class Result:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    model: str | None = None
    reasoning: str = ""
    usage: dict | None = None


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
        self.extra_tools = True   # advisor / subagent / fusion, switched off on the same signal
        self.fallbacks_ok = True
        self.usage_sink = None    # callable(chat_id, purpose, model, cost, tokens_in, tokens_out)

    @property
    def headers(self) -> dict:
        return {"Authorization": "Bearer " + self.api_key, "X-Title": "Telegram LLM bot"}

    def record(self, model: str | None, usage: dict | None, cost: float | None = None):
        if not self.usage_sink or (usage is None and cost is None):
            return
        usage = usage or {}
        try:
            value = float(usage.get("cost") if usage.get("cost") is not None else cost or 0)
            self.usage_sink(chat_var.get(), purpose_var.get(), model, value,
                            int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))
        except (TypeError, ValueError):
            log.warning("usage not recorded")

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

    async def key_info(self) -> dict:
        """Spend and limit of the API key itself, as OpenRouter counts them."""
        response = await self.client.get(API + "/key", headers=self.headers, timeout=30)
        return response.json().get("data") or {}

    async def complete(self, messages: list[dict], model: str | None = None, max_tokens: int = 64) -> str:
        """Short non-streamed completion: titles, suggestions."""
        body = {"model": model or self.model, "messages": messages, "max_tokens": max_tokens}
        try:
            response = await self.client.post(API + "/chat/completions", json=body, headers=self.headers, timeout=60)
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            raise LLMError("OpenRouter: request failed") from None
        self.record(data.get("model") or body["model"], data.get("usage"))
        if response.status_code != 200 or not isinstance(text, str) or not text.strip():
            raise LLMError("OpenRouter: empty completion")
        return text

    async def run(self, messages: list[dict], model: str | None = None, tools: list[dict] | None = None,
                  plugins: list[dict] | None = None, on_text=None, reasoning: dict | None = None,
                  fallbacks: list[str] | None = None, on_reasoning=None) -> Result:
        """One streamed model call. on_text(accumulated) is called for every text delta.

        A request rejected with HTTP 400 is retried with less: without the model-to-model tools
        (advisor, subagent, fusion), then without server tools, then without tools, then without
        fallback models, so an unsupported option never breaks plain answers."""
        variants = []
        if tools:
            basic = [t for t in tools if t.get("type") == "function" or t.get("type") in BASIC_SERVER_TOOLS]
            own = [t for t in tools if t.get("type") == "function"]
            candidates = ([tools] if self.extra_tools and self.server_tools else []) + ([basic] if self.server_tools else []) + [own, None]
            for candidate in candidates:
                if candidate is not None and not candidate:
                    continue
                if candidate not in variants:
                    variants.append(candidate)
        else:
            variants = [None]
        fallbacks = fallbacks if self.fallbacks_ok else None
        attempts = [(v, fallbacks) for v in variants] + ([(None, None)] if fallbacks else [])
        for index, (variant, chosen) in enumerate(attempts):
            try:
                return await self._run(messages, model, variant, plugins, on_text, reasoning, chosen, on_reasoning)
            except LLMError as exc:
                if not str(exc).startswith("OpenRouter HTTP 400") or index == len(attempts) - 1:
                    raise
                if variant and any(t.get("type") not in BASIC_SERVER_TOOLS and t.get("type") != "function" for t in variant):
                    self.extra_tools = False
                elif variant and any(t.get("type") != "function" for t in variant):
                    self.server_tools = False
                elif variant is None and chosen:
                    self.fallbacks_ok = False
                log.warning("retrying with fewer options: %s", exc)
        raise LLMError("unreachable")

    async def _run(self, messages, model, tools, plugins, on_text, reasoning, fallbacks, on_reasoning) -> Result:
        model = model or self.model
        body = {"model": model, "messages": messages, "max_tokens": self.max_tokens, "stream": True}
        if fallbacks:
            # OpenRouter tries these in order when the first model is down or refuses.
            body["models"] = [model, *[m for m in fallbacks if m != model]]
        if tools:
            body["tools"] = tools
        if plugins:
            body["plugins"] = plugins
        if reasoning:
            body["reasoning"] = reasoning
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
                    if chunk.get("usage"):
                        result.usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if isinstance(delta.get("reasoning"), str) and delta["reasoning"]:
                            result.reasoning += delta["reasoning"]
                            if on_reasoning:
                                on_reasoning(result.reasoning)
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
        finally:
            self.record(result.model or model, result.usage)
        result.tool_calls = [calls[i] for i in sorted(calls) if calls[i]["function"]["name"]]
        if not result.text.strip() and not result.tool_calls:
            raise LLMError("Модель вернула пустой ответ")
        return result

    async def image(self, prompt: str, model: str, aspect_ratio: str | None = None) -> tuple[bytes, str]:
        """Generate one image through chat completions with image output. Returns (bytes, mime)."""
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "modalities": ["image", "text"]}
        if aspect_ratio:
            body["image_config"] = {"aspect_ratio": aspect_ratio}
        for attempt in range(2):
            try:
                response = await self.client.post(API + "/chat/completions", json=body, headers=self.headers, timeout=180)
            except httpx.HTTPError as exc:
                raise LLMError("Генерация картинки: " + type(exc).__name__) from None
            if response.status_code == 400 and attempt == 0:
                body["modalities"] = ["image"]  # image-only models reject the text modality
                continue
            if response.status_code != 200:
                raise LLMError(f"Генерация картинки: HTTP {response.status_code} {_detail(response.content)}".strip()[:300])
            break
        try:
            data = response.json()
            self.record(data.get("model") or model, data.get("usage"))
            url = data["choices"][0]["message"]["images"][0]["image_url"]["url"]
            header, encoded = url.split(",", 1)
            return base64.b64decode(encoded), header.removeprefix("data:").split(";")[0] or "image/png"
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("Модель не вернула картинку") from None

    async def speech(self, text: str, model: str, voice: str, price_per_char: float = 0) -> bytes:
        """Text to speech as MP3 (Telegram plays MP3 as a voice message)."""
        body = {"model": model, "input": text, "voice": voice, "response_format": "mp3"}
        try:
            response = await self.client.post(API + "/audio/speech", json=body, headers=self.headers, timeout=180)
        except httpx.HTTPError as exc:
            raise LLMError("Озвучка: " + type(exc).__name__) from None
        if response.status_code != 200 or not response.content:
            raise LLMError(f"Озвучка: HTTP {response.status_code} {_detail(response.content)}".strip()[:300])
        self.record(model, None, cost=len(text) * price_per_char)  # the endpoint returns raw audio, no usage
        return response.content

    async def transcribe(self, audio: bytes, fmt: str, model: str) -> str:
        body = {"model": model, "input_audio": {"data": base64.b64encode(audio).decode(), "format": fmt}}
        try:
            response = await self.client.post(API + "/audio/transcriptions", json=body, headers=self.headers, timeout=120)
            if response.status_code != 200:
                raise LLMError(f"Распознавание речи: HTTP {response.status_code} {_detail(response.content)}".strip()[:300])
            data = response.json()
            text = data["text"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise LLMError("Распознавание речи не удалось") from None
        self.record(model, data.get("usage") if isinstance(data.get("usage"), dict) else None)
        if not isinstance(text, str) or not text.strip():
            raise LLMError("Не удалось разобрать речь")
        return text.strip()

    async def embed(self, texts: list[str], model: str, dimensions: int = 512) -> list[list[float]]:
        body = {"model": model, "input": texts, "dimensions": dimensions}
        try:
            response = await self.client.post(API + "/embeddings", json=body, headers=self.headers, timeout=60)
            if response.status_code != 200:
                raise LLMError(f"Эмбеддинги: HTTP {response.status_code} {_detail(response.content)}".strip()[:300])
            data = response.json()
            vectors = [item["embedding"] for item in sorted(data["data"], key=lambda item: item.get("index", 0))]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise LLMError("Эмбеддинги не получены") from None
        self.record(model, data.get("usage"))
        if len(vectors) != len(texts):
            raise LLMError("Эмбеддинги: неполный ответ")
        return vectors
