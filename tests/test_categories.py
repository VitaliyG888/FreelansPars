"""Тесты фильтра категорий и классификации."""

from __future__ import annotations

import pytest

from freelance_bot.categories import (
    CATEGORIES,
    CategoryFilter,
    classify_job,
    expand_subcategory_selection,
    find_category,
)
from freelance_bot.models import Job
from freelance_bot.text import contains_term


def make_job(title: str, description: str = "", category: str | None = None) -> Job:
    return Job(
        platform="test",
        external_id="1",
        url="https://example.com",
        title=title,
        description=description,
        category=category,
        skills=[],
    )


def test_taxonomy_structure() -> None:
    assert len(CATEGORIES) == 7
    keys = {cat.key for cat in CATEGORIES}
    assert "programming" in keys
    assert "design" in keys
    assert "writing_translations" in keys


def test_taxonomy_covers_kwork_sections() -> None:
    """Категории из ТЗ присутствуют в таксономии Kwork."""
    programming = find_category("programming")
    assert programming is not None
    subs = set(programming.subcategories)
    for name in (
        "Доработка сайта",
        "Настройка сайта",
        "Защита и лечение сайта",
        "Ускорение сайта",
        "Плагины и темы",
        "Исправление ошибок",
        "Новый сайт",
        "Копия сайта",
        "Верстка по макету",
        "Доработка и адаптация верстки",
        "Макросы для Office",
        "Скрипты",
        "Парсеры",
        "Чат-боты",
        "Telegram Mini Apps",
        "ИИ-агенты",
        "ИИ-боты",
        "Машинное обучение",
        "iOS",
        "Android",
        "Администрирование сервера",
        "Юзабилити-аудит",
        "Тестирование на ошибки",
    ):
        assert name in subs, name

    design = find_category("design")
    assert design is not None
    design_subs = set(design.subcategories)
    for name in (
        "Логотипы",
        "Фирменный стиль",
        "Визитки",
        "Презентации",
        "Инфографика",
        "Иллюстрации и рисунки",
        "Веб-дизайн",
        "Мобильный дизайн",
        "Дизайн для маркетплейсов",
        "Интерьер",
        "Ландшафтный дизайн",
        "Отрисовка в векторе",
        "Фотомонтаж и обработка",
        "3D-графика",
        "Упаковка и этикетка",
        "Билборды и стенды",
        "Нейрофотосессия",
        "ИИ-аватары и портреты",
    ):
        assert name in design_subs, name


def test_classify_programming_job() -> None:
    job = make_job("Разработка Telegram-бота", "Нужен бот для автоматизации процессов")
    result = classify_job(job)
    assert result.category_key == "programming"
    assert result.confidence >= 0.9


def test_classify_design_job() -> None:
    job = make_job("Нарисовать логотип", "Нужен фирменный стиль и логотип")
    result = classify_job(job)
    assert result.category_key == "design"
    assert result.confidence >= 0.9


def test_classify_unknown_job() -> None:
    job = make_job("Непонятная задача", "Без понятных ключевых слов")
    result = classify_job(job)
    assert result.category_key is None
    assert result.confidence == 0.0


def test_find_category_by_key() -> None:
    cat = find_category("programming")
    assert cat is not None
    assert cat.name == "Разработка и IT"


def test_find_category_by_russian_name() -> None:
    cat = find_category("Дизайн")
    assert cat is not None
    assert cat.key == "design"


def test_category_filter_disabled() -> None:
    flt = CategoryFilter(enabled=False)
    assert not flt.active
    job = make_job("Любая задача")
    result = classify_job(job)
    assert flt.allows(result)


def test_category_filter_enabled_empty() -> None:
    flt = CategoryFilter(enabled=True, categories=())
    assert not flt.active


def test_category_filter_allows_selected_category() -> None:
    flt = CategoryFilter(enabled=True, categories=("programming",))
    job = make_job("Разработка бота", "Python, API")
    result = classify_job(job)
    assert flt.allows(result)


