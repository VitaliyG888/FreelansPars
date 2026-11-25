"""Типизированная конфигурация: YAML + переопределения окружения через pydantic-settings.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeConfig(BaseModel):
    timezone: str = "Europe/Moscow"
    dry_run: bool = True
    poll_interval_seconds: int = 600
    #: Сводка «цикл завершён» в Telegram. По умолчанию выключена: в чат должны
    #: приходить только новые заказы, а не отчёт о каждом прогоне.
    cycle_report: bool = False
    max_jobs_per_platform: int = 40
    request_timeout_seconds: int = 25
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    accept_language: str = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"


class RateConfig(BaseModel):
    requests_per_minute: float = 4.0
    burst: int = 2
    # Человеческая пауза между запросами к домену: поверх вёдра токенов
    # выдерживается случайный интервал в этих границах, как будто страницы
    # просматривает человек. 0 — пауза не добавляется.
    pause_min_seconds: float = Field(default=0.0, ge=0)
    pause_max_seconds: float = Field(default=0.0, ge=0)


class BackoffConfig(BaseModel):
    base_seconds: float = 3.0
    max_seconds: float = 180.0
    jitter: bool = True
    max_retries: int = 3


class BrowserConfig(BaseModel):
    enabled: bool = True
    headless: bool = True
    locale: str = "ru-RU"
    timezone: str = "Europe/Moscow"
    viewport: dict[str, int] = Field(default_factory=lambda: {"width": 1440, "height": 900})
    settle_ms: int = 4500
    navigation_timeout_ms: int = 45000
    # Сколько ждать автоматического прохождения Cloudflare/Qrator challenge.
    challenge_wait_ms: int = 20000


class RelayConfig(BaseModel):
    """Внешний reader-сервис для площадок, недоступных с IP сервера.

    Прямой запрос выполняется первым; relay используется как запасной путь,
    когда TCP-соединение с площадкой блокируется на уровне сети. Значения
    ``api_key`` в YAML нет: ключ читается из ``JINA_API_KEY``.
    """

    enabled: bool = True
    url_template: str = "https://r.jina.ai/{url}"
    timeout_seconds: int = 90
    return_format: str = "html"


class NetworkConfig(BaseModel):
    proxy_strategy: str = "round_robin"
    proxy_cooldown_seconds: int = 300
    respect_robots_txt: bool = True
    default_rate: RateConfig = Field(default_factory=RateConfig)
    domains: dict[str, RateConfig] = Field(default_factory=dict)
    backoff: BackoffConfig = Field(default_factory=BackoffConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    relay: RelayConfig = Field(default_factory=RelayConfig)

    def rate_for(self, domain: str) -> RateConfig:
        return self.domains.get(domain, self.default_rate)


class CaptchaConfig(BaseModel):
    enabled: bool = True
    provider: str = "capsolver"
    solve_yandex_smartcaptcha: bool = True
    solve_recaptcha_v2: bool = True
    solve_hcaptcha: bool = True
    max_attempts: int = 2
    timeout_seconds: int = 180


class PlatformConfig(BaseModel):
    enabled: bool = True
    transport: str = "http"
    listing_url: str
    detail_enrichment: bool = False
    notes: str = ""
    max_pages: int = 1
    # Некоторые площадки блокируют IP дата-центра на уровне TCP. Для них
    # разрешён запасной путь через reader-relay (см. NetworkConfig.relay).
    relay_fallback: bool = False
    # Укороченный таймаут прямого запроса, чтобы быстрее перейти к relay.
    direct_timeout_seconds: int = 0
    # Площадка отдаёт контент только с российского резидентного прокси.
    requires_proxy: bool = False
    # Предпочтительный TLS-отпечаток; пусто — автоматический выбор.
    impersonate: str = ""
    # Пер-платформенный интервал опроса в секундах: 0 — опрашивать в каждом
    # цикле сервиса. Нужен площадкам со строгим антиботом (kwork/Qrator):
    # частые однотипные визиты с одного IP приводят к блоку на часы.
    poll_interval_seconds: int = Field(default=0, ge=0)
    # Стратегия прокси конкретной площадки: round_robin | sticky | none.
    # Пусто — берётся network.proxy_strategy. Для площадок с куками и
    # прогревом (kwork/Qrator) нужен sticky: один домен — один IP, иначе
    # куки, полученные с одного адреса, уходят с другого.
    proxy_strategy: Literal["", "round_robin", "sticky", "none"] = ""


class AuthenticatedOnlyConfig(BaseModel):
    reason: str


class SkillMatchConfig(BaseModel):
    skills: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)


class CompetitionConfig(BaseModel):
    k: float = 10.0


class ClientQualityConfig(BaseModel):
    hire_rate_weight: float = 0.5
    spend_weight: float = 0.3
    verification_weight: float = 0.2
    spend_reference_rub: float = 100_000.0


class ScoringWeights(BaseModel):
    budget: float = 0.28
    skill_match: float = 0.22
    client_quality: float = 0.18
    competition: float = 0.14
    freshness: float = 0.10
    urgency_fit: float = 0.08


class ScoringConfig(BaseModel):
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    red_flag_penalty: float = 0.35
    skill_match: SkillMatchConfig = Field(default_factory=SkillMatchConfig)
    market_median_rub: dict[str, float] = Field(default_factory=lambda: {"default": 8000.0})
    assumed_hours_default: float = 8.0
    freshness_half_life_hours: float = 18.0
    competition: CompetitionConfig = Field(default_factory=CompetitionConfig)
    client_quality: ClientQualityConfig = Field(default_factory=ClientQualityConfig)

    def median_for(self, category: str | None) -> float:
        if category and category in self.market_median_rub:
            return self.market_median_rub[category]
        return self.market_median_rub.get("default", 8000.0)


class RedFlagRule(BaseModel):
    enabled: bool = True
    severity: float = 0.5
    patterns: list[str] = Field(default_factory=list)
    budget_ratio_below: float | None = None
    min_scope_chars: int | None = None
    max_description_chars: int | None = None


class RedFlagsConfig(BaseModel):
    enabled: bool = True
    rules: dict[str, RedFlagRule] = Field(default_factory=dict)


class StyleConfig(BaseModel):
    banned_phrases: list[str] = Field(default_factory=list)
    max_emoji: int = 1
    max_exclamation: int = 1
    min_specificity_overlap: float = 0.12


class LLMProviderConfig(BaseModel):
    """OpenAI-совместимый провайдер, добавленный через Telegram.

        Ключ хранится не в конфиге, а в ``.env`` под именем ``api_key_env``.
    """

    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_key_env: str = ""
    api_key_required: bool = True
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        url = value.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("base_url должен начинаться с http:// или https://")
        return url

    @field_validator("model")
    @classmethod
    def _check_model(cls, value: str) -> str:
        model = value.strip()
        if not model:
            raise ValueError("model не может быть пустым")
        return model


class LLMConfig(BaseModel):
    enabled: bool = True
    temperature: float = 0.4
    max_output_tokens: int = 900
    timeout_seconds: int = 60
    fallback_enabled: bool = True
    #: Сколько секунд не трогать модель после ответа 429 (квота исчерпана).
    #: Без паузы бот каждые полчаса заново пробовал полсотни заказов, и все
    #: они падали в fallback: письма выходили шаблонными, а провайдер получал
    #: лавину заведомо отклоняемых запросов.
    quota_pause_seconds: int = 1800
    # Пустая строка означает встроенный SYSTEM_PROMPT. Непустое значение
    # полностью заменяет его; extra_instructions добавляются в конец.
    system_prompt: str = ""
    extra_instructions: str = ""
    # Провайдеры, добавленные командами /llm provider. Пустое имя активного
    # провайдера означает встроенный вариант из LLM_BASE_URL/LLM_MODEL.
    active_provider: str = ""
    providers: dict[str, LLMProviderConfig] = Field(default_factory=dict)
    style: StyleConfig = Field(default_factory=StyleConfig)

    def active_llm_provider(self) -> LLMProviderConfig | None:
        if self.active_provider and self.active_provider in self.providers:
            return self.providers[self.active_provider]
        return None


class MessageLimit(BaseModel):
    max_chars: int = 2000
    allow_links: bool = False


class MessagingConfig(BaseModel):
    limits: dict[str, MessageLimit] = Field(default_factory=dict)
    require_human_approval: bool = True
    max_sends_per_hour: int = 6
    min_seconds_between_sends: int = 240
    # Сколько первых писем можно подготовить за цикл. Каждый заказ, который
    # проходит фильтр, всё равно уходит в Telegram; заказы сверх этого лимита
    # приходят без готового письма.
    max_messages_per_cycle: int = 50

    def limit_for(self, platform: str) -> MessageLimit:
        return self.limits.get(platform, MessageLimit())


class CategoryFilterConfig(BaseModel):
    enabled: bool = False
    categories: list[str] = Field(default_factory=list)
    subcategories: dict[str, list[str]] = Field(default_factory=dict)
    min_confidence: float = 0.35
    unknown_policy: str = "exclude"
    # Минимальная цена в рублях для показа заказа. Заказы без бюджета
    # («по договорённости») проходят всегда — после обсуждения цена
    # может устроить.
    min_price_rub: float = Field(default=0.0, ge=0)


class AppConfig(BaseModel):
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    captcha: CaptchaConfig = Field(default_factory=CaptchaConfig)
    platforms: dict[str, PlatformConfig] = Field(default_factory=dict)
    authenticated_only: dict[str, AuthenticatedOnlyConfig] = Field(default_factory=dict)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    red_flags: RedFlagsConfig = Field(default_factory=RedFlagsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    messaging: MessagingConfig = Field(default_factory=MessagingConfig)
    category_filter: CategoryFilterConfig = Field(default_factory=CategoryFilterConfig)
    # Ориентировочные курсы валют к рублю. Перед продакшеном обновите из реального источника.
    fx_to_rub: dict[str, float] = Field(
        default_factory=lambda: {"RUB": 1.0, "UAH": 2.30, "USD": 95.0, "EUR": 102.0, "BYN": 29.0}
    )

    def enabled_platforms(self) -> dict[str, PlatformConfig]:
        return {name: cfg for name, cfg in self.platforms.items() if cfg.enabled}


class Secrets(BaseSettings):
    """Секреты из окружения. Значения никогда не попадают в config.yaml или логи.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    telegram_bot_token: str = ""
    telegram_admin_chat_ids: str = ""
    # Короткий алиас ADMIN_ID из .env: тот же смысл, что и TELEGRAM_ADMIN_CHAT_IDS,
    # но удобен, когда администратор один. Значение склеивается со списком.
    admin_id: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    capsolver_api_key: str = ""
    twocaptcha_api_key: str = ""
    jina_api_key: str = ""
    ru_proxy_urls: str = ""
    freelance_bot_db: str = "var/freelance.db"
    freelance_bot_outbox: str = "var/outbox"

    @field_validator("ru_proxy_urls", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v

    @property
    def proxies(self) -> list[str]:
        urls: list[str] = []
        for raw in self.ru_proxy_urls.split(","):
            url = raw.strip()
            if not url:
                continue
            # Провайдеры часто выдают прокси без схемы; без неё тип туннеля
            # неоднозначен — считаем такие HTTP-прокси.
            if "://" not in url:
                url = "http://" + url
            urls.append(url)
        return urls

    @property
    def telegram_admins(self) -> list[int]:
        """Разбирает admin chat id через запятую.

        Источники объединяются: ``TELEGRAM_ADMIN_CHAT_IDS`` (список) и
        ``ADMIN_ID`` (одиночный ID). Дубликаты и нечисловые значения
        отбрасываются, порядок сохраняется.
        """
        raw = ",".join(filter(None, (self.telegram_admin_chat_ids, self.admin_id)))
        admins: list[int] = []
        for chat_id in raw.split(","):
            value = chat_id.strip()
            # id групп отрицательный, поэтому проверяем цифры после знака минус.
            if value.lstrip("-").isdigit():
                admin = int(value)
                if admin not in admins:
                    admins.append(admin)
        return admins


OVERRIDES_ENV = "FREELANCE_BOT_OVERRIDES"
DEFAULT_OVERRIDES_PATH = "var/runtime_settings.yaml"

# Границы интервала между проходами. Настраивается только администратором:
# слишком частые проходы вредят площадкам (антибот), редкие — задерживают
# доставку заказов.
MIN_POLL_INTERVAL_SECONDS = 600
MAX_POLL_INTERVAL_SECONDS = 3600


def clamp_poll_interval(value: int) -> int:
    """Обрезает интервал опроса в разрешённые границы 600..3600 секунд."""
    return max(MIN_POLL_INTERVAL_SECONDS, min(MAX_POLL_INTERVAL_SECONDS, int(value)))


def overrides_path() -> Path:
    """Путь к YAML-оверлею, который пишут команды Telegram."""
    return Path(os.getenv(OVERRIDES_ENV, DEFAULT_OVERRIDES_PATH))


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Рекурсивно накладывает *patch* на *base*, не мутируя исходные словари."""
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def load_overrides(path: str | Path | None = None) -> dict[str, Any]:
    """Читает оверлей настроек; битый/отсутствующий файл считается пустым."""
    p = Path(path) if path else overrides_path()
    if not p.is_file():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config(path: str | Path | None = None, *, with_overrides: bool = True) -> AppConfig:
    """Загружает YAML-конфиг. Без файла — безопасные значения по умолчанию (dry-run включён).

        Оверлей ``var/runtime_settings.yaml`` применяется только к конфигу по
        умолчанию (``path=None``), чтобы явно переданный файл читался как есть.
    """
    candidates = [path] if path else [os.getenv("FREELANCE_BOT_CONFIG"), "config.yaml"]
    data: dict[str, Any] = {}
    for candidate in candidates:
        if not candidate:
            continue
        p = Path(candidate)
        if p.is_file():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            break

    if with_overrides and path is None:
        overlay = load_overrides().get("config")
        if isinstance(overlay, dict):
            data = deep_merge(data, overlay)

    return AppConfig.model_validate(data)


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    return Secrets()


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return load_config()
