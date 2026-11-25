"""Четырёхшаговый AI-квиз: письмо-ответ под конкретный заказ.

Пользователь отвечает на четыре вопроса (тон, акцент, цена, уточняющий
вопрос), после чего LLM собирает письмо. Готовый текст показывается с
кнопками «Редактировать», «Скопировать», «Перегенерировать».

Состояние квиза — в памяти процесса: это короткий диалог, который живёт
минуты. Черновик письма, в отличие от ответов, сохраняется в SQLite
(``store.set_draft``), поэтому кнопки копирования работают после рестарта.
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass, field
from html import escape as html_escape
from typing import TYPE_CHECKING, Any

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .llm.client import LLMClient, LLMError
from .llm.guard import check
from .llm.prompts import build_system_prompt
from .models import Job

if TYPE_CHECKING:
    from .config import AppConfig, Secrets
    from .profile import FreelancerProfile
    from .storage import JobStore

log = logging.getLogger(__name__)

#: Теги карточки, из которой восстанавливается описание заказа.
_HTML_TAG_RE = re.compile(r"<[^>]*>")


@dataclass(frozen=True)
class QuizOption:
    key: str
    label: str


@dataclass(frozen=True)
class QuizStep:
    key: str
    title: str
    question: str
    options: tuple[QuizOption, ...]


QUIZ_STEPS: tuple[QuizStep, ...] = (
    QuizStep(
        "tone",
        "Тон письма",
        "Как писать заказчику?",
        (
            QuizOption("friendly", "Дружелюбный"),
            QuizOption("professional", "Деловой"),
            QuizOption("neutral", "Нейтральный"),
        ),
    ),
    QuizStep(
        "focus",
        "Что подчеркнуть",
        "На чём сделать акцент?",
        (
            QuizOption("experience", "Опыт с похожими задачами"),
            QuizOption("plan", "Конкретный план работ"),
            QuizOption("speed", "Скорость и сроки"),
        ),
    ),
    QuizStep(
        "price",
        "Цена в письме",
        "Говорить о деньгах?",
        (
            QuizOption("name", "Назвать цену"),
            QuizOption("ask", "Спросить бюджет"),
            QuizOption("skip", "Не упоминать"),
        ),
    ),
    QuizStep(
        "question",
        "Уточняющий вопрос",
        "Что спросить у заказчика?",
        (
            QuizOption("details", "По деталям задачи"),
            QuizOption("deadline", "По срокам"),
            QuizOption("budget", "По бюджету"),
        ),
    ),
)

TONE_HINTS = {
    "friendly": "спокойный, живой, слегка неформальный тон",
    "professional": "деловой, сдержанный тон без панибратства",
    "neutral": "нейтральный тон, только по делу",
}
FOCUS_HINTS = {
    "experience": "подчеркни релевантный опыт похожими задачами",
    "plan": "покажи короткий конкретный план работ по шагам",
    "speed": "подчеркни скорость старта и реальные сроки",
}
PRICE_HINTS = {
    "name": "назови ориентировочную цену и объясни, из чего она складывается",
    "ask": "не называй цену, спроси, какой бюджет заложен",
    "skip": "не упоминай деньги вовсе",
}
QUESTION_HINTS = {
    "details": "задай один вопрос по деталям задачи",
    "deadline": "задай один вопрос по срокам",
    "budget": "задай один вопрос по бюджету",
}

#: Сколько ответов хранится одновременно: старые сессии не нужны.
MAX_SESSIONS = 200

Q_START = "aq:start"
Q_STEP = "aq:step"
Q_EDIT = "aq:edit"
Q_COPY = "aq:copy"
Q_REGENERATE = "aq:regen"
Q_CANCEL = "aq:cancel"


@dataclass
class QuizSession:
    """Ответы пользователя на шаги квиза по одному заказу."""

    chat_id: int
    job_uid: str
    answers: dict[str, str] = field(default_factory=dict)
    step: int = 0
    draft: str = ""
    #: True, когда ждём свободный текст вместо нажатия кнопки.
    awaiting_text: bool = False


def _session_key(chat_id: int, job_uid: str) -> str:
    return f"{chat_id}|{job_uid}"


def quiz_progress(session: QuizSession) -> tuple[int, int]:
    """(номер текущего шага, всего шагов) для заголовка сообщения."""
    return min(session.step + 1, len(QUIZ_STEPS)), len(QUIZ_STEPS)


def quiz_prompt_text(session: QuizSession, job: Job) -> str:
    """Текст текущего шага квиза."""
    index = min(session.step, len(QUIZ_STEPS) - 1)
    step = QUIZ_STEPS[index]
    current, total = quiz_progress(session)
    chosen = session.answers.get(step.key)
    note = f"\nВаш ответ: <b>{html_escape(_option_label(step, chosen))}</b>" if chosen else ""
    return (
        f"🪄 <b>Письмо под заказ</b>\n"
        f"<i>{html_escape(job.title[:120])}</i>\n\n"
        f"Шаг {current} из {total} · {html_escape(step.title)}\n"
        f"<b>{html_escape(step.question)}</b>{note}"
    )


def _option_label(step: QuizStep, key: str | None) -> str:
    for option in step.options:
        if option.key == key:
            return option.label
    return "—"


def quiz_keyboard(session: QuizSession, job: Job) -> InlineKeyboardMarkup:
    """Кнопки текущего шага: варианты, «свой вариант», отмена."""
    index = min(session.step, len(QUIZ_STEPS) - 1)
    step = QUIZ_STEPS[index]
    rows: list[list[InlineKeyboardButton]] = []
    for option in step.options:
        rows.append(
            [
                InlineKeyboardButton(
                    option.label,
                    callback_data=f"{Q_STEP}:{index}:{option.key}:{job.uid}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "✍️ Свой вариант", callback_data=f"{Q_EDIT}:{job.uid}"
            )
        ]
    )
    rows.append([InlineKeyboardButton("✖️ Отмена", callback_data=f"{Q_CANCEL}:{job.uid}")])
    return InlineKeyboardMarkup(rows)


def edit_keyboard(job: Job) -> InlineKeyboardMarkup:
    """Кнопки режима правки: выйти можно всегда, чтобы не застрять в ожидании."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✖️ Отмена", callback_data=f"{Q_CANCEL}:{job.uid}")],
        ]
    )