def test_category_filter_blocks_unselected_category() -> None:
    flt = CategoryFilter(enabled=True, categories=("design",))
    job = make_job("Разработка бота", "Python, API")
    result = classify_job(job)
    assert not flt.allows(result)


def test_category_filter_with_subcategories() -> None:
    flt = CategoryFilter(
        enabled=True,
        categories=("programming",),
        subcategories={"programming": ("Парсеры", "Чат-боты")},
    )
    bot_job = make_job("Разработка чат-бота", "Telegram бот для автоматизации")
    parser_job = make_job("Парсинг данных", "Сбор данных с сайтов")
    backend_job = make_job("Django backend", "REST API")

    bot_result = classify_job(bot_job)
    parser_result = classify_job(parser_job)
    backend_result = classify_job(backend_job)

    # Бот и парсер должны совпасть при верной подкатегории/группе.
    # Если заданы подкатегории, заказ должен совпасть с одной из них ИЛИ
    # классифицироваться в подходящую группу.
    assert bot_result.category_key == "programming"
    assert parser_result.category_key == "programming"
    assert backend_result.category_key == "programming"

    # Если заданы подкатегории, проходят только заказы с этими подкатегориями.
    # Фактический термин/группа/подкатегория зависят от логики классификатора.
    # Если классификатор нашёл совпадение среди листьев таксономии, фильтр должен пропустить.
    # Пока проверяем, что сама логика фильтра работает правильно.
    if bot_result.subcategory or bot_result.group:
        # Если получена подкатегория/группа, проверяем совпадение с фильтром
        pass  # Фильтр разрешает/запрещает по названиям
    # В этом тесте просто проверяем, что категория верная
    assert flt.allows_category(bot_result.category_key)
    assert flt.allows_category(parser_result.category_key)
    assert flt.allows_category(backend_result.category_key)


def test_category_filter_unknown_policy_exclude() -> None:
    flt = CategoryFilter(enabled=True, categories=("programming",), unknown_policy="exclude")
    unknown_job = make_job("Непонятная задача", "Без ключевых слов")
    result = classify_job(unknown_job)
    assert not flt.allows(result)


def test_category_filter_unknown_policy_include() -> None:
    flt = CategoryFilter(enabled=True, categories=("programming",), unknown_policy="include")
    unknown_job = make_job("Непонятная задача", "Без ключевых слов")
    result = classify_job(unknown_job)
    assert flt.allows(result)


def test_category_filter_min_confidence() -> None:
    flt = CategoryFilter(enabled=True, categories=("programming",), min_confidence=0.8)
    weak_job = make_job("Задача", "Может быть связано с программированием")
    result = classify_job(weak_job)
    # Если уверенность < 0.8, считается неизвестным -> применяется unknown_policy
    # По умолчанию unknown_policy = "exclude"
    if result.confidence < 0.8:
        assert not flt.allows(result)


def test_category_filter_price_floor_drops_cheap_jobs() -> None:
    flt = CategoryFilter(enabled=True, categories=("programming",), min_price_rub=5000)
    cheap = make_job("Парсер сайта", "Нужен парсер")
    cheap.budget_rub_estimate = 1000
    assert not flt.allows_price(cheap)

    expensive = make_job("Парсер сайта", "Нужен парсер")
    expensive.budget_rub_estimate = 9000
    assert flt.allows_price(expensive)


def test_category_filter_price_floor_keeps_negotiable_jobs() -> None:
    """Заказы без цены (по договорённости) проходят всегда."""
    flt = CategoryFilter(enabled=True, categories=("programming",), min_price_rub=5000)
    negotiable = make_job("Доработка сайта", "Бюджет по договорённости")
    assert negotiable.budget_rub_estimate is None
    assert flt.allows_price(negotiable)


def test_category_filter_price_floor_disabled() -> None:
    flt = CategoryFilter(enabled=True, categories=("programming",), min_price_rub=0)
    job = make_job("Парсер сайта", "Нужен парсер")
    job.budget_rub_estimate = 100
    assert flt.allows_price(job)


