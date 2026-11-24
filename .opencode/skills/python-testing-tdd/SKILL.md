---
name: python-testing-tdd
description: Apply professional Python testing, pytest, TDD, fixtures, mocks, property-based tests, integration tests, and coverage-driven verification for bots and scrapers.
compatibility: opencode
metadata:
  domain: python-engineering
  stage: quality
---

# Python Testing and TDD

## Test-first default
For bug fixes and behavior changes, reproduce the failure with a focused test before changing implementation. Use RED → GREEN → REFACTOR:
1. Write the smallest test that expresses observable behavior.
2. Run it and confirm it fails for the expected reason.
3. Implement the minimum change.
4. Run the focused test, then the relevant suite.
5. Refactor while keeping tests green.

Do not fabricate a failing result. If the environment prevents execution, state that clearly.

## Test pyramid
- Unit: pure domain logic, parsers, validators, normalizers, state transitions.
- Integration: DB repositories, HTTP adapters, queue, platform adapters.
- Contract: external API schemas and webhook payloads.
- End-to-end: critical user journeys and scraper run orchestration.
Keep most tests fast and deterministic.

## pytest standards
- Use descriptive names: `test_<behavior>_<condition>_<expected>`.
- Use fixtures for reusable setup; keep fixture scope minimal and avoid hidden global state.
- Parameterize meaningful input matrices.
- Prefer real small collaborators over deep mock chains.
- Mock at I/O boundaries, not internal functions merely to make tests pass.
- Use `tmp_path`, `monkeypatch`, `caplog`, and `pytest.raises` appropriately.
- Async tests must use the project's configured async plugin/strictness.
- Avoid order dependence, shared mutable fixtures, sleeps, and tests that require the public internet.
- Freeze/inject time and randomness instead of waiting.
- Clean up resources and background tasks.

## Required coverage dimensions
For every important feature, consider happy path, invalid input, boundary values, empty/missing data, authorization, duplicate delivery, concurrency, timeout/cancellation, dependency failure, and recovery. Select cases based on risk; do not mechanically test every theoretical combination.

## Property-based and fuzz testing
Use Hypothesis for invariants over broad input spaces where valuable: parser robustness, serialization round trips, normalization idempotence, pagination termination, and state machine invariants. Use fuzzing for untrusted parsers only with bounded resource use.

## Bot test patterns
- Feed synthetic update objects through handler adapter.
- Assert use-case calls and outgoing platform effects.
- Test duplicate update idempotency, cross-user state isolation, authorization, callback tampering, rate limits, retry behavior, and shutdown.
- Keep platform SDK mocks narrow and version-aware.

## Scraper test patterns
- Test parsers against saved fixtures, not live sites.
- Include selector drift, missing fields, malformed/empty HTML, encoding, locale, duplicate pages, pagination loops, 429, timeouts, and partial runs.
- Assert both extracted values and provenance/validation metadata.

## Quality and coverage
Run focused tests first, then full suite. Use coverage to find untested critical paths, not as a vanity percentage. Configure branch coverage where risk warrants it. A high percentage does not prove meaningful assertions.

## Verification record
Report exact commands and outcomes. Separate passed, failed, skipped, and not-run checks. Never claim tests passed unless they were actually executed.
