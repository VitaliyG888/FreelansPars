"""Тесты профиля фрилансера, стратегий цены и защиты от нечеловекоподобного текста.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from freelance_bot.config import MessageLimit, StyleConfig
from freelance_bot.llm.guard import check
from freelance_bot.llm.message import MessageGenerator
from freelance_bot.models import Job, JobAnalysis
from freelance_bot.profile import (
    AvailabilityConfig,
    FreelancerProfile,
    MessageStyleConfig,
    PricingConfig,
    expand_skill,
    load_profile,
    skill_domains,
)


def make_job(
    title: str = "Разработать Telegram-бота для рассылки",
    description: str = "Нужен бот на Python с интеграцией Google Sheets и PostgreSQL. "
    "Команды, состояние пользователей, обработка ошибок.",
    budget: float | None = 15000,
    currency: str = "RUB",
) -> Job:
    return Job(
        platform="fl_ru",
        external_id="1",
        url="https://www.fl.ru/projects/1/x.html",
        title=title,
        description=description,
        budget_amount=budget,
        budget_rub_estimate=budget,
        currency=currency,
        skills=["python", "telegram", "бот"],
    )


# --------------------------------------------------------------------------- #
# Модель профиля
# --------------------------------------------------------------------------- #


def test_profile_defaults() -> None:
    profile = FreelancerProfile()
    assert profile.pricing.strategy == "markup"
    assert profile.pricing.min_acceptable_rub > 0
    assert profile.all_skills == []


def test_profile_all_skills_combines_primary_and_secondary() -> None:
    profile = FreelancerProfile(
        skills=["Python", "Docker"],
        secondary_skills=["Nginx", "CI/CD"],
    )
    assert profile.all_skills == ["Python", "Docker", "Nginx", "CI/CD"]


def test_profile_load_from_yaml(tmp_path: Path) -> None:
    p = tmp_path / "profile.yaml"
    p.write_text(
        "name: Иван\n"
        "skills:\n  - Python\n  - FastAPI\n"
        "pricing:\n"
        "  strategy: minimum\n"
        "  min_acceptable_rub: 7000\n",
        encoding="utf-8",
    )
    profile = load_profile(p)
    assert profile.name == "Иван"
    assert profile.skills == ["Python", "FastAPI"]
    assert profile.pricing.strategy == "minimum"
    assert profile.pricing.min_acceptable_rub == 7000


def test_profile_missing_file_returns_defaults(tmp_path: Path) -> None:
    profile = load_profile(tmp_path / "nope.yaml")
    assert profile.name == ""
    assert profile.pricing.strategy == "markup"


# --------------------------------------------------------------------------- #
# Стратегии ценообразования
# --------------------------------------------------------------------------- #


def test_pricing_minimum_uses_budget() -> None:
    profile = FreelancerProfile(pricing=PricingConfig(strategy="minimum", min_acceptable_rub=5000))
    price = profile.calculate_price(20000, None)
    assert price == 19000  # 95% бюджета


def test_pricing_minimum_respects_floor() -> None:
    profile = FreelancerProfile(pricing=PricingConfig(strategy="minimum", min_acceptable_rub=8000))
    price = profile.calculate_price(5000, None)
    assert price == 8000


def test_pricing_markup_applies_multiplier() -> None:
    profile = FreelancerProfile(
        pricing=PricingConfig(strategy="markup", markup_percent=1.2, min_acceptable_rub=5000)
    )
    price = profile.calculate_price(10000, None)
    assert price == 12000


def test_pricing_markup_respects_floor() -> None:
    profile = FreelancerProfile(
        pricing=PricingConfig(strategy="markup", markup_percent=1.0, min_acceptable_rub=9000)
    )
    price = profile.calculate_price(4000, None)
    assert price == 9000


def test_pricing_llm_estimate_uses_hours() -> None:
    profile = FreelancerProfile(
        pricing=PricingConfig(
            strategy="llm_estimate", hourly_rate_rub=2000, min_acceptable_rub=5000
        )
    )
    price = profile.calculate_price(15000, 10)
    assert price == 20000


def test_pricing_llm_estimate_respects_floor() -> None:
    profile = FreelancerProfile(
        pricing=PricingConfig(
            strategy="llm_estimate", hourly_rate_rub=1000, min_acceptable_rub=8000
        )
    )
    price = profile.calculate_price(15000, 2)
    assert price == 8000


# --------------------------------------------------------------------------- #
# MessageGenerator.resolve_price
# --------------------------------------------------------------------------- #


def _generator(profile: FreelancerProfile | None) -> MessageGenerator:
    from freelance_bot.config import AppConfig, Secrets

    config = AppConfig()
    secrets = Secrets()
    return MessageGenerator(config, secrets, profile=profile)


def test_resolve_price_without_profile_is_none() -> None:
    gen = _generator(None)
    price, strategy = gen.resolve_price(make_job(), JobAnalysis())
    assert price is None
    assert strategy == "no-profile"


def test_resolve_price_minimum_strategy() -> None:
    profile = FreelancerProfile(pricing=PricingConfig(strategy="minimum", min_acceptable_rub=5000))
    gen = _generator(profile)
    price, strategy = gen.resolve_price(make_job(budget=20000), JobAnalysis())
    assert strategy == "minimum"
    assert price == 19000


def test_resolve_price_markup_strategy() -> None:
    profile = FreelancerProfile(
        pricing=PricingConfig(strategy="markup", markup_percent=1.15, min_acceptable_rub=5000)
    )
    gen = _generator(profile)
    price, strategy = gen.resolve_price(make_job(budget=20000), JobAnalysis())
    assert strategy == "markup"
    assert price == 23000


def test_resolve_price_llm_estimate_strategy() -> None:
    profile = FreelancerProfile(
        pricing=PricingConfig(
            strategy="llm_estimate", hourly_rate_rub=1500, min_acceptable_rub=5000
        )
    )
    gen = _generator(profile)
    analysis = JobAnalysis(estimated_hours=20)
    price, strategy = gen.resolve_price(make_job(), analysis)
    assert strategy == "llm-estimate"
    assert price == 30000


def test_resolve_price_llm_estimate_without_hours_falls_back() -> None:
    profile = FreelancerProfile(
        pricing=PricingConfig(
            strategy="llm_estimate", hourly_rate_rub=1500, min_acceptable_rub=5000
        )
    )
    gen = _generator(profile)
    price, strategy = gen.resolve_price(make_job(budget=12000), JobAnalysis())
    assert strategy == "llm-estimate-fallback"
    assert price == 12000


def test_price_is_acceptable_without_profile() -> None:
    gen = _generator(None)
    assert gen.price_is_acceptable(None) is True


def test_price_is_acceptable_below_floor() -> None:
    profile = FreelancerProfile(pricing=PricingConfig(strategy="minimum", min_acceptable_rub=10000))
    gen = _generator(profile)
    assert gen.price_is_acceptable(5000) is False
    assert gen.price_is_acceptable(15000) is True


# --------------------------------------------------------------------------- #
# Защита: признаки ИИ и шаблонные фразы
# --------------------------------------------------------------------------- #


def _style(**kwargs) -> StyleConfig:  # type: ignore[no-untyped-def]
    return StyleConfig(**kwargs)


def test_guard_flags_ai_tell() -> None:
    msg = (
        "Здравствуйте. Как опытный специалист, я уверен, что смогу выполнить эту задачу "
        "на Python с интеграцией Google Sheets и PostgreSQL. Какой у вас бюджет?"
    )
    violations = check(msg, make_job(), MessageLimit(max_chars=3000), _style())
    assert any("машинного текста" in v for v in violations)


def test_guard_flags_template_body_phrase() -> None:
    msg = (
        "Здравствуйте. Мой опыт и навыки позволяют выполнить эту задачу на Python "
        "с интеграцией Google Sheets и PostgreSQL. Какой бюджет закладываете?"
    )
    violations = check(msg, make_job(), MessageLimit(max_chars=3000), _style())
    assert any("шаблонная формулировка" in v for v in violations)


def test_guard_flags_filler() -> None:
    msg = (
        "Здравствуйте. В текущих реалиях задача с Telegram-ботом на Python и Google Sheets "
        "решается через PostgreSQL. Какой бюджет?"
    )
    violations = check(msg, make_job(), MessageLimit(max_chars=3000), _style())
    assert any("вода в тексте" in v for v in violations)


def test_guard_flags_profile_banned_phrase() -> None:
    msg = (
        "Здравствуйте. Готов предложить решение на Python с Google Sheets и PostgreSQL. "
        "Какой бюджет?"
    )
    violations = check(
        msg,
        make_job(),
        MessageLimit(max_chars=3000),
        _style(),
        extra_banned=["готов предложить"],
    )
    assert any("из профиля" in v for v in violations)


def test_guard_flags_numbered_list() -> None:
    msg = "Здравствуйте. Сделаю так: 1. бот 2. Sheets 3. PostgreSQL. Какой бюджет?"
    violations = check(msg, make_job(), MessageLimit(max_chars=3000), _style())
    assert any("нумерованный список" in v for v in violations)


def test_guard_flags_dash_overuse() -> None:
    msg = "Здравствуйте. Бот — Python — Sheets — PostgreSQL — команды — состояние. Какой бюджет?"
    violations = check(msg, make_job(), MessageLimit(max_chars=3000), _style())
    assert any("длинных тире" in v for v in violations)


def test_guard_accepts_human_message() -> None:
    msg = (
        "Здравствуйте. Задача с Telegram-ботом на Python, который тянет данные из "
        "Google Sheets и пишет в PostgreSQL, мне знакома. Соберу команды, состояние "
        "пользователей и обработку ошибок, чтобы при сбое видеть причину сразу. "
        "Какой бюджет закладываете на эту работу?"
    )
    violations = check(msg, make_job(), MessageLimit(max_chars=3000), _style())
    assert violations == [], violations


# --------------------------------------------------------------------------- #
# Запасное сообщение использует профиль
# --------------------------------------------------------------------------- #


def test_fallback_mentions_matched_skills() -> None:
    profile = FreelancerProfile(
        skills=["Python", "PostgreSQL", "Telegram Bot API"],
        pricing=PricingConfig(strategy="minimum", min_acceptable_rub=1000),
    )
    gen = _generator(profile)
    job = make_job()
    analysis = gen._heuristic_analysis(job)
    text = gen._fallback_message(job, analysis)
    assert "Python" in text
    assert "?" in text


def test_fallback_has_no_exclamation_marks() -> None:
    gen = _generator(FreelancerProfile())
    job = make_job()
    text = gen._fallback_message(job, gen._heuristic_analysis(job))
    assert "!" not in text


def test_fallback_strips_domains() -> None:
    gen = _generator(FreelancerProfile())
    job = make_job(
        title="Нужен бот для example.com",
        description="Пишем на Python, выгружаем в csv.",
    )
    text = gen._fallback_message(job, gen._heuristic_analysis(job))
    assert "example.com" not in text


# --------------------------------------------------------------------------- #
# Стиль, задаваемый профилем
# --------------------------------------------------------------------------- #


def test_profile_style_banned_phrases_flow_into_guard() -> None:
    profile = FreelancerProfile(message_style=MessageStyleConfig(banned_phrases=["под ключ"]))
    gen = _generator(profile)
    assert "под ключ" in gen._extra_banned


def test_profile_availability_defaults() -> None:
    cfg = AvailabilityConfig()
    assert cfg.immediate_start is True
    assert cfg.preferred_duration == "any"


@pytest.mark.parametrize("strategy", ["minimum", "markup", "llm_estimate"])
def test_all_strategies_produce_a_price(strategy: str) -> None:
    profile = FreelancerProfile(pricing=PricingConfig(strategy=strategy, min_acceptable_rub=1000))
    gen = _generator(profile)
    price, label = gen.resolve_price(make_job(budget=10000), JobAnalysis(estimated_hours=5))
    assert price is not None
    assert price >= 1000
    assert label


# --------------------------------------------------------------------------- #
# Области навыков
# --------------------------------------------------------------------------- #


def test_expand_skill_splits_separators() -> None:
    assert "html" in expand_skill("HTML/CSS")
    assert "css" in expand_skill("HTML/CSS")
    assert "html/css" in expand_skill("HTML/CSS")


def test_expand_skill_drops_generic_api_suffix() -> None:
    variants = expand_skill("Telegram Bot API")
    assert "telegram bot api" in variants
    assert "telegram bot" in variants
    assert "telegram" in variants


def test_expand_skill_keeps_short_api_skill_intact() -> None:
    variants = expand_skill("REST API")
    assert "rest api" in variants
    assert "rest" not in variants


def test_skill_domains_web_and_backend() -> None:
    assert "web" in skill_domains("HTML/CSS")
    assert "web" in skill_domains("React")
    assert "backend" in skill_domains("Django")
    assert "telegram" in skill_domains("Telegram Bot API")


def test_skill_domains_python_is_backend_and_telegram() -> None:
    domains = skill_domains("Python")
    assert "backend" in domains
    assert "telegram" in domains
    assert "web" not in domains


# --------------------------------------------------------------------------- #
# Выбор навыков с учётом области
# --------------------------------------------------------------------------- #


def _web_profile() -> FreelancerProfile:
    return FreelancerProfile(
        skills=["Python", "Django", "FastAPI", "PostgreSQL", "Telegram Bot API"],
        secondary_skills=["JavaScript", "React", "Vue.js", "HTML/CSS", "Google Sheets API"],
        pricing=PricingConfig(strategy="minimum", min_acceptable_rub=1000),
    )


def test_web_job_mentions_web_skills_not_backend() -> None:
    """Заказ на вёрстку должен представлять веб-разработчика, а не бэкендера."""
    gen = _generator(_web_profile())
    job = Job(
        platform="fl_ru",
        external_id="2",
        url="https://www.fl.ru/projects/2/x.html",
        title="Сверстать лендинг",
        description="Нужен адаптивный лендинг на HTML и CSS, есть макет в Figma.",
        budget_amount=12000,
        budget_rub_estimate=12000,
        currency="RUB",
        # Площадка пометила его python; это не должно побеждать.
        skills=["python", "веб-дизайн"],
    )
    domain, _ = gen._domain_and_approach(job)
    assert domain == "web"

    matched = gen._matched_skills(job)
    assert "HTML/CSS" in matched
    assert "JavaScript" in matched
    assert "Python" not in matched
    assert "Django" not in matched
    assert "FastAPI" not in matched


def test_web_job_letter_names_web_skills() -> None:
    gen = _generator(_web_profile())
    job = Job(
        platform="fl_ru",
        external_id="3",
        url="https://www.fl.ru/projects/3/x.html",
        title="Сверстать лендинг",
        description="Нужен адаптивный лендинг на HTML и CSS, есть макет в Figma.",
        budget_amount=12000,
        budget_rub_estimate=12000,
        currency="RUB",
        skills=["python"],
    )
    text = gen._fallback_message(job, gen._heuristic_analysis(job))
    assert "HTML/CSS" in text
    assert "Python" not in text


def test_telegram_job_mentions_bot_skills() -> None:
    gen = _generator(_web_profile())
    job = Job(
        platform="fl_ru",
        external_id="4",
        url="https://www.fl.ru/projects/4/x.html",
        title="Разработка Telegram-бота",
        description="Нужен бот на Python с интеграцией Google Sheets.",
        budget_amount=15000,
        budget_rub_estimate=15000,
        currency="RUB",
        skills=["python", "telegram"],
    )
    domain, _ = gen._domain_and_approach(job)
    assert domain == "telegram"
    matched = gen._matched_skills(job)
    assert "Telegram Bot API" in matched
    assert "Python" in matched


def test_parsing_job_does_not_claim_web_skills() -> None:
    gen = _generator(_web_profile())
    job = Job(
        platform="fl_ru",
        external_id="5",
        url="https://www.fl.ru/projects/5/x.html",
        title="Спарсить базу дизайнеров",
        description="Нужно собрать контакты дизайнеров с сайтов, выгрузка в CSV.",
        budget_amount=8000,
        budget_rub_estimate=8000,
        currency="RUB",
        skills=["python", "парсинг"],
    )
    domain, _ = gen._domain_and_approach(job)
    assert domain == "parsing"
    matched = gen._matched_skills(job)
    assert "HTML/CSS" not in matched
    assert "React" not in matched


def test_backend_job_prefers_backend_skills() -> None:
    gen = _generator(_web_profile())
    job = Job(
        platform="fl_ru",
        external_id="6",
        url="https://www.fl.ru/projects/6/x.html",
        title="Backend на Django",
        description="Нужен REST API на Django с PostgreSQL.",
        budget_amount=30000,
        budget_rub_estimate=30000,
        currency="RUB",
        skills=["django", "python"],
    )
    matched = gen._matched_skills(job)
    assert "Django" in matched
    assert "React" not in matched


# --------------------------------------------------- письмо о той же работе
def test_site_job_is_not_mistaken_for_bot() -> None:
    """Заказ на сайт, где в описании упомянут бот, остаётся веб-заказом.

    «Найти клиента на разработку сайта или автоматизации» упоминает бота в
    теле объявления. Раньше побеждало первое совпадение по склеенному
    тексту, и письмо предлагало сделать Telegram-бота вместо сайта.
    """
    gen = _generator(_web_profile())
    job = Job(
        platform="kwork_ru",
        external_id="10",
        url="https://kwork.ru/projects/10",
        title="Найти клиента на разработку сайта или автоматизации",
        description="Ищу партнёра, который найдёт клиента на разработку сайта, "
        "интернет-магазина, приложения, бота либо автоматизации.",
        budget_amount=50000,
        budget_rub_estimate=50000,
        currency="RUB",
    )
    domain, _ = gen._domain_and_approach(job)
    assert domain == "web"

    text = gen._fallback_message(job, gen._heuristic_analysis(job))
    # Цитата описания может содержать слово «бота» — важно, что предлагается
    # не бот, а сайт.
    assert "сделаю бота" not in text.lower()
    assert "сделаю telegram" not in text.lower()
    assert "структура страниц" in text.lower()


def test_bot_job_stays_bot_when_site_mentioned() -> None:
    """Обратный случай: заказ на ботов не уезжает в веб из-за слова «сайт»."""
    gen = _generator(_web_profile())
    job = Job(
        platform="kwork_ru",
        external_id="11",
        url="https://kwork.ru/projects/11",
        title="Разработка чат-ботов в Telegram и MAX",
        description="Настроить чат-ботов для отправки материалов после "
        "заполнения формы на сайте.",
        budget_amount=20000,
        budget_rub_estimate=20000,
        currency="RUB",
    )
    domain, _ = gen._domain_and_approach(job)
    assert domain == "telegram"


def test_fallback_message_states_readiness_to_take_job() -> None:
    """Письмо прямо говорит о готовности взяться, а не рассуждает о теме."""
    gen = _generator(_web_profile())
    job = Job(
        platform="kwork_ru",
        external_id="12",
        url="https://kwork.ru/projects/12",
        title="Разработка сайта автономного без конструктора",
        description="Нужно разработать коммерческий многоязычный сайт компании с нуля.",
        budget_amount=80000,
        budget_rub_estimate=80000,
        currency="RUB",
    )
    text = gen._fallback_message(job, gen._heuristic_analysis(job))
    assert "готов взяться" in text.lower()
    assert "сайт" in text.lower()


def test_guard_rejects_letter_about_wrong_work() -> None:
    """Проверка ловит письмо про бота в заказе на сайт."""
    site = Job(
        platform="kwork_ru",
        external_id="13",
        url="https://kwork.ru/projects/13",
        title="Разработка сайта для компании",
        description="Нужен коммерческий сайт компании с нуля, без конструктора.",
        budget_amount=80000,
        budget_rub_estimate=80000,
        currency="RUB",
    )
    violations = check(
        "Готов взяться именно за это: сделаю Telegram-бота, команды и хранение "
        "состояния, обработка ошибок и запуск на вашем сервере.",
        site,
        MessageLimit(),
        StyleConfig(),
    )
    assert any("не о том" in v for v in violations)


def test_guard_allows_site_letter_with_bot_mention() -> None:
    """Сайт с ботом — не ошибка: заказчик сам просит оба."""
    site = Job(
        platform="kwork_ru",
        external_id="14",
        url="https://kwork.ru/projects/14",
        title="Разработка сайта с Telegram-ботом",
        description="Нужен сайт компании и бот для приёма заявок.",
        budget_amount=90000,
        budget_rub_estimate=90000,
        currency="RUB",
    )
    violations = check(
        "Готов взяться: сделаю сайт со структурой страниц и адаптивной "
        "вёрсткой, а заявки будут приходить в Telegram-бота.",
        site,
        MessageLimit(),
        StyleConfig(),
    )
    assert not any("не о том" in v for v in violations)


def test_text_job_for_site_is_text_not_web() -> None:
    """«Тексты для сайта» — работа про тексты, хотя в заголовке есть «сайт».

    Без веса начала заголовка побеждало слово «сайт», и копирайтеру
    предлагали вёрстку.
    """
    gen = _generator(_web_profile())
    for title in (
        "Написание текстов для страниц услуг медицинского сайта",
        "Тексты для сайта кадрового агентства",
    ):
        job = Job(
            platform="kwork_ru",
            external_id="15",
            url="https://kwork.ru/projects/15",
            title=title,
            description="Готовим корпоративный сайт, нужны тексты для страниц.",
            budget_amount=15000,
            budget_rub_estimate=15000,
            currency="RUB",
        )
        domain, _ = gen._domain_and_approach(job)
        assert domain == "text", title


def test_site_job_with_texts_stays_web() -> None:
    """Обратный порядок слов: «Разработка сайта с текстами» — это сайт."""
    gen = _generator(_web_profile())
    job = Job(
        platform="kwork_ru",
        external_id="16",
        url="https://kwork.ru/projects/16",
        title="Разработка сайта с текстами",
        description="Нужно сделать сайт и наполнить его текстами.",
        budget_amount=60000,
        budget_rub_estimate=60000,
        currency="RUB",
    )
    domain, _ = gen._domain_and_approach(job)
    assert domain == "web"


def test_design_job_is_design_not_web() -> None:
    """Дизайн сайта — дизайн, а не вёрстка: предлагаем макет, а не код."""
    gen = _generator(_web_profile())
    job = Job(
        platform="kwork_ru",
        external_id="17",
        url="https://kwork.ru/projects/17",
        title="Дизайн сайта кадрового агентства с анимацией и 3D",
        description="Нужен дизайн корпоративного сайта, есть структура.",
        budget_amount=40000,
        budget_rub_estimate=40000,
        currency="RUB",
    )
    domain, _ = gen._domain_and_approach(job)
    assert domain == "design"


def test_skill_domains_does_not_match_inside_other_words() -> None:
    """Короткие маркеры не должны находиться внутри чужих слов.

    ``ux`` находится внутри ``linux``, ``ml`` — внутри ``html``. Из-за этого
    навык «Linux администрирование» уезжал в дизайн, и в заказе на дизайн
    сайта письмо хвалилось администрированием Linux.
    """
    assert "design" not in skill_domains("Linux администрирование")
    assert "ai" not in skill_domains("HTML/CSS")
    assert "devops" in skill_domains("Linux администрирование")
    assert "web" in skill_domains("HTML/CSS")