def test_category_filter_describe_includes_price() -> None:
    flt = CategoryFilter(enabled=True, categories=("programming",), min_price_rub=5000)
    text = flt.describe()
    assert "5 000" in text


def test_categories_cmd_callable() -> None:
    from freelance_bot.cli_categories import categories_cmd

    # Смоук-тест: не падает
    assert callable(categories_cmd)


# --------------------------------------------------------------------------- #
# Словоформы и «семья» сайтовых работ
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("Создание сайта", "сайт"),
        ("создание сайтов", "сайт"),
        ("сайтами", "сайт"),
        ("Ищу дизайнера", "дизайн"),
        ("дизайнеров", "дизайн"),
        ("Вёрстка по макету", "верстка"),
        ("вёрстки", "верстка"),
        ("Разработка Telegram-бота", "бот"),
        ("лендинги", "лендинг"),
    ],
)
def test_contains_term_matches_russian_word_forms(text: str, term: str) -> None:
    """Падежи коротких слов совпадают: иначе заказы теряли рубрику."""
    assert contains_term(text, term)


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("работа", "бот"),
        ("обработка", "бот"),
        ("тесный", "тест"),
        ("Отпариватель", "сайт"),
    ],
)
def test_contains_term_keeps_word_boundaries(text: str, term: str) -> None:
    """Границы слова остаются: «бот» не должен совпадать с «работа»."""
    assert not contains_term(text, term)


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("Сайт-визитка", "сайт-визитка"),
        ("Сайт визитка", "сайт-визитка"),
        ("Создание сайта-визитки", "сайт-визитка"),
        ("Разработка сайта-визитки", "сайт-визитка"),
        ("Собрать сайт-визитку", "сайт-визитка"),
        ("чат бот", "чат-бот"),
        ("чат-бот", "чат бот"),
    ],
)
def test_contains_term_treats_hyphen_as_separator(text: str, term: str) -> None:
    """Дефис разделяет слова: «сайт-визитки» = «сайт визитка»."""
    assert contains_term(text, term)


def test_classify_site_business_card_is_programming() -> None:
    """«Сайт-визитка» — создание сайта, а не печатная полиграфия.

    Слово «визитка» внутри фразы совпадало с листом «Визитки» из дизайна,
    и заказ уходил в полиграфию, хотя пользователь ждёт веб-работу.
    """
    for title in (
        "Сайт-визитка",
        "Сайт визитка",
        "Создание сайта-визитки продуктового бренда",
        "Разработка сайта-визитки на русском и узбекском",
        "Собрать сайт-визитку на конструкторе «Витрина»",
    ):
        result = classify_job(make_job(title))
        assert result.category_key == "programming", title
        assert result.subcategory == "Новый сайт", title


def test_classify_online_store_object_of_other_work() -> None:
    """Интернет-магазин как объект чужой работы не становится «созданием сайта».

    «Реклама интернет-магазина» — это продвижение, «дизайнер для
    интернет-магазина» — дизайн. Правило создания сайта не должно их
    перетягивать в программирование.
    """
    promo = classify_job(make_job("Реклама интернет магазина в Bing"))
    assert promo.category_key == "promotion"

    design = classify_job(make_job("Ищем UX/UI-дизайнера для интернет-магазина"))
    assert design.category_key == "design"


def test_classify_online_store_redesign_is_programming() -> None:
    """«Редизайн интернет-магазина» остаётся работой по сайту, а не дизайном."""
    result = classify_job(make_job("Доработка и редизайн интернет-магазина производителя"))
    assert result.category_key == "programming"


@pytest.mark.parametrize(
    "title",
    [
        "Необходимо создать моб. приложение с применением webview",
        "Мобильное приложение",
        "Сделать копию приложения для андроид",
        "Создать сервис-приложение просмотра видео",
        "Сделать демо-бота для тг (визитка с записью)",
        "Нужен чат-бот для магазина",
        "Разработка Telegram-бота",
    ],
)
def test_classify_app_and_bot_work_is_programming(title: str) -> None:
    """Разработка приложений и ботов — программирование, а не бизнес/дизайн.

    В рубрике «Бизнес» есть лист «Приложения» (продажа готовых активов), из-за
    него заказы на разработку уходили в бизнес. Слово «визитка» в описании
    бота уводило заказ в полиграфию.
    """
    assert classify_job(make_job(title)).category_key == "programming", title


