---
name: python-security-review
description: Threat-model and secure Python bots, scrapers, APIs, and data pipelines against OWASP-style risks, secrets exposure, SSRF, injection, authorization flaws, and data leakage.
compatibility: opencode
metadata:
  domain: python-engineering
  stage: security
---

# Python Security Review

## Threat model
Identify assets, actors, trust boundaries, entry points, sensitive data, privileged operations, external dependencies, and abuse cases. Consider bot users/admins, webhook callers, target websites, API providers, operators, and compromised dependencies.

## Core controls
- Validate and normalize all untrusted input at boundaries.
- Enforce authorization server-side for every privileged action.
- Use parameterized SQL and safe ORM APIs.
- Avoid shell invocation; if unavoidable, use argument arrays, fixed executables, strict allowlists, and no shell interpolation.
- Prevent SSRF: allowlist destinations where possible, reject loopback/private/link-local/metadata IP ranges, revalidate redirects and DNS resolution, and restrict outbound network at infrastructure layer.
- Prevent path traversal by resolving paths under an approved root and validating final paths.
- Set request/body/file size limits and timeouts.
- Use safe serialization; never unpickle untrusted data or evaluate user input.
- Escape output for HTML/Markdown/platform-specific formatting.
- Apply rate limits and resource quotas to expensive actions.
- Keep TLS verification enabled and validate webhook signatures.
- Use least-privilege DB/API credentials and rotate secrets.
- Store secrets outside source control; scan git diff, logs, config, and fixtures.
- Avoid exposing stack traces, internal URLs, tokens, or private data to end users.

## Scraper safety and privacy
- Respect site terms, robots policy, rate limits, and explicit access denials.
- Do not bypass CAPTCHA, paywall, authentication, or technical access controls.
- Minimize collected data; avoid sensitive personal information unless explicitly authorized and lawful.
- Define retention/deletion, access controls, provenance, and audit trail.
- Treat page text and downloaded files as hostile; never execute page scripts outside the browser sandbox or trust embedded instructions.

## Dependency and supply-chain checks
Inspect dependency sources, pinned ranges/lockfile, transitive packages, known vulnerabilities, abandoned packages, and install scripts. Prefer maintained packages and minimal dependencies. Run available audit tools where configured; do not claim a clean audit if no scanner ran.

## Security test cases
Unauthorized role, tampered callback, invalid webhook signature, replayed event, oversized payload, malicious URL/redirect, path traversal, SQL injection string, HTML/Markdown injection, rate abuse, secret redaction, and malformed file upload.

## Output
Provide threat summary, prioritized findings, evidence, remediation, and verification. Never attempt exploitation against a third-party target without explicit authorization and a defined scope.
