"""Turn Telegram photos, documents and voice messages into model input."""
import base64
from dataclasses import dataclass, field

from .llm import LLMError

MAX_FILE = 20 * 1024 * 1024  # getFile downloads up to 20 MB
TEXT_TYPES = ("text/", "application/json", "application/xml", "application/x-yaml", "application/javascript")
TEXT_SUFFIXES = (".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html", ".css", ".xml", ".yaml", ".yml", ".log", ".sql", ".sh", ".ini", ".toml")
AUDIO_FORMATS = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/wav": "wav",
                 "audio/x-wav": "wav", "audio/flac": "flac", "audio/aac": "aac", "audio/webm": "webm"}


@dataclass
class Input:
    text: str                                       # stored in the history
    parts: list[dict] = field(default_factory=list)  # extra content parts for this turn only
    needs: str | None = None                        # input modality the model must support
    transcript: str | None = None
    pdf: bool = False


async def download(bot, file_id: str, size: int | None) -> bytes:
    if size and size > MAX_FILE:
        raise LLMError("Файл больше 20 МБ — Telegram не отдаёт ботам такие файлы")
    info = await bot.api.call("getFile", file_id=file_id)
    return await bot.api.download(info["file_path"])


async def read(bot, message: dict) -> Input | None:
    """None means the message has no content the bot understands."""
    caption = (message.get("caption") or message.get("text") or "").strip()
    if message.get("photo"):
        photo = message["photo"][-1]  # the largest size
        data = await download(bot, photo["file_id"], photo.get("file_size"))
        url = "data:image/jpeg;base64," + base64.b64encode(data).decode()
        return Input(text=("[фото] " + caption).strip(), needs="image",
                     parts=[{"type": "text", "text": caption or "Опиши, что на фото."}, {"type": "image_url", "image_url": {"url": url}}])
    if message.get("voice") or message.get("audio"):
        audio = message.get("voice") or message["audio"]
        fmt = AUDIO_FORMATS.get(audio.get("mime_type", "audio/ogg"), "ogg")
        data = await download(bot, audio["file_id"], audio.get("file_size"))
        transcript = await bot.llm.transcribe(data, fmt, bot.config.transcribe_model)
        text = (caption + "\n\n" + transcript).strip() if caption else transcript
        return Input(text=text, transcript=transcript)
    if message.get("document"):
        document = message["document"]
        name, mime = document.get("file_name") or "file", document.get("mime_type") or ""
        pdf = mime == "application/pdf" or name.lower().endswith(".pdf")
        textual = mime.startswith(TEXT_TYPES) or name.lower().endswith(TEXT_SUFFIXES)
        if not (pdf or textual or mime.startswith("image/")):
            raise LLMError(f"Не умею читать файлы типа {mime or name}. Поддерживаются фото, PDF, текстовые файлы и голосовые.")
        data = await download(bot, document["file_id"], document.get("file_size"))
        if pdf:
            url = "data:application/pdf;base64," + base64.b64encode(data).decode()
            return Input(text=f"[PDF {name}] {caption}".strip(), pdf=True,
                         parts=[{"type": "text", "text": caption or "Кратко перескажи документ."},
                                {"type": "file", "file": {"filename": name, "file_data": url}}])
        if mime.startswith("image/"):
            url = f"data:{mime};base64," + base64.b64encode(data).decode()
            return Input(text=f"[изображение {name}] {caption}".strip(), needs="image",
                         parts=[{"type": "text", "text": caption or "Опиши изображение."}, {"type": "image_url", "image_url": {"url": url}}])
        body = data.decode("utf-8", errors="replace")[:60000]
        return Input(text=f"{caption or 'Разбери файл.'}\n\nФайл {name}:\n```\n{body}\n```")
    return None
