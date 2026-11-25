"""Генерация сообщений: путь через LLM (замокан), путь исправления,
детерминированный запасной вариант."""

from __future__ import annotations

import json

import pytest

from conftest import make_job
from freelance_bot.config import AppConfig, MessageLimit, PlatformConfig, Secrets
from freelance_bot.llm.message import MessageGenerator

JOB = make_job(platform="fl_ru")


def generator(config: AppConfig | None = None, **secret_overrides) -> MessageGenerator:
    cfg = config or AppConfig(
        platforms={
            "fl_ru": PlatformConfig(listing_url="https://www.fl.ru/projects/", transport="http")
        },
        messaging={"limits": {"fl_ru": MessageLimit(max_chars=1000)}},
    )
    secrets = Secrets(**secret_overrides)
    return MessageGenerator(cfg, secrets)


@pytest.mark.asyncio
async def test_fallback_when_no_llm_key() -> None:
    gen = generator(llm_api_key="")
    analysis, message = await gen.generate(JOB)
    assert message.source == "fallback"
    assert message.text
    assert "парсер" in message.text.lower() or "каталог" in message.text.lower()
    assert analysis.need_summary_ru


@pytest.mark.asyncio
async def test_llm_path_uses_model_output(monkeypatch) -> None:
    gen = generator(llm_api_key="test-key")

    async def fake_complete_json(system, user):
        return {
            "need_summary_ru": "Нужен парсер каталога с выгрузкой в PostgreSQL.",
            "requirements": ["Python", "PostgreSQL"],
            "deliverables": ["Скрипт парсера"],
            "risks": [],
            "questions_to_ask": ["Нужна ли дедупликация?"],
            "estimated_hours": 12,
            "suggested_price_rub": 24000,
            "first_message": (
                "Здравствуйте! По парсеру каталога: сделаю выгрузку товаров в PostgreSQL "
                "и настрою ежедневный запуск по расписанию. Уточните, нужна ли дедупликация "
                "карточек при повторном сборе?"
            ),
        }

    monkeypatch.setattr(gen.client, "complete_json", fake_complete_json)
    analysis, message = await gen.generate(JOB)
    assert message.source == "llm"
    assert message.model
    assert analysis.estimated_hours == 12
    assert message.violations == []


@pytest.mark.asyncio
async def test_llm_output_with_link_is_repaired_or_replaced(monkeypatch) -> None:
    gen = generator(llm_api_key="test-key")

    async def bad_json(system, user):
        return {
            "need_summary_ru": "Парсер каталога.",
            "first_message": "Здравствуйте! Вот мой сайт https://example.com, сделаю парсер.",
        }

    async def repair(system, user, **kwargs):
        return (
            "Здравствуйте! По парсеру каталога: соберу товары в PostgreSQL и настрою "
            "ежедневный запуск по расписанию. Уточните, нужна ли дедупликация карточек?"
        )

    monkeypatch.setattr(gen.client, "complete_json", bad_json)
    monkeypatch.setattr(gen.client, "complete", repair)
    _, message = await gen.generate(JOB)
    assert "https://" not in message.text
    assert message.violations == []


@pytest.mark.asyncio
async def test_llm_failure_falls_back(monkeypatch) -> None:
    gen = generator(llm_api_key="test-key")

    async def boom(system, user):
        raise RuntimeError("provider down")

    monkeypatch.setattr(gen.client, "complete_json", boom)
    _, message = await gen.generate(JOB)
    assert message.source == "fallback"


@pytest.mark.asyncio
async def test_fallback_respects_platform_limit() -> None:
    cfg = AppConfig(
        platforms={
            "kwork_ru": PlatformConfig(listing_url="https://kwork.ru/projects", transport="browser")
        },
        messaging={"limits": {"kwork_ru": MessageLimit(max_chars=260)}},
    )
    gen = generator(cfg, llm_api_key="")
    job = make_job(platform="kwork_ru")
    _, message = await gen.generate(job)
    assert len(message.text) <= 260 or message.violations


@pytest.mark.asyncio
async def test_fallback_is_russian_and_specific() -> None:
    gen = generator(llm_api_key="")
    _, message = await gen.generate(JOB)
    assert sum(1 for c in message.text if "а" <= c.lower() <= "я") > 50
    assert "{" not in message.text and "}" not in message.text
    assert "None" not in message.text


