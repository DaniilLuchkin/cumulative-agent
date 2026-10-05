# Последовательные улучшения системы

## 1. Работа с проектом: context → worktree → patch → tests → review

`coding` — ограниченный workflow с одним LLM completion, не произвольный shell-agent.
Система читает **committed HEAD**, выбирает явно разрешённые текстовые файлы,
создаёт небольшой snapshot без Git history/секретных файлов и отдельный worktree
в `/var/lib/cumulative-agent/artifacts/<run-id>/tree`. Исходный `.git`, основная
ветка, незакоммиченные изменения и production-файлы остаются без изменений.

Разрешите корень в `/etc/cumulative-agent.yaml` и перезапустите сервис:

```yaml
project_roots:
  - /srv/projects
test_runtime_roots:
  - /srv/agent-runtimes
limits:
  max_steps: 8
  max_tokens: 8192
  max_context_bytes: 5000
  max_snapshot_bytes: 20000000
  max_patch_bytes: 100000
  test_timeout_seconds: 60
```

Это фрагмент: остальные budgets/models из вашего config сохраняются. Для LLM
нужны enabled coding model и проверенные цены/квоты. Не расширяйте budgets без
собственного решения; API-key credit cap OpenRouter остаётся последним ограничителем.

Зарегистрируйте проект через `POST /api/projects` с авторизацией:

```json
{
  "id": "job-assistant",
  "path": "/srv/projects/job-assistant",
  "description": "Job assistant",
  "allowed_actions": ["read_code", "worktree", "code_patch", "run_tests", "todo_scan", "file_index"],
  "share_code_with_llm": true,
  "context_paths": ["src/**", "tests/**"],
  "editable_paths": ["src/**", "tests/**"],
  "test_commands": [["python3", "-m", "unittest", "discover", "-s", "tests"]],
  "maintenance_interval_hours": 24
}
```

`share_code_with_llm` по умолчанию false: его включение разрешает передачу выбранных
файлов настроенному LLM provider. Из индекса/TODO context автоматически не берётся.
В `context_paths` и task `code_paths` используются glob patterns. Context ограничен
12 файлами и byte cap; большие файлы исключаются целиком. Для точного анализа
укажите несколько конкретных `code_paths`. Dotfiles, symlinks, secret/credential
names, key/database suffixes исключаются. Inline secrets в обычном коде нельзя
надёжно распознать: проверяйте содержимое разрешённых файлов перед opt-in.

Создать coding task можно в Mini App, выбрав «Анализ проекта и patch», или через API:

```json
{
  "title": "Проверь расчёт сроков и предложи исправление",
  "handler": "coding",
  "project": "job-assistant",
  "code_paths": ["src/deadlines.py", "tests/test_deadlines.py"],
  "required_capabilities": ["coding"],
  "max_steps": 8,
  "max_tokens": 8192,
  "timeout_seconds": 180
}
```

Model возвращает проверяемый JSON со summary и полным содержимым изменённых файлов.
Неправильные/неразрешённые пути, дубликаты, выход за пределы проекта и oversized
patch отклоняются до записи. Удалений файлов и выполнения model-generated команд нет.
`changes.patch` создаётся до тестов; успешный patch может получить локальный commit.
В `max_steps` учитываются шесть основных действий coding workflow, каждая test
command и дополнительный commit. Если места для commit нет, проверенный patch
остаётся без commit; пропущенные из-за лимита tests явно помечаются `limited`.
После изменений создаётся отдельная blocked review/merge proposal. Approval только
фиксирует решение: **применение patch, merge и deployment выполняет оператор**.

## Тестовая среда

Установщик добавляет `bubblewrap`. Разрешённые оператором `test_commands` запускаются
без shell interpolation, с read-only worktree, отдельными mount/PID/network namespaces,
без API/Telegram/GitHub secrets, с CPU/memory/file/output/time bounds. Писать fixtures
можно в `/tmp` (128 MB tmpfs). Сеть отключена; установка dependencies во время теста
не поддерживается. Для pytest настройте cache в `/tmp` или выключите cache provider.

Для зависимостей заранее подготовьте доверенный venv вне production-проекта,
например `/srv/agent-runtimes/job-assistant`, и зарегистрируйте:

```json
{"test_runtime": "/srv/agent-runtimes/job-assistant"}
```

Это дополнительное поле полного project spec при `POST /api/projects`, не PATCH.
Runtime должен быть внутри `test_runtime_roots`; он монтируется read-only, его `bin`
добавляется в PATH. Доступ к project/runtime каталогам нужно выдать сервису явно.
Не храните credentials внутри runtime. DynamicUser/ProtectHome в systemd сохраняются.

