---
name: python-database-engineering
description: Design and implement reliable Python persistence with PostgreSQL, SQLite, SQLAlchemy, migrations, transactions, repositories, indexing, pooling, and data lifecycle.
compatibility: opencode
metadata:
  domain: python-engineering
  stage: implementation
---

# Python Database Engineering

## Design
- Model entities, ownership, lifecycle, constraints, uniqueness, retention, and query patterns before schema design.
- Use database constraints as the final integrity boundary; application validation alone is insufficient.
- Choose PostgreSQL for concurrent production services; SQLite is excellent for embedded/local/single-writer workloads when its concurrency limits fit.
- Use SQLAlchemy or the project's established data layer; avoid introducing a second ORM without a strong reason.
- Keep schema migrations versioned, reviewed, and reproducible.

## Transactions and consistency
- Define transaction boundaries around one business operation.
- Keep transactions short; never hold DB locks while waiting on remote APIs or browser navigation.
- Use unique constraints/upserts for idempotent ingestion.
- Handle serialization/deadlock conflicts with bounded, safe retries.
- Use optimistic locking or explicit row locks when concurrent updates can conflict.
- For external side effects, consider an outbox/inbox pattern; DB commit and remote delivery are not one atomic transaction.
- Never assume an in-memory lock protects multiple processes or replicas.

## Async and connection lifecycle
- Match sync/async driver to application model.
- Use bounded connection pools and explicit pool sizing.
- Create engine/session factories at application startup and dispose them at shutdown.
- Do not share a mutable ORM session across concurrent tasks.
- Scope sessions to request/job/use case; commit/rollback explicitly.
- Avoid blocking DB calls inside the event loop.

## Schema and query quality
- Add indexes based on measured query patterns and explain plans.
- Avoid N+1 loading; choose eager loading deliberately.
- Paginate large datasets; avoid loading unbounded tables into memory.
- Store timestamps in UTC and convert at presentation boundaries.
- Use explicit decimal types for money, not binary float.
- Add foreign keys, checks, not-null, and uniqueness constraints as required.
- Define backup/restore and retention policy for production data.

## Migrations
- Inspect generated migration before applying.
- Prefer backward-compatible expand/migrate/contract changes for zero-downtime deployment.
- Never delete or rewrite applied production migrations.
- Test upgrade from previous schema and rollback/forward recovery strategy.
- Back up before destructive changes; require explicit approval for production mutation.

## Testing
Use disposable test DBs or isolated schemas. Test transaction rollback, uniqueness races, concurrent writes, migration upgrade, query behavior, and repository contracts. Never run destructive tests against production.

## Completion
Document schema decisions, migration commands, pool settings, backup/restore assumptions, and operational failure handling.
