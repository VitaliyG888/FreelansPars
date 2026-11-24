---
name: python-debugging
description: Diagnose Python defects systematically using reproduction, evidence, hypotheses, root-cause analysis, logs, traces, and regression tests without speculative patching.
compatibility: opencode
metadata:
  domain: python-engineering
  stage: quality
---

# Python Systematic Debugging

## Four-phase method
### 1. Reproduce
- Capture exact command, environment, Python/package versions, inputs, and expected vs actual behavior.
- Reduce to the smallest deterministic reproduction.
- Preserve the failing test/log/fixture before changing code.

### 2. Localize
- Read the full traceback and identify the first project-owned frame.
- Inspect callers, data transformations, state transitions, and recent relevant diffs.
- Add temporary targeted diagnostics or use debugger/profiler; do not dump secrets or entire user payloads.
- Compare working and failing paths.

### 3. Hypothesize and test
- State one falsifiable hypothesis at a time.
- Predict what evidence should appear if it is true.
- Run the smallest experiment or test.
- Reject hypotheses that do not match evidence.
- Avoid changing multiple variables simultaneously.

### 4. Fix and prevent
- Fix the root cause at the correct boundary.
- Add a regression test that fails before the fix.
- Run focused and broader tests, lint/type checks as appropriate.
- Check for related failure modes and update docs/metrics if operationally relevant.

## Python-specific diagnostic checklist
- Sync/async boundary errors and blocking calls in event loop.
- Unawaited coroutines, leaked tasks, cancellation swallowed, missing cleanup.
- Mutable default arguments, shared global state, import-time side effects.
- Timezone-naive timestamps, clock assumptions, locale/encoding.
- Dependency/version mismatch and environment drift.
- Transaction boundaries, race conditions, duplicate events, stale caches.
- Resource leaks: sockets, sessions, DB connections, browser processes, files.
- Parsing assumptions: selector drift, null values, unexpected encodings, schema changes.
- Retry loops, retrying permanent failures, and duplicate side effects.

## Guardrails
Do not hide errors with broad `except Exception: pass`, arbitrary sleeps, increased timeouts without diagnosis, or retries that mask a deterministic defect. Do not rewrite an entire subsystem to fix a localized issue. After three failed fix attempts, stop and reassess the mental model, architecture, and reproduction.

## Output
Summarize: symptom, reproduction, root cause with evidence, minimal fix, regression coverage, verification commands/results, and residual risks.
