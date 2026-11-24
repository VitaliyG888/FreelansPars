"""Матчинг личных фильтров: биржи, рубрики, подкатегории, мин. цена."""

from __future__ import annotations

import pytest

from conftest import make_job
from freelance_bot.user_prefs import UserFilterPrefs


def prefs(**kwargs) -> UserFilterPrefs:
    return UserFilterPrefs(chat_id=1, **kwargs)


def test_empty_prefs_allow_everything() -> None:
    """Пустой фильтр = показывать всё, что прошло общий фильтр сбора."""
    p = prefs()
    assert p.matches(platform="fl_ru", budget_rub_estimate=1000)
    assert p.matches(platform=None, category_key=None, budget_rub_estimate=None)
    assert p.describe_platforms() == "все"


def test_platform_filter_selects_platforms() -> None:
    p = prefs(platforms=["kwork_ru"])
    assert p.allows_platform("kwork_ru")
    assert not p.allows_platform("fl_ru")
    # Заказ без платформы не проходит, если пользователь выбрал конкретные.
    assert not p.allows_platform(None)


def test_category_filter_whole_rubric() -> None:
    p = prefs(categories=["programming"])
    assert p.allows_category("programming")
    assert not p.allows_category("design")
    # Неизвестная рубрика не проходит при непустом выборе.
    assert not p.allows_category(None)


def test_category_filter_with_subcategories() -> None:
    """Подкатегории сужают рубрику: совпадение по названию без регистра."""
    p = prefs(categories=["programming"], subcategories={"programming": ["Парсеры", "боты"]})
    assert p.allows_category("programming", subcategory="парсеры")
    assert p.allows_category("programming", subcategory=None, group="Боты")
    assert not p.allows_category("programming", subcategory="лендинг", group="Сайты")


def test_price_filter_keeps_negotiable_orders() -> None:
    """Порог цены отсекает дешёвые; заказы «по договорённости» проходят."""
    p = prefs(min_price_rub=5000)
    assert p.allows_price(6000)
    assert not p.allows_price(4999.99)
    assert p.allows_price(None)


def test_matches_combines_all_conditions() -> None:
    p = prefs(platforms=["fl_ru"], categories=["programming"], min_price_rub=1000)
    job = make_job(external_id="1", budget=20000)
    assert p.matches(
        platform=job.platform,
        category_key="programming",
        budget_rub_estimate=job.budget_rub_estimate,
    )
    assert not p.matches(
        platform="kwork_ru", category_key="programming", budget_rub_estimate=20000
    )
    assert not p.matches(
        platform="fl_ru", category_key="design", budget_rub_estimate=20000
    )
    assert not p.matches(
        platform="fl_ru", category_key="programming", budget_rub_estimate=500
    )


def test_from_row_parses_json_columns() -> None:
    row = {
        "chat_id": 7,
        "platforms": '["fl_ru", "kwork_ru"]',
        "categories": '["programming"]',
        "subcategories": '{"programming": ["парсеры"]}',
        "min_price_rub": 3000,
    }
    p = UserFilterPrefs.from_row(row)
    assert p.chat_id == 7
    assert p.platforms == ["fl_ru", "kwork_ru"]
    assert p.categories == ["programming"]
    assert p.subcategories == {"programming": ["парсеры"]}
    assert p.min_price_rub == 3000


@pytest.mark.parametrize(
    "raw", [None, "", "не-json", "[список]", '{"a": "не список"}']
)
def test_from_row_survives_corrupted_json(raw: str | None) -> None:
    """Битый JSON не должен ломать чтение фильтров: поля становятся пустыми."""
    p = UserFilterPrefs.from_row(
        {
            "chat_id": 3,
            "platforms": raw,
            "categories": raw,
            "subcategories": raw,
            "min_price_rub": None,
        }
    )
    assert p.platforms == []
    assert p.categories == []
    assert p.subcategories == {}
    assert p.min_price_rub == 0.0


def test_describe_lists_choices() -> None:
    p = prefs(platforms=["fl_ru"], categories=["programming"])
    text = p.describe()
    assert "fl_ru" in text
    assert "programming" in text or "Разработка" in text
    assert "выключена" in text  # мин. цена не задана
