"""Конфигурация профиля фрилансера для персонализированной генерации сообщений.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Области навыков
# --------------------------------------------------------------------------- #
#
# В профиле может быть широкий стек (бэкенд, веб, боты, данные). Когда заказ
# об одной области, в письме нужны только навыки этой области. Без
# иначе макетный заказ с меткой python от площадки заставит письмо
# приписать бэкенд-опыт вместо веб-навыков, которые реально нужны.

SKILL_DOMAINS: dict[str, tuple[str, ...]] = {
    "web": (
        "html",
        "css",
        "javascript",
        "react",
        "vue",
        "typescript",
        "вёрстк",
        "верстк",
        "фронтенд",
        "frontend",
        "figma",
        "tailwind",
        "sass",
        "webpack",
        "адаптив",
        "лендинг",
        "сайт",
    ),
    "backend": (
        "python",
        "django",
        "fastapi",
        "flask",
        "php",
        "golang",
        "java",
        "node",
        "postgresql",
        "mysql",
        "redis",
        "sql",
        "graphql",
        "бэкенд",
        "backend",
        "микросервис",
        "rest api",
    ),
    "telegram": (
        "telegram",
        "telebot",
        "aiogram",
        "бот",
        "bot",
        "python",
        "aiogram",
    ),
    "parsing": (
        "парсинг",
        "парсер",
        "scraping",
        "скрейпинг",
        "beautifulsoup",
        "selenium",
        "playwright",
        "выгрузк",
        "сбор данных",
        "spider",
        "парсеры",
    ),
    "data": (
        "sql",
        "база данных",
        "etl",
        "аналитик",
        "crm",
        "битрикс24",
        "google sheets",
    ),
    "devops": (
        "docker",
        "nginx",
        "linux",
        "ci/cd",
        "cicd",
        "деплой",
        "развертыван",
        "ansible",
        "kubernetes",
        "k8s",
        "systemd",
    ),
    "ai": (
        "llm",
        "gpt",
        "нейросет",
        "nlp",
        "machine learning",
        "ml",
        "ai",
        "ии",
    ),
    "automation": (
        "автоматиза",
        "интеграц",
        "api",
    ),
    "design": (
        "дизайн",
        "photoshop",
        "иллюстрац",
        "логотип",
        "макет",
        "figma",
        "ui",
        "ux",
    ),
    "text": (
        "текст",
        "перевод",
        "копирайтинг",
        "редактор",
        "smm",
        "контент",
    ),
}

# Общие хвостовые слова, не несущие смысла сами по себе. Их удаление
# позволяет "Telegram Bot API" совпасть с ТЗ, где сказано только «Telegram-бот».
_NOISE_SUFFIXES = ("api", "администрирование", "admin")


def expand_skill(skill: str) -> list[str]:
    """Разворачивает навык из профиля в фразы, которые его опознают.

        ``HTML/CSS`` -> ``['html/css', 'html', 'css']``
        ``Telegram Bot API`` -> ``['telegram bot api', 'telegram bot', 'telegram']``
        ``Python`` -> ``['python']``
    """
    raw = skill.strip()
    if not raw:
        return []

    variants: list[str] = []

    def add(value: str) -> None:
        cleaned = value.strip(" ,;/+").lower()
        if cleaned and cleaned not in variants:
            variants.append(cleaned)

    # Сначала навык целиком: «html/css» должен быть отдельным вариантом.
    add(raw)

    for part in re.split(r"[/,;+]", raw):
        part = part.strip()
        if not part:
            continue
        add(part)
        words = part.split()
        if not words:
            continue
        # Отбрасываем общее хвостовое слово («API»), только когда осмысленная фраза
        # остаётся: "Telegram Bot API" -> "Telegram Bot", а "REST API" не трогаем.
        if len(words) - 1 >= 2 and words[-1].lower() in _NOISE_SUFFIXES:
            add(" ".join(words[:-1]))
        if len(words) >= 3:
            add(" ".join(words[:2]))
            # Одно головное существительное («Telegram» из «Telegram Bot API») часто
            # совпадает с ТЗ, где остальное написано по-русски.
            if len(words[0]) >= 5:
                add(words[0])

    return variants


def skill_domains(skill: str) -> set[str]:
    """Возвращает области, к которым относится навык профиля."""
    variants = expand_skill(skill)
    found: set[str] = set()
    for domain, markers in SKILL_DOMAINS.items():
        for marker in markers:
            if any(marker in variant for variant in variants):
                found.add(domain)
                break
    return found


class PricingConfig(BaseModel):
    """Стратегия цены для оценки заказов."""

    strategy: Literal["minimum", "markup", "llm_estimate"] = "markup"
    markup_percent: float = 1.0
    min_acceptable_rub: float = 5000
    hourly_rate_rub: float = 2000
    include_price_in_message: bool = False


class MessageStyleConfig(BaseModel):
    """Предпочтения стиля генерации сообщений."""

    tone: Literal["professional", "friendly", "casual"] = "friendly"
    length: Literal["short", "medium", "long"] = "medium"
    mention_portfolio: bool = True
    ask_questions: bool = True
    max_emoji: int = 0
    banned_phrases: list[str] = Field(default_factory=list)


class AvailabilityConfig(BaseModel):
    """Доступность и загрузка фрилансера."""

    hours_per_week: int = 20
    immediate_start: bool = True
    preferred_duration: Literal["short", "medium", "long", "any"] = "any"


class CommunicationConfig(BaseModel):
    """Предпочтения общения."""

    preferred_platforms: list[str] = Field(default_factory=lambda: ["telegram", "email"])
    typical_response_hours: int = 2


class FreelancerProfile(BaseModel):
    """Полный профиль фрилансера для генерации сообщений через LLM."""

    name: str = ""
    experience_years: int = 3
    location: str = "Россия"
    skills: list[str] = Field(default_factory=list)
    secondary_skills: list[str] = Field(default_factory=list)
    experience_summary: str = ""
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    message_style: MessageStyleConfig = Field(default_factory=MessageStyleConfig)
    availability: AvailabilityConfig = Field(default_factory=AvailabilityConfig)
    communication: CommunicationConfig = Field(default_factory=CommunicationConfig)

    @property
    def all_skills(self) -> list[str]:
        """Объединённые основные и дополнительные навыки."""
        return self.skills + self.secondary_skills

    def calculate_price(
        self, job_budget_rub: float | None, estimated_hours: float | None
    ) -> float | None:
        """Вычисляет цену предложения по выбранной стратегии."""
        if self.pricing.strategy == "minimum":
            if job_budget_rub and job_budget_rub >= self.pricing.min_acceptable_rub:
                return max(job_budget_rub * 0.95, self.pricing.min_acceptable_rub)
            return self.pricing.min_acceptable_rub

        elif self.pricing.strategy == "markup":
            if job_budget_rub:
                quoted = job_budget_rub * self.pricing.markup_percent
                return max(quoted, self.pricing.min_acceptable_rub)
            return None

        elif self.pricing.strategy == "llm_estimate":
            if estimated_hours:
                return max(
                    estimated_hours * self.pricing.hourly_rate_rub,
                    self.pricing.min_acceptable_rub,
                )
            return None

        return None


def load_profile(
    path: str | Path = "profile.yaml", *, with_overrides: bool = True
) -> FreelancerProfile:
    """Загружает профиль фрилансера из YAML.

        Для стандартного ``profile.yaml`` сверху накладывается секция ``profile``
        из ``var/runtime_settings.yaml`` — так правки из Telegram переживают рестарт.
    """
    p = Path(path)
    data: dict = {}
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    if with_overrides:
        from .config import deep_merge, load_overrides

        overlay = load_overrides()
        raw_meta = overlay.get("meta")
        meta: dict = raw_meta if isinstance(raw_meta, dict) else {}
        target = meta.get("profile_path")
        matches = False
        if target:
            try:
                matches = Path(str(target)).resolve() == p.resolve()
            except OSError:
                matches = False
        elif p.name == "profile.yaml":
            # Оверлеи старого формата не содержали meta.
            matches = True
        if matches:
            profile_overlay = overlay.get("profile")
            if isinstance(profile_overlay, dict):
                data = deep_merge(data, profile_overlay)

    return FreelancerProfile.model_validate(data)
