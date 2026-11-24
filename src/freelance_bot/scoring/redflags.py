"""Обнаружение красных флагов: явное, на правилах и объяснимое."""

from __future__ import annotations

import re

from ..config import RedFlagRule, RedFlagsConfig, ScoringConfig
from ..models import Job, RedFlag


def _match_patterns(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            start = max(0, m.start() - 30)
            end = min(len(text), m.end() + 30)
            return text[start:end].strip()
    return None


def detect(job: Job, cfg: RedFlagsConfig, scoring: ScoringConfig) -> list[RedFlag]:
    if not cfg.enabled:
        return []

    flags: list[RedFlag] = []
    haystack = f"{job.title}\n{job.description}".lower()
    description = job.description or ""

    for code, rule in cfg.rules.items():
        if not isinstance(rule, RedFlagRule) or not rule.enabled:
            continue

        if rule.patterns:
            evidence = _match_patterns(haystack, rule.patterns)
            if evidence:
                flags.append(RedFlag(code=code, severity=rule.severity, evidence=evidence))
                continue

        if code == "scope_mismatch" and rule.budget_ratio_below is not None:
            median = scoring.median_for(job.category)
            estimate = job.budget_rub_estimate
            min_scope = rule.min_scope_chars or 0
            if (
                estimate is not None
                and median > 0
                and len(description) >= min_scope
                and (estimate / median) < rule.budget_ratio_below
            ):
                flags.append(
                    RedFlag(
                        code=code,
                        severity=rule.severity,
                        evidence=(
                            f"бюджет {estimate:.0f} ₽ при медиане {median:.0f} ₽ "
                            f"и описании {len(description)} символов"
                        ),
                    )
                )
                continue

        if (
            code == "vague_brief"
            and rule.max_description_chars is not None
            and 0 < len(description) <= rule.max_description_chars
        ):
            flags.append(
                RedFlag(
                    code=code,
                    severity=rule.severity,
                    evidence=f"описание {len(description)} символов",
                )
            )

    return flags


def severity_sum(flags: list[RedFlag]) -> float:
    """Убывающая отдача: множество мелких флагов не должны перевешивать один серьёзный.
    """
    total = 0.0
    for i, flag in enumerate(sorted(flags, key=lambda f: f.severity, reverse=True)):
        total += flag.severity * (0.75**i)
    return min(total, 2.4)
