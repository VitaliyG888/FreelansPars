---
name: python-browser-automation
description: Implement reliable Python browser automation and JavaScript-rendered data extraction with Playwright, including browser lifecycle, selectors, waits, sessions, downloads, and deterministic tests.
compatibility: opencode
metadata:
  domain: python-engineering
  stage: implementation
---

# Python Browser Automation

Use only when HTTP/API retrieval cannot meet the requirement because content or interaction depends on browser execution.

## Before implementation
Confirm the site permits the intended automation. Use official APIs when available. Do not evade authentication, CAPTCHAs, paywalls, bot defenses, or access restrictions. Never reuse a user's session or credentials without explicit authorization and secure handling.

## Playwright engineering
- Prefer Playwright's async Python API in async applications; do not mix sync and async APIs.
- Reuse one browser process where practical, but isolate browser contexts per account/job for cookie and storage isolation.
- Close pages, contexts, browser, and Playwright runtime in `finally`/async context managers.
- Pin and document browser/dependency versions in reproducible environments.
- Keep browser setup in an adapter; expose typed application-level results.
- Use locators by role, label, placeholder, text, or stable test id; avoid fragile XPath and positional selectors.
- Wait for observable state (`locator.wait_for`, expected URL, response, or specific element). Avoid arbitrary sleeps as synchronization.
- Set navigation/action timeouts and an overall job deadline.
- Limit tabs, contexts, downloads, memory, and concurrency.

## Extraction
- Wait for the exact content condition, not merely `networkidle` (many sites never become idle).
- Extract structured data from the rendered DOM; normalize and validate it in a separate pure function.
- Handle pagination with explicit next-page conditions, duplicate detection, and a hard page cap.
- Capture screenshots/traces only when useful for debugging; redact secrets and personal data.
- Validate downloads by expected file type, size, and content; never trust the extension alone.

## Reliability
- Distinguish navigation, selector, browser crash, timeout, and site-response errors.
- Retry only transient failures and only when repeating the action is safe.
- Use bounded retries with jitter; avoid retrying non-idempotent form submissions without a recovery strategy.
- Detect login expiry and report it; do not attempt to defeat access controls.
- Ensure cancellation closes browser resources and does not leave orphan processes.
- For long jobs, checkpoint extracted records outside the browser.

## Testing
- Prefer a local fixture site or controlled test environment.
- Test selector behavior, delayed rendering, empty state, pagination, timeout, navigation failure, download validation, and cleanup.
- Keep browser tests separate from fast unit tests; configure CI browser installation explicitly.
- Use Playwright tracing/screenshots for failed E2E diagnostics with sensitive data redacted.

## Definition of done
No fixed sleep is the sole synchronization mechanism. Resource cleanup is guaranteed. Browser access is bounded and policy-compliant. Extractors are independently testable and output is validated.
