# Telegram LLM bot (ветка Alt)

LLM-агент для Telegram, собранный из возможностей самого Telegram
([Bot Features → AI Agents](https://core.telegram.org/bots/features)). Модели — через OpenRouter.

## Как выглядит в Telegram

| Что видит пользователь | Bot API |
|---|---|
| Сразу после вопроса — «Thinking…», реакция 👀 на сообщении | `sendMessageDraft` с пустым `text`, `setMessageReaction` |
| Ответ набирается в реальном времени; кнопка «Стоп» | `sendRichMessageDraft` (`markdown`), `can_stop` → update `stopped_message_generation` |
| Пока агент работает инструментом — «Ставлю напоминание…» | `sendRichMessageDraft` с `<tg-thinking>` |
| Итог — Rich Message: таблицы, task lists, `<details>`, сноски | `sendRichMessage`; при ошибке — `sendMessage` частями по 4000 |
| Под ответом: 3 вопроса для продолжения и «🔄 Перегенерировать» | inline-клавиатура, `editMessageReplyMarkup`; вопросы предлагает `UTILITY_MODEL` |
| Исправили своё сообщение — ответ переписывается | update `edited_message`, `deleteMessage` старого ответа |
| «Напомни завтра в 9…» → 🫡, в срок приходит ⏰ с кнопками +15 мин / +1 час / Завтра | tool calling, бот пишет первым; время — `tg://time` (показывается в часовом поясе читателя) |
| «Следи за …» → отдельный топик 👁 с закреплённым статусом и кнопками «Проверить сейчас / Остановить»; отчёты — в топик, только когда есть новое | `createForumTopic`, `pinChatMessage`, `editMessageText`, веб-поиск `openrouter:web_search` |
| `/tasks` — напоминания и фоновые задачи с кнопками отмены | inline-клавиатура, правка на месте |
| Фото, PDF, текстовые файлы, голосовые | `getFile`; фото — vision-модель (если выбранная не умеет, берётся первая из списка, которая умеет); PDF — `file-parser`; голос — `/audio/transcriptions` |
| Отдельные диалоги в топиках, названия ставит модель | `createForumTopic`, `editForumTopic`, `forum_topic_created.is_name_implicit` |
| `@бот вопрос` в любом чате | Inline mode: `answerInlineQuery` → `chosen_inline_result` → ответ дописывается в сообщение через `editMessageText(inline_message_id)` |
| Упоминание `@бот` в чате, где бота нет (с контекстом сообщения, на которое ответили) | Guest Mode: update `guest_message` → `answerGuestQuery` → правка `inline_message_id` |
| `/info` — справочник по всем возможностям: разделы, таблица форматов, сворачиваемые блоки и кнопки «Настройки / Часовой пояс / Мои задачи / Новый диалог» | `sendRichMessage` (Rich Markdown, `<details>`), inline-клавиатура |
| `/settings` — **🤖 Выбрать модель** (DeepSeek V4.1 Flash или GPT-6.2 Sol, с ценами; «← Назад») и **🕒 Указать часовой пояс**; если пояс задан — «Сбросить на UTC» | inline-клавиатура, правка сообщения на месте (`editMessageText`) |
| Часовой пояс — кнопка в `/settings`, `/timezone` или геолокация в чат. Геолокация не сохраняется: хранится только название пояса; по умолчанию UTC | `KeyboardButton.request_location`; пояс определяет open-meteo.com по координатам, округлённым до ~10 км; при ошибке — по долготе |

Агенту доступны инструменты: `set_reminder`, `start_watch`, `list_scheduled`,
`cancel_scheduled` и серверные `openrouter:web_search` / `openrouter:web_fetch`. Если
OpenRouter отклонит серверные инструменты, бот продолжит работу без них.

Диалоги — только в личном чате; inline и Guest Mode работают в любых чатах, но только для
владельца. Доступ есть у пользователей из `ALLOWED_USER_IDS`, а если список пуст — у
первого, кто напишет боту. История хранится в SQLite; в модель
уходят последние сообщения общим объёмом до `HISTORY_CHARS` символов.

## Установка на Ubuntu

```bash
curl -fsSL https://raw.githubusercontent.com/DaniilLuchkin/cumulative-agent/Alt/deploy/install.sh | sudo bash
```

Если на сервере стоял `cumulative-agent`, скрипт берёт из `/etc/cumulative-agent.env`
токен бота и ключ OpenRouter, а ID владельца — из его конфигурации или БД. Ничего
спрашивать не будет. Старый сервис останавливается и отключается: Telegram отдаёт
обновления только одному получателю. Его код, настройки и БД остаются на месте.
Вернуться: `sudo systemctl disable --now llm-bot && sudo systemctl enable --now cumulative-agent`.
На чистом сервере скрипт спросит токен и ключ. Повторный запуск обновляет код и
сохраняет настройки и историю.

* Настройки: `/etc/llm-bot.env`. После изменения: `sudo systemctl restart llm-bot`.
* Логи: `journalctl -u llm-bot -f`.
Настройки в @BotFather:

| Что | Где | Без этого |
|---|---|---|
| Режим топиков | Mini App → настройки бота | `/new` очищает контекст вместо нового топика; наблюдения пишут в текущий чат |
| Inline mode | `/setinline` (текст подсказки, например «вопрос…») | `@бот вопрос` не работает |
| Inline feedback | `/setinlinefeedback` → Enabled | ответ в inline-сообщении не появится (нет `chosen_inline_result`) |
| Guest Mode | Mini App → настройки бота → Guest Mode | бота нельзя позвать в чат, где его нет |

| Переменная | По умолчанию | |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | обязательна |
| `OPENROUTER_API_KEY` | — | обязательна |
| `LLM_MODEL` | модель из старого `cumulative-agent.yaml`, иначе `openrouter/auto` | любой id с openrouter.ai/models; фактическая модель пишется в лог |
| `LLM_MODELS` | `deepseek/deepseek-v4.1-flash`, `openai/gpt-6.2-sol` | список для `/settings` через запятую; `LLM_MODEL` добавляется всегда |
| `ALLOWED_USER_IDS` | пусто | ID через запятую |
| `UTILITY_MODEL` | `deepseek/deepseek-v4.1-flash` | названия топиков и вопросы под ответом |
| `TRANSCRIBE_MODEL` | `openai/whisper-large-v3-turbo` | голосовые |
| `TIMEZONE` | `UTC` | пояс, пока пользователь не задал свой |
| `SUGGESTIONS` | `on` | `off` — без вопросов под ответом (на 1 запрос меньше) |
| `SYSTEM_PROMPT` | короткий промпт ассистента | |
| `MAX_OUTPUT_TOKENS` | 4096 | |
| `HISTORY_CHARS` | 24000 | |
| `BOT_DB` | `data/bot.sqlite3` | |

## Автодеплой

Установщик ставит таймер `llm-bot-update`: раз в 5 минут сервер проверяет ветку `Alt`
(`git ls-remote`). Если появился новый коммит, установщик:

1. собирает новую версию и прогоняет тесты; если тесты не прошли, работающая версия остаётся;
2. переключает бота, сохранив предыдущую версию в `/opt/llm-bot/previous`;
3. ждёт в логе строку `started as @…` до 30 с; если её нет, возвращает предыдущую версию.

Коммит, который не прошёл тесты или не запустился, помечается в `/opt/llm-bot/FAILED` и
повторно не ставится — исправление выкатится следующим коммитом. Входящие подключения
к серверу и секреты в GitHub не нужны.

* История обновлений: `journalctl -u llm-bot-update`.
* Обновить сразу: `sudo systemctl start llm-bot-update`.
* Отключить: `sudo systemctl disable --now llm-bot-update.timer`.

## Локально

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
TELEGRAM_BOT_TOKEN=... OPENROUTER_API_KEY=... .venv/bin/llm-bot
.venv/bin/python -m unittest discover -s tests
```
