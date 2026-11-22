"""Rich outputs the agent can send: widgets, polls and quizzes, images, files, voice."""
import json
import logging
import re
import zlib
from pathlib import Path

from .llm import LLMError, purpose_var
from .telegram import TelegramError

log = logging.getLogger("llm_bot")

WIDGET_RULES = (Path(__file__).with_name("widget_prompt.txt")).read_text()
WIDGET_PROTOCOL = """
THIS BOT
- Reply with the widget's HTML only: no explanations, no Markdown fences.
- Write all visible text in the language of the request.
- Interaction normally happens locally in JavaScript (state in variables and localStorage).
- Only if the request says the widget may message the assistant: on a tap, call
  TelegramWidget.request('say:' + text) where text is a short phrase (the whole data at most 64 bytes)
  describing the user's choice. It is delivered to the assistant as the user's message and the
  assistant answers in the chat. Show "Отправлено" (or the request language equivalent) on success
  and the error code on failure. Otherwise never call request().
- Always render something useful immediately; the widget can be destroyed and recreated at any time."""
MAX_HTML, MAX_COMPRESSED = 32768, 4096
WIDGET_MAX_TOKENS = 16000
SAFE_FILE = re.compile(r"^[\w\-. ]{1,64}\.(txt|md|csv|tsv|json|xml|yaml|yml|html|css|js|ts|py|sql|sh|ini|toml|svg|ics|vcf|log)$", re.UNICODE)
MIME = {"csv": "text/csv", "tsv": "text/tab-separated-values", "json": "application/json", "html": "text/html", "svg": "image/svg+xml",
        "ics": "text/calendar", "vcf": "text/vcard", "xml": "application/xml", "md": "text/markdown"}