def draft_keyboard(job: Job) -> InlineKeyboardMarkup:
    """Кнопки готового письма: редактировать, скопировать, перегенерировать."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Редактировать", callback_data=f"{Q_EDIT}:{job.uid}"),
                InlineKeyboardButton("📋 Скопировать", callback_data=f"{Q_COPY}:{job.uid}"),
            ],
            [
                InlineKeyboardButton(
                    "🔄 Перегенерировать", callback_data=f"{Q_REGENERATE}:{job.uid}"
                )
            ],
        ]
    )


def draft_text(
    job: Job,
    draft: str,
    *,
    regenerated: bool = False,
    fallback: bool = False,
) -> str:
    """Сообщение с готовым письмом (в <pre>, чтобы копировалось тапом)."""
    if fallback:
        title = "✍️ Письмо собрано по шаблону"
        note = (
            "LLM недоступен, поэтому текст собран из ваших ответов по шаблону. "
            "Проверьте и поправьте под себя."
        )
    else:
        title = "🔄 Письмо перегенерировано" if regenerated else "✅ Письмо готово"
        note = "Что дальше: отредактировать, скопировать или перегенерировать."
    return (
        f"{title}\n"
        f"<i>{html_escape(job.title[:120])}</i>\n\n"
        f"<pre>{html_escape(draft)}</pre>\n\n"
        f"{note}"
    )


def build_fallback_draft(
    job: Job,
    answers: dict[str, str],
    profile: FreelancerProfile | None = None,
) -> str:
    """Письмо по шаблону, когда LLM недоступен.

    Квиз уже собрал ответы пользователя, поэтому шаблон не «здравствуйте, я
    специалист»: он подставляет выбранный тон, акцент, цену и вопрос. Это
    заметно лучше, чем тупик с ошибкой, и текст можно сразу править.
    """
    title = " ".join((job.title or "").split())[:90].rstrip(" ,.;:—-")
    tone = answers.get("tone", "neutral")
    focus = answers.get("focus", "plan")
    price_mode = answers.get("price", "ask")
    question_mode = answers.get("question", "details")

    if tone == "friendly":
        hello = "Здравствуйте!"
    elif tone == "professional":
        hello = "Добрый день."
    else:
        hello = "Здравствуйте."

    lines = [f"{hello} Увидел задачу «{title}»."]

    if focus == "experience":
        skills = ", ".join((profile.skills if profile else [])[:3])
        lines.append(
            f"С похожими задачами работал не раз{f', в том числе по {skills}' if skills else ''}."
        )
    elif focus == "speed":
        lines.append("Могу взяться сразу и держать вас в курсе на каждом этапе.")
    else:
        lines.append(
            "Вижу задачу так: сначала уточняю детали, затем делаю основную часть "
            "и показываю результат."
        )

    amount = job.budget_rub_estimate or job.budget_amount
    if price_mode == "name" and amount:
        lines.append(f"По стоимости ориентируюсь на {amount:,.0f} ₽.".replace(",", " "))
    elif price_mode == "ask":
        lines.append("Подскажите, какой бюджет заложен на эту задачу?")

    if question_mode == "deadline":
        lines.append("К какому сроку нужен результат?")
    elif question_mode == "budget" and price_mode != "ask":
        lines.append("На какой бюджет рассчитываете?")
    elif question_mode == "details":
        lines.append(
            "Что уже есть по задаче — материалы, доступы или примеры того, "
            "что вам нравится?"
        )

    lines.append("Готов обсудить детали в удобном для вас формате.")
    text = " ".join(lines)
    limit = 900
    if len(text) > limit:
        cut = text[:limit]
        space = cut.rfind(" ")
        text = cut[:space].rstrip(" ,.;:") + "."
    return text


def build_prompt(
    job: Job,
    answers: dict[str, str],
    *,
    price_rub: float | None,
    profile: FreelancerProfile | None,
) -> str:
    """Собирает пользовательский промпт из ответов квиза."""
    parts = [
        "Составь первое сообщение заказчику на бирже фриланса.",
        "",
        f"Заголовок: {job.title}",
        f"Бюджет: {job.budget_rub_estimate or job.budget_amount or 'не указан'}",
        f"Описание: {job.description[:2500]}",
    ]
    if profile is not None and profile.skills:
        parts.append("Мои навыки: " + ", ".join(profile.skills[:10]))
    parts += [
        "",
        "Пожелания к письму:",
        "- " + TONE_HINTS.get(answers.get("tone", ""), TONE_HINTS["neutral"]),
        "- " + FOCUS_HINTS.get(answers.get("focus", ""), FOCUS_HINTS["plan"]),
        "- " + PRICE_HINTS.get(answers.get("price", ""), PRICE_HINTS["ask"]),
        "- " + QUESTION_HINTS.get(answers.get("question", ""), QUESTION_HINTS["details"]),
    ]
    if price_rub:
        parts.append(f"- ориентир по цене: {price_rub:.0f} ₽")
    parts += [
        "",
        "Требования: 4–6 предложений, без markdown, без эмодзи, без ссылок, "
        "без восклицательных знаков, обращение на «вы». Верни только текст письма.",
    ]
    return "\n".join(parts)


class QuizCommandsMixin:
    """AI-квиз и обращение к LLM для персонального письма."""

    config: AppConfig
    profile: FreelancerProfile | None
    secrets: Secrets | None
    store: JobStore | None
    pending_approvals: dict[str, Any]
    _quiz_sessions: dict[str, QuizSession]
    #: Выставляется SettingsCommandsMixin; квиз читает его в /cancel.
    _awaiting_input: str | None

    async def _deny_insufficient_rights(self, update: Update) -> None:  # pragma: no cover
        raise NotImplementedError

    async def _reply(
        self,
        update: Update,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    async def _bot_send(self, **kwargs: object) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # ------------------------------------------------------------------ состояние
    def _quiz_session(self, chat_id: int, job_uid: str) -> QuizSession:
        key = _session_key(chat_id, job_uid)
        session = self._quiz_sessions.get(key)
        if session is None:
            # Простейшая защита от роста памяти: давние диалоги не нужны.
            if len(self._quiz_sessions) >= MAX_SESSIONS:
                self._quiz_sessions.pop(next(iter(self._quiz_sessions)))
            session = QuizSession(chat_id=chat_id, job_uid=job_uid)
            self._quiz_sessions[key] = session
        return session

    def _render_quiz(self, session: QuizSession, job: Job) -> tuple[str, InlineKeyboardMarkup]:
        return quiz_prompt_text(session, job), quiz_keyboard(session, job)

    def _session_draft(self, session: QuizSession) -> str:
        """Черновик из сессии, а после рестарта — из БД.

        Сессии живут в памяти, поэтому готовое письмо теряется при
        перезапуске бота. Без подъёма из БД кнопка «Редактировать» на старом
        сообщении предлагала бы заново пройти квиз вместо правки письма.
        """
        if session.draft:
            return session.draft
        if self.store is None:
            return ""
        try:
            order = self.store.get_order(session.job_uid)
        except Exception:  # noqa: BLE001 - БД не должна ломать квиз
            log.exception("cannot load draft for %s", session.job_uid)
            return ""
        draft = str(order.get("draft_text") or "") if order else ""
        if draft:
            session.draft = draft
            session.step = len(QUIZ_STEPS)
        return draft

    # ------------------------------------------------------------------ callback'и
    async def _handle_quiz_callback(
        self, update: Update, query: CallbackQuery, data: str
    ) -> bool:
        """Обрабатывает callback'и ``aq:*``. Возвращает True, если обработал."""
        if not data.startswith("aq:"):
            return False
        chat = update.effective_chat
        if chat is None:
            await query.answer("⚠️ Не удалось определить чат.", show_alert=True)
            return True
        chat_id = chat.id

        if data.startswith(Q_START):
            job_uid = data.split(":", 2)[2]
            await self._quiz_open(update, query, chat_id, job_uid)
            return True

        if data.startswith(Q_STEP):
            # aq:step:<index>:<key>:<uid> — uid содержит ':', поэтому он последний.
            parts = data.split(":", 4)
            if len(parts) < 5:
                await query.answer("⚠️ Неверный формат ответа.", show_alert=True)
                return True
            _, _, raw_index, key, job_uid = parts
            try:
                index = int(raw_index)
            except ValueError:
                index = 0
            await self._quiz_answer(update, query, chat_id, job_uid, index, key)
            return True

        if data.startswith(Q_EDIT):
            job_uid = data.split(":", 2)[2]
            await self._quiz_begin_edit(update, query, chat_id, job_uid)
            return True

        if data.startswith(Q_COPY):
            job_uid = data.split(":", 2)[2]
            await self._quiz_copy(update, query, chat_id, job_uid)
            return True

        if data.startswith(Q_REGENERATE):
            job_uid = data.split(":", 2)[2]
            await self._quiz_regenerate(update, query, chat_id, job_uid)
            return True

        if data.startswith(Q_CANCEL):
            job_uid = data.split(":", 2)[2]
            self._quiz_sessions.pop(_session_key(chat_id, job_uid), None)
            await query.answer("Отменено")
            with contextlib.suppress(TelegramError):
                await query.edit_message_reply_markup(reply_markup=None)
            return True

        await query.answer("⚠️ Неизвестный шаг квиза.", show_alert=True)
        return True

    async def _quiz_open(
        self, update: Update, query: CallbackQuery, chat_id: int, job_uid: str
    ) -> None:
        job = self._quiz_job(job_uid)
        if job is None:
            await query.answer("⚠️ Заказ не найден.", show_alert=True)
            return
        session = self._quiz_session(chat_id, job_uid)
        session.answers.clear()
        session.step = 0
        session.awaiting_text = False
        text, markup = self._render_quiz(session, job)
        await query.answer()
        await self._quiz_show(query, chat_id, text, markup)

    async def _quiz_answer(
        self,
        update: Update,
        query: CallbackQuery,
        chat_id: int,
        job_uid: str,
        index: int,
        key: str,
    ) -> None:
        job = self._quiz_job(job_uid)
        if job is None:
            await query.answer("⚠️ Заказ не найден.", show_alert=True)
            return
        if not 0 <= index < len(QUIZ_STEPS):
            await query.answer("⚠️ Неверный шаг.", show_alert=True)
            return
        step = QUIZ_STEPS[index]
        if key not in {option.key for option in step.options}:
            await query.answer("⚠️ Неверный вариант.", show_alert=True)
            return

        session = self._quiz_session(chat_id, job_uid)
        # Ответ на пропущенный шаг не ломает порядок: квиз всегда идёт вперёд.
        session.answers[step.key] = key
        session.step = max(session.step, index + 1)
        session.awaiting_text = False
        await query.answer()

        if session.step >= len(QUIZ_STEPS):
            await self._quiz_finish(update, query, session, job)
            return
        text, markup = self._render_quiz(session, job)
        await self._quiz_show(query, chat_id, text, markup)

    async def _quiz_begin_edit(
        self, update: Update, query: CallbackQuery, chat_id: int, job_uid: str
    ) -> None:
        """«✏️ Редактировать»: свой вариант ответа или правка готового письма."""
        job = self._quiz_job(job_uid)
        if job is None:
            await query.answer("⚠️ Заказ не найден.", show_alert=True)
            return
        session = self._quiz_session(chat_id, job_uid)
        session.awaiting_text = True
        await query.answer("Пришлите текст одним сообщением")
        # Черновик мог остаться в БД после рестарта: тогда это правка письма,
        # а не заново пройденный квиз.
        if self._session_draft(session) and session.step >= len(QUIZ_STEPS):
            text = (
                "✏️ <b>Правка письма</b>\n\n"
                "Пришлите новый текст письма <b>одним сообщением</b> — он заменит текущий.\n\n"
                "<i>Можно прислать и часть: например, только первый абзац — "
                "письмо заменится целиком, так что пришлите весь текст.</i>"
            )
        else:
            index = min(session.step, len(QUIZ_STEPS) - 1)
            step = QUIZ_STEPS[index]
            text = (
                f"✍️ <b>Свой вариант · {html_escape(step.title)}</b>\n\n"
                f"{html_escape(step.question)}\n\n"
                "Напишите свой ответ <b>одним сообщением</b>.\n"
                "<i>Отмена — /cancel или кнопка ниже.</i>"
            )
        # Это сообщение несёт инструкцию, поэтому оно не должно зависеть от
        # возможности править карточку: ошибку правки видно только в логе,
        # а пользователь в это время видит «кнопка не работает».
        await self._quiz_show(query, chat_id, text, edit_keyboard(job))

    async def _quiz_copy(
        self, update: Update, query: CallbackQuery, chat_id: int, job_uid: str
    ) -> None:
        session = self._quiz_sessions.get(_session_key(chat_id, job_uid))
        draft = session.draft if session else ""
        if not draft and self.store is not None:
            order = self.store.get_order(job_uid)
            draft = str(order.get("draft_text") or "") if order else ""
        if not draft:
            await query.answer("⚠️ Письмо ещё не готово.", show_alert=True)
            return
        await query.answer("📋 Текст скопирован")
        # Отдельное сообщение в <pre>: Telegram показывает кнопку копирования.
        await self._bot_send(
            chat_id=chat_id,
            text=f"📋 <b>Письмо для заказа</b>\n\n<pre>{html_escape(draft)}</pre>",
            parse_mode="HTML",
        )

    async def _quiz_regenerate(
        self, update: Update, query: CallbackQuery, chat_id: int, job_uid: str
    ) -> None:
        job = self._quiz_job(job_uid)
        if job is None:
            await query.answer("⚠️ Заказ не найден.", show_alert=True)
            return
        session = self._quiz_session(chat_id, job_uid)
        if not session.answers:
            # Ответы потерялись (рестарт бота): начинаем квиз заново.
            await query.answer("Начнём заново", show_alert=True)
            session.step = 0
            text, markup = self._render_quiz(session, job)
            await self._quiz_show(query, chat_id, text, markup)
            return
        await query.answer("🔄 Генерирую заново")
        await self._quiz_finish(update, query, session, job, regenerated=True)

    async def _quiz_finish(
        self,
        update: Update,
        query: CallbackQuery,
        session: QuizSession,
        job: Job,
        *,
        regenerated: bool = False,
    ) -> None:
        """Генерирует письмо по ответам квиза и показывает результат."""
        # Ответы уже собраны, поэтому запасной шаблон лучше тупика: письмо
        # будет без LLM, но кнопки правки и копирования на месте.
        draft = await self.generate_quiz_draft(
            job, session.answers, query=query, chat_id=session.chat_id, with_fallback=True
        )
        used_fallback = False
        if not draft:
            await self._quiz_show(
                query,
                session.chat_id,
                "⚠️ Не удалось собрать письмо. Пришлите текст вручную — "
                "кнопка «✏️ Редактировать» ниже.",
                draft_keyboard(job),
            )
            return
        if self.secrets is None or not LLMClient(self.config.llm, self.secrets).available:
            used_fallback = True
        session.draft = draft
        session.step = len(QUIZ_STEPS)
        session.awaiting_text = False
        if self.store is not None:
            try:
                self.store.set_draft(job.uid, draft)
            except Exception:  # noqa: BLE001 - БД не должна ломать показ письма
                log.exception("cannot persist quiz draft for %s", job.uid)
        await self._quiz_show(
            query,
            session.chat_id,
            draft_text(job, draft, regenerated=regenerated, fallback=used_fallback),
            draft_keyboard(job),
        )

    # ------------------------------------------------------------------ ввод текста
    async def _handle_text_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Перехватывает свободный текст для квиза, иначе отдаёт настройкам.

        Квиз стоит первым в MRO, поэтому его ввод проверяется раньше путей
        конфига: у квиза нет ``_awaiting_input``, и общий обработчик вернулся
        бы сразу, не увидев ответа пользователя.
        """
        chat_id = update.effective_chat.id if update.effective_chat else None
        text = update.message.text if update.message else None
        if chat_id is not None and text and await self._apply_quiz_input(update, text, chat_id):
            return
        await super()._handle_text_input(update, context)  # type: ignore[misc]

    async def _cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """«/cancel» снимает ожидание текста и у квиза, и у настроек.

        Пользователь не должен помнить, чей именно ввод он начал: команда
        отмены обязана работать в обоих случаях.
        """
        chat_id = update.effective_chat.id if update.effective_chat else None
        cancelled_quiz = False
        if chat_id is not None:
            for session in self._quiz_sessions.values():
                if session.chat_id == chat_id and session.awaiting_text:
                    session.awaiting_text = False
                    cancelled_quiz = True
        if cancelled_quiz and self._awaiting_input is None:
            if update.message:
                await update.message.reply_text("❌ Ввод отменён.")
            return
        await super()._cmd_cancel(update, context)  # type: ignore[misc]

    async def _apply_quiz_input(self, update: Update, text: str, chat_id: int) -> bool:
        """Принимает свободный текст для квиза. True, если он был нужен."""
        session = next(
            (
                item
                for item in self._quiz_sessions.values()
                if item.chat_id == chat_id and item.awaiting_text
            ),
            None,
        )
        if session is None:
            return False
        job = self._quiz_job(session.job_uid)
        if job is None:
            session.awaiting_text = False
            return False
        session.awaiting_text = False
        if update.message is None:
            return True

        # После рестарта черновик живёт в БД: поднимаем его, иначе правка
        # письма снова ушла бы в шаги квиза.
        if self._session_draft(session) and session.step >= len(QUIZ_STEPS):
            # Правка готового письма: текст заменяет черновик целиком.
            session.draft = text.strip()
            if self.store is not None:
                try:
                    self.store.set_draft(job.uid, session.draft)
                except Exception:  # noqa: BLE001
                    log.exception("cannot persist edited draft for %s", job.uid)
            await update.message.reply_text(
                draft_text(job, session.draft), parse_mode="HTML",
                reply_markup=draft_keyboard(job),
            )
            return True

        # Свой вариант ответа на текущий шаг квиза.
        index = min(session.step, len(QUIZ_STEPS) - 1)
        step = QUIZ_STEPS[index]
        session.answers[step.key] = f"custom:{text.strip()}"
        session.step = index + 1
        prompt, markup = self._render_quiz(session, job)
        if session.step >= len(QUIZ_STEPS):
            await self.generate_and_send(job, session, chat_id)
            return True
        await update.message.reply_text(prompt, parse_mode="HTML", reply_markup=markup)
        return True

    # ------------------------------------------------------------------ генерация
    async def generate_and_send(
        self, job: Job, session: QuizSession, chat_id: int
    ) -> None:
        """Генерирует письмо после ответа текстом и отправляет в чат."""
        draft = await self.generate_quiz_draft(
            job, session.answers, chat_id=chat_id, with_fallback=True
        )
        if not draft:
            await self._bot_send(
                chat_id=chat_id,
                text=(
                    "⚠️ Не удалось собрать письмо. Пришлите текст вручную — "
                    "кнопка «✏️ Редактировать» ниже."
                ),
                reply_markup=draft_keyboard(job),
            )
            return
        fallback = self.secrets is None or not LLMClient(
            self.config.llm, self.secrets
        ).available
        session.draft = draft
        session.step = len(QUIZ_STEPS)
        if self.store is not None:
            try:
                self.store.set_draft(job.uid, draft)
            except Exception:  # noqa: BLE001
                log.exception("cannot persist quiz draft for %s", job.uid)
        await self._bot_send(
            chat_id=chat_id,
            text=draft_text(job, draft, fallback=fallback),
            parse_mode="HTML",
            reply_markup=draft_keyboard(job),
        )

    async def generate_quiz_draft(
        self,
        job: Job,
        answers: dict[str, str],
        *,
        query: CallbackQuery | None = None,
        chat_id: int | None = None,
        with_fallback: bool = False,
    ) -> str | None:
        """Вызывает LLM с ответами квиза; None, если письмо собрать не удалось.

        ``with_fallback`` включает запасной текст по шаблону: квиз уже собрал
        ответы, и оставлять пользователя с тупиком «LLM недоступен» хуже, чем
        дать черновик, который можно поправить кнопкой «Редактировать».
        """
        del query, chat_id  # параметры для симметрии вызовов и будущих уведомлений
        client = LLMClient(self.config.llm, self.secrets) if self.secrets else None
        if client is None or not client.available:
            log.info("quiz draft for %s: LLM не настроен", job.uid)
            return build_fallback_draft(job, answers, self.profile) if with_fallback else None

        price = job.budget_rub_estimate or job.budget_amount
        prompt = build_prompt(job, answers, price_rub=price, profile=self.profile)
        limit = self.config.messaging.limit_for(job.platform)
        try:
            draft = await client.complete(
                build_system_prompt(self.config.llm), prompt, temperature=0.6
            )
        except LLMError as exc:
            log.warning("quiz generation failed for %s: %s", job.uid, exc)
            return build_fallback_draft(job, answers, self.profile) if with_fallback else None

        draft = draft.strip().strip('"').strip()
        if not draft:
            return build_fallback_draft(job, answers, self.profile) if with_fallback else None
        violations = check(
            draft,
            job,
            limit,
            self.config.llm.style,
            list(self.profile.message_style.banned_phrases) if self.profile else [],
        )
        if violations:
            log.info("quiz draft for %s has violations: %s", job.uid, violations)
            try:
                draft = await client.complete(
                    build_system_prompt(self.config.llm),
                    build_prompt(job, answers, price_rub=price, profile=self.profile)
                    + "\n\nУчти замечания и перепиши: " + ", ".join(violations),
                    temperature=0.3,
                )
                draft = draft.strip().strip('"').strip()
            except LLMError:
                pass
        return draft or None

    # ------------------------------------------------------------------ помощники
    def _quiz_job(self, job_uid: str) -> Job | None:
        """Восстанавливает Job из pending_approvals или из БД.

        В БД лежит полный payload заказа, поэтому квиз работает и после
        рестарта бота, и из старых сообщений.
        """
        pending = self.pending_approvals.get(job_uid)
        if pending is not None:
            return pending.job
        if self.store is None:
            return None
        order = self.store.get_order(job_uid)
        if order is None:
            return None
        payload = order.get("payload")
        if payload:
            try:
                return Job.model_validate_json(payload)
            except ValueError:
                log.debug("cannot restore job payload for %s", job_uid)
        # Минимальная реконструкция: payload битый, но заголовок и описание
        # есть в других колонках. card_text — это готовая HTML-карточка, из неё
        # вырезаем теги, чтобы LLM не получил разметку вместо текста задачи.
        description = _HTML_TAG_RE.sub(" ", str(order.get("card_text") or ""))
        description = " ".join(description.split())
        return Job(
            platform=str(order.get("platform") or ""),
            external_id=str(order.get("external_id") or ""),
            url=str(order.get("url") or ""),
            title=str(order.get("title") or ""),
            description=description,
        )

    async def _quiz_edit(
        self, query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None
    ) -> bool:
        """Правит сообщение квиза. True, если правка удалась.

        Возвращает False вместо молчаливого проглатывания: вызывающий код
        обязан отправить новое сообщение, иначе нажатие кнопки выглядит как
        «ничего не произошло» — что и случалось на старых сообщениях.
        """
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        except TelegramError as exc:
            log.debug("cannot edit quiz message: %s", exc)
            return False
        return True

    async def _quiz_show(
        self,
        query: CallbackQuery,
        chat_id: int,
        text: str,
        markup: InlineKeyboardMarkup | None,
    ) -> bool:
        """Показывает шаг квиза: правкой сообщения, а при отказе — новым.

        Telegram не даёт править старые сообщения и тексты, которые не
        изменились. Без этой страховки нажатие кнопки выглядело как поломка:
        ошибка уходила в debug-лог, а в чате ничего не менялось.
        """
        if await self._quiz_edit(query, text, markup):
            return True
        await self._bot_send(
            chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup
        )
        return False
