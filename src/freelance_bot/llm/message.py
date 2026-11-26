"""Русский анализ + генерация первого сообщения с исправлением
и детерминированным запасным вариантом."""

from __future__ import annotations

import logging
import re
from html import unescape as html_unescape
from typing import Literal

from ..config import AppConfig, Secrets
from ..domains import detect_domain
from ..models import GeneratedMessage, Job, JobAnalysis
from ..profile import FreelancerProfile, expand_skill, skill_domains
from ..text import contains_term, split_sentences
from .client import LLMClient, LLMError, LLMQuotaExhausted
from .guard import check
from .prompts import analysis_prompt, build_system_prompt, repair_prompt

log = logging.getLogger(__name__)


def _as_list(value: object, limit: int = 5) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()][:limit]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(re.sub(r"[^\d.]", "", value))
        except ValueError:
            return None
    return None


class MessageGenerator:
    def __init__(
        self,
        config: AppConfig,
        secrets: Secrets,
        profile: FreelancerProfile | None = None,
    ) -> None:
        self.config = config
        self.llm_cfg = config.llm
        self.client = LLMClient(config.llm, secrets)
        self.profile = profile

    @property
    def _system_prompt(self) -> str:
        return build_system_prompt(self.llm_cfg)

    # ---------------------------------------------------------------- анализ
    async def analyse(self, job: Job) -> tuple[JobAnalysis, str | None]:
        """Возвращает (анализ, сырое первое сообщение). При неудаче — эвристики."""
        if not self.client.available:
            return self._heuristic_analysis(job), None
        if self.client.quota_exhausted():
            # Квота провайдера исчерпана: каждый вызов вернёт 429. Заказ всё
            # равно должен дойти до чата, поэтому идём запасным путём — он
            # теперь пишет про ту же работу, что и заказ. Модель не трогаем,
            # чтобы не тратить цикл на заведомо отклоняемые запросы.
            log.warning(
                "LLM quota exhausted, using fallback for %s (letters stay "
                "on-topic but without model wording)",
                job.uid,
            )
            return self._heuristic_analysis(job), None
        domain, _approach = self._domain_and_approach(job)
        matched = self._matched_skills(job)
        try:
            payload = await self.client.complete_json(
                self._system_prompt, analysis_prompt(job, self.profile, matched, domain)
            )
        except LLMQuotaExhausted as e:
            log.warning("LLM quota exhausted for %s: %s", job.uid, e)
            return self._heuristic_analysis(job), None
        except Exception as e:  # noqa: BLE001 - never fail the cycle on LLM
            # Молчаливый переход на запасной текст скрывал, что модель не
            # отвечает вовсе: в базе все письма были «fallback», и причину
            # приходилось искать по логам. Теперь отказ видно сразу, а текст
            # письма всё равно готовится детерминированно.
            log.warning("LLM analysis failed for %s: %s", job.uid, e)
            return self._heuristic_analysis(job), None

        analysis = JobAnalysis(
            need_summary_ru=str(payload.get("need_summary_ru") or "").strip(),
            requirements=_as_list(payload.get("requirements")),
            deliverables=_as_list(payload.get("deliverables")),
            risks=_as_list(payload.get("risks"), 3),
            questions_to_ask=_as_list(payload.get("questions_to_ask"), 2),
            estimated_hours=_as_float(payload.get("estimated_hours")),
            suggested_price_rub=_as_float(payload.get("suggested_price_rub")),
            skill_match=_as_list(payload.get("skill_match")),
        )
        draft = payload.get("first_message")
        return analysis, str(draft).strip() if draft else None

    # ---------------------------------------------------------------- цены
    def resolve_price(self, job: Job, analysis: JobAnalysis | None) -> tuple[float | None, str]:
        """Определяет цену предложения и описывает, как она получена.

                Возвращает ``(price_rub, strategy_label)``. Стратегию выбирает
                пользователь в ``profile.yaml``:
                  - ``minimum``  -> цена заказа (чуть ниже) или нижняя граница
                  - ``markup``   -> бюджет заказа * markup_percent
                  - ``llm_estimate`` -> оценка LLM в часах * часовая ставка
        """
        profile = self.profile
        if profile is None:
            return None, "no-profile"

        budget = job.budget_rub_estimate or job.budget_amount
        pricing = profile.pricing

        if pricing.strategy == "minimum":
            if budget and budget >= pricing.min_acceptable_rub:
                price = max(budget * 0.95, pricing.min_acceptable_rub)
                return price, "minimum"
            return pricing.min_acceptable_rub, "minimum"

        if pricing.strategy == "markup":
            if budget:
                price = max(budget * pricing.markup_percent, pricing.min_acceptable_rub)
                return price, "markup"
            # В ТЗ нет бюджета: используем нижнюю границу.
            return pricing.min_acceptable_rub, "markup-floor"

        # llm_estimate
        hours = analysis.estimated_hours if analysis is not None else None
        if hours:
            price = max(hours * pricing.hourly_rate_rub, pricing.min_acceptable_rub)
            return price, "llm-estimate"
        if budget:
            price = max(budget, pricing.min_acceptable_rub)
            return price, "llm-estimate-fallback"
        return pricing.min_acceptable_rub, "llm-estimate-floor"

    def price_is_acceptable(self, price_rub: float | None) -> bool:
        """Проходит ли рассчитанная цена настроенную нижнюю границу.

                Без профиля нижней границы нет, поэтому ничего не отфильтровывается.
        """
        if self.profile is None:
            return True
        if price_rub is None:
            return False
        return price_rub >= self.profile.pricing.min_acceptable_rub

    # ---------------------------------------------------------------- генерация
    @property
    def _extra_banned(self) -> list[str]:
        """Запрещённые фразы из профиля фрилансера."""
        if self.profile is None:
            return []
        return list(self.profile.message_style.banned_phrases)

    async def generate(self, job: Job) -> tuple[JobAnalysis, GeneratedMessage]:
        limit = self.config.messaging.limit_for(job.platform)
        analysis, draft = await self.analyse(job)
        source: Literal["llm", "fallback"] = "llm"

        if not draft:
            if not self.llm_cfg.fallback_enabled:
                raise LLMError("LLM unavailable and fallback disabled")
            draft = self._fallback_message(job, analysis)
            source = "fallback"

        violations = check(draft, job, limit, self.llm_cfg.style, self._extra_banned)
        if violations and source == "llm":
            draft = await self._repair(draft, violations, limit, job)
            violations = check(draft, job, limit, self.llm_cfg.style, self._extra_banned)

        if violations and self.llm_cfg.fallback_enabled:
            candidate = self._fallback_message(job, analysis)
            cand_violations = check(candidate, job, limit, self.llm_cfg.style, self._extra_banned)
            if len(cand_violations) < len(violations):
                draft, violations, source = candidate, cand_violations, "fallback"

        return analysis, GeneratedMessage(
            platform=job.platform,
            job_uid=job.uid,
            text=draft.strip(),
            source=source,
            model=self.client.model if source == "llm" else None,
            violations=violations,
        )

    async def _repair(self, draft: str, violations: list[str], limit, job: Job) -> str:
        try:
            repaired = await self.client.complete(
                self._system_prompt,
                repair_prompt(draft, violations, limit.max_chars),
                temperature=0.3,
            )
            return repaired.strip().strip('"').strip()
        except LLMError:
            return draft

    # ---------------------------------------------------------------- запасной путь
    def _heuristic_analysis(self, job: Job) -> JobAnalysis:
        text = job.description or ""
        sentences = split_sentences(text)
        summary = sentences[0][:220] if sentences else job.title
        requirements = [s[:160] for s in sentences[1:4]]
        risks = []
        if len(text) < 120:
            risks.append("Краткое описание — детали придётся уточнять.")
        if job.budget_amount is None:
            risks.append("Бюджет не указан.")
        return JobAnalysis(
            need_summary_ru=summary,
            requirements=requirements,
            deliverables=[],
            risks=risks,
            questions_to_ask=[],
        )

    def _job_detail(self, job: Job, analysis: JobAnalysis, limit: int = 260) -> str:
        """Собирает фрагмент деталей под конкретный заказ из ТЗ.

        Берётся первое содержательное предложение описания, но с двумя
        ограничениями. Во-первых, дословная цитата объявления в первом
        сообщении читается как автоответчик: заказчик видит свой же текст.
        Поэтому длинные куски отбрасываются, а из оставшегося убираются
        служебные обороты площадки. Во-вторых, цитата должна быть именно про
        работу, а не про компанию заказчика: «Мы — digital-агентство» ничего
        не говорит о задаче.
        """
        source = analysis.need_summary_ru or job.description or job.title
        sentences = split_sentences(source)
        if len(sentences) < 2 and job.description:
            extra = [
                s for s in split_sentences(job.description) if s not in sentences
            ]
            sentences.extend(extra[:2])

        # Одного предложения часто мало: заказчик пишет короткими фразами
        # («Нужно анимировать сцену. Много гринпенсела. 3 секунд.»), и письмо
        # оставалось без деталей — проверка конкретики его блокировала.
        # Поэтому набираем фразы, пока не получится связный кусок.
        parts: list[str] = []
        for sentence in sentences:
            # Сначала отрезаем повтор заголовка, потом оцениваем: фраза
            # «Вывести в ТОП-50 … эту страницу» начинается с заголовка, но
            # именно она и содержит деталь задачи.
            candidate = self._strip_title_prefix(sentence, job)
            if not self._detail_is_useful(candidate, job):
                continue
            parts.append(candidate)
            if len(" ".join(parts)) >= 90:
                break
        detail = " ".join(parts)
        # После отрезанного заголовка фраза начинается со строчной («по 50%
        # запросов…»), а в письме она идёт после точки — нужна заглавная.
        if detail and detail[0].islower():
            detail = detail[0].upper() + detail[1:]
        return self._clean_for_message(detail, limit)

    @staticmethod
    def _strip_title_prefix(text: str, job: Job) -> str:
        """Убирает заголовок заказа из начала фразы."""
        title = " ".join((job.title or "").split())
        if not title or not text:
            return text
        lowered_text, lowered_title = text.casefold(), title.casefold()
        if not lowered_text.startswith(lowered_title):
            return text
        rest = text[len(title) :].lstrip(" .,;:—–-")
        # Если после заголовка почти ничего нет, повтор безвреден.
        return rest if len(rest) >= 25 else text

    @staticmethod
    def _detail_is_useful(sentence: str, job: Job) -> bool:
        """Годится ли предложение как деталь в письме."""
        text = sentence.strip()
        if len(text) < 15:
            return False
        # Слишком длинный кусок — это уже пересказ ТЗ, а не деталь.
        if len(text) > 220:
            return False
        lowered = text.casefold()
        # Заказчик рассказывает о себе, а не о задаче.
        if lowered.startswith(("мы ", "наша компания", "наш ", "меня зовут")):
            return False
        # Отказ от канала связи и служебные пометки — не деталь задачи.
        # «p.s.: на почту и в телеграм просьба не писать» в письме читается
        # как совет заказчику, а не как понимание работы.
        if lowered.startswith(("p.s", "ps.", "p. s", "примечание", "важно:")):
            return False
        if re.search(
            r"(?:не\s+(?:писать|пишите|звонить|звоните)|просьба\s+не|"
            r"альтернативн\w*\s+канал)",
            lowered,
        ):
            return False
        # Повтор заголовка не добавляет ничего нового.
        title = job.title.casefold().strip()
        repeats_title = bool(
            title and (lowered.startswith(title[:20]) or title.startswith(lowered[:20]))
        )
        return not repeats_title

    @staticmethod
    def _clean_for_message(text: str, limit: int = 140) -> str:
        from .guard import EMOJI_RE

        # Текст из базы мог быть сохранён до нормализации: там остались
        # «&mdash;», «&nbsp;» и «&laquo;». Заказчик не должен их видеть.
        text = html_unescape(text or "")
        text = " ".join(text.split())
        text = EMOJI_RE.sub("", text)
        # Никогда не цитируем ссылки, ники, email и голые домены из ТЗ.
        text = re.sub(
            r"(https?://\S+|www\.\S+|\bt\.me/\S+|\b[\w.+-]+@[\w-]+\.[\w.]+\b|@\w{4,})",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\b[\w-]+\.(?:ru|com|net|io|org|info|me|рф)\b",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = text.replace("!", ".")
        text = re.sub(r"\.{2,}", ".", text)
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"\s+([,.;:])", r"\1", text)
        # Площадка часто ставит в начало описания служебную подпись
        # («Сайт: Задача: …»). В письме она читается как обрывок формы.
        for _ in range(3):
            stripped = re.sub(
                r"^(?:сайт|задача|тз|описание|суть|что нужно|что требуется|нужно)"
                r"\s*:\s*",
                "",
                text,
                flags=re.IGNORECASE,
            )
            if stripped == text:
                break
            text = stripped
        text = re.sub(r"^[\s.,;:—–-]+", "", text)
        text = re.sub(r"\s*[—–]\s*", " — ", text)
        if len(text) > limit:
            text = text[:limit].rsplit(" ", 1)[0]
        text = MessageGenerator._balance(text)
        return text.rstrip(" .,;:—–-")

    @staticmethod
    def _balance(text: str) -> str:
        """Убирает незакрытую скобку или кавычку после жёсткого обрезания."""
        for open_ch, close_ch in (("(", ")"), ("«", "»")):
            while text.count(open_ch) > text.count(close_ch):
                idx = text.rfind(open_ch)
                text = text[:idx] if idx > 0 else ""
        return text

    @staticmethod
    def _domain_and_approach(job: Job) -> tuple[str | None, str]:
        """Возвращает (область, подход) для заказа.

                Само определение живёт в `domains`: им же пользуется проверка
                качества, чтобы поймать письмо не о той работе. Раньше логика
                была здесь и смотрела на склеенный текст, из-за чего заказ
                «сайт или автоматизация» уходил в область бота.
        """
        return detect_domain(job)

    @classmethod
    def _approach_for(cls, job: Job) -> str:
        """Выбирает правдоподобный, необязывающий подход по сигналам самого заказа.
        """
        return cls._domain_and_approach(job)[1]

    def _fallback_message(self, job: Job, analysis: JobAnalysis) -> str:
        """Детерминированный русский текст под конкретный заказ. Не замена LLM,
                но он должен оставаться пригодным к отправке, если модель недоступна.

                Использует профиль фрилансера (навыки, цену), чтобы даже запасной
                путь звучал как тот же человек, что и путь через LLM.
        """
        title = self._clean_for_message(job.title, 80)
        detail = self._job_detail(job, analysis, 260)
        detail = re.sub(
            r"^(?:здравствуйте|добрый день|добрый вечер|привет(?:ствую)?)[!,. ]+",
            "",
            detail,
            flags=re.IGNORECASE,
        )
        if len(detail) < 25 or detail.casefold().startswith("тз "):
            detail = ""
        if detail.casefold().startswith(title.casefold()[:20]):
            detail = ""

        intro = f"Здравствуйте. Увидел задачу «{title}»."
        if detail and detail.casefold() != title.casefold():
            intro += f" {detail.rstrip('.')}."

        # Заказчик должен понять из первой фразы, что речь именно о его
        # заказе, а не о рассылке. Раньше письмо начиналось с общего
        # «Увидел задачу», и в заказе на сайт дальше шёл текст про ботов.
        approach = self._approach_for(job)
        if approach:
            approach = f"Готов взяться: {approach[0].lower()}{approach[1:]}"

        # Упоминаем навыки из профиля, которые действительно подходят к заказу.
        matched = self._matched_skills(job)
        skill_line = ""
        if matched:
            skill_line = " По опыту: " + ", ".join(matched[:3]) + "."

        # Строка цены зависит от выбранной пользователем стратегии.
        price_line = ""
        if self.profile is not None:
            price, _strategy = self.resolve_price(job, analysis)
            if self.profile.pricing.include_price_in_message and price:
                price_line = f" По стоимости ориентируюсь на {price:,.0f} ₽.".replace(",", " ")

        # Вопрос, который подталкивает клиента ответить и назвать бюджет.
        if job.budget_amount is None:
            question = "Какой бюджет закладываете на эту задачу?"
        elif not job.deadline_at:
            question = "К какому сроку нужен результат?"
        elif job.description and len(job.description) < 140:
            question = "Что уже есть по задаче — материалы, доступы или примеры?"
        else:
            question = "Что уже готово по проекту, на что можно опереться?"

        if analysis.questions_to_ask:
            question = analysis.questions_to_ask[0]

        price_hint = ""
        if self.profile is not None and not self.profile.pricing.include_price_in_message:
            price_hint = " По объёму сразу скажу, сколько это будет стоить."

        parts = [intro, f"{approach}.", skill_line, price_line, f"{question}{price_hint}"]
        text = " ".join(p.strip() for p in parts if p and p.strip())
        text = re.sub(r"\s+", " ", text).strip()
        text = text.replace("!", ".")
        return text

    def _matched_skills(self, job: Job) -> list[str]:
        """Навыки профиля, релевантные заказу, отсортированные по значимости.

                Комбинируются три сигнала:

                * где найден навык — заголовок говорит, о чём заказ, а список
                  тегов площадки может быть шумом;
                * относится ли навык к области заказа — заказ на вёрстку должен
                  представлять веб-разработчика, а не бэкендера;
                * навыки из *другой* области вытесняются.
        """
        if self.profile is None:
            return []

        title = job.title.lower()
        description = job.description.lower()
        tags = " ".join(job.skills).lower()
        domain, _approach = self._domain_and_approach(job)

        scored: list[tuple[int, int, str]] = []
        for order, skill in enumerate(self.profile.all_skills):
            variants = expand_skill(skill)
            if not variants:
                continue

            in_title = any(contains_term(title, v) for v in variants)
            in_description = any(contains_term(description, v) for v in variants)
            in_tags = any(contains_term(tags, v) for v in variants)
            text_hit = in_title or in_description

            domains = skill_domains(skill)
            in_domain = domain is not None and domain in domains

            if text_hit:
                # Упомянуто в самом ТЗ: всегда уместно, что бы ни
                # говорит область.
                score = (4 if in_title else 0) + (3 if in_description else 0)
                if in_domain:
                    score += 2
            elif domain is not None:
                # Нет в ТЗ: оставляем, только если относится к этой области.
                score = 2 if in_domain else -1
            else:
                score = 1 if in_tags else 0

            if score > 0:
                scored.append((score, -order, skill))

        scored.sort(reverse=True)
        return [skill for _score, _order, skill in scored]
