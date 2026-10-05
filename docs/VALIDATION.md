# Validation

Среда: Debian 13 x86_64, Python 3.12.14. Версии Python dependencies зафиксированы
в `requirements.lock`; внешние платные сервисы не вызывались.

Автоматические проверки (`python -m unittest discover -s tests -v`):
**87 tests passed**, включая Mini App API/auth/resources/owner/deploy tests, в `.venv` с dependency pins.

* строгий user tier, background claim уступает новой user task;
* immutable dependencies, parent/subtask, idempotent intake;
* inbox остаётся без исполнения до явного ready;
* результат, normalized usage/cost, decision metadata, quota refund;
* unknown remote usage -> conservative charge, bounded retry/backoff;
* timeout и reservation, task budget across attempts;
* atomic daily reservations, rate/quota/context/capability eligibility;
* exhausted paid user budget допускает локальную бесплатную background task;
* provider balance, shared rate, quota reset с in-flight reservations;
* provider price overrun сохраняет истинный cost и останавливает scheduler;
* deadline scoring и фиксация in-flight run при shutdown;
* stale lease recovery, идемпотентное завершение run;
* pause, background toggle и manual block;
* approval не выполняет dangerous action;
* planner dedup/cooldown/backlog;
* scoped opt-in bounded memory;
* local file index пропускает secrets/symlinks и не меняет файлы;
* project path allowlist;
* Telegram private-owner ACL и update dedup;
* OpenRouter request limits/pricing parameters и sanitized invalid responses;
* human quality feedback влияет на routing;
* event-driven worker просыпается из другого потока;
* SQLite race двух connections и сохранение quota при restart;
* HTTP auth, tasks/priority/control, request size, timezone validation;
* HTTP enqueue приводит к реальному worker completion.

Шесть последовательных улучшений дополнительно проверены на fixture Git repository
и mock providers: snapshot/worktree, patch и review proposal без изменения исходного
main; отказ от path traversal/secret paths и LLM-вызова без opt-in; недоступный sandbox
не запускает команды и не считается успешным тестом; cancellation с сохранением
conservative billing; отдельная idempotent retry task; даты/timezone/DST/месячные
повторения, подтверждение ввода и outbox dedup; timestamped API observations,
сохранение прежних данных при ошибке, блокировка изменённой цены и Retry-After cooldown.
Фоновый coding workflow уступает пользовательской задаче и продолжает с checkpoint
без второго LLM-вызова; тестовая оценка привязана к исходному model run.
Artifact downloads проверены на owner auth, запрет внешнего path/symlink и отсутствие
серверных путей в detail API.

Mini App extension: проверены официальный bot-token HMAC с optional signature,
подделка/неверный owner/истёкший auth_date, защищённые API, безопасное Telegram
framing/CSP, timezone deadlines, explicit clearing, optimistic edit conflict,
изменение priority/deadline выполняемой задачи без подмены её содержимого,
manual complete/reopen без fake agent run, approval restrictions, пагинация,
Unicode search, sort и local-day filters, Bot API Web App/menu button.

`node tests/test_task_utils.mjs`: local timezone, DST, удаление дедлайна,
сохранение исходной точности, доступные действия, фильтры и приоритеты.
`PYTHONPATH=src python3 tests/smoke_browser.py`: Playwright Chromium, реальный HTTP,
desktop/mobile login, synthetic signed Telegram bridge, создание/изменение/
complete/reopen, поиск, безопасный текст, сохранение в БД с Moscow -> UTC conversion,
native BackButton, dark theme и ширины 390/320 px. Также проверены recurrence,
история, cancel/retry и постановка resource sync в очередь. Screenshots в `docs/screenshots/`.
Отложенный ответ save не закрывает новый редактор и не уничтожает новый draft.
Resources extension: проверены auth/secrets boundaries, unknown quotas/null,
adapter availability, бюджеты с in-flight reservations across midnight и после
settlement. Chromium проверяет resource navigation, balances/model limits,
unknown quota/unsupported adapter, refresh с сохранением раскрытых details,
отсутствие keys в DOM и отсутствие overflow на mobile 390/320 в light/dark theme.
Обновлены task screenshots и добавлены `resources-*.png` со synthetic fixtures.

Owner bootstrap: первый private text update, opt-in по умолчанию false,
запрет group/bot/пустого текста/неверного ID; unauthorized commands/send закрыты.
Привязка сохраняется при reopening SQLite и выключенном bootstrap; explicit
owner IDs имеют приоритет. Две connections могут назначить ровно одного owner.
Signed HTTP login до привязки отклоняется, после неё принимает только этого owner.
Resource readiness учитывает persisted owner; env toggle принимает только boolean.

Deploy renderer: reject private/reserved IP и config injection, bootstrap HTTP
не публикует API, TLS сохраняет ACME path/Authorization/Telegram framing.
Оба shell script проходят `bash -n`; timer calendar проверен systemd-analyze.
Certbot 5.8 установлен в отдельный dev venv и проверены флаги IP/profile — без
запроса сертификата или регистрации реального ACME account.
Все pinned dependencies успешно скачаны как wheels для Python 3.12 ARM64
(`manylinux2014_aarch64`), включая Pydantic Core и PyYAML. Это проверка наличия
пакетов, не фактический запуск на ARM64 сервере.
Nginx 1.26.3 `-t` проверил оба generated IP virtual hosts: challenge-only bootstrap
и TLS с fixture certificate; paths/log/temp dirs адаптированы к `/tmp`.

Tests используют HTTPX ASGITransport и явный lifespan. Реальные сетевые запросы
проверены отдельно через Uvicorn (`tests/smoke_http.py`): completion двух tasks,
user priority, online backup + integrity check, restart persistence, отсутствие
секрета в logs и graceful shutdown. API handlers async; SQLite operations —
короткие synchronous transactions.

Дополнительно: dependencies установлены из `requirements.lock` в отдельный venv,
Compose config прошёл `docker compose config --quiet`, структура systemd unit
проверена `systemd-analyze verify` с локально адаптированным ExecStart/WorkingDirectory.
Собран Python wheel; проверено включение Mini App HTML/CSS/JS/helpers и auth module.

В интеграционных тестах coding использует mock sandbox; отдельные проверки
подтверждают отказ без bubblewrap/при запрете запуска. Реальные namespaces и AppArmor
на Oracle Ubuntu должны быть проверены отдельно; Docker privilege не повышается.

Не проверено: реальный Telegram token/polling, OpenRouter billing/модель,
Ubuntu ARM64 deployment и Docker build (daemon socket недоступен sandbox).
Предполагаемый target access и secrets необходимо предоставить через SSH/env.
