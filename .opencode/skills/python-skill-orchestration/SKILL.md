---
name: python-skill-orchestration
description: Orchestrate the Python Engineering Skills workflow in OpenCode, selecting the right skills and coordinating planning, design, implementation, testing, review, security, and release.
compatibility: opencode
metadata:
  domain: python-engineering
  stage: orchestration
---

# Python Engineering Workflow Orchestrator

Use this skill for end-to-end Python projects or substantial features. Load only the specialist skills relevant to the current phase; avoid loading every skill for a trivial change.

## Standard lifecycle
1. **Discover** — `python-project-planning`: inspect repository, clarify goals, acceptance criteria, constraints, risks.
2. **Design** — `python-architecture-design`: components, interfaces, state, persistence, lifecycle, ADRs.
3. **Implement** — `python-feature-implementation` plus one or more domain skills:
   - chat platform bot: `python-bot-development`
   - static/API scraping: `python-web-scraping`
   - JS-rendered browser flow: `python-browser-automation`
   - external API/webhooks: `python-api-integration`
   - persistence: `python-database-engineering`
4. **Verify continuously** — `python-testing-tdd`; use `python-debugging` for failures.
5. **Review** — `python-code-review` and `python-security-review`.
6. **Harden** — `python-performance-observability` and `python-dependency-management` when scale, production, or dependency changes warrant them.
7. **Deliver** — `python-documentation` and `python-production-release`.

## Gate policy
- Do not skip requirements/architecture for a new multi-component system.
- Do not implement a feature without acceptance criteria.
- Prefer tests before implementation for behavior changes and bug fixes.
- Do not claim tests, scans, builds, or deployments ran unless tool output proves it.
- Do not deploy, delete data, apply destructive migrations, publish packages, or incur costs without explicit approval.
- Stop when requirements conflict, a security boundary is unclear, or an external access policy forbids the intended action.
- Keep user informed at meaningful checkpoints; ask only for decisions that block safe progress.

## Parallel work
Delegate only bounded, independent tasks with explicit inputs, outputs, and file ownership. Examples: independent review of architecture, threat model, or test plan. Avoid parallel edits to the same files. Integrate findings and resolve conflicts centrally. A reviewer should challenge the implementation rather than rubber-stamp it.

## Final report
Summarize delivered behavior, architecture decisions, changed files, tests/checks with exact outcomes, security/operational notes, known limitations, and next steps. Separate verified facts from assumptions.
