"""Таксономия категорий в стиле Kwork, классификация и фильтрация заказов.

Таксономия повторяет меню Kwork: семь верхних категорий, каждая разбита
на группы подкатегорий. Каждый заказ с любой площадки отображается
на это дерево, поэтому один профиль фильтров работает для всех источников.

Для классификации используются два слоя:

1. сама таксономия (названия групп и подкатегорий — естественные русские
   фразы, поэтому они напрямую совпадают с заголовками заказов);
2. написанная вручную таблица синонимов, покрывающая лексику других
   площадок («Веб-программирование», «Интеграция API», «Сбор данных», ...),
   которые не отдают таксономию Kwork.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .kwork_taxonomy import TAXONOMY
from .models import Job
from .text import contains_term

# --------------------------------------------------------------------------- #
# Таксономия
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Subcategory:
    name: str
    url: str = ""


@dataclass(frozen=True)
class CategoryGroup:
    name: str
    items: tuple[Subcategory, ...]


@dataclass(frozen=True)
class Category:
    key: str
    name: str
    kwork_id: int
    kwork_slug: str
    url: str
    groups: tuple[CategoryGroup, ...]

    @property
    def group_names(self) -> tuple[str, ...]:
        return tuple(group.name for group in self.groups)

    @property
    def subcategories(self) -> tuple[str, ...]:
        return tuple(item.name for group in self.groups for item in group.items)

    def group_of(self, subcategory: str) -> CategoryGroup | None:
        needle = normalise(subcategory)
        for group in self.groups:
            if normalise(group.name) == needle:
                return group
            if any(normalise(item.name) == needle for item in group.items):
                return group
        return None


def _build_categories() -> tuple[Category, ...]:
    built: list[Category] = []
    for key, raw in TAXONOMY.items():
        groups: list[CategoryGroup] = []
        raw_groups = raw.get("groups")
        if not isinstance(raw_groups, dict):
            continue
        for group_name, items in raw_groups.items():
            groups.append(
                CategoryGroup(
                    name=group_name,
                    items=tuple(Subcategory(name=name, url=url) for name, url in items),
                )
            )
        kwork_id_raw = raw.get("kwork_id")
        kwork_id = (
            int(kwork_id_raw) if isinstance(kwork_id_raw, (int, float, str)) and kwork_id_raw else 0
        )
        built.append(
            Category(
                key=key,
                name=str(raw.get("name", "")),
                kwork_id=kwork_id,
                kwork_slug=str(raw.get("kwork_slug", "")),
                url=str(raw.get("url", "")),
                groups=tuple(groups),
            )
        )
    return tuple(built)


CATEGORIES: tuple[Category, ...] = _build_categories()
CATEGORY_BY_KEY: dict[str, Category] = {category.key: category for category in CATEGORIES}
CATEGORY_KEYS: tuple[str, ...] = tuple(category.key for category in CATEGORIES)


def normalise(value: str) -> str:
    """Приводит к нижнему регистру, убирает различия «ё» и схлопывает пробелы.
    """
    return " ".join(value.replace("ё", "е").split()).casefold()


def find_category(name: str) -> Category | None:
    """Ищет категорию по ключу или русскому названию."""
    needle = normalise(name)
    for category in CATEGORIES:
        if normalise(category.key) == needle or normalise(category.name) == needle:
            return category
    return None


# --------------------------------------------------------------------------- #
# Синонимы
# --------------------------------------------------------------------------- #

# Дополнительный словарь по категориям. Термины из самой таксономии добавляются
# автоматически, поэтому здесь только формулировки, которых нет у Kwork.
EXTRA_KEYWORDS: dict[str, tuple[str, ...]] = {
    "design": (
        "дизайн",
        "логотип",
        "логотипы",
        "фирменный стиль",
        "айдентика",
        "баннер",
        "иконка",
        "фотошоп",
        "photoshop",
        "illustrator",
        "figma",
        "ui",
        "ux",
        "инфографика",
        "презентация",
        "макет",
        "полиграфия",
        "визитка",
        "каталог",
        "меню",
        "3d",
        "ретушь",
        "фотомонтаж",
        "вектор",
        "иллюстрация",
        "скетч",
        "стикер",
        "открытка",
        "плакат",
        "флаер",
        "вывеска",
        "витрина",
        "билборд",
        "дизайн упаковки",
        "дизайн интерьера",
        "прямой эфир",
    ),
    "programming": (
        "программирование",
        "разработка",
        "разработать",
        "программист",
        "python",
        "php",
        "java",
        "javascript",
        "typescript",
        "c#",
        "golang",
        "react",
        "vue",
        "django",
        "fastapi",
        "flask",
        "node.js",
        "чат-бот",
        "чат бот",
        "телеграм-бот",
        "телеграм бот",
        "telegram-бот",
        "telegram бот",
        "бот",
        "скрипт",
        "парсер",
        "парсинг",
        "scraping",
        "api",
        "интеграция",
        "база данных",
        "sql",
        "postgres",
        "mysql",
        "1с",
        "макрос",
        "excel",
        "сайт",
        "веб-сайт",
        "лендинг",
        "верстка",
        "фронтенд",
        "бэкенд",
        "backend",
        "frontend",
        "docker",
        "сервер",
        "хостинг",
        "домен",
        "администрирование",
        "тестирование",
        "юзабилити",
        "ios",
        "android",
        "мобильное приложение",
        "разработка игр",
        "unity",
        "wordpress",
        "opencart",
        "tilda",
        "битрикс",
        "выгрузка",
        "сбор данных",
        "автоматизация",
        "интеграция api",
        "создание веб-приложений",
        "создание чат-ботов",
        "веб-программирование",
        "системное программирование",
        "автоматизация процессов",
        "ai",
        "ии",
        "llm",
        "gpt",
        "нейросет",
        "машинное обучение",
    ),
    "writing_translations": (
        "текст",
        "статья",
        "копирайт",
        "копирайтинг",
        "рерайт",
        "перевод",
        "переводчик",
        "транскрипция",
        "расшифровка",
        "набор текста",
        "продающий текст",
        "коммерческое предложение",
        "сценарий",
        "резюме",
        "сопроводительное письмо",
        "вакансия",
        "эссе",
        "отзыв",
        "слоган",
        "нейминг",
        "вычитка",
        "корректура",
        "редактура",
        "контент для сайта",
        "заполнение сайта",
        "тексты",
    ),
    "seo": (
        "seo",
        "продвижение сайта",
        "семантическое ядро",
        "поисковая выдача",
        "аудит сайта",
        "мета-теги",
        "перелинковка",
        "трафик",
        "поведенческие факторы",
        "веб-аналитика",
        "счетчик",
        "google analytics",
        "яндекс.метрика",
        "яндекс метрика",
        "поисковые запросы",
        "оптимизация сайта",
        "продвижение в топ",
        "внутренняя оптимизация",
        "анализ сайтов",
    ),
    "promotion": (
        "smm",
        "контекстная реклама",
        "google ads",
        "яндекс директ",
        "таргет",
        "таргетированная реклама",
        "вконтакте",
        "vk",
        "telegram канал",
        "youtube",
        "tiktok",
        "дзен",
        "vc.ru",
        "reddit",
        "email рассылка",
        "e-mail рассылка",
        "рассылка",
        "маркетплейс",
        "wildberries",
        "ozon",
        "яндекс маркет",
        "контент-маркетинг",
        "сбор базы",
        "база клиентов",
        "лидогенерация",
        "реклама",
        "продвижение группы",
        "продвижение канала",
        "подписчики",
        "накрутка",
        "instagram",
        "соцсети",
        "сбор данных",
        "проверка базы",
    ),
    "audio_video": (
        "озвучка",
        "диктор",
        "аудио",
        "аудиоролик",
        "видео",
        "видеомонтаж",
        "монтаж",
        "ролик",
        "рекламный ролик",
        "проморолик",
        "анимация",
        "анимационный ролик",
        "gif",
        "музыка",
        "песня",
        "вокал",
        "аранжировка",
        "скринкаст",
        "видеообзор",
        "видеосъемка",
        "фотосессия",
        "субтитры",
        "заставка",
        "интро",
        "рилс",
        "reels",
        "кино",
        "кастинг",
    ),
    "business": (
        "бухгалтер",
        "бухгалтерия",
        "налог",
        "налоговая",
        "юрист",
        "юридическая",
        "юридической",
        "договор",
        "доверенность",
        "суд",
        "судопроизводство",
        "ооо",
        "ип",
        "индивидуальный предприниматель",
        "продажи",
        "обзвон",
        "холодные звонки",
        "менеджмент",
        "подбор персонала",
        "рекрутер",
        "кадров",
        "собеседование",
        "ассистент",
        "персональный помощник",
        "работа в ms office",
        "работа в excel",
        "инвестиции",
        "трейдинг",
        "крипто",
        "финансы",
        "зарплата",
        "строительство",
        "ремонт",
        "проектирование",
        "управление проектами",
        "бизнес-план",
        "финансовый анализ",
        "аудит финансов",
        "расчет зарплаты",
    ),
}

# Названия листьев слишком общие, чтобы быть ключевыми словами классификации.
GENERIC_TERMS: frozenset[str] = frozenset({"другие", "74 языка"})

# Вес ключевого слова в зависимости от места находки.
FIELD_WEIGHTS: dict[str, float] = {"title": 3.0, "meta": 2.5, "description": 1.0}
WEIGHT_GROUP = 0.8
WEIGHT_LEAF = 1.0
WEIGHT_EXTRA = 0.7

# Такой балл означает «точно эта категория».
FULL_CONF_SCORE = 3.0


@dataclass(frozen=True)
class Classification:
    """Результат сопоставления заказа с таксономией."""

    category_key: str | None = None
    category_name: str | None = None
    group: str | None = None
    subcategory: str | None = None
    confidence: float = 0.0
    matched: str | None = None

    @property
    def is_known(self) -> bool:
        return self.category_key is not None


def _fields(job: Job) -> dict[str, str]:
    meta = " ".join(part for part in (job.category or "", *job.skills) if part)
    return {"title": job.title, "meta": meta, "description": job.description}


def _term_score(text_fields: dict[str, str], term: str) -> float:
    return sum(
        weight for name, weight in FIELD_WEIGHTS.items() if contains_term(text_fields[name], term)
    )


def classify_job(job: Job) -> Classification:
    """Сопоставляет заказ с таксономией Kwork.

        Побеждает категория с наибольшим взвешенным баллом ключевых слов;
        группа и подкатегория — лучшие совпадения внутри этой категории.
    """
    fields = _fields(job)
    best: tuple[float, float, int, Category, str | None, str | None, str | None] | None = None

    for index, category in enumerate(CATEGORIES):
        category_score = 0.0
        best_leaf_score = 0.0
        best_group_name: str | None = None
        best_subcategory: str | None = None
        best_matched: str | None = None

        for group in category.groups:
            group_score = _term_score(fields, group.name)
            group_total = group_score * WEIGHT_GROUP
            group_best_leaf: tuple[float, str] | None = None

            for leaf in group.items:
                if normalise(leaf.name) in GENERIC_TERMS:
                    continue
                leaf_score = _term_score(fields, leaf.name)
                if leaf_score <= 0:
                    continue
                group_total += leaf_score * WEIGHT_LEAF
                if group_best_leaf is None or leaf_score > group_best_leaf[0]:
                    group_best_leaf = (leaf_score, leaf.name)

            if group_total <= 0:
                continue
            category_score += group_total
            if group_best_leaf is not None:
                if group_best_leaf[0] > best_leaf_score:
                    best_leaf_score = group_best_leaf[0]
                    best_group_name = group.name
                    best_subcategory = group_best_leaf[1]
                    best_matched = group_best_leaf[1]
                elif best_group_name is None:
                    best_group_name = group.name
                    best_matched = group.name

        for term in EXTRA_KEYWORDS.get(category.key, ()):
            term_score = _term_score(fields, term)
            if term_score <= 0:
                continue
            category_score += term_score * WEIGHT_EXTRA
            if term_score > best_leaf_score:
                best_leaf_score = term_score
                best_group_name = None
                best_subcategory = None
                best_matched = term

        if category_score <= 0:
            continue

        candidate: tuple[float, float, int, Category, str | None, str | None, str | None] = (
            category_score,
            best_leaf_score,
            -index,
            category,
            best_group_name,
            best_subcategory,
            best_matched,
        )
        if best is None or candidate[:3] > best[:3]:
            best = candidate

    if best is None:
        return Classification()

    score, _leaf, _neg_idx, cat, group_name, subcat, matched = best
    confidence = min(1.0, score / FULL_CONF_SCORE)
    return Classification(
        category_key=cat.key,
        category_name=cat.name,
        group=group_name,
        subcategory=subcat,
        confidence=round(confidence, 3),
        matched=matched,
    )


# --------------------------------------------------------------------------- #
# Фильтрация
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CategoryFilter:
    """Решает, какие категории (и подкатегории) обрабатываются."""

    enabled: bool = True
    categories: tuple[str, ...] = ()
    subcategories: dict[str, tuple[str, ...]] | None = None
    min_confidence: float = 0.35
    unknown_policy: str = "exclude"
    # Минимальная цена в рублях; 0 — фильтр выключен. Заказы без цены
    # («по договорённости») проходят всегда.
    min_price_rub: float = 0.0

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.categories)

    def allows_category(self, key: str | None) -> bool:
        if key is None:
            return self.unknown_policy == "include"
        return key in self.categories

    def allows_price(self, job: Job) -> bool:
        """Проверяет минимальную цену.

        У заказа без бюджета оценка в рублях ``None`` — такие заказы
        показываются всегда: после договорённости цена может устроить.
        """
        if self.min_price_rub <= 0:
            return True
        estimate = job.budget_rub_estimate
        return estimate is None or estimate >= self.min_price_rub

    def allows(self, classification: Classification) -> bool:
        if not self.active:
            return True
        if classification.category_key is None:
            return self.unknown_policy == "include"
        if classification.confidence < self.min_confidence:
            return self.unknown_policy == "include"
        if not self.allows_category(classification.category_key):
            return False

        wanted = (self.subcategories or {}).get(classification.category_key)
        if not wanted:
            return True
        needles = {normalise(item) for item in wanted}
        if classification.subcategory and normalise(classification.subcategory) in needles:
            return True
        return bool(classification.group and normalise(classification.group) in needles)

    def describe(self) -> str:
        if not self.enabled:
            return "фильтр категорий выключен"
        if not self.categories:
            return "фильтр категорий: все категории"
        names = ", ".join(
            CATEGORY_BY_KEY[key].name if key in CATEGORY_BY_KEY else key for key in self.categories
        )
        extra = ""
        if self.subcategories:
            extra = "; подкатегории: " + "; ".join(
                f"{CATEGORY_BY_KEY.get(key, Category(key, key, 0, key, '', ())).name}: "
                f"{', '.join(items)}"
                for key, items in self.subcategories.items()
            )
        if self.min_price_rub > 0:
            extra += f"; мин. цена {self.min_price_rub:,.0f} ₽".replace(",", " ")
        return f"фильтр категорий: {names}{extra}"


def category_tree_lines(include_urls: bool = False) -> list[str]:
    """Рендерит таксономию для CLI."""
    lines: list[str] = []
    for category in CATEGORIES:
        lines.append(f"{category.key}  —  {category.name}  (Kwork id {category.kwork_id})")
        for group in category.groups:
            lines.append(f"    {group.name}")
            for item in group.items:
                suffix = f"  {item.url}" if include_urls and item.url else ""
                lines.append(f"        - {item.name}{suffix}")
    return lines


def taxonomy_as_dict() -> dict[str, Any]:
    """Представление таксономии, удобное для JSON."""
    return {
        category.key: {
            "name": category.name,
            "kwork_id": category.kwork_id,
            "url": category.url,
            "groups": {
                group.name: [item.name for item in group.items] for group in category.groups
            },
        }
        for category in CATEGORIES
    }
