# Cumulative Agent — рабочий MVP

Пользовательские задачи имеют строгий приоритет; во время простоя выполняются
ограниченные полезные локальные задания. Python 3.12, FastAPI, SQLite, OpenRouter,
Telegram, без broker/Redis. Реализованы интерфейсы модулей, долговечная очередь,
зависимости/subtasks, retries, история/результаты/attachments, rule routing,
резервирование бюджета/квот, восстановление leases, Telegram Mini App для задач
и read-only dashboard для мониторинга.

Проект создан и проверен в `/workspace/cumulative-agent` на Debian x86_64.
Установка на Ubuntu 24.04 ARM64 требует доступа к целевому серверу. Без ключей
OpenRouter/Telegram выполнены mock-интеграции; живые платные запросы не делались.
Docker daemon недоступен в текущем sandbox; image/ARM64 build ещё нужно проверить.

## Установка на Oracle Ubuntu 24.04 одной командой

Репозиторий: [DaniilLuchkin/cumulative-agent](https://github.com/DaniilLuchkin/cumulative-agent).
Для новой установки запустите как обычный Ubuntu-пользователь с sudo:

```bash
sudo apt-get update && sudo apt-get install -y git && git clone https://github.com/DaniilLuchkin/cumulative-agent.git && sudo bash cumulative-agent/deploy/install-ubuntu.sh
```

Установщик спросит Telegram token (скрытый ввод), необязательный OpenRouter key,
публичный IPv4. Если задан IP, предложит получить бесплатный доверенный HTTPS
сертификат на этот IP: домен не требуется. Понадобятся контактный email и принятие
условий Let's Encrypt. В Oracle Security List/NSG и firewall сервера заранее
разрешите входящие TCP 80/443; скрипт не меняет firewall.

Сразу после запуска отправьте `/start` боту: **первый написавший в личный чат
становится единственным владельцем**. ID сохраняется в SQLite и переживает
перезапуски. Затем `/app` открывает `https://ВАШ_IP/app` внутри Telegram.
HTTPS IP требует Certbot ≥5.4 и short-lived certificate; настроено автоматическое
продление дважды в день. Если выдача сертификата не удалась, чат продолжает
работать, публичный Mini App остаётся выключенным; повторная настройка описана
в [OPERATIONS.md](docs/OPERATIONS.md).

Без IP работают Telegram-чат и локальный веб-интерфейс через SSH tunnel.
Платные вызовы по умолчанию выключены. Секреты находятся в
`/etc/cumulative-agent.env` (0600), модели/бюджеты — `/etc/cumulative-agent.yaml`,
БД — `/var/lib/cumulative-agent/agent.sqlite3`. Установщик только для новой
установки: существующие файлы сервиса не перезаписываются.

Для установки из архива скачайте готовый source archive
`cumulative-agent-improved.tar.gz`, перенесите на сервер и выполните:

```bash
tar -xzf cumulative-agent-improved.tar.gz && sudo bash cumulative-agent/deploy/install-ubuntu.sh
```

## Чат-режим (ветка Alt)

Обычный текст боту — диалог с LLM: стриминг ответа черновиками, кнопка «Стоп»,
отдельный контекст в каждом топике (`/new`), итоговый ответ в Rich Markdown.
Задачи создаются через `/task …` или сообщения «Напомни…». Нужна включённая модель
и ненулевой `daily_cost_limit`. Подробности и разбор документации Telegram —
[TELEGRAM_CHAT.md](docs/TELEGRAM_CHAT.md); обновление сервера —
[OPERATIONS.md](docs/OPERATIONS.md#переключение-на-ветку).

## Быстрый локальный запуск

```bash
cd /workspace/cumulative-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
cp config/default.yaml config/local.yaml
export AGENT_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export AGENT_CONFIG=config/local.yaml
.venv/bin/cumulative-agent serve
```

Интерфейс задач: `http://127.0.0.1:8000/app` (также `/`). Создавайте и редактируйте
задачи, дедлайны, приоритеты; используйте поиск, фильтры и сортировку. Старый
мониторинг доступен по `/dashboard`. Введите свой token на странице; он не сохраняется
в URL/localStorage. По умолчанию paid budget = 0, модели disabled; локальная очередь
работает без внешних credentials. Значение token не выводится в лог.

Вкладка «Ресурсы» показывает API/providers и возможности моделей, стоимость,
квоты и resets, rate/context limits, дневной/фоновый бюджеты и состояние сервера.
Неизвестные квоты обозначены явно. Реестр и локальный учёт дополняются отдельными
наблюдениями OpenRouter API с временем проверки. Изменившаяся цена блокирует
модель до проверки настроек; новые модели автоматически не включаются.
[Предпросмотр](docs/screenshots/resources-mobile.png).

Создать локальную пользовательскую задачу:

```bash
curl -sS http://127.0.0.1:8000/api/tasks \
  -H "Authorization: Bearer $AGENT_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"Проверь состояние системы","handler":"system_report","required_capabilities":[],"priority":10}'
```

Обычная задача `handler=llm` выполнится после включения модели и её provider.
Для обычного `handler=llm` один run — один completion с заданным description/memory.
`handler=coding` добавляет bounded workflow: разрешённые committed files, отдельный
snapshot/worktree, patch, sandboxed configured tests, artifacts и human review.
Доступ к коду и передача LLM включаются явно per-project. Model не получает shell.
Merge/deployment выполняет оператор. Настройка и ограничения:
[PROJECT_WORKFLOWS.md](docs/PROJECT_WORKFLOWS.md).

## Подключить OpenRouter и Telegram

1. Укажите актуальный model ID, capabilities, context и **проверенные текущие** цены
   в `config/local.yaml`, включите `enabled: true`. Цены в примере не live quote.
2. Задайте положительные `daily_cost_limit` и `background_cost_limit`, если разрешены
   платные вызовы; `task_cost_limit` ограничивает общую стоимость задачи с retries.
   USD input/output prices указаны за миллион токенов. Бесплатная модель должна
   иметь проверенные нулевые цены; она всё равно ограничена tokens/time/rate/quota.
3. Передайте через env `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_OWNER_IDS=123456789`. Либо оставьте IDs пустыми и укажите
   `TELEGRAM_CLAIM_FIRST_USER=true`: первый private text update привяжет владельца
   в SQLite. Установщик включает этот режим. Без IDs, сохранённого владельца и
   opt-in Telegram выключен. Явные IDs из config/env имеют приоритет над привязкой.
4. Настройте API-key credit limit в OpenRouter. Ошибка pricing провайдера не может
   быть абсолютно предотвращена клиентским бюджетом; overrun автоматически
   ставит весь scheduler на паузу для проверки оператором.
5. Перезапустите сервис после config changes. Не используйте несколько процессов
   ASGI и одновременно второй Telegram poller. SQLite atomic claims защищают
   задачу от двойного исполнения, но Telegram имеет одного polling владельца.

Обычный текст боту создаёт user task. `priority=10` задаёт приоритет; известное
название проекта связывает задачу с проектом. Команды:
`/app /tasks /status /resources /projects /background [on|off] /pause /resume /help`.
Работают только private owner chats. Результат приходит в тот же разрешённый чат;
при аварии между send и фиксацией notification возможен повтор сообщения.

Для кнопки Web App укажите `TELEGRAM_WEBAPP_URL=https://your-domain.com/app`
и отправьте `/app` боту. Telegram-вход работает по server-verified signed initData,
API token внутри Telegram вводить не нужно. Полная инструкция и screenshots:
[TELEGRAM_MINI_APP.md](docs/TELEGRAM_MINI_APP.md).

## Проекты и фоновые агенты

Добавьте абсолютные allowlisted `project_roots` в config и перезапустите сервис.
Зарегистрируйте проект через API:

```json
{
  "id": "job-assistant",
  "path": "/srv/projects/job-assistant",
  "description": "Personal job assistant",
  "technologies": ["Python"],
  "allowed_actions": ["file_index", "todo_scan"]
}
```

`POST /api/projects`. Planner создаёт metadata index/TODO tasks только для
разрешённых действий. Файлы не удаляются; symlinks, dotfiles, keys, databases,
credentials и dependency directories исключаются. В TODO report сохраняются
места, а не строки исходника. Индекс ограничен по числу файлов и обходу каталогов;
записи не исчезнувших файлов не удаляются, поэтому индекс — постепенно обновляемый
снимок с timestamps, не гарантия полного актуального inventory.

Локальные агенты: `system_report`, `usage_review`, `file_index`, `todo_scan`.
Planner имеет cooldown, dedup, minimum value и max backlog. Когда полезной работы
нет, процесс ждёт таймера/события — бессмысленное расходование ресурсов не считается
целевой автономностью. Resource sync наблюдает OpenRouter metadata и предлагает
новые catalog free models без их включения. Общего web-search discovery пока нет;
`POST /api/discoveries` сохраняет проверяемое предложение и создаёт resource task.

## Лимиты и восстановление

До remote call атомарно резервируются стоимость, provider balance, model token
quota и rate window. Conservative input bound — UTF-8 bytes плюс framing;
output cap задаётся запросом. Неизвестный usage после timeout/ошибки/сбоя учитывается
по полному резерву; метрика `usage_unknown_runs` это показывает. При известном
usage остаток возвращается. Дневной бюджет считается по UTC; активные reservations
учитываются и после полуночи. Исчерпанный task budget сохраняется между retries.

Quota unit — tokens, не процент подписки. Для процентных subscription quotas
нужен отдельный официальный adapter: их нельзя выдуманно переводить в tokens.
`quota_remaining: null` означает неизвестную внешнюю квоту, provider rate limit
всё равно действует. Reset пополняет квоту только с `quota_limit`, `resets_at`
и `reset_period_seconds`. Квота сохраняется при restart с неизменённым config;
изменение config/manual update — явное операторское изменение.

Задачи со статусом `blocked` и причиной `wait:*` переоцениваются после обновления
ресурсов; `manual:*` и approvals — только оператором. Failed dependencies не
считаются выполненными. `/pause` запрещает новые запуски, текущие завершаются;
shutdown отменяет calls и фиксирует неопределённый usage. После crash истёкшая
lease превращается в failed attempt с bounded backoff/retry.

Memory retrieval ограничен scope/count/bytes. `share_with_llm` по умолчанию false.
Все опасные предложения заблокированы; approval endpoint фиксирует рассмотрение,
но не запускает удаление, merge, deployment, платёж или аккаунт. В MVP нет такого
исполнителя. Main branch production-проектов не модифицируется.

## Проверки и deployment

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Тесты используют стандартный unittest, httpx mock и ASGI transport, без pytest,
Telegram credentials, сетевых/платных API запросов. Сценарии описаны в
[VALIDATION.md](docs/VALIDATION.md). Для Ubuntu ARM64 смотрите
[OPERATIONS.md](docs/OPERATIONS.md), systemd unit и Docker Compose.

Отдельная проверка реального HTTP/CLI/backup/restart (требует loopback sockets):

```bash
.venv/bin/python tests/smoke_http.py
```

[ARCHITECTURE.md](ARCHITECTURE.md) фиксирует решения и safety boundaries;
[ROADMAP.md](ROADMAP.md) отделяет MVP от следующих milestones;
[API.md](docs/API.md) содержит endpoints и примеры управления.