Если binary/user namespaces/AppArmor profile не позволяют sandbox, тесты помечаются
`unavailable`, commit не выдаётся за проверенный, patch сохраняется для ручного review.
Автоматического unsandboxed fallback и отключения системной защиты нет. Для
враждебных проектов нужен отдельный VM executor: bubblewrap использует общий kernel.

## 2. Контроль выполнения

Карточка показывает этапы, расходы/резервы, tokens, routing explanation, результаты,
артефакты и историю. `cancel` останавливает активный coroutine/process group;
запрос с неизвестным remote usage остаётся учтённым по резерву. Отмена не делает
автоматический retry. `retry` создаёт новую явную задачу с parent link и новым
attempt budget; старые attempts/cost/history не обнуляются, daily cap общий.

## 3. Telegram и повторения

Пример: «Напомни завтра в 18:30, высокий приоритет». Простые даты/приоритеты
разбираются локально без LLM. Даты, повторения и coding selection показываются
перед созданием через owner-only inline confirmation; параметры можно исправить
новым сообщением или в Mini App после создания. Сложные/неверные даты не угадываются.

`/timezone Europe/Moscow` задаёт часовой пояс пользователя; default — config `timezone`.
Поддержаны сегодня/завтра, `YYYY-MM-DD`, `DD.MM[.YYYY]`, `в HH:MM`, каждый день/неделю/месяц.
`/cancel ID`, `/retry ID`; естественные «покажи задачи», «поставь агента на паузу».

В Mini App доступны daily/weekly/monthly recurrence и оповещение до deadline.
После completion создаётся только один следующий occurrence, просроченные периоды
пропускаются. Время сохраняется в локальной зоне, month-end anchor сохраняется;
DST gap следующего occurrence нормализуется вперёд. Для agent recurrence `start_after`
не позволяет немедленно выполнять бесконечную цепочку будущих occurrences.
Отмена прекращает создание следующего occurrence. Deadline остаётся сроком окончания.

Уведомления идут только текущим разрешённым владельцам. SQLite outbox имеет
dedup по task/deadline/kind, stale alerts после edit/completion отменяются, попытки
доставки ограничены тремя. При crash после отправки до фиксации возможен повтор;
доставка at-least-once, не exactly-once. Polling даёт задержку в десятки секунд.

## 4. Ресурсы

`resource_sync` читает `/api/v1/key` и публичный `/api/v1/models` OpenRouter. Авто-
сверка раз в `resource_sync_seconds` включается config `resource_auto_sync` только
при наличии ключа, как задача background queue. Кнопка «Сверить с OpenRouter API»
создаёт user task. Snapshot `/api/resources` сам сетевых запросов не делает.

Наблюдения имеют source/time; ошибка сохраняет последний успешный снимок.
API-key USD limit отличается от account balance и token quotas. Неизвестное
значение остаётся неизвестным. Локальные reservations и последующие calls
вычитаются из наблюдаемого key budget консервативно. Изменённая цена или исчезновение
модели приостанавливают её routing до проверки config; новые модели не включаются.
`429/503` с Retry-After создают общий persisted provider cooldown без обхода лимитов.

Новые публичные `:free` models дают до трёх deduplicated discovery proposals за
сверку в пределах backlog. Нулевая цена каталога не подтверждает отсутствие квот
или подходящее качество; условия проверяет оператор. Аккаунты не создаются.

## 5. Routing по результатам

Последние 100 похожих запусков (task type/handler/complexity/coding requirement)
влияют на reliability/latency; quality ratings смешиваются с priors, чтобы один
отзыв не полностью определял routing. Тесты дают предварительную оценку patch,
human rating имеет приоритет и отдельный source в `run_evaluations`. Недоступный
sandbox не считается отрицательной оценкой модели. Learned router пока не обучается;
rules/evidence достаточны для накопления проверяемых данных.

## 6. Полезный фон

Planner создаёт локальные проверки только для разрешённых project actions и
с maintenance cooldown/minimum value/backlog cap. `usage_review` создаёт inbox
proposals по дорогим моделям, recurring failures и слишком большому context;
config/бюджеты автоматически не меняются. Metrics считают patches/reviews/yields.

Coding workflow сохраняет checkpoint после patch и после tests. При появлении
eligible user task фон отдаёт слот на границе этапа. Resume не повторяет LLM call;
cost и total active-time budget сохраняются. File indexing сохраняет обработанные
пути и findings между уступками. Внутри отдельного API call или тестовой команды
работа завершается в пределах timeout; немедленный abort ради preemption не делается.

SQLite tables добавляются к существующей v1 database без удаления данных. Перед
обновлением сделайте online backup; старый executable не понимает новые statuses/
handlers, поэтому downgrade выполняйте с проверенным backup и остановленным сервисом.
