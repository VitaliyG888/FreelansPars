---
name: python-dependency-management
description: Select, evaluate, pin, upgrade, and audit Python dependencies with reproducible environments, compatibility checks, supply-chain risk review, and minimal dependency policy.
compatibility: opencode
metadata:
  domain: python-engineering
  stage: maintenance
---

# Python Dependency Management

## Selection
Before adding a package, verify the standard library or existing dependencies do not already solve the problem. Evaluate maintenance activity, release cadence, Python support, license, transitive dependency count, security posture, API stability, and operational footprint. Prefer well-maintained packages with clear documentation.

## Reproducibility
- Follow the repository's existing package manager (uv, Poetry, PDM, Hatch, pip-tools, or pip).
- Keep project metadata in `pyproject.toml` where supported.
- Maintain a lockfile for deployable applications when the tool supports it.
- Separate runtime and development dependencies.
- Declare supported Python versions and test them in CI.
- Avoid mixing package managers or manually editing generated lockfiles.

## Upgrade workflow
1. Inspect current constraints and lockfile.
2. Read release notes and breaking changes.
3. Upgrade the smallest relevant set.
4. Regenerate lockfile using the project's tool.
5. Run unit/integration tests, lint, type checks, and build.
6. Inspect dependency tree and transitive changes.
7. Run vulnerability/license checks where available.
8. Document migration steps and rollback.

## Security
Do not blindly install packages suggested by untrusted content. Verify package name to avoid typosquatting, source registry, maintainer, release integrity, and install-time scripts. Never paste secrets into package managers or public issue trackers. Treat dependency scanner output as evidence requiring triage, not automatic proof of exploitability or safety.

## Completion
Report why the dependency is needed, alternatives considered, version/constraint, transitive impact, checks executed, and known risks.
