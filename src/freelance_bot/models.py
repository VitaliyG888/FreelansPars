"""Доменные модели, общие для адаптеров, скоринга, LLM и отправителя."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class BudgetKind(StrEnum):
    FIXED = "fixed"
    HOURLY = "hourly"
    RANGE = "range"
    UNKNOWN = "unknown"


class TransportKind(StrEnum):
    HTTP = "http"
    BROWSER = "browser"


class OrderStatus(StrEnum):
    """Жизненный цикл карточки заказа в Telegram.

    NEW → SHOWN → SENT_CLICKED → SKIPPED. Переходы проверяет хранилище,
    поэтому повторный callback не ломает состояние.
    """

    NEW = "new"
    SHOWN = "shown"
    SENT_CLICKED = "sent_clicked"
    SKIPPED = "skipped"


class Outcome(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    CHALLENGE = "challenge"
    BLOCKED = "blocked"
    LOGIN_WALL = "login_wall"
    PARSE_ERROR = "parse_error"
    NETWORK_ERROR = "network_error"
    DISABLED = "disabled"


class ClientInfo(BaseModel):
    """То, что площадка раскрывает о заказчике. Все поля необязательны."""

    model_config = ConfigDict(extra="ignore")

    external_id: str | None = None
    name: str | None = None
    rating: float | None = None
    reviews_count: int | None = None
    hire_rate: float | None = Field(
        default=None, description="0..1 share of posted projects that resulted in a hire"
    )
    total_spend_rub: float | None = None
    projects_posted: int | None = None
    verified: bool | None = None
    country: str | None = None
    registered_at: datetime | None = None


class RawJob(BaseModel):
    """Вывод адаптера до нормализации. Адаптеры заполняют то, что даёт страница.
    """

    model_config = ConfigDict(extra="allow")

    platform: str
    external_id: str
    url: str
    #: Проверенная https-ссылка на карточку (см. order_links.resolve_order_url).
    order_url: str | None = None
    title: str
    description: str = ""
    budget_text: str | None = None
    budget_amount: float | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    currency: str = "RUB"
    budget_kind: BudgetKind = BudgetKind.UNKNOWN
    category: str | None = None
    skills: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    published_text: str | None = None
    deadline_at: datetime | None = None
    deadline_text: str | None = None
    proposals_count: int | None = None
    views_count: int | None = None
    is_urgent: bool = False
    client: ClientInfo = Field(default_factory=ClientInfo)
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("title", "description", mode="before")
    @classmethod
    def _clean_text(cls, v: Any) -> str:
        if v is None:
            return ""
        return " ".join(str(v).replace("\xa0", " ").split())


class Job(BaseModel):
    """Нормализованный заказ, не зависящий от площадки, готовый к скорингу."""

    model_config = ConfigDict(extra="ignore")

    platform: str
    external_id: str
    url: str
    order_url: str | None = None
    status: OrderStatus = OrderStatus.NEW
    title: str
    description: str
    budget_amount: float | None = None
    budget_min: float | None = None
    budget_max: float | None = None
    currency: str = "RUB"
    budget_kind: BudgetKind = BudgetKind.UNKNOWN
    budget_rub_estimate: float | None = None
    category: str | None = None
    skills: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    deadline_at: datetime | None = None
    deadline_text: str | None = Field(
        default=None, description="Срок как его написала площадка: «3–4 недели», «2 дня»"
    )
    proposals_count: int | None = None
    is_urgent: bool = False
    client: ClientInfo = Field(default_factory=ClientInfo)
    fetched_at: datetime = Field(default_factory=utcnow)

    @property
    def uid(self) -> str:
        return f"{self.platform}:{self.external_id}"

    @property
    def searchable_text(self) -> str:
        parts = [self.title, self.description, self.category or "", " ".join(self.skills)]
        return " ".join(parts).lower()


class RedFlag(BaseModel):
    code: str
    severity: float = Field(ge=0.0, le=1.0)
    evidence: str = ""


class ScoreFactor(BaseModel):
    name: str
    raw: float = Field(description="Normalised 0..1 sub-score")
    weight: float
    contribution: float
    rationale: str


class ScoreBreakdown(BaseModel):
    total: float = Field(ge=0.0, le=100.0)
    factors: list[ScoreFactor]
    red_flags: list[RedFlag] = Field(default_factory=list)
    penalty: float = 0.0
    summary_ru: str = ""


class ScoredJob(BaseModel):
    job: Job
    score: ScoreBreakdown


class JobAnalysis(BaseModel):
    """Структурированный вывод LLM о заказе."""

    need_summary_ru: str = ""
    requirements: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    questions_to_ask: list[str] = Field(default_factory=list)
    estimated_hours: float | None = None
    suggested_price_rub: float | None = None
    skill_match: list[str] = Field(default_factory=list)


class GeneratedMessage(BaseModel):
    platform: str
    job_uid: str
    text: str
    source: Literal["llm", "fallback"] = "llm"
    model: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    violations: list[str] = Field(default_factory=list)
    approved: bool | None = None


class PlatformRunResult(BaseModel):
    platform: str
    transport: TransportKind
    outcome: Outcome
    jobs_found: int = 0
    duration_ms: int = 0
    attempts: int = 0
    used_proxy: str | None = None
    http_status: int | None = None
    challenge: str | None = None
    error: str | None = None


class CycleReport(BaseModel):
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    dry_run: bool = True
    platforms: list[PlatformRunResult] = Field(default_factory=list)
    jobs_new: int = 0
    jobs_scored: int = 0
    jobs_eligible: int = 0
    # Сколько новых подходящих заказов реально показано в Telegram за цикл.
    jobs_announced: int = 0
    messages_generated: int = 0
    messages_sent: int = 0
    # Черновики, не прошедшие проверку качества и поэтому не поставленные в очередь.
    messages_rejected: int = 0
    top: list[ScoredJob] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
