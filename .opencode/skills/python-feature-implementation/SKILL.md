---
name: python-feature-implementation
description: Implement Python features end-to-end from approved requirements and architecture using small vertical slices, typed interfaces, clean boundaries, and verified behavior.
compatibility: opencode
metadata:
  domain: python-engineering
  stage: implementation
---

# Python Feature Implementation

## Before editing
Read requirements, architecture, implementation plan, nearby code, tests, pyproject, and repository instructions. Confirm the feature's observable acceptance criteria. If design is materially ambiguous, return to planning rather than inventing behavior.

## Implementation loop
1. Select one vertical slice with clear acceptance criteria.
2. Identify affected interfaces, data model, migrations, configuration, and failure modes.
3. Add/adjust a failing test for the behavior.
4. Implement the smallest cohesive change.
5. Keep I/O at boundaries and business logic independently testable.
6. Add type annotations and explicit error handling at public boundaries.
7. Update docs/config/examples when behavior changes.
8. Run focused tests and relevant quality checks.
9. Review diff for scope creep, accidental files, secrets, and compatibility.
10. Record completed tasks and remaining work.

## Code standards
- Follow existing project style and supported Python version.
- Prefer small cohesive functions and modules with meaningful names.
- Use explicit dependencies, context managers, and deterministic cleanup.
- Validate untrusted data at boundaries.
- Do not swallow exceptions or return fake success values.
- Avoid premature abstractions and speculative extensibility.
- Preserve backward compatibility unless a breaking change is explicitly approved.
- Keep functions deterministic where possible; inject clocks, random generators, and transports when tests need control.

## Scope control
Do not refactor unrelated modules, change frameworks, add infrastructure, or alter public contracts without necessity. If a broader issue is discovered, document it and propose a separate change.

## Final verification
Run focused tests, full relevant suite, lint/format, and type checks if configured. State exact commands and outcomes. Inspect `git diff --check` and the final diff. Never claim completion based on code inspection alone when execution was possible.
