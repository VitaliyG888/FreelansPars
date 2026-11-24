---
name: python-production-release
description: Prepare Python applications for production with reproducible packaging, configuration, Docker, CI, health checks, deployment, migrations, rollback, and operational runbooks.
compatibility: opencode
metadata:
  domain: python-engineering
  stage: delivery
---

# Python Production and Release

## Repository readiness
- Confirm supported Python versions and OS.
- Use `pyproject.toml` and a reproducible lockfile where the project uses one.
- Document installation, development, test, lint, type-check, run, migration, and release commands.
- Separate runtime and development dependencies.
- Build/install in a clean environment to detect undeclared dependencies.
- Avoid unpinned floating production dependencies where reproducibility matters.

## Configuration
- Validate settings at startup and fail fast on missing required configuration.
- Separate dev/test/staging/prod configuration.
- Inject secrets via secret manager/environment; never bake them into image layers or commit them.
- Document rotation and least-privilege access.
- Do not log configuration values wholesale.

## Containerization
- Use a small supported base image and multi-stage builds when useful.
- Run as non-root.
- Pin base image strategy and scan images.
- Use exec-form entrypoint and handle SIGTERM.
- Add health checks only when meaningful.
- Do not include compilers, caches, credentials, or test fixtures in runtime image unless required.
- Keep writable paths explicit and use persistent volumes only for durable state.

## CI quality gates
A typical pipeline:
1. Install locked dependencies.
2. Format/lint (Ruff or project equivalent).
3. Type-check (mypy/pyright if configured).
4. Unit tests.
5. Integration tests with disposable services.
6. Coverage report / critical-path threshold.
7. Build package/image.
8. Dependency/security scan.
9. Artifact provenance and release checks.

Do not add tools without checking existing configuration. Keep CI reproducible and avoid live third-party dependencies.

## Runtime operations
- Implement graceful startup/shutdown and resource cleanup.
- Separate liveness from readiness.
- Use structured logs and actionable metrics.
- Define restart policy, worker concurrency, queue behavior, and dead-letter/retry handling.
- Configure timeouts at every network boundary.
- Ensure persistent state and backups are external to ephemeral containers.
- Provide runbook for provider outage, queue backlog, DB outage, failed scrape, and credential rotation.

## Release and migrations
- Review diff, changelog, compatibility, and migration plan.
- Use staged/canary rollout when risk warrants it.
- Apply backward-compatible DB migrations before code that depends on them.
- Define rollback and forward-fix options; code rollback may not reverse data migrations.
- Require explicit confirmation before production deploy, destructive migration, or data deletion.
- Verify deployed version, health, error rate, and key business behavior after release.

## Definition of done
A clean install/build works, CI gates are green, secrets are external, health/observability are useful, migrations are safe, and deployment/rollback instructions are documented.
