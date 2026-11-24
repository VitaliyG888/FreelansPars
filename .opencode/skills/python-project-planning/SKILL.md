---
name: python-project-planning
description: Plan Python applications, bots, APIs, automation systems, and web scrapers from requirements through measurable acceptance criteria and an implementation roadmap.
compatibility: opencode
metadata:
  domain: python-engineering
  stage: discovery
---

# Python Project Planning

Use this skill before implementation when requirements are incomplete, a new project is starting, or a substantial feature needs decomposition.

## Mission
Turn an idea into a buildable, testable specification. Do not jump directly to code. Separate confirmed facts, assumptions, decisions, and open questions.

## Workflow
1. Inspect the repository first: tree, README, pyproject.toml, existing tests, CI, runtime constraints, deployment files, and recent conventions. Preserve existing decisions unless there is evidence to change them.
2. Clarify the product goal, users, operating environment, expected workload, external systems, data sensitivity, reliability needs, budget/hosting constraints, and definition of done. Ask only blocking questions; otherwise record explicit assumptions.
3. Classify the system: Telegram/Discord/chat bot, webhook/API service, scheduled worker, static HTTP scraper, browser scraper, data pipeline, or hybrid.
4. Convert requirements into user journeys and functional requirements. Add non-functional requirements: latency, throughput, availability, recovery, observability, privacy, retention, accessibility, maintainability, and cost.
5. Identify actors, integrations, trust boundaries, data flows, state transitions, failure modes, and external dependencies.
6. Define measurable acceptance criteria using Given/When/Then or equivalent examples. Include normal, boundary, failure, and abuse cases.
7. Decompose into vertical, independently verifiable milestones. Order by risk reduction and dependency, not by file or layer alone.
8. Produce/update `docs/requirements.md`, `docs/architecture.md`, `docs/implementation-plan.md`, and `docs/risks.md` when appropriate. Avoid creating paperwork for a tiny task.
9. Present architecture options and trade-offs where a meaningful choice exists. Recommend one and explain why.
10. Obtain approval before irreversible actions, paid services, production changes, destructive migrations, or scope-expanding decisions.

## Bot-specific discovery
- Platform and library constraints; polling vs webhook; update delivery semantics; concurrency; conversation state/FSM; command and permission model; rate limits; retry and deduplication; localization; admin operations.
- Define behavior when the platform is unavailable, a handler times out, duplicate updates arrive, or the user sends malformed input.
- Decide what belongs in the bot process versus API, worker, scheduler, and persistence layer.

## Scraper-specific discovery
- Target domains and exact pages/data fields; public/authenticated access; robots.txt and terms; permitted request rate; pagination; localization; content churn; JS rendering; anti-bot controls; data freshness; deduplication; provenance; retention.
- Prefer a documented API or static HTTP retrieval over browser automation when it satisfies the requirements.
- Define stop conditions, maximum pages/items/time, retry budget, checkpointing, and partial-result semantics.
- Treat access controls and explicit denial as boundaries; do not plan bypasses of authentication, paywalls, CAPTCHAs, or technical restrictions.

## Deliverables
A concise plan should include:
- Problem statement and scope / out of scope.
- User stories and acceptance criteria.
- System context and data-flow description.
- Proposed stack with rationale and alternatives.
- Component boundaries and interfaces.
- Data model and lifecycle.
- Security/privacy/threat considerations.
- Test strategy and quality gates.
- Deployment/operations strategy.
- Milestones, dependencies, risks, and open questions.

## Quality gate
Do not mark planning complete until each requirement maps to an acceptance criterion and at least one verification method. Flag assumptions rather than silently turning them into facts.