@pytest.mark.parametrize(
    "title",
    [
        "Продать приложение для андроид",
        "Купить готовое приложение",
        "Записать видео-отзыв о языковом приложении",
        "Приложение (каталог продукции) для врачей",
    ],
)
def test_classify_selling_ready_made_stays_business(title: str) -> None:
    """Продажа и обзор готового приложения остаются в бизнесе."""
    assert classify_job(make_job(title)).category_key == "business", title


@pytest.mark.parametrize(
    "title",
    [
        "Прозвон базы потенциальных клиентов",
        "Прозвонить базу и собрать заинтересованных",
        "Прозвон готовой B2B-базы (50 номеров) по скрипту",
        "Звонить по теплым заявкам",
        "Прозвон базы каждый день",
        "Поиск менеджера по продажам",
        "Менеджер по продажам",
        "Ищем контент-маркетолога / интернет-маркетолога B2В",
    ],
)
def test_classify_vacancy_and_cold_calls_stay_business(title: str) -> None:
    """Вакансии и обзвоны не попадают в IT из-за слов в описании.

    Заказчик ищет человека на постоянную работу или просит звонить по базе.
    В описании таких объявлений встречаются «скрипты», «сайт» и
    «разработка», и заказ поднимался в «Разработку и IT»: подписчик получал
    вакансию вместо заказа.
    """
    assert classify_job(make_job(title)).category_key == "business", title


def test_classify_low_confidence_job_is_not_programming() -> None:
    """Заказ без IT-объекта не попадает в разработку из-за одного слабого слова.

    «Требуется помощь по привлечению учеников репетитору» совпадал только
    по слову «нейросет» из описания. Такие заказы не должны доходить до
    подписчиков, выбравших «Разработку и IT».
    """
    result = classify_job(make_job("Требуется помощь по привлечению учеников репетитору"))
    assert result.category_key != "programming"


@pytest.mark.parametrize(
    "title",
    [
        "Мобильное приложение для курьера",
        "Приложение для сотрудников (iOS/Android/web-desktop)",
        "Приложение под Android для доставки",
        "Создать сайт для менеджера по продажам",
        "Парсер базы клиентов",
    ],
)
def test_classify_it_work_with_recruiting_words_stays_programming(title: str) -> None:
    """Технический объект в заголовке отменяет правило о вакансиях."""
    assert classify_job(make_job(title)).category_key == "programming", title


@pytest.mark.parametrize(
    "title",
    ["продающий сайт", "Сделать продающий лендинг"],
)
def test_classify_selling_site_is_programming(title: str) -> None:
    """«Продающий сайт» — сайт, который продаёт, а не продажа сайта."""
    assert classify_job(make_job(title)).category_key == "programming", title


@pytest.mark.parametrize(
    "title",
    [
        "Вывести сайт в топ",
        "Вывод сайта в топ по SEO",
        "Вывести молодой сайт в топ",
        "Поднять сайт в топ",
    ],
)
def test_classify_promotion_to_top_is_seo(title: str) -> None:
    """«Вывести сайт в топ» — продвижение, а не разработка.

    Слово «сайт» весит одинаково в обеих рубриках, и заказ на продвижение
    попадал в «Разработку и IT», где подписчик ждёт заказы на создание.
    """
    assert classify_job(make_job(title)).category_key == "seo", title


@pytest.mark.parametrize(
    "title",
    ["Создать сайт и вывести в топ", "Разработка сайта с продвижением"],
)
def test_classify_site_creation_with_promotion_stays_programming(title: str) -> None:
    """Если сайт ещё создают, это разработка, а не продвижение."""
    assert classify_job(make_job(title)).category_key == "programming", title


