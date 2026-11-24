---
name: python-code-review
description: Perform rigorous Python code reviews for correctness, architecture, typing, security, concurrency, performance, maintainability, and test quality.
compatibility: opencode
metadata:
  domain: python-engineering
  stage: quality
---

# Python Code Review

Review the actual diff and relevant surrounding code. Do not review imagined changes. Do not modify code unless explicitly asked to fix findings.

## Review order
1. Understand intended behavior and acceptance criteria.
2. Inspect diff, callers, interfaces, tests, configuration, and migrations.
3. Look for correctness/data-loss/security issues first.
4. Evaluate async/concurrency and lifecycle safety.
5. Check architecture, error handling, typing, observability, and performance.
6. Evaluate tests against risks and regression paths.
7. Run safe, relevant checks when possible.

## Python defect checklist
- Incorrect async usage, blocking event loop, unbounded task fan-out, lost cancellation.
- Missing timeout, unsafe retry, resource/session leak.
- Race conditions, non-atomic read-modify-write, duplicate side effects.
- Mutable shared state, import-time network/DB operations.
- Unvalidated external input, unsafe deserialization, path traversal, SSRF.
- SQL injection, command injection, unsafe template/HTML handling.
- Secret exposure in logs, exceptions, fixtures, or URLs.
- Broken transaction/rollback behavior or migration compatibility.
- Type annotations that misrepresent runtime behavior.
- Broad exception swallowing and misleading fallback behavior.
- Tests coupled to private implementation or relying on live services.

## Bot-specific review
Check authorization at use-case boundary, callback validation, cross-chat isolation, duplicate update handling, rate-limit compliance, webhook authenticity, safe formatting, and durable state.

## Scraper-specific review
Check policy compliance, request bounds, host concurrency, timeouts, pagination termination, parser fixture coverage, deduplication, provenance, storage validation, and no attempt to evade access controls.

## Finding format
For each finding provide:
- Severity: Critical / High / Medium / Low.
- Location: file and line(s).
- Evidence: concrete code path or scenario.
- Impact: user/data/security/operational consequence.
- Recommendation: specific minimal change.

Only report actionable findings. Do not inflate severity for style preferences. If no findings, say so and list remaining test/verification gaps. Distinguish confirmed defects from questions and suggestions.
