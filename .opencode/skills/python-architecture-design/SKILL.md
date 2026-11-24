---
name: python-architecture-design
description: Design maintainable Python application architecture, module boundaries, interfaces, data models, concurrency, and deployment topology for bots, parsers, and backend services.
compatibility: opencode
metadata:
  domain: python-engineering
  stage: design
---

# Python Architecture Design

## Principles
Prefer the simplest architecture that satisfies explicit requirements. Optimize for cohesion, explicit dependencies, testability, observability, and change isolation. Avoid speculative microservices, generic frameworks, and needless abstractions.

## Initial inspection
Read the existing structure and conventions before proposing changes. Identify entry points, dependency direction, state ownership, I/O boundaries, configuration, persistence, and deployment model.

## Design sequence
1. Define bounded contexts and responsibilities.
2. Draw a component/data-flow map in Mermaid or concise text.
3. Define ports/interfaces at volatile boundaries: platform SDK, HTTP transport, repositories, clock, queue, and external APIs.
4. Decide synchronous vs asynchronous execution from actual concurrency and I/O requirements.
5. Define data contracts, validation, serialization, and error taxonomy.
6. Define state ownership, transaction boundaries, idempotency, and consistency.
7. Specify lifecycle: startup, readiness, shutdown, cancellation, cleanup, and recovery.
8. Record significant decisions in short ADRs (`docs/adr/NNNN-title.md`).

## Recommended modular layout
Adapt to project size; do not force every folder into a small script.

```text
src/package_name/
  __init__.py
  main.py                 # composition root / entry point
  config.py               # validated settings
  domain/                 # business rules and domain types
  application/             # use cases, orchestration
  ports/                   # protocols/interfaces if justified
  adapters/
    inbound/               # bot handlers, API routes, CLI
    outbound/              # HTTP, DB, queue, platform clients
  infrastructure/          # concrete wiring and persistence
  observability/           # logging, metrics, tracing
tests/
  unit/
  integration/
  contract/
  e2e/
docs/
```

Keep business logic independent of framework objects. Handlers/controllers should validate and translate input, call a use case, and translate results. Do not bury business rules in route handlers, bot callbacks, ORM models, or scraper selectors.

## Architecture choices
- A modular monolith is the default for most bots and scrapers. Split deployable services only for independently scaled, secured, owned, or released components.
- Use a queue when work is long-running, retryable, bursty, or must survive process restarts. Avoid a queue for trivial synchronous work.
- Use a database for durable state; in-memory state is not reliable across restarts or replicas.
- Use repository abstractions only when they isolate meaningful domain behavior or volatile persistence. Do not wrap every ORM call mechanically.
- Use dependency injection through explicit constructors/composition root; avoid global service locators and hidden import-time initialization.
- Use `asyncio` for concurrent I/O when supported end-to-end. Do not mix blocking calls into the event loop; use a thread/process boundary deliberately.
- Use bounded concurrency, backpressure, timeouts, and cancellation. Unbounded task creation is a production defect.

## Bot architecture checklist
- Inbound adapter for platform updates/webhooks.
- Domain/application use cases independent of Telegram/Discord SDK.
- Per-user/chat state with explicit persistence and expiry policy.
- Deduplication/idempotency for redelivered updates.
- Outbound platform client with rate-limit handling and safe retries.
- Background worker/scheduler separated from request handling when needed.
- Authorization checks at use-case boundaries, not only hidden UI buttons.

## Scraper architecture checklist
- Source registry and per-source adapter.
- Fetcher separated from parser/extractor and normalizer.
- Explicit request policy: timeout, user agent, concurrency, rate, retry budget.
- Parser functions deterministic and testable against saved fixtures.
- Validation and deduplication before persistence.
- Checkpointing, incremental runs, and resumability for long jobs.
- Raw evidence/provenance retained when auditability matters.
- Browser context lifecycle isolated from extraction logic.

## Python implementation standards
- Use `pyproject.toml` as the central project configuration.
- Prefer `pathlib`, dataclasses or validated models where appropriate, `Protocol` only for useful seams, and explicit exception types.
- Type public interfaces and nontrivial internal boundaries.
- Avoid mutable global state, import side effects, giant modules, circular imports, and catch-all exception swallowing.
- Configuration is injected and validated at startup; secrets never live in source control.

## Deliverable
Produce component diagram, module map, key interfaces, data model, lifecycle/failure behavior, ADRs for consequential choices, and a trace from requirements to components and tests.