FUNCTIONS = [
    {"type": "function", "function": {
        "name": "show_widget",
        "description": ("Send an interactive or visual HTML widget drawn inside the chat bubble. Use it when interaction or a visual "
                        "explains better than text: calculators and converters, timers, charts and comparisons, flashcards, step-by-step "
                        "explanations, trackers and checklists, mini-games, pickers. Not for plain answers."),
        "parameters": {"type": "object", "required": ["description", "caption"], "properties": {
            "description": {"type": "string", "description": "What the widget shows and does, with all data and numbers it must contain"},
            "caption": {"type": "string", "description": "One-line caption under the widget, in the user's language"},
            "ask_bot": {"type": "boolean", "description": "True if taps must reach you as messages (choices you should react to)"}}}}},
    {"type": "function", "function": {
        "name": "create_poll",
        "description": "Send a native Telegram poll or quiz: to check knowledge, train, or let the user pick among options.",
        "parameters": {"type": "object", "required": ["question", "options"], "properties": {
            "question": {"type": "string", "description": "Up to 300 characters"},
            "options": {"type": "array", "items": {"type": "string"}, "description": "2-12 options, each up to 100 characters"},
            "quiz": {"type": "boolean", "description": "True for a quiz with correct answers"},
            "correct_option_ids": {"type": "array", "items": {"type": "integer"}, "description": "0-based ids of correct options (quiz)"},
            "explanation": {"type": "string", "description": "Shown after a wrong answer, up to 200 characters (quiz)"},
            "multiple": {"type": "boolean", "description": "Allow several answers"}}}}},
    {"type": "function", "function": {
        "name": "generate_image",
        "description": "Generate an image (illustration, picture, logo, diagram sketch) and send it to the user.",
        "parameters": {"type": "object", "required": ["prompt"], "properties": {
            "prompt": {"type": "string", "description": "Detailed image description in English"},
            "caption": {"type": "string", "description": "Short caption in the user's language"},
            "aspect_ratio": {"type": "string", "enum": ["1:1", "16:9", "9:16", "4:3", "3:4"]}}}}},
    {"type": "function", "function": {
        "name": "send_file",
        "description": "Send a text-based file the user can save: CSV or TSV tables, JSON, Markdown, code, .ics calendar events, .vcf contacts, SVG.",
        "parameters": {"type": "object", "required": ["filename", "content"], "properties": {
            "filename": {"type": "string", "description": "File name with extension, e.g. budget.csv"},
            "content": {"type": "string"},
            "caption": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "answer_with_voice",
        "description": "Call when the user asks you to answer by voice or to read something aloud: your final answer is sent as a voice message plus its text.",
        "parameters": {"type": "object", "properties": {}}}},
]
STATUS = {"show_widget": "Рисую виджет…", "create_poll": "Готовлю опрос…", "generate_image": "Рисую картинку…",
          "send_file": "Готовлю файл…", "answer_with_voice": "Ответ будет голосом…"}
NAMES = {f["function"]["name"] for f in FUNCTIONS}


def compressed_size(html: str) -> int:
    return len(zlib.compress(html.encode("utf-8"), 9))


def strip_fences(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text, re.S)
    return match.group(1).strip() if match else text


def speakable(markdown: str) -> str:
    """Markdown to plain sentences for text-to-speech."""
    text = re.sub(r"```.*?```", " (код — в тексте) ", markdown, flags=re.S)
    text = re.sub(r"<details>.*?</details>", " ", text, flags=re.S)
    text = re.sub(r"^\s*\|.*\|\s*$", " ", text, flags=re.M)        # tables
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)            # images, tg://time
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)             # links
    text = re.sub(r"^\s*\[\^[^\]]+\]:.*$", " ", text, flags=re.M)   # footnote definitions
    text = re.sub(r"\[\^[^\]]+\]", "", text)                        # footnote references
    text = re.sub(r"<[^>]+>|\$\$?[^$]+\$\$?", " ", text)             # html, formulas
    text = re.sub(r"[*_`#>~=|]+", "", text)
    text = re.sub(r"^\s*[-+]\s*(\[[ x]\])?\s*", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()


class Outputs:
    def __init__(self, bot):
        self.bot = bot

    async def call(self, name: str, args: dict, ctx: dict) -> str:
        token = purpose_var.set({"show_widget": "widget", "generate_image": "image"}.get(name, purpose_var.get()))
        try:
            return await self._call(name, args, ctx)
        finally:
            purpose_var.reset(token)

    async def _call(self, name: str, args: dict, ctx: dict) -> str:
        try:
            if name == "show_widget":
                return await self.widget(ctx, str(args.get("description", "")), str(args.get("caption", "")), bool(args.get("ask_bot")))
            if name == "create_poll":
                return await self.poll(ctx, args)
            if name == "generate_image":
                return await self.image(ctx, str(args.get("prompt", "")), str(args.get("caption") or ""), args.get("aspect_ratio"))
            if name == "send_file":
                return await self.file(ctx, str(args.get("filename", "")), str(args.get("content", "")), str(args.get("caption") or ""))
            if name == "answer_with_voice":
                ctx["voice"] = True
                return json.dumps({"ok": True, "note": "Write the final answer as natural speech: short, no tables or code."})
        except (LLMError, TelegramError, ValueError, TypeError) as exc:
            log.warning("%s failed: %s", name, exc)
            return json.dumps({"error": str(exc)[:300], "note": "Tell the user briefly and answer in text instead."}, ensure_ascii=False)
        return json.dumps({"error": "unknown tool"})

    def target(self, ctx: dict) -> dict:
        params = {"chat_id": ctx["chat_id"]}
        if ctx["thread_id"]:
            params["message_thread_id"] = ctx["thread_id"]
        return params

    # --- widgets -------------------------------------------------------------------

    async def widget(self, ctx: dict, description: str, caption: str, ask_bot: bool) -> str:
        if not description:
            raise ValueError("description is empty")
        request = f"Caption: {caption}\nThe widget may message the assistant: {'yes' if ask_bot else 'no'}\n\n{description}"
        messages = [{"role": "system", "content": WIDGET_RULES + "\n" + WIDGET_PROTOCOL}, {"role": "user", "content": request}]
        models = [self.bot.current_model(ctx["chat_id"])]
        models += [m for m in self.bot.models if m not in models and m != "openrouter/auto"][:1]
        html, sent = "", None
        for attempt in range(4):
            html = strip_fences(await self.author(messages, models)).replace("\x00", "")
            size, packed = len(html.encode("utf-8")), compressed_size(html)
            if not html or size > MAX_HTML or packed > MAX_COMPRESSED:
                problem = f"Too large: {size} bytes, {packed} bytes compressed (limits {MAX_HTML} and {MAX_COMPRESSED})."
            else:
                try:
                    sent = await self.bot.api.call("sendWidget", html=html, caption=caption[:1024] or None,
                                                   allow_queries=ask_bot or None, **self.target(ctx))
                    break
                except TelegramError as exc:
                    log.warning("sendWidget rejected: %s (html %s bytes, %s compressed)", exc.description, size, packed)
                    if "WIDGET" not in exc.description.upper() or attempt == 3:
                        raise
                    problem = f"Telegram rejected the widget: {exc.description}."
            messages += [{"role": "assistant", "content": html},
                         {"role": "user", "content": problem + " Rewrite it smaller and simpler, within the widget profile: "
                                                               "drop decoration and repetition, keep the function. HTML only."}]
        else:
            raise ValueError("the widget does not fit Telegram's limits")
        message_id = sent.get("message_id") if isinstance(sent, dict) else None
        if message_id:
            self.bot.store.run("INSERT INTO widgets(chat_id,thread_id,message_id,caption) VALUES(?,?,?,?)",
                               (ctx["chat_id"], ctx["thread_id"], message_id, caption))
        ctx.setdefault("artifacts", []).append(f"[виджет: {caption}]")
        return json.dumps({"sent": True, "compressed_bytes": compressed_size(html),
                           "note": "The widget is already in the chat above your answer; do not repeat its content, add a short comment at most."},
                          ensure_ascii=False)

    async def author(self, messages: list[dict], models: list[str]) -> str:
        """Write the widget HTML. Room for reasoning plus up to ~32 KB of HTML; another model if the first returns nothing."""
        error = None
        for model in models:
            try:
                return (await self.bot.llm.run(messages, model, reasoning={"effort": "low"}, max_tokens=WIDGET_MAX_TOKENS)).text
            except LLMError as exc:
                error = exc
                log.warning("widget author %s failed: %s", model, exc)
        raise LLMError(f"модель не написала HTML виджета ({error})")

    async def on_widget_query(self, query: dict):
        """A tap in a widget sent with ask_bot: answer at once, then let the assistant react in the chat."""
        user, data = query.get("from", {}), str(query.get("data", ""))
        allowed = self.bot.allowed(user.get("id"))
        await self.bot.api.call("answerWidgetQuery", widget_query_id=query["id"], data="ok" if allowed else "forbidden")
        chat_id, message_id = (query.get("chat") or {}).get("id"), query.get("message_id")
        if not allowed or not chat_id or not data.startswith("say:"):
            return
        row = self.bot.store.one("SELECT * FROM widgets WHERE chat_id=? AND message_id=?", (chat_id, message_id))
        thread_id, caption = (row["thread_id"], row["caption"]) if row else (0, "")
        from .media import Input
        text = data[4:].strip()
        self.bot.spawn(self.bot.answer(chat_id, thread_id, Input(text=f"[в виджете «{caption}»] {text}"), None, prefix=f"> {text}\n\n"))

    # --- polls, images, files -----------------------------------------------------------

    async def poll(self, ctx: dict, args: dict) -> str:
        options = [str(o).strip()[:100] for o in args.get("options") or [] if str(o).strip()][:12]
        question = str(args.get("question", "")).strip()[:300]
        if not question or len(options) < 2:
            raise ValueError("a question and at least 2 options are required")
        params = {"question": question, "options": [{"text": o} for o in options], **self.target(ctx)}
        if args.get("quiz"):
            correct = sorted({int(i) for i in args.get("correct_option_ids") or [] if 0 <= int(i) < len(options)})
            if not correct:
                raise ValueError("a quiz needs correct_option_ids")
            params.update(type="quiz", correct_option_ids=correct, allows_multiple_answers=len(correct) > 1)
            if args.get("explanation"):
                params["explanation"] = str(args["explanation"])[:200]
        elif args.get("multiple"):
            params["allows_multiple_answers"] = True
        await self.bot.api.call("sendPoll", **params)
        ctx.setdefault("artifacts", []).append(f"[{'викторина' if args.get('quiz') else 'опрос'}: {question}]")
        return json.dumps({"sent": True, "note": "The poll is in the chat; do not repeat the options or reveal the answer."})

    async def image(self, ctx: dict, prompt: str, caption: str, aspect_ratio) -> str:
        if not prompt:
            raise ValueError("prompt is empty")
        data, mime = await self.bot.llm.image(prompt, self.bot.config.image_model, aspect_ratio)
        extension = mime.split("/")[-1].replace("jpeg", "jpg")
        await self.bot.api.upload("sendPhoto", "photo", f"image.{extension}", data, mime, caption=caption[:1024] or None, **self.target(ctx))
        ctx.setdefault("artifacts", []).append(f"[картинка: {caption or prompt[:100]}]")
        return json.dumps({"sent": True, "note": "The image is in the chat; comment briefly if useful."})

    async def file(self, ctx: dict, filename: str, content: str, caption: str) -> str:
        filename = filename.strip().replace("/", "_")
        if not SAFE_FILE.match(filename):
            raise ValueError("unsupported file name or extension")
        if not content or len(content.encode("utf-8")) > 2_000_000:
            raise ValueError("content is empty or larger than 2 MB")
        mime = MIME.get(filename.rsplit(".", 1)[-1].lower(), "text/plain")
        await self.bot.api.upload("sendDocument", "document", filename, content.encode("utf-8"), mime, caption=caption[:1024] or None, **self.target(ctx))
        ctx.setdefault("artifacts", []).append(f"[файл: {filename}]")
        return json.dumps({"sent": True, "note": "The file is in the chat; do not paste its content again."})

    # --- voice ---------------------------------------------------------------------

    def voice_for(self, text: str) -> str:
        cyrillic = sum("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in text[:500])
        return self.bot.config.tts_voice_ru if cyrillic > len(text[:500]) * 0.2 else self.bot.config.tts_voice_en

    async def send_voice(self, chat_id: int, thread_id: int, markdown: str, reply_to: int | None) -> bool:
        text = speakable(markdown)[:3000]
        if not text:
            return False
        token = purpose_var.set("voice")
        try:
            audio = await self.bot.llm.speech(text, self.bot.config.tts_model, self.voice_for(text), self.bot.config.tts_price_per_char)
            target = self.bot._target(chat_id, thread_id, reply_to)
            await self.bot.api.upload("sendVoice", "voice", "answer.mp3", audio, "audio/mpeg", **target)
            return True
        except (LLMError, TelegramError) as exc:
            log.warning("voice answer failed: %s", exc)
            return False
        finally:
            purpose_var.reset(token)
