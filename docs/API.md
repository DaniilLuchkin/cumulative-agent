# Protected REST API

Все `/api/*`: `Authorization: Bearer <AGENT_API_TOKEN>` для браузера или
`Authorization: Telegram <raw-initData>` для Mini App. Bearer token минимум
24 символа; Telegram проверяется HMAC bot token + auth_date + owner allowlist.
`/health`, оболочки `/app` и `/dashboard` публичны; данных без auth нет.
API keys не возвращаются. Max JSON body 256 KB.

| Method | Path | Purpose |
|---|---|---|
| POST/GET | /api/tasks | Create/list tasks (limit <=1000, optional status) |
| GET | /api/auth/me | Verified auth method and Telegram owner ID |
| GET | /api/task-list | Paginated filtered/sorted list with counts for Mini App |
| GET | /api/tasks/{id} | Task, runs/results/decision metadata, attachments, subtasks |
| PATCH | /api/tasks/{id} | title, description, priority, deadline, project, recurrence, task_timezone, notify_before_minutes, expected_updated_at |
| POST | /api/tasks/{id}/control | `action: block|ready|complete|reopen|cancel|retry`, reason |
| GET | /api/artifacts/{id} | Authenticated patch/report download |
| PATCH | /api/tasks/{id}/priority | `priority: integer` |
| POST | /api/tasks/{id}/attachments | `name, uri` metadata; no fetch or filesystem read |
| GET | /api/status | Pause/background state, limits, metrics |
| POST | /api/pause | `enabled: true` pauses new starts |
| POST | /api/background | `enabled: false` disables background starts |
| POST/GET | /api/projects | Register/list allowlisted project paths/actions |
| GET | /api/models | Configured registry, quota state |
| GET | /api/resources | Models/providers/local health, adapter availability, budgets/execution limits, credential readiness booleans |
| POST | /api/resources/quota/{model_id} | `remaining` tokens, timezone-aware `resets_at` |
| POST | /api/resources/sync | Queue one bounded read-only OpenRouter observation task |
| GET | /api/metrics | Completed/failed/tokens/cost/latency/background/discoveries |
| GET | /api/usage | Last 500 run measurements; unknown usage explicitly flagged |
| POST | /api/runs/{id}/rating | Human `quality_score: 0..1` for future routing |
| GET | /api/events | Last 100 structured events |
| POST | /api/memory | scope global/project/task, scope_id, content, tags, share_with_llm |
| POST/GET | /api/discoveries | Public-source resource proposals; creates resource task |
| GET | /api/approvals | Blocked dangerous-action proposals |
| POST | /api/approvals/{id} | `enabled` records approve/reject; never executes action |

Example task with dependencies/subtask:

```json
{
  "title": "Review job-assistant architecture",
  "description": "Context supplied here or explicitly shared memory",
  "type": "user",
  "priority": 10,
  "project": "job-assistant",
  "dependencies": ["existing-task-uuid"],
  "parent_task": "existing-parent-uuid",
  "required_capabilities": ["coding"],
  "max_tokens": 4096,
  "max_cost": 0.02,
  "max_attempts": 2,
  "timeout_seconds": 60
}
```

Task dependencies immutable once created: this prevents future-reference cycles.
`status: inbox` при создании оставляет задачу на рассмотрение; default `ready`.
Parent is organizational; dependency order must be explicit. Manually failed
tasks may become ready only with remaining attempts. Task `max_cost` spans all
attempts; global task cap further clamps it. Running/done tasks cannot be blocked
or retried in place. Done task manually completed by the operator may be reopened
to its previous status; automatically completed tasks cannot. Manual completion
creates no agent run. `idempotency_key` prevents duplicate external intake.

`PATCH` accepts explicit `deadline: null` to clear the date. Deadlines must include
timezone; omitted fields remain unchanged. Title/description/project cannot change
while running or after completion; priority/deadline can. `expected_updated_at`
detects concurrent edits (409), and edits create a system event with field names.

`GET /api/task-list`: `status=active|all|inbox|ready|running|blocked|done|failed|cancelled`,
`kind=user|background|all`, `project`, `search`, `sort=priority|deadline|created|updated`,
`due=all|today|overdue`, `offset`, `limit` (1..200). `day_start/day_end` are timezone-
aware bounds of the client's local day (20..28 hours to allow DST); default is UTC.
Response: `items,total,offset,limit,next_offset,summary`. Search escapes SQL wildcard
characters and folds Unicode case; deadlines sort by absolute instant, null last.

`GET /api/resources` retains existing `models/providers/local/credential_readiness`
fields and adds `providers[].adapter_available`, `budgets.daily/background`
(`used,reserved,limit,remaining`) and `execution_limits`
(`task_cost_limit,max_tokens,timeout_seconds,max_concurrent_agents`). Remaining
budget is clamped at zero; usage includes current UTC day and every active
reservation across midnight, consistent with worker admission. Unknown quotas
and balances remain null; API keys are never included. Snapshot is read-only,
does not fetch upstream balances or validate configured credentials remotely.

Workflow extensions: detail includes `stages`, `artifacts`, structured `events`
and `evaluations`. Project registration accepts explicit `context_paths`,
`editable_paths`, `share_code_with_llm`, `test_commands`, optional allowlisted
`test_runtime`. New handlers: `coding`, `project_tests`, `resource_sync`.
Task create/patch support recurrence, task_timezone, notify_before_minutes;
create also accepts code_paths/start_after. Retry returns a NEW linked task,
idempotent for the same source version; cancellation settles an active run.
Resource snapshots add timestamped `observations`; they never enable models.
See [PROJECT_WORKFLOWS.md](PROJECT_WORKFLOWS.md) for contracts and examples.
