"""Личные фильтры пользователя: категории, подкатегории, биржи, мин. цена.

У каждого подписчика свой набор фильтров; конвейер доставляет заказ только
тем, чьим фильтрам он соответствует. Пустой фильтр означает «показывать всё»:
заказы проходят по категории/подкатегории и минимальной цене, порогов балла
и совпадения навыков больше нет.

Классификация заказа (рубрика/категория таксономии Kwork) вычисляется в
конвейере и хранится в базе, поэтому фильтры применяются и при доставке,
и при запросе последних заказов командой /jobs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


def _json_loads_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]


def _json_loads_dict(raw: str | None) -> dict[str, list[str]]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Битые значения (не список) выбрасываем: инвариант — в значении только
    # непустые списки имён.
    return {
        str(key): [str(item) for item in value]
        for key, value in data.items()
        if isinstance(value, list) and value
    }


@dataclass
class UserFilterPrefs:
    """Снимок фильтров одного пользователя.

    Пустые ``platforms``/``categories`` означают «все биржи/все рубрики».
    ``subcategories`` ограничивает выбранные рубрики отдельными категориями
    (названия категорий или групп таксономии).
    """

    chat_id: int = 0
    platforms: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    subcategories: dict[str, list[str]] = field(default_factory=dict)
    min_price_rub: float = 0.0

    @classmethod
    def from_row(cls, row: dict) -> UserFilterPrefs:
        return cls(
            chat_id=int(row["chat_id"]),
            platforms=_json_loads_list(row.get("platforms")),
            categories=_json_loads_list(row.get("categories")),
            subcategories=_json_loads_dict(row.get("subcategories")),
            min_price_rub=float(row.get("min_price_rub") or 0.0),
        )

    @classmethod
    def default_for(cls, chat_id: int) -> UserFilterPrefs:
        """Фильтр без ограничений: всё, что прошло общий фильтр сбора."""
        return cls(chat_id=chat_id)

    # ---------------------------------------------------------------- матчинг
    def allows_platform(self, platform: str | None) -> bool:
        if not self.platforms:
            return True
        return platform is not None and platform in self.platforms

    def allows_price(self, budget_rub_estimate: float | None) -> bool:
        """Мин. цена: заказы без бюджета («по договорённости») проходят всегда."""
        if self.min_price_rub <= 0:
            return True
        return budget_rub_estimate is None or budget_rub_estimate >= self.min_price_rub

    def allows_category(
        self,
        category_key: str | None,
        subcategory: str | None = None,
        group: str | None = None,
    ) -> bool:
        """Совпадение рубрики и подкатегории с выбором пользователя.

        Если для рубрики выбраны подкатегории, заказ проходит, когда его
        подкатегория или группа входят в выбор. Категория без подкатегорий
        в выборе означает «вся рубрика».

        Лист может быть не определён: «Создание сайта» классифицируется в
        рубрику без конкретной подкатегории. Такой заказ пропускается —
        иначе он молча терялся бы, хотя рубрика выбрана.
        """
        if not self.categories:
            return True
        if category_key is None:
            return False
        if category_key not in self.categories:
            return False
        wanted = self.subcategories.get(category_key) or []
        if not wanted:
            return True
        from .categories import expand_subcategory_selection

        # Выбор расширяется внутри семьи сайтовых работ: верстка, создание
        # сайта и его доработка — одна работа для заказчика.
        needles = expand_subcategory_selection(category_key, wanted)
        if subcategory and subcategory.casefold() in needles:
            return True
        if group and group.casefold() in needles:
            return True
        return not subcategory and not group

    def matches(
        self,
        *,
        platform: str | None,
        category_key: str | None = None,
        subcategory: str | None = None,
        group: str | None = None,
        budget_rub_estimate: float | None = None,
    ) -> bool:
        """Заказ проходит все фильтры пользователя?"""
        return (
            self.allows_platform(platform)
            and self.allows_category(category_key, subcategory, group)
            and self.allows_price(budget_rub_estimate)
        )

    def describe_platforms(self) -> str:
        """Список выбранных бирж или «все», если выбор пуст."""
        return ", ".join(self.platforms) if self.platforms else "все"

    def describe(self) -> str:
        """Человекочитаемое описание для карточки /filters."""
        from .categories import CATEGORY_BY_KEY

        if not self.categories:
            cats = "все рубрики"
        else:
            parts = []
            for key in self.categories:
                name = CATEGORY_BY_KEY[key].name if key in CATEGORY_BY_KEY else key
                chosen = self.subcategories.get(key) or []
                parts.append(f"{name} ({len(chosen)} кат.)" if chosen else name)
            cats = ", ".join(parts)
        price = (
            f"{self.min_price_rub:,.0f} ₽".replace(",", " ")
            if self.min_price_rub > 0
            else "выключена"
        )
        return (
            f"Биржи: {', '.join(self.platforms) if self.platforms else 'все'}\n"
            f"Рубрики: {cats}\n"
            f"Мин. цена: {price}"
        )
