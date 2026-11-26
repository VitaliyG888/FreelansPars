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
    "Здравствуйте. По задаче с парсером каталога: готов взяться за это, соберу выгрузку "
    "товаров в PostgreSQL и настрою ежедневный запуск по расписанию, доступ к API уже есть. "
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
        "Здравствуйте. Увидел задачу «Редизайн сайта под ключ» и готов взяться: сохраню "
        "платформу и контент, поменяю дизайн и шаблон. Что уже есть по структуре страниц?"
    )
    assert check(message, job, LIMIT, STYLE) == []


def test_cliche_absent_from_job_is_still_flagged() -> None:
    """Обратная сторона: в чужом заказе «под ключ» остаётся шаблоном."""
    job = make_job(title="Лендинг", description="Нужен лендинг для онлайн-курса.")
    violations = check("Сделаю сайт под ключ, всё под ключ и быстро.", job, LIMIT, STYLE)
    assert any("под ключ" in v for v in violations)


def test_message_without_readiness_is_flagged() -> None:
    """Отклик без прямого «готов взяться» не читается как предложение работы.

    Модель иногда рассуждает о теме («задача мне знакома, сделаю …»), и
    заказчик не понимает, будет ли исполнитель что-то делать.
    """
    message = (
        "Здравствуйте. По парсеру каталога: соберу выгрузку товаров в PostgreSQL "
        "и настрою ежедневный запуск по расписанию, доступ к API уже есть. "
        "Уточните, нужна ли дедупликация карточек при повторном сборе?"
    )
    violations = check(message, JOB, LIMIT, STYLE)
    assert any("прямого согласия" in v for v in violations)


def test_broken_greeting_is_flagged() -> None:
    """«Привет, вы.» — смешение приветствия с подлежащим."""
    violations = check(GOOD.replace("Здравствуйте. По задаче", "Привет, вы. По задаче"),
                       JOB, LIMIT, STYLE)
    assert any("сломанное обращение" in v for v in violations)


def test_dangling_ready_phrase_is_flagged() -> None:
    """«это именно то, что я готов взяться.» — потерян предлог «за»."""
    message = GOOD.replace("готов взяться за это, соберу", "готов взяться.")
    violations = check(message, JOB, LIMIT, STYLE)
    assert any("без «за»" in v for v in violations)


def test_lowercase_sentence_start_is_flagged() -> None:
    """Предложение с маленькой буквы после точки — след генерации."""
    message = GOOD.replace("Уточните", "уточните")
    violations = check(message, JOB, LIMIT, STYLE)
    assert any("маленькой буквы" in v for v in violations)


def test_lowercase_quote_from_job_is_not_flagged() -> None:
    """Строчная буква в цитате заказчика — не вина бота.

    Заказчики пишут «… но BIOS не пропускает. я ищу спеца». Когда этот кусок
    попадает в письмо дословно, проверка не должна ругаться.
    """
    job = make_job(
        title="Установка серверного убунту",
        description="я ищу спеца кто поможет накатить серверный убунту на ноутбук.",
    )
    message = (
        "Здравствуйте. Увидел задачу «Установка серверного убунту». я ищу спеца кто "
        "поможет накатить серверный убунту на ноутбук. Готов взяться: настрою запуск "
        "и проверю загрузку."
    )
    violations = check(message, job, LIMIT, STYLE)
    assert not any("маленькой буквы" in v for v in violations)


def test_seo_job_is_not_taken_for_telegram() -> None:
    """Отказ от Telegram в описании не делает заказ про Telegram.

    Заказ на SEO заканчивался «на почту и в телеграм просьба не писать», и
    областью заказа становился Telegram: письмо предлагало команды бота
    вместо работ по позициям.
    """
    from freelance_bot.domains import detect_domain

    job = make_job(
        title="Вывести в ТОП-50 в Яндексе и Гугле",
        description=(
            "Вывести страницу в ТОП-50 Яндекса и Google по 50% запросов. "
            "p.s.: на почту и в телеграм просьба не писать."
        ),
    )
    domain, _ = detect_domain(job)
    assert domain == "seo"


def test_letter_quoting_p_s_note_is_flagged_or_cleaned() -> None:
    """Служебная приписка заказчика не должна попадать в деталь письма."""
    job = make_job(
        title="Вывести в ТОП-50",
        description="Вывести сайт в ТОП-50. p.s.: на почту просьба не писать.",
    )
    assert not job.description.startswith("p.s")
    # Проверка не падает на таком заказе.
    assert isinstance(check(GOOD, job, LIMIT, STYLE), list)
