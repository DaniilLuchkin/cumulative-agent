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
| Под ответом: 3 вопроса для продолжения (переписать ответ — исправить свой вопрос) | inline-клавиатура, `editMessageReplyMarkup`; вопросы предлагает `UTILITY_MODEL` |
| Исправили своё сообщение — ответ переписывается | update `edited_message`, `deleteMessage` старого ответа |
| «Напомни завтра в 9…» → 🫡, в срок приходит ⏰ с кнопками +15 мин / +1 час / Завтра | tool calling, бот пишет первым; время — `tg://time` (показывается в часовом поясе читателя) |
| «Следи за …» → отдельный топик 👁 с закреплённым статусом и кнопками «Проверить сейчас / Остановить»; отчёты — в топик, только когда есть новое | `createForumTopic`, `pinChatMessage`, `editMessageText`, веб-поиск `openrouter:web_search` |
| `/tasks` — напоминания и фоновые задачи с кнопками отмены | inline-клавиатура, правка на месте |
| Фото, PDF, текстовые файлы, голосовые | `getFile`; фото — vision-модель (если выбранная не умеет, берётся первая из списка, которая умеет); PDF — `file-parser`; голос — `/audio/transcriptions` |
| Отдельные диалоги в топиках, названия ставит модель | `createForumTopic`, `editForumTopic`, `forum_topic_created.is_name_implicit` |
| Виджет в сообщении: калькулятор, график, таймер, флеш-карточки, мини-игра — модель решает сама | инструмент `show_widget` → отдельный вызов модели с [официальными правилами Telegram для LLM](https://core.telegram.org/bots/widgets.md#writing-widgets-with-an-llm) пишет HTML; размер проверяется zlib (≤ 4096 байт), при превышении или отказе Telegram — переписывание; `sendWidget`. Нажатия с `ask_bot` → `widget_query` → `answerWidgetQuery` и ответ агента в чате. **С 13.11.2026 Telegram отклоняет виджеты обычных ботов** (`INPUT_MEDIA_WIDGET_NOT_ALLOWED`, [tdlib/telegram-bot-api#947](https://github.com/tdlib/telegram-bot-api/issues/947)): бот запоминает это, убирает инструмент и раз в 6 часов проверяет крошечным виджетом без вызова модели |
| Опросы и викторины | `create_poll` → `sendPoll` (`type: quiz`, `correct_option_ids`, `explanation`) |
| Картинки | `generate_image` → OpenRouter Image API `POST /api/v1/images` (`IMAGE_MODEL`, около $0,05 за картинку) → `sendPhoto` |
| Файлы: CSV, JSON, Markdown, код, .ics, .vcf, SVG | `send_file` → `sendDocument` (multipart) |
| Голосовой ответ на голосовое (или по просьбе «ответь голосом») + тот же ответ текстом | OpenRouter `/audio/speech` (`TTS_MODEL`, MP3, голос по языку текста) → `sendVoice`; если озвучка не удалась — только текст |
| Rich-блоки: формулы, карты, коллажи, сноски, `<details>`, `<aside>`, task lists | системный промпт описывает возможности Rich Markdown |
| Советы сильной модели: быстрая модель на трудном вопросе консультируется с `ADVISOR_MODEL` | `openrouter:advisor` (предлагается, только если текущая модель — не сам советник) |
| Делегирование рутины дешёвой модели с веб-поиском | `openrouter:subagent` с `WORKER_MODEL` (только когда текущая модель — не сам исполнитель) |
| «Спроси несколько моделей» — панель моделей и сводный анализ | `openrouter:fusion` с `FUSION_MODELS`; модель вызывает его только по явной просьбе |
| Рассуждения: авто / быстро / глубоко; при «глубоко» ход рассуждений — в `<details>` под ответом (в историю не попадает) | `reasoning.effort`; потоковые `delta.reasoning`, в черновике — «Размышляю…» |
| Резервные модели: если выбранная недоступна, отвечает другая из списка | параметр `models` в Chat Completions |
| Поиск по всем прошлым разговорам по смыслу, со ссылкой на топик | `search_history` → эмбеддинги `EMBEDDING_MODEL` (512 измерений) в SQLite, косинусное сходство; переписка индексируется в фоне |
| `/usage` — расходы за день, 7 и 30 дней, по назначению и моделям, остаток ключа | `usage.cost` из каждого ответа OpenRouter (включая поиск и серверные инструменты), `GET /api/v1/key`; озвучка — оценка по цене за символ |
| Группы: `/ask вопрос`, ответ на сообщение бота, `/ask` ответом на чужое сообщение — с ним как контекстом | privacy mode остаётся включённым: Telegram доставляет боту только команды для него и ответы на его сообщения; вместо черновиков — `sendChatAction typing`; справка и ошибки — эфемерные сообщения (`ephemeral_message_parameters.receiver_user_id`); отвечает только владельцу; поиск по истории — только по этой группе; группа не может «присвоить» бота |
| Секретарь (Telegram Business, нужен Premium): черновики ответов на входящие в ваших личных чатах; выключен до `/secretary on` | updates `business_connection` / `business_message` / `deleted_business_messages`; входящие за 15 с собираются в один черновик; карточка с кнопками; отправка `sendMessage(business_connection_id)` только по нажатию; черновик пишется без инструментов и веба; подключение от чужого аккаунта игнорируется |
| Боты-специалисты (заготовка, выключена до `/bots on`): `/bots new <роль>` создаёт бота через Managed Bots, основной бот запускает его в том же процессе со своей ролью и БД; основной спрашивает специалиста инструментом `ask_bot` | кнопка `request_managed_bot` → update `managed_bot` → `getManagedBotToken`; Bot-to-Bot через `sendMessage` боту; `setMySettings(can_manage_bots, can_communicate_with_bots)`; защита от циклов: специалист отвечает только менеджеру, без инструментов, ≥ 3 с между сообщениями, ≤ 30 в час, дубли отбрасываются, менеджер принимает только ожидаемые ответы. Токены специалистов хранятся в БД бота |
| `@бот вопрос` в любом чате | Inline mode: `answerInlineQuery` → `chosen_inline_result` → ответ дописывается в сообщение через `editMessageText(inline_message_id)` |
| Упоминание `@бот` в чате, где бота нет (с контекстом сообщения, на которое ответили) | Guest Mode: update `guest_message` → `answerGuestQuery` → правка `inline_message_id` |
| `/info` — справочник по всем возможностям: разделы, таблица форматов, сворачиваемые блоки и кнопки «Настройки / Локация / Мои задачи / Новый диалог» | `sendRichMessage` (Rich Markdown, `<details>`), inline-клавиатура |
| `/settings` — две кнопки: **🤖 Set model** (DeepSeek V4.1 Flash или GPT-6.2 Sol, с ценами; «← Назад») и **📍 Set timezone/location** | inline-клавиатура, правка сообщения на месте (`editMessageText`) |
| Локация — кнопка в `/settings`, `/location` (или `/timezone`) или просто геолокация в чат. Координаты сохраняются (`location:<chat>` в SQLite: широта, долгота, пояс, время), по ним ставится часовой пояс; без локации — UTC | `KeyboardButton.request_location`; пояс определяет open-meteo.com по координатам, округлённым до ~10 км; при ошибке — по долготе |

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
Настройки бота бот выставляет сам при запуске через `setMySettings` (Bot API 10.5): inline-режим
с подсказкой «вопрос…», inline feedback, Guest Mode, топики в личке с созданием топиков пользователем
и боковой панелью. В @BotFather ничего включать не нужно; `BOT_SELF_SETTINGS=off` отключает это.
Если Telegram откажет, бот продолжит работу, а в логе будет `setMySettings failed`.

Deep link `https://t.me/<бот>?start=q_<base64url вопроса>` открывает бота и сразу задаёт вопрос.

| Переменная | По умолчанию | |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | обязательна |
| `OPENROUTER_API_KEY` | — | обязательна |
| `LLM_MODEL` | модель из старого `cumulative-agent.yaml`, иначе `openrouter/auto` | любой id с openrouter.ai/models; фактическая модель пишется в лог |
| `LLM_MODELS` | `deepseek/deepseek-v4.1-flash`, `openai/gpt-6.2-sol` | список для `/settings` через запятую; `LLM_MODEL` добавляется всегда |
| `ALLOWED_USER_IDS` | пусто | ID через запятую |
| `UTILITY_MODEL` | `deepseek/deepseek-v4.1-flash` | названия топиков и вопросы под ответом |
| `TRANSCRIBE_MODEL` | `openai/whisper-large-v3-turbo` | голосовые |
| `TIMEZONE` | `UTC` | пояс, пока локация не указана |
| `IMAGE_MODEL` | `openai/gpt-image-2.6-flare` | картинки |
| `TTS_MODEL` | `microsoft/mai-voice-2.1-flash` | голосовые ответы, $15 за 1 млн символов |
| `TTS_VOICE_RU` / `TTS_VOICE_EN` | `ru-RU-Lev:…` / `en-US-Grant:…` | голос выбирается по языку ответа |
| `ADVISOR_MODEL` | `openai/gpt-6.2-sol` | советник; пусто — выключен |
| `WORKER_MODEL` | `deepseek/deepseek-v4.1-flash` | исполнитель подзадач; пусто — выключен |
| `FUSION_MODELS` | DeepSeek V4.1 Flash, GPT-6.2 Sol, Gemini 4 Hydrogen | панель для Fusion; пусто — выключен |
| `EMBEDDING_MODEL` | `openai/text-embedding-3-small` | поиск по истории, $0,02 за 1 млн токенов |
| `FALLBACKS` | `on` | `off` — без резервных моделей |
| `WEB_PORT` | `8000` | порт панели на 127.0.0.1; `0` — выключить |
| `MINIAPP_URL` | из старого `TELEGRAM_WEBAPP_URL` | публичный HTTPS-адрес панели |
| `SUGGESTIONS` | `on` | `off` — без вопросов под ответом (на 1 запрос меньше) |
| `SYSTEM_PROMPT` | короткий промпт ассистента | |
| `MAX_OUTPUT_TOKENS` | 16000 | ответ вместе с рассуждениями; в режиме «глубоко» — вдвое больше; при нехватке — повтор с короткими рассуждениями | |
| `HISTORY_CHARS` | 24000 | |
| `BOT_DB` | `data/bot.sqlite3` | |

## Mini App (панель)

Бот сам отдаёт панель на `127.0.0.1:8000` (`WEB_PORT`; `0` — выключить): модель и рассуждения,
напоминания и фоновые задачи с отменой, расходы, поиск по памяти. Каждый запрос проверяется по
подписи Telegram `initData` (HMAC от токена бота, не старше 24 часов) и только для владельца.

Нужен публичный HTTPS. Панель слушает тот же порт 8000, что и старый cumulative-agent, поэтому его HTTPS подходит как есть.
Установщик находит любой nginx-сайт с `listen 443`, проксирующий на `127.0.0.1:8000`, у которого есть
сертификат, берёт адрес из `server_name`, записывает `MINIAPP_URL=https://<адрес>/app` и при
необходимости запускает nginx; бот ставит кнопку меню «Панель». Если HTTPS нет, панель выключена; включить её — `sudo bash deploy/enable-ip-https.sh <IP>`
из ветки `main`, затем `MINIAPP_URL=https://<IP>/app` в `/etc/llm-bot.env`.

Альтернатива на будущее — Telegram Serverless (хостинг Mini App у Telegram), но тогда серверную
часть пришлось бы переписать на JavaScript: данные бота живут в SQLite на этом сервере.

## Автодеплой

Установщик ставит таймер `llm-bot-update`: раз в 5 минут сервер проверяет ветку `Alt`
(`git ls-remote`). Если появился новый коммит, установщик:

1. собирает новую версию и прогоняет тесты от пользователя `nobody` (код ветки не исполняется от root); если тесты не прошли, работающая версия остаётся;
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
