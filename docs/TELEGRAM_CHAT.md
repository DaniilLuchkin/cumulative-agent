# Telegram: чат-режим с LLM

Ветка `Alt`. Бот отвечает как AI-ассистент по рекомендациям Telegram для AI-ботов
и сохраняет прежнее поведение задач: очередь, бюджеты, роутинг моделей и `/app`.

## Что рекомендует Telegram (Bot API 9.3 – 10.4)

Источники: [Bot Features → Streaming Replies, Topics in Private Chats, AI Agents](https://core.telegram.org/bots/features),
[Bot API](https://core.telegram.org/bots/api), [changelog](https://core.telegram.org/bots/api-changelog). Прочитано 2026-11-21.

| Возможность | Версия | Методы / поля | Зачем AI-боту |
|---|---|---|---|
| Стриминг ответа | 9.3 (31.12.2025), всем ботам с 9.5 | `sendMessageDraft(chat_id, draft_id, text, message_thread_id?)` | Ответ появляется по мере генерации; черновик живёт около 30 с; итог нужно сохранить через `sendMessage` |
| «Thinking…» | 9.5 | `sendMessageDraft` с пустым `text` | Плейсхолдер, пока модель не выдала первый токен |
| Кнопка «Стоп» | 10.3 (24.08.2026) | `can_stop`, `keep_on_stop`; update `stopped_message_generation` (`chat`, `message_thread_id`, `draft_id`) | Пользователь останавливает генерацию, бот отменяет вызов модели |
| Rich Messages | 10.1 (11.06.2026) | `sendRichMessage`, `sendRichMessageDraft`, `InputRichMessage{markdown \| html \| blocks}` | Markdown в стиле GFM: заголовки, таблицы, код, формулы; до 32768 символов против 4096 у обычного сообщения |
| Топики в личном чате | 9.3; создание ботом с 9.4 | `User.has_topics_enabled`, `createForumTopic`, `editForumTopic`, `message_thread_id`, `is_topic_message` | Отдельный диалог и контекст на каждую тему; включается в @BotFather |
| Ссылки на топик | 10.4 (09.11.2026) | `https://t.me/<bot>/<message_thread_id>` | Ссылка на конкретный диалог |
| Меню команд | — | `setMyCommands` со scope `all_private_chats` | Подсказки команд в поле ввода |
| Индикатор набора | — | `sendChatAction(action=typing)` | Запасной вариант, если черновики недоступны |

Из раздела AI Agents: Bot API отвечает за обмен сообщениями и отображение, а модель,
память и логика остаются на стороне бота. Для AI-интерфейсов рекомендуются три
сочетаемых приёма: топики, стриминг и Rich Messages для итогового ответа.

## Как это применено

```
сообщение → задача handler=chat (priority 100, max_attempts 1)
          → sendMessageDraft("")                 «Thinking…», can_stop
          → OpenRouter stream=true → on_delta → sendRichMessageDraft(markdown), не чаще 0,8 с
          → sendRichMessage(markdown, reply_parameters)    итоговый ответ, сохраняется в чате
Стоп      → stopped_message_generation → cancel задачи → частичный ответ + «Генерация остановлена»
```

* **Контекст:** таблица `chat_messages` с ключом владелец + топик. В запрос
  уходят последние сообщения в пределах `chat_history_bytes` (по умолчанию 24 КБ).
* **Топики:** `/new [название]` создаёт топик через `createForumTopic`. Без названия
  топик получает имя по первому вопросу (`editForumTopic`). Если топики выключены,
  `/new` и `/reset` очищают контекст текущего чата.
* **Ресурсы:** каждый ход — обычная пользовательская задача. Действуют дневной
  бюджет, лимит на задачу, квоты и rate limits; учёт идёт в `task_runs`/`usage`.
  Если модели нет или бюджет исчерпан, бот сообщает причину через 4 с и не держит
  ход в очереди.
* **Задачи:** сообщения, начинающиеся с «Напомни…», «Напоминание», «Remind» или
  «Задача:», а также `/task текст` идут в прежний разбор задач. Ходы чата не
  попадают в `/tasks` и в списки Mini App.
* **Запасные варианты:** если Rich Messages отклонены, бот переходит на
  `sendMessageDraft`/`sendMessage` частями по 4000 символов; если не работают
  черновики — на `sendChatAction typing`.
* **Перезапуск:** ответ, готовый к моменту рестарта, доставляется из
  `notify_results` без черновиков.
* Без включённой модели бот работает как раньше (текст → задача).

## Настройка

1. В @BotFather (Mini App → настройки бота) включите режим топиков, если нужны
   отдельные диалоги. Бот определяет его через `getMe.has_topics_enabled` при старте.
2. Включите модель и бюджет в `/etc/cumulative-agent.yaml`:

   ```yaml
   telegram_mode: chat          # chat | tasks (прежнее поведение)
   chat_history_bytes: 24000
   chat_max_tokens: 4096
   limits:
     daily_cost_limit: 1.0      # 0 запрещает платные вызовы
     task_cost_limit: 0.10
   models:
     - id: <модель OpenRouter>
       provider: openrouter
       enabled: true
       input_price: ...         # проверьте текущие цены
       output_price: ...
   ```

3. В `/etc/cumulative-agent.env` нужен `OPENROUTER_API_KEY`. Режим также задаётся
   через `TELEGRAM_MODE=chat|tasks`.

Обновление сервера на ветку `Alt`: [OPERATIONS.md](OPERATIONS.md#переключение-на-ветку).