@pytest.mark.asyncio
async def test_fallback_uses_task_appropriate_approach() -> None:
    gen = generator(llm_api_key="")
    job = make_job(
        title="Нарезка Twitch-стримов",
        description="Нужно нарезать моменты из Twitch в короткие видео для TikTok и Reels.",
    )
    _, message = await gen.generate(job)
    assert "twitch" in message.text.lower()
    assert "telegram-бота" not in message.text.lower()
    assert "сборщик данных" not in message.text.lower()


@pytest.mark.asyncio
async def test_fallback_mentions_parser_for_parser_job() -> None:
    gen = generator(llm_api_key="")
    _, message = await gen.generate(JOB)
    assert "источники, пагинация" in message.text.lower()
    assert "По заданию" not in message.text


@pytest.mark.asyncio
async def test_fallback_strips_salutation_from_source_brief() -> None:
    gen = generator(llm_api_key="")
    job = make_job(
        title="Вёрстка книги",
        description="Здравствуйте, у меня есть книга по Minecraft, она почти готова.",
    )
    _, message = await gen.generate(job)
    assert message.text.lower().count("здравствуйте") == 1


def test_prompts_forbid_copying_cliches_from_job() -> None:
    """Промпт запрещает копировать клише из заказа.

    Заказчик пишет «сайт под ключ», модель повторяла «под ключ» в письме —
    и guard блокировал отправку как шаблонную фразу. Просить «не копируй»
    дешевле, чем потом чинить каждое письмо.
    """
    from freelance_bot.llm.prompts import SYSTEM_PROMPT, analysis_prompt, repair_prompt

    assert "не копируй" in SYSTEM_PROMPT.lower()

    job = make_job(title="Создание сайта под ключ", description="Нужен сайт под ключ")
    prompt = analysis_prompt(job)
    assert "не копируй клише" in prompt.lower()

    repair = repair_prompt("Текст", ["шаблонная фраза: «под ключ»"], 1000)
    assert "не копируй" in repair.lower()


def test_clean_for_message_drops_dangling_parenthetical() -> None:
    text = "Разработка сервиса многоканальных рассылок (Email, SMS, Telegram, MAX, WhatsApp, VK)"
    cleaned = MessageGenerator._clean_for_message(text, 80)
    assert cleaned.count("(") == cleaned.count(")")
    assert cleaned == "Разработка сервиса многоканальных рассылок"


@pytest.mark.asyncio
async def test_analysis_json_is_parsed_loosely(monkeypatch) -> None:
    from freelance_bot.llm.client import parse_json_loose

    wrapped = "```json\n" + json.dumps({"need_summary_ru": "тест"}, ensure_ascii=False) + "\n```"
    assert parse_json_loose(wrapped)["need_summary_ru"] == "тест"
    assert parse_json_loose('Вот результат: {"a": 1}')["a"] == 1


def test_analysis_prompt_contains_whole_job_description() -> None:
    """В промпт уходит описание целиком, а не первые 4000 символов.

    kwork часто пишет длинное вступление про компанию, а суть — в конце.
    Обрезанное описание приводило к письму не о той работе.
    """
    from freelance_bot.llm.prompts import analysis_prompt

    tail = "В конце ТЗ: нужен именно сайт-каталог с фильтрами, не бот."
    description = ("Вступление про компанию. " * 400) + tail
    job = make_job(title="Разработка сайта-каталога", description=description)
    prompt = analysis_prompt(job)
    assert tail in prompt
    assert len(prompt) > 4000


def test_analysis_prompt_names_job_domain() -> None:
    """Промпт явно называет область заказа, чтобы модель не уходила в другую."""
    from freelance_bot.llm.prompts import analysis_prompt
    from freelance_bot.profile import FreelancerProfile

    job = make_job(
        title="Разработка сайта компании",
        description="Нужен коммерческий сайт с нуля, без конструктора.",
    )
    prompt = analysis_prompt(job, FreelancerProfile(), domain="web")
    assert "ОБЛАСТЬ ЭТОГО ЗАКАЗА: web" in prompt
    assert "не должен получать предложение сделать Telegram-бота" in prompt
