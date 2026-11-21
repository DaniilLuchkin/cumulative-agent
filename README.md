# Telegram LLM bot (ветка Alt)

Простой LLM-чат-бот, сделанный по рекомендациям Telegram для AI-ботов
([Bot Features → Streaming Replies, Topics in Private Chats, AI Agents](https://core.telegram.org/bots/features)).
Без задач, очередей и Mini App. Модель вызывается через OpenRouter.

## Как выглядит в Telegram

| Что видит пользователь | Bot API |
|---|---|
| Сразу после вопроса — «Thinking…» | `sendMessageDraft` с пустым `text` |
| Ответ набирается в реальном времени, с форматированием | `sendRichMessageDraft` (`markdown`) каждые 0,8 с; черновик обновляется раньше, чем истекут его 30 с |
| Кнопка «Стоп» под черновиком | `can_stop: true` → update `stopped_message_generation` → вызов модели отменяется, частичный ответ сохраняется |
| Итоговый ответ остаётся в чате: заголовки, таблицы, код | `sendRichMessage` (до 32768 символов), ответ на сообщение пользователя; при ошибке — `sendMessage` частями по 4000 |
| Отдельные диалоги в топиках | `/new` → `createForumTopic`, имя по первому вопросу через `editForumTopic`; контекст свой у каждого топика |
| Подсказки команд | `setMyCommands`: `/new`, `/reset`, `/help` |

Бот работает только в личном чате. Доступ есть у пользователей из `ALLOWED_USER_IDS`,
а если список пуст — у первого, кто напишет боту. История хранится в SQLite; в модель
уходят последние сообщения общим объёмом до `HISTORY_CHARS` символов.

## Установка на Ubuntu

```bash
curl -fsSL https://raw.githubusercontent.com/DaniilLuchkin/cumulative-agent/Alt/deploy/install.sh | sudo bash
```

Скрипт спросит токен бота, ключ OpenRouter, модель и разрешённые ID и поставит
systemd-сервис `llm-bot`. Если на сервере запущен `cumulative-agent` с тем же ботом,
скрипт предложит его остановить: Telegram отдаёт обновления только одному получателю.
Повторный запуск обновляет код и сохраняет настройки и историю.

* Настройки: `/etc/llm-bot.env`. После изменения: `sudo systemctl restart llm-bot`.
* Логи: `journalctl -u llm-bot -f`.
* Для топиков включите для бота режим топиков в @BotFather и перезапустите сервис.

| Переменная | По умолчанию | |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | обязательна |
| `OPENROUTER_API_KEY` | — | обязательна |
| `LLM_MODEL` | `openrouter/auto` | любой id с openrouter.ai/models |
| `ALLOWED_USER_IDS` | пусто | ID через запятую |
| `SYSTEM_PROMPT` | короткий промпт ассистента | |
| `MAX_OUTPUT_TOKENS` | 4096 | |
| `HISTORY_CHARS` | 24000 | |
| `BOT_DB` | `data/bot.sqlite3` | |

## Локально

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
TELEGRAM_BOT_TOKEN=... OPENROUTER_API_KEY=... .venv/bin/llm-bot
.venv/bin/python -m unittest discover -s tests
```
