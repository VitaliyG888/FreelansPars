"""Объяснимое настраиваемое скорингование заказов.

Каждый фактор возвращает подбалл 0..1 и обоснование на русском. Итог:
`100 * weighted_mean * penalty`, поэтому высокооплачиваемый заказ с ТЗ-разводом
не может обогнать просто хороший.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..config import RedFlagsConfig, ScoringConfig
from ..models import Job, ScoreBreakdown, ScoredJob, ScoreFactor
from ..text import matching_terms, strip_contact_handles
from . import redflags as rf

# Опорные точки бюджета относительно медианы категории.
BUDGET_ANCHORS: list[tuple[float, float]] = [
    (0.0, 0.02),
    (0.25, 0.15),
    (0.5, 0.45),
    (0.8, 0.75),
    (1.5, 1.0),
    (100.0, 1.0),
]


def _interp(x: float, anchors: list[tuple[float, float]]) -> float:
    if x <= anchors[0][0]:
        return anchors[0][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:], strict=False):
        if x <= x1:
            if x1 == x0:
                return y1
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return anchors[-1][1]


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


class ScoringEngine:
    def __init__(self, cfg: ScoringConfig, red_flags: RedFlagsConfig | None = None) -> None:
        self.cfg = cfg
        self.red_flags = red_flags or RedFlagsConfig()

    # ------------------------------------------------------------------ факторы
    def budget_factor(self, job: Job) -> ScoreFactor:
        median = self.cfg.median_for(job.category)
        estimate = job.budget_rub_estimate
        if estimate is None or median <= 0:
            return ScoreFactor(
                name="budget",
                raw=0.40,
                weight=self.cfg.weights.budget,
                contribution=0.0,
                rationale="Бюджет не указан — нейтральная оценка.",
            )
        ratio = estimate / median
        raw = _interp(ratio, BUDGET_ANCHORS)
        if ratio >= 1.5:
            text = f"Бюджет {estimate:,.0f} ₽ — в {ratio:.1f}× выше медианы по категории."
        elif ratio >= 0.8:
            text = f"Бюджет {estimate:,.0f} ₽ — около медианы ({ratio:.1f}×)."
        else:
            text = f"Бюджет {estimate:,.0f} ₽ — ниже медианы ({ratio:.1f}×)."
        return ScoreFactor(
            name="budget",
            raw=raw,
            weight=self.cfg.weights.budget,
            contribution=0.0,
            rationale=text.replace(",", " "),
        )

    def skill_factor(self, job: Job) -> ScoreFactor:
        # Заголовок и заявленные навыки — более сильное свидетельство, чем текст: Telegram
        # в подвале, не должен превращать заказ на видеомонтаж в
        # Проект Telegram-бота.
        head = " ".join([job.title, job.category or "", *job.skills])
        title_matched = matching_terms(head, self.cfg.skill_match.skills)
        body_matched = [
            term
            for term in matching_terms(
                strip_contact_handles(job.description), self.cfg.skill_match.skills
            )
            if term not in title_matched
        ]
        matched = title_matched + body_matched
        blocked = matching_terms(job.searchable_text, self.cfg.skill_match.exclude_keywords)

        if blocked:
            raw = 0.10
            why = f"Совпадение с нежелательной темой: {', '.join(blocked[:3])}."
        elif title_matched:
            raw = _clamp(0.55 + 0.12 * len(matched))
            why = f"Совпали навыки: {', '.join(matched[:5])}."
        elif body_matched:
            raw = _clamp(0.32 + 0.08 * len(body_matched))
            why = f"Навыки упомянуты в описании: {', '.join(body_matched[:5])}."
        else:
            raw = 0.20
            why = "Явных совпадений с профилем нет."
        return ScoreFactor(
            name="skill_match",
            raw=raw,
            weight=self.cfg.weights.skill_match,
            contribution=0.0,
            rationale=why,
        )

    def client_factor(self, job: Job) -> ScoreFactor:
        c = job.client
        cq = self.cfg.client_quality
        parts: list[tuple[float, float]] = []
        notes: list[str] = []

        if c.hire_rate is not None:
            parts.append((_clamp(c.hire_rate), cq.hire_rate_weight))
            notes.append(f"нанимает в {c.hire_rate * 100:.0f}% проектов")
        if c.total_spend_rub is not None and cq.spend_reference_rub > 0:
            parts.append((_clamp(c.total_spend_rub / cq.spend_reference_rub), cq.spend_weight))
            notes.append(f"потратил ≈{c.total_spend_rub:,.0f} ₽".replace(",", " "))
        if c.verified is not None:
            parts.append((1.0 if c.verified else 0.2, cq.verification_weight))
            notes.append("верифицирован" if c.verified else "не верифицирован")
        if c.reviews_count:
            rating_part = _clamp((c.rating or 0) / 5.0)
            parts.append((rating_part, 0.2))
            notes.append(f"рейтинг {c.rating or '—'} ({c.reviews_count} отзывов)")

        if not parts:
            return ScoreFactor(
                name="client_quality",
                raw=0.50,
                weight=self.cfg.weights.client_quality,
                contribution=0.0,
                rationale="Данных о заказчике нет — нейтральная оценка.",
            )
        total_weight = sum(w for _, w in parts)
        raw = sum(s * w for s, w in parts) / total_weight if total_weight else 0.5
        return ScoreFactor(
            name="client_quality",
            raw=_clamp(raw),
            weight=self.cfg.weights.client_quality,
            contribution=0.0,
            rationale="Заказчик: " + ", ".join(notes) + ".",
        )

    def competition_factor(self, job: Job) -> ScoreFactor:
        n = job.proposals_count
        if n is None:
            return ScoreFactor(
                name="competition",
                raw=0.60,
                weight=self.cfg.weights.competition,
                contribution=0.0,
                rationale="Число откликов неизвестно — нейтральная оценка.",
            )
        k = max(self.cfg.competition.k, 0.1)
        raw = 1.0 / (1.0 + n / k)
        level = "низкая" if n <= 5 else "средняя" if n <= 20 else "высокая"
        return ScoreFactor(
            name="competition",
            raw=raw,
            weight=self.cfg.weights.competition,
            contribution=0.0,
            rationale=f"{n} откликов — {level} конкуренция.",
        )

    def freshness_factor(self, job: Job, now: datetime | None = None) -> ScoreFactor:
        now = now or datetime.now(UTC)
        if job.published_at is None:
            return ScoreFactor(
                name="freshness",
                raw=0.50,
                weight=self.cfg.weights.freshness,
                contribution=0.0,
                rationale="Время публикации неизвестно.",
            )
        published = job.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        age_h = max(0.0, (now - published).total_seconds() / 3600)
        hl = max(self.cfg.freshness_half_life_hours, 0.5)
        raw = 0.5 ** (age_h / hl)
        age_text = f"{age_h:.0f} ч" if age_h >= 1 else f"{age_h * 60:.0f} мин"
        return ScoreFactor(
            name="freshness",
            raw=raw,
            weight=self.cfg.weights.freshness,
            contribution=0.0,
            rationale=f"Опубликован {age_text} назад.",
        )

    def urgency_factor(self, job: Job, now: datetime | None = None) -> ScoreFactor:
        now = now or datetime.now(UTC)
        if job.deadline_at is None:
            raw = 0.60 if job.is_urgent else 0.50
            why = "Срочный заказ без явного дедлайна." if job.is_urgent else "Дедлайн не указан."
            return ScoreFactor(
                name="urgency_fit",
                raw=raw,
                weight=self.cfg.weights.urgency_fit,
                contribution=0.0,
                rationale=why,
            )
        deadline = job.deadline_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        hours_left = (deadline - now).total_seconds() / 3600
        if hours_left <= 0:
            raw, why = 0.10, "Срок уже истёк."
        elif hours_left <= 24:
            raw, why = 1.0, f"Дедлайн через {hours_left:.0f} ч — подходит для быстрого старта."
        elif hours_left <= 72:
            raw, why = 0.80, f"Дедлайн через {hours_left / 24:.1f} дн."
        else:
            raw, why = 0.55, f"Дедлайн через {hours_left / 24:.0f} дн — запас времени."
        return ScoreFactor(
            name="urgency_fit",
            raw=raw,
            weight=self.cfg.weights.urgency_fit,
            contribution=0.0,
            rationale=why,
        )

    # ------------------------------------------------------------------- публичные
    def score(self, job: Job, now: datetime | None = None) -> ScoredJob:
        factors = [
            self.budget_factor(job),
            self.skill_factor(job),
            self.client_factor(job),
            self.competition_factor(job),
            self.freshness_factor(job, now),
            self.urgency_factor(job, now),
        ]
        total_weight = sum(f.weight for f in factors) or 1.0
        for f in factors:
            f.contribution = f.raw * f.weight / total_weight

        flags = rf.detect(job, self.red_flags, self.cfg)
        penalty_units = rf.severity_sum(flags)
        penalty = min(0.8, penalty_units * self.cfg.red_flag_penalty)

        base = sum(f.contribution for f in factors)
        total = round(100 * base * (1 - penalty), 1)

        summary = self._summary(factors, flags, penalty)
        return ScoredJob(
            job=job,
            score=ScoreBreakdown(
                total=total,
                factors=factors,
                red_flags=flags,
                penalty=round(penalty, 3),
                summary_ru=summary,
            ),
        )

    def eligible(self, scored: ScoredJob) -> bool:
        """Заказ проходит, если не отсеян фильтрами категорий/цены.

        Порогов балла и совпадения навыков больше нет: скоринг влияет только
        на порядок показа (штраф красных флагов и старые заказы — в конце).
        Метод оставлен для обратной совместимости и всегда возвращает True.
        """
        return True

    def rank(
        self, jobs: list[Job], top_n: int | None = None, now: datetime | None = None
    ) -> list[ScoredJob]:
        scored = [self.score(job, now) for job in jobs]
        scored.sort(
            key=lambda s: (-s.score.total, s.job.published_at or datetime.min.replace(tzinfo=UTC))
        )
        if top_n is None:
            return scored
        return scored[:top_n]

    @staticmethod
    def _summary(factors: list[ScoreFactor], flags, penalty: float) -> str:
        best = sorted(factors, key=lambda f: f.contribution, reverse=True)[:2]
        worst = min(factors, key=lambda f: f.raw)
        parts = [f.rationale for f in best]
        if worst.raw < 0.4:
            parts.append(f"Слабое место: {worst.rationale.lower()}")
        if flags:
            names = {
                "unpaid_test": "бесплатный тест",
                "offplatform_payment": "оплата вне безопасной сделки",
                "contact_before_hire": "контакт до найма",
                "scope_mismatch": "несоответствие объёма и бюджета",
                "vague_brief": "размытое ТЗ",
                "urgency_pressure": "давление сроком",
            }
            parts.append(
                "Красные флаги: " + ", ".join(names.get(f.code, f.code) for f in flags) + "."
            )
        if penalty:
            parts.append(f"Штраф {penalty * 100:.0f}%.")
        return " ".join(parts)
