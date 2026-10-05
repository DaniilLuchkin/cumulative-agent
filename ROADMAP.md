# Roadmap

## M0 — architecture and repository
Status: completed locally.
Inspect existing environment; establish architecture, security boundaries,
module interfaces, configuration and deployment constraints. No reuse candidates.

## M1 — executable MVP (this delivery)
Status: implemented and verified locally; target-server/live-provider acceptance pending.
1. Versioned SQLite schema, repository, task/dependency/run history.
2. Task Manager, retries, blocked tasks, parent/subtask and attachment metadata.
3. Scheduler with strict user tier and documented score.
4. Provider protocol, OpenRouter adapter, configured model allowlist.
5. Rule router with usage statistics and explicit decision metadata.
6. Event-driven worker with atomic reservations, budgets, quotas, leases/heartbeat,
   bounded concurrency/steps/tokens/time, crash recovery.
7. Owner-only Telegram conversational input and management commands.
8. Background queue, bounded planner and local useful maintenance.
9. Resource registry, manual/provider quota tracking, server pressure checks.
10. Structured logs, metrics, protected REST API and minimal read-only dashboard.

Acceptance: local tasks work without credentials; mock LLM exercises limits and
routing; user tasks precede background; budget reservation survives races/restart;
unknown remote usage is conservatively charged; Telegram unauthorized chats cannot
enqueue; service can recover and shut down cleanly. Real API/server acceptance
requires operator credentials and target host.

## M2 — project maintenance
Status: all six steps implemented and verified locally; live target acceptance pending.
1. Bounded coding workflow: selected code → isolated snapshot/worktree → patch → sandboxed tests → human review.
2. Execution stages, durable artifacts, cancellation, explicit retry and task history in Mini App.
3. Local conversational intake, timezone-aware reminders and recurrence.
4. Timestamped OpenRouter resource observations and provider retry cooldowns.
5. Smoothed evaluations by handler/capabilities and visible routing explanations.
6. Measured local maintenance, improvement proposals and yielding background stages.

Acceptance uses fixture Git projects and mock providers. Production integration,
live provider billing and target-server sandbox support remain
separate operational checks. Main repositories remain read-only; merge/deploy stay
operator actions. Existing database/API data are migrated additively.

Project health reports, metadata-only file index and TODO reports are initial local
agents. Extend with opt-in tests/audit commands in sandbox, separate worktrees,
patch artifacts, review and explicit merge policy. No autonomous shell in MVP.

## Task interface — delivered local extension
Telegram Mini App with signed owner authentication, deadline/priority editing,
local-time Today/Overdue views, project/search/sort/pagination, manual reminder
completion/reopen and agent handoff. Desktop/mobile browser acceptance covers the
flow. Public HTTPS domain, target-server deployment and real Telegram client
acceptance pending. Due-date alerts and recurring reminders now use a durable outbox.

## M3 — knowledge and resource discovery
Extend metadata index to opt-in content parsing, local full-text/semantic search,
document relationships, duplicate hashes and staleness reports. Add public-source
discovery adapters with verification timestamps, source citations and conditions;
produce proposals only. No registration/data disclosure.

## M4 — measured improvement
Local usage-analysis agent initially reports costs/failures and creates deduplicated
improvement proposals. Extend cache, context reduction and per-task evaluations;
learned router only after representative human quality ratings and offline eval.

## M5 — operations and expansion
Verify Ubuntu ARM64 deployment, backup/restore drill and API-key spending caps;
TLS reverse proxy if remote dashboard is needed. Add official provider adapters,
PostgreSQL repository and richer web UI only when measured need appears.
Before PostgreSQL, move the remaining service SQL into typed repository query and
atomic claim methods and test both backends against the same behavior contract.
