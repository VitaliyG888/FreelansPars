---
name: python-api-integration
description: Design and implement robust Python integrations with REST, GraphQL, webhooks, OAuth, external SDKs, pagination, retries, schemas, and contract tests.
compatibility: opencode
metadata:
  domain: python-engineering
  stage: implementation
---

# Python API Integration

## Contract-first workflow
1. Read official API documentation and installed SDK version.
2. Record authentication, scopes, pagination, quotas, idempotency, error schema, versioning, and webhook semantics.
3. Define typed request/response models and an adapter boundary.
4. Implement transport with explicit timeout, bounded connection pool, TLS verification, and safe retry policy.
5. Normalize provider errors into application-level exceptions without discarding diagnostic context.
6. Add contract tests and sanitized fixtures.
7. Document credentials, scopes, sandbox/test mode, quota behavior, and rotation.

## HTTP client rules
- Reuse `httpx.AsyncClient` or a suitable session rather than creating a client per request.
- Set connect/read/write/pool timeouts; never rely on indefinite defaults.
- Validate base URLs and avoid accepting arbitrary user-controlled destinations (SSRF).
- Keep TLS certificate verification enabled.
- Use structured request IDs and redact authorization, cookies, secrets, and sensitive payload fields.
- Bound response body size and validate content type before parsing.
- Handle 3xx redirects deliberately; do not forward credentials across untrusted origins.
- Retry only transient errors and safe/idempotent operations. Respect Retry-After and cap attempts.
- Use idempotency keys for supported non-idempotent operations.
- Implement pagination without infinite loops; enforce page and item budgets.

## Authentication and webhooks
- Use least-privilege scopes and short-lived credentials where supported.
- Keep tokens in environment/secret manager; support rotation without source changes.
- Verify webhook signatures over the exact raw body and enforce timestamp/replay protection when supported.
- Acknowledge webhooks quickly; enqueue expensive processing.
- Deduplicate webhook event IDs and make processing safe under redelivery.
- Never log full authorization headers or raw secrets.

## Data contracts
Validate external payloads at the boundary. Tolerate additive unknown fields where safe, but fail visibly on missing required fields or incompatible types. Use explicit API versioning and compatibility tests. Avoid leaking provider DTOs into domain code.

## Testing
Mock HTTP transport for success, malformed JSON, unexpected content type, 401/403, 404, 409, 429, 5xx, timeout, connection reset, pagination, duplicate webhook, and signature mismatch. Use provider sandbox only for a small smoke suite. No live secret is required in ordinary CI.