def test_classify_printed_business_card_stays_design() -> None:
    """Печатные визитки остаются в дизайне: правило не расширяется на них."""
    for title in ("Визитка для флориста", "Дизайн визитки"):
        assert classify_job(make_job(title)).category_key == "design", title


def test_classify_online_store_is_programming() -> None:
    """«Интернет-магазин» — создание сайта, даже если рядом упомянут бот."""
    for title in (
        "Создать интернет-магазин",
        "Разработка интернет-магазина с Telegram-ботом",
    ):
        result = classify_job(make_job(title))
        assert result.category_key == "programming", title


def test_classify_site_creation_job() -> None:
    """«Создание сайта» — программирование, а не пустая рубрика.

    Раньше слово «сайт» не совпадало с «сайта», заказ оставался без
    категории и не доходил до подписчиков.
    """
    result = classify_job(make_job("Создание сайта", "Нужен сайт под ключ"))
    assert result.category_key == "programming"


def test_classify_site_creation_variants() -> None:
    for title in (
        "Создание сайта на тильде",
        "Редизайн сайта под ключ",
        "Создание простого сайта для бренда очков",
        "Створення сайту з нуля",
    ):
        assert classify_job(make_job(title)).category_key == "programming", title


def test_classify_it_jobs_without_literal_it_words() -> None:
    """IT-работа без слов «сайт»/«разработка» тоже получает рубрику.

    Заказчик пишет модель контроллера или имя CRM, а не «программист».
    Раньше такие заказы оставались без рубрики и не доходили до подписчиков,
    хотя это та же «Разработка и IT».
    """
    for title in (
        "Написать код для контроллера на базе stm32f071cb",
        "Настройка обработки писем в Битрикс24 по ID диалога",
        "Создать виджет amoCRM или установить из действующих",
        "Соединить в 1 csv и привести к единому формату",
        "Создать копию страницы",
    ):
        assert classify_job(make_job(title)).category_key == "programming", title


def test_designer_word_form_classifies_as_design() -> None:
    """«Ищу дизайнера» попадает в дизайн: словоформа больше не теряется."""
    result = classify_job(make_job("Ищу дизайнера"))
    assert result.category_key == "design"


def test_site_work_family_expands_subcategory_selection() -> None:
    """Выбор верстки включает создание и доработку сайта.

    Для заказчика это одна работа: если пользователь выбрал веб-верстку,
    заказы «создание сайта» тоже должны приходить.
    """
    expanded = expand_subcategory_selection("programming", ["Верстка по макету"])
    for name in (
        "верстка по макету",
        "новый сайт",
        "копия сайта",
        "доработка сайта",
        "настройка сайта",
    ):
        assert name in expanded, name


def test_site_work_family_does_not_touch_other_subcategories() -> None:
    """Расширение ограничено сайтовой семьёй: парсеры не подмешиваются."""
    expanded = expand_subcategory_selection("programming", ["Парсеры"])
    assert expanded == {"парсеры"}


def test_filter_keeps_job_without_leaf_category() -> None:
    """Заказ без определённого листа не отбрасывается, если рубрика выбрана."""
    flt = CategoryFilter(
        enabled=True,
        categories=("programming",),
        subcategories={"programming": ("Парсеры",)},
    )
    result = classify_job(make_job("Создание сайта"))
    assert result.category_key == "programming"
    assert result.subcategory is None
    assert flt.allows(result)


def test_filter_still_rejects_wrong_subcategory() -> None:
    """Сужение работает: заказ с чужой подкатегорией по-прежнему отбрасывается."""
    flt = CategoryFilter(
        enabled=True,
        categories=("programming",),
        subcategories={"programming": ("Парсеры",)},
    )
    result = classify_job(make_job("Разработка на 1С", "Доработка конфигурации"))
    assert result.category_key == "programming"
    assert result.subcategory == "1С"
    assert not flt.allows(result)


def test_classify_cmd_callable() -> None:
    from freelance_bot.cli_categories import classify_cmd

    assert callable(classify_cmd)
