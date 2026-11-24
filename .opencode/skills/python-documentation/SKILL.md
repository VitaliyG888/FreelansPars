---
name: python-documentation
description: Create and maintain professional Python project documentation, API references, architecture records, setup guides, scraper source contracts, bot operations guides, and runbooks.
compatibility: opencode
metadata:
  domain: python-engineering
  stage: documentation
---

# Python Engineering Documentation

## Documentation principles
Documentation must describe the actual implementation, not intended or imagined behavior. Inspect code/config/tests before writing. Prefer concise, task-oriented docs and runnable examples.

## Core documents
- README: purpose, capabilities, requirements, quick start, configuration pointer, common commands, links.
- `docs/architecture.md`: components, boundaries, data flow, deployment, key decisions.
- `docs/development.md`: environment setup, quality commands, test strategy.
- `docs/configuration.md`: variables, required/optional status, examples without real secrets.
- `docs/operations.md`: startup, shutdown, health, logs, metrics, backups, recovery.
- ADRs: context, decision, alternatives, consequences.
- API docs: request/response, auth, errors, pagination, idempotency, examples.

## Bot documentation
Describe platform, polling/webhook mode, permissions/scopes, commands, event lifecycle, FSM states, admin operations, rate limits, recovery, and local testing.

## Scraper documentation
Describe source and permitted access, URL scope, extracted fields, selectors/schema assumptions, request budget, rate limits, pagination, deduplication, provenance, storage, retention, and failure alerts.

## Quality
- Verify every command in a clean environment when possible.
- Mark unverified commands/examples clearly.
- Keep docs aligned with package versions and configuration names.
- Never include tokens, cookies, real personal data, internal production URLs, or sensitive logs.
- Add troubleshooting based on observed failure modes.
- Avoid duplicating the same instructions across multiple files; link to the canonical location.

## Completion
Check links, paths, commands, config names, and examples against the repository. Summarize changed docs and any unverified statements.
