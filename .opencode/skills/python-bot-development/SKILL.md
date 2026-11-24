---
name: python-bot-development
description: Build production-grade Python chat bots for Telegram, Discord, Slack, and similar platforms, including handlers, FSM, webhooks, persistence, queues, permissions, and platform reliability.
compatibility: opencode
metadata:
  domain: python-engineering
  stage: implementation
---

# Python Bot Development

Use for new bots, bot features, platform integrations, handler refactors, and reliability work.

## First identify the platform
Inspect existing dependencies and versions. Do not assume aiogram, python-telegram-bot, discord.py, or a specific framework. Verify APIs against the installed version and official documentation when uncertain. Do not upgrade frameworks opportunistically.

## Implementation workflow
1. Read project plan, architecture, platform docs, and current handler conventions.
2. Define update/event contract and expected user-visible behavior.
3. Implement domain use case separately from SDK handler.
4. Validate input at the boundary; normalize platform-specific objects into application DTOs.
5. Apply authorization and ownership checks in the application layer.
6. Implement state transitions explicitly. Use durable storage for state that must survive restarts or multiple replicas.
7. Add timeout, cancellation, retry, and rate-limit behavior appropriate to the operation.
8. Ensure duplicate delivery is safe using idempotency keys or durable update tracking where needed.
9. Add structured logs with correlation/update identifiers while excluding tokens, credentials, and private message content by default.
10. Add unit, adapter/integration, and end-to-end tests. Verify behavior with the platform's test facilities or mocked transport.
11. Document setup, commands, permissions, webhook/polling configuration, and operational recovery.

## Handler design
A handler should be thin:
- Parse and validate event.
- Check authentication/authorization.
- Call one application use case.
- Map domain result to platform response.
- Handle expected user-facing errors separately from unexpected failures.

Do not put long blocking work inside an update handler. Offload long jobs to a worker and return a job identifier/status when appropriate.

## State and conversations
- Model states and transitions explicitly; define entry/exit conditions and invalid transitions.
- Define key scope (user, chat, guild/workspace, conversation) to prevent cross-user leakage.
- Set TTL and cleanup rules for ephemeral state.
- Persist critical workflows and use transactions around multi-step state changes.
- Never trust callback payloads or hidden buttons as authorization.
- Make cancellation and restart recovery explicit.

## Delivery and platform reliability
- Polling and webhooks are alternative delivery modes; document the selected mode and operational implications.
- For webhooks, validate platform authenticity where supported, enforce request limits, return promptly, and queue expensive work.
- For polling, handle graceful shutdown and offset/acknowledgement semantics correctly.
- Respect platform rate limits and retry-after guidance. Retry only safe/idempotent operations.
- Avoid retry storms: bounded exponential backoff with jitter and a retry budget.
- Treat platform outages as recoverable; surface persistent failures through metrics/alerts.

## Security
- Store bot tokens in environment/secret manager, never logs or repository.
- Verify webhook secrets/signatures where supported.
- Apply least privilege to bot permissions and API scopes.
- Rate-limit user-triggered expensive actions; defend against spam and resource exhaustion.
- Escape/format user-supplied text safely for platform markup.
- Validate file type, size, and origin before processing attachments.
- Restrict admin commands by server-side authorization.

## Tests
Cover: valid update, malformed payload, unknown command, unauthorized actor, duplicate update, rate limit, platform timeout, handler exception, state expiry, concurrent updates, and restart recovery where applicable. Assert outgoing calls, state mutations, and user-visible result.

## Completion criteria
No business rule exists only in a handler. No secret is committed. No unbounded background task is spawned. Critical workflows are idempotent or explicitly documented as at-least-once. Tests and static checks pass.
