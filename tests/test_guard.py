"""Защита сообщений: проходит только отправляемый русский текст."""

from __future__ import annotations

from conftest import make_job
from freelance_bot.config import MessageLimit, StyleConfig
from freelance_bot.llm.guard import check, cyrillic_ratio, specificity

LIMIT = MessageLimit(max_chars=1000, allow_links=False)
STYLE = StyleConfig(
    banned_phrases=["в современном мире", "как опытный"],
    max_emoji=1,
    max_exclamation=1,
    min_specificity_overlap=0.12,
)

JOB = make_job(
    title="Разработка парсера каталога на Python",
    description=(
        "Нужен парсер каталога товаров на Python с выгрузкой в PostgreSQL и "
        "ежедневным запуском по расписанию. Доступ к API заказчик предоставит."
    ),
)

GOOD = (
    "Здравствуйте! По задаче с парсером каталога: соберу выгрузку товаров в PostgreSQL "
    "и настрою ежедневный запуск по расписанию, доступ к API уже есть. "
    "Уточните, нужна ли дедупликация карточек при повторном сборе?"
)


def test_good_message_passes() -> None:
    assert check(GOOD, JOB, LIMIT, STYLE) == []


def test_link_is_rejected() -> None:
    violations = check(GOOD + " Портфолио: https://example.com", JOB, LIMIT, STYLE)
    assert any("ссылк" in v for v in violations)


def test_bare_domain_is_rejected() -> None:
    violations = check(GOOD + " Пишите на example.ru", JOB, LIMIT, STYLE)
    assert any("ссылк" in v for v in violations)


def test_length_limit_enforced() -> None:
    violations = check(GOOD * 5, JOB, MessageLimit(max_chars=500), STYLE)
    assert any("длина" in v for v in violations)


def test_banned_phrase_rejected() -> None:
    violations = check("В современном мире " + GOOD, JOB, LIMIT, STYLE)
    assert any("шаблонная" in v for v in violations)


def test_ai_tell_rejected() -> None:
    violations = check(GOOD + " Таким образом, задача решаема.", JOB, LIMIT, STYLE)
    assert any("машинного текста" in v for v in violations)


def test_generic_opener_rejected() -> None:
    violations = check(
        "Здравствуйте! Меня зовут Иван. Сделаю парсер каталога в PostgreSQL по расписанию.",
        JOB,
        LIMIT,
        STYLE,
    )
    assert any("шаблонное начало" in v for v in violations)


def test_emoji_limit() -> None:
    violations = check(GOOD + " 🚀🔥💯", JOB, LIMIT, STYLE)
    assert any("эмодзи" in v for v in violations)


def test_exclamation_limit() -> None:
    violations = check(GOOD + " Очень жду ответа!!!", JOB, LIMIT, STYLE)
    assert any("восклицательных" in v for v in violations)


def test_non_russian_text_rejected() -> None:
    violations = check(
        "Hello! I can build your parser in Python with PostgreSQL.", JOB, LIMIT, STYLE
    )
    assert any("русского" in v for v in violations)


def test_vague_message_rejected_for_low_specificity() -> None:
    violations = check(
        "Здравствуйте! Готов выполнить вашу задачу быстро и качественно. Напишите, обсудим детали.",
        JOB,
        LIMIT,
        STYLE,
    )
    assert any("конкретики" in v for v in violations)


def test_markdown_list_rejected() -> None:
    violations = check(GOOD + "\n- пункт один\n- пункт два", JOB, LIMIT, STYLE)
    assert any("список" in v for v in violations)


def test_many_questions_rejected() -> None:
    violations = check(GOOD + " А какой стек? А какие сроки? А какой бюджет?", JOB, LIMIT, STYLE)
    assert any("вопросов" in v for v in violations)


def test_empty_message_rejected() -> None:
    assert check("   ", JOB, LIMIT, STYLE) == ["пустое сообщение"]


def test_cyrillic_ratio() -> None:
    assert cyrillic_ratio("Привет") == 1.0
    assert cyrillic_ratio("Hello") == 0.0
    assert 0 < cyrillic_ratio("Python парсер") < 1


def test_specificity_is_higher_for_specific_text() -> None:
    specific = specificity(GOOD, JOB)
    generic = specificity("Готов выполнить задачу быстро и качественно.", JOB)
    assert specific > generic


def test_cliche_from_job_title_is_not_flagged() -> None:
    """Клише из самого заказа не считается шаблоном бота.

    Заказ называется «Сайт под ключ». Письмо обязано назвать его так же,
    иначе непонятно, о чём оно. Раньше проверка блокировала такой текст,
    и заказ уходил в чат вообще без письма.
    """
    job = make_job(
        title="Редизайн сайта под ключ",
        description="Нужен редизайн сайта под ключ, сохранив платформу и контент.",
    )
    message = (
        "Здравствуйте. Увидел задачу «Редизайн сайта под ключ». Сохраню платформу "
        "и контент, поменяю дизайн и шаблон. Что уже есть по структуре страниц?"
    )
    assert check(message, job, LIMIT, STYLE) == []


def test_cliche_absent_from_job_is_still_flagged() -> None:
    """Обратная сторона: в чужом заказе «под ключ» остаётся шаблоном."""
    job = make_job(title="Лендинг", description="Нужен лендинг для онлайн-курса.")
    violations = check("Сделаю сайт под ключ, всё под ключ и быстро.", job, LIMIT, STYLE)
    assert any("под ключ" in v for v in violations)
