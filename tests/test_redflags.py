"""Правила красных флагов: каждое должно срабатывать на реалистичном русском ТЗ.
"""

from __future__ import annotations

import pytest

from conftest import make_job
from freelance_bot.config import RedFlagRule, RedFlagsConfig, ScoringConfig
from freelance_bot.scoring.redflags import detect, severity_sum

SCORING = ScoringConfig(market_median_rub={"default": 10000})


def cfg(**rules) -> RedFlagsConfig:
    return RedFlagsConfig(rules={name: RedFlagRule(**rule) for name, rule in rules.items()})


def codes(flags) -> set[str]:
    return {f.code for f in flags}


def test_unpaid_test_detected() -> None:
    c = cfg(unpaid_test={"severity": 0.8, "patterns": [r"бесплатн\w* (тестов|пробн)"]})
    job = make_job(description="Сначала нужно выполнить бесплатное тестовое задание.")
    assert "unpaid_test" in codes(detect(job, c, SCORING))


def test_offplatform_payment_detected() -> None:
    c = cfg(
        offplatform_payment={
            "severity": 0.7,
            "patterns": [r"перев\w* на карту", r"без (безопасной сделки|эскроу)"],
        }
    )
    job = make_job(description="Работаем без безопасной сделки, переведу на карту после сдачи.")
    flags = detect(job, c, SCORING)
    assert "offplatform_payment" in codes(flags)
    assert any("без безопасной сделки" in f.evidence for f in flags)


def test_vague_brief_detected() -> None:
    c = cfg(vague_brief={"severity": 0.25, "max_description_chars": 90})
    job = make_job(description="Нужен сайт.")
    assert "vague_brief" in codes(detect(job, c, SCORING))


def test_vague_brief_not_triggered_for_detailed_brief() -> None:
    c = cfg(vague_brief={"severity": 0.25, "max_description_chars": 90})
    job = make_job(description="Подробное описание. " * 20)
    assert "vague_brief" not in codes(detect(job, c, SCORING))


def test_scope_mismatch_uses_budget_ratio_and_length() -> None:
    c = cfg(scope_mismatch={"severity": 0.6, "budget_ratio_below": 0.25, "min_scope_chars": 100})
    big_scope_small_budget = make_job(budget=1000, description="Очень большой объём работ. " * 20)
    assert "scope_mismatch" in codes(detect(big_scope_small_budget, c, SCORING))

    small_scope_small_budget = make_job(budget=1000, description="Мелкая правка.")
    assert "scope_mismatch" not in codes(detect(small_scope_small_budget, c, SCORING))


def test_disabled_rules_do_not_fire() -> None:
    c = cfg(unpaid_test={"enabled": False, "severity": 0.9, "patterns": [r"бесплатн"]})
    job = make_job(description="Бесплатно не работаю.")
    assert detect(job, c, SCORING) == []


def test_severity_sum_is_capped_and_diminishing() -> None:
    from freelance_bot.models import RedFlag

    many = [RedFlag(code=f"r{i}", severity=0.9) for i in range(10)]
    assert severity_sum(many) <= 2.4
    one = [RedFlag(code="a", severity=0.9)]
    two = [RedFlag(code="a", severity=0.9), RedFlag(code="b", severity=0.9)]
    assert severity_sum(two) < 2 * severity_sum(one)


@pytest.mark.parametrize(
    "text,code",
    [
        ("Оплата после сдачи проекта", "offplatform_payment"),
        ("Нужно срочно сегодня сделать", "urgency_pressure"),
    ],
)
def test_pattern_rules_from_shipped_config(text: str, code: str) -> None:
    """Правила из поставляемого config.example.yaml должны реально совпадать с этими формулировками.
    """
    from pathlib import Path

    import yaml

    raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
    shipped = RedFlagsConfig.model_validate(raw["red_flags"])
    job = make_job(description=text)
    assert code in codes(detect(job, shipped, SCORING))
