"""Поведение движка скоринга: порядок, веса, штрафы, объяснения."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conftest import make_job
from freelance_bot.config import RedFlagRule, RedFlagsConfig, ScoringConfig, ScoringWeights
from freelance_bot.models import ClientInfo
from freelance_bot.scoring.engine import ScoringEngine


def engine(**overrides) -> ScoringEngine:
    cfg = ScoringConfig(
        skill_match={
            "skills": ["python", "парсинг", "api", "автоматизация"],
            "exclude_keywords": ["1с", "бухгалтер"],
        },
        market_median_rub={"default": 10000, "Разработка и IT": 15000},
        **overrides,
    )
    return ScoringEngine(cfg, RedFlagsConfig())


def test_score_within_bounds_and_explainable() -> None:
    scored = engine().score(make_job())
    assert 0 <= scored.score.total <= 100
    assert len(scored.score.factors) == 6
    assert all(f.rationale for f in scored.score.factors)
    assert scored.score.summary_ru
    # Взвешенные вклады должны восстанавливать итог (без скрытых слагаемых).
    base = sum(f.contribution for f in scored.score.factors)
    assert scored.score.total == pytest.approx(round(100 * base * (1 - scored.score.penalty), 1))


def test_high_budget_beats_low_budget() -> None:
    cheap = engine().score(make_job(budget=3000))
    rich = engine().score(make_job(budget=45000))
    assert rich.score.total > cheap.score.total
    assert rich.score.factors[0].raw > cheap.score.factors[0].raw


def test_fewer_proposals_beat_many() -> None:
    quiet = engine().score(make_job(proposals=1))
    crowded = engine().score(make_job(proposals=80))
    assert quiet.score.total > crowded.score.total
    comp = {f.name: f for f in crowded.score.factors}["competition"]
    assert comp.raw < 0.2


def test_fresh_job_beats_stale() -> None:
    fresh = engine().score(make_job(hours_ago=0.5))
    stale = engine().score(make_job(hours_ago=72))
    assert fresh.score.total > stale.score.total


def test_skill_match_rewards_relevant_and_punishes_excluded() -> None:
    good = engine().score(make_job(title="Парсер на Python с выгрузкой в API"))
    bad = engine().score(
        make_job(title="Бухгалтер в 1С", description="Нужен бухгалтер, работа в 1С.", budget=40000)
    )
    good_skill = {f.name: f for f in good.score.factors}["skill_match"]
    bad_skill = {f.name: f for f in bad.score.factors}["skill_match"]
    assert good_skill.raw > bad_skill.raw
    assert bad_skill.raw <= 0.1


def test_client_quality_neutral_when_unknown() -> None:
    scored = engine().score(make_job(client=ClientInfo()))
    factor = {f.name: f for f in scored.score.factors}["client_quality"]
    assert factor.raw == 0.5
    assert "нейтральная" in factor.rationale.lower()


def test_client_quality_rewards_good_history() -> None:
    good_client = ClientInfo(hire_rate=0.9, total_spend_rub=500_000, verified=True, rating=4.9)
    scored = engine().score(make_job(client=good_client))
    factor = {f.name: f for f in scored.score.factors}["client_quality"]
    assert factor.raw > 0.8


def test_red_flag_penalty_reduces_total() -> None:
    base = ScoringEngine(ScoringConfig(market_median_rub={"default": 10000}), RedFlagsConfig())
    flagged_cfg = RedFlagsConfig(
        rules={
            "unpaid_test": RedFlagRule(enabled=True, severity=0.9, patterns=[r"бесплатн\w* тестов"])
        }
    )
    flagged = ScoringEngine(ScoringConfig(market_median_rub={"default": 10000}), flagged_cfg)
    job = make_job(description="Нужно сделать бесплатное тестовое задание, потом обсудим оплату.")
    assert flagged.score(job).score.total < base.score(job).score.total
    assert flagged.score(job).score.red_flags


def test_weights_are_configurable_and_flip_ranking() -> None:
    job_a = make_job(external_id="a", budget=40000, proposals=200)
    job_b = make_job(external_id="b", budget=6000, proposals=1)

    budget_heavy = ScoringEngine(
        ScoringConfig(
            weights=ScoringWeights(
                budget=0.9,
                competition=0.01,
                skill_match=0.01,
                client_quality=0.01,
                freshness=0.01,
                urgency_fit=0.01,
            )
        )
    )
    competition_heavy = ScoringEngine(
        ScoringConfig(
            weights=ScoringWeights(
                budget=0.01,
                competition=0.9,
                skill_match=0.01,
                client_quality=0.01,
                freshness=0.01,
                urgency_fit=0.01,
            )
        )
    )
    assert budget_heavy.score(job_a).score.total > budget_heavy.score(job_b).score.total
    assert competition_heavy.score(job_b).score.total > competition_heavy.score(job_a).score.total


def test_unknown_budget_is_neutral_not_zero() -> None:
    job = make_job(budget=0).model_copy(update={"budget_rub_estimate": None, "budget_amount": None})
    factor = {f.name: f for f in engine().score(job).score.factors}["budget"]
    assert factor.raw == 0.4


def test_urgent_with_tight_deadline_scores_urgency() -> None:
    now = datetime.now(UTC)
    job = make_job(deadline_hours=6)
    factor = {f.name: f for f in engine().score(job, now=now).score.factors}["urgency_fit"]
    assert factor.raw == 1.0


def test_rank_returns_top_n_sorted() -> None:
    jobs = [make_job(external_id=str(i), budget=1000 * i) for i in range(1, 11)]
    ranked = engine().rank(jobs, top_n=3)
    assert len(ranked) == 3
    assert [s.score.total for s in ranked] == sorted([s.score.total for s in ranked], reverse=True)


def test_short_skill_does_not_match_inside_unrelated_word() -> None:
    cfg = ScoringConfig(
        skill_match={"skills": ["бот"], "exclude_keywords": []},
        market_median_rub={"default": 10000},
    )
    scored = ScoringEngine(cfg, RedFlagsConfig()).score(
        make_job(title="Удалённая работа", description="Простая работа с текстами.")
    )
    factor = {f.name: f for f in scored.score.factors}["skill_match"]
    assert factor.raw == 0.2
    assert "совпадений" in factor.rationale.lower()


def test_contact_handle_is_not_a_skill_signal() -> None:
    job = make_job(
        title="Сделать видеомонтаж",
        description="Монтаж ролика. Телеграм: @manager, t.me/example",
    )
    factor = {f.name: f for f in engine().score(job).score.factors}["skill_match"]
    assert factor.raw == 0.2


def test_russian_skill_matches_inflected_form() -> None:
    cfg = ScoringConfig(
        skill_match={"skills": ["автоматизация"], "exclude_keywords": []},
        market_median_rub={"default": 10000},
    )
    job = make_job(
        title="Нужна автоматизация отчётов",
        description="Речь про автоматизации рутины и выгрузки.",
    )
    factor = {f.name: f for f in ScoringEngine(cfg, RedFlagsConfig()).score(job).score.factors}[
        "skill_match"
    ]
    assert factor.raw > 0.5


def test_eligibility_gate_is_always_open() -> None:
    """Порогов отбора больше нет: eligible() всегда True.

    Заказы скрывают только фильтры (категории/цена) и личные настройки
    пользователей; скоринг задаёт лишь порядок показа.
    """
    cfg = ScoringConfig(
        skill_match={"skills": ["python"], "exclude_keywords": []},
        market_median_rub={"default": 10000},
    )
    engine = ScoringEngine(cfg, RedFlagsConfig())
    relevant = engine.score(make_job(title="Python-разработчик"))
    unrelated = engine.score(make_job(title="Копирайтер", description="Тексты."))
    assert engine.eligible(relevant)
    assert engine.eligible(unrelated)


def test_scoring_is_deterministic() -> None:
    job = make_job()
    a = engine().score(job).score.total
    b = engine().score(job).score.total
    assert a == b
