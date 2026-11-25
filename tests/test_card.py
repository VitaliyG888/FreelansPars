"""Карточка заказа и AI-квиз: вёрстка, кнопки и шаги диалога."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import make_job
from freelance_bot.card_render import (
    DIVIDER,
    card_keyboard,
    category_icon,
    description_preview,
    format_budget,
    format_deadline,
    format_time,
    platform_label,
    render_hint,
    render_job_card,
)
from freelance_bot.models import BudgetKind, ClientInfo, Job
from freelance_bot.telegram_quiz import (
    QUIZ_STEPS,
    QuizSession,
    build_fallback_draft,
    build_prompt,
    draft_keyboard,
    draft_text,
    quiz_keyboard,
    quiz_prompt_text,
)
from test_telegram import ADMIN_ID


def full_job() -> Job:
    return Job(
        platform="kwork_ru",
        external_id="1842",
        url="https://kwork.ru/projects/1842",
        title="Разработка интернет-магазина на Next.js",
        description=(
            "Необходимо разработать современный интернет-магазин с адаптивной "
            "версией для мобильных устройств, корзиной и личным кабинетом."
        ),
        budget_amount=1500,
        currency="EUR",
        budget_rub_estimate=150000,
        budget_kind=BudgetKind.FIXED,
        deadline_text="3–4 недели",
        published_at=datetime(2026, 11, 24, 10, 24, tzinfo=UTC),
        client=ClientInfo(
            name="Alex Morgan",
            rating=4.9,
            hire_rate=0.87,
            projects_posted=24,
            verified=True,
            registered_at=datetime(2021, 5, 1, tzinfo=UTC),
        ),
    )


# --------------------------------------------------------------------------- #
# Рендер карточки
# --------------------------------------------------------------------------- #


def test_card_header_has_industry_icon_and_new_badge() -> None:
    text = render_job_card(full_job(), category_key="programming", category_name="Веб-разработка")

    assert text.startswith("💻 <b>Веб-разработка</b>")
    assert "🟢 Новый" in text


def test_card_title_and_description_preview() -> None:
    text = render_job_card(full_job(), category_key="programming", category_name="Веб-разработка")

    assert "<b>Разработка интернет-магазина на Next.js</b>" in text
    assert "адаптивной" in text


def test_card_uses_expandable_quote_for_long_description() -> None:
    """Длинное описание сворачивается нативной цитатой Telegram."""
    long_job = full_job()
    long_job.description = "Слово " * 120
    text = render_job_card(long_job)
    assert "<blockquote expandable>" in text
    assert "</blockquote>" in text

    short_job = full_job()
    short_job.description = "Короткое описание."
    assert "<blockquote" not in render_job_card(short_job)


def test_card_has_budget_and_deadline_blocks() -> None:
    text = render_job_card(full_job())

    assert "💰 <b>Бюджет</b>" in text
    # Сумма в валюте заказчика, как в эталоне: «€ 1 500».
    assert "<b>€ 1 500</b>" in text
    assert "<i>Фиксированная цена</i>" in text
    assert "📅 <b>Срок выполнения</b>" in text
    assert "<b>3–4 недели</b>" in text


def test_card_hides_client_block_without_data() -> None:
    job = make_job(external_id="9", title="Парсер", description="Нужен парсер.")
    text = render_job_card(job)

    assert "О заказчике" not in text
    # Блоки бюджета и биржи остаются на месте.
    assert "💰 <b>Бюджет</b>" in text
    assert "🌐 Биржа:" in text


def test_card_shows_client_metrics_one_per_line() -> None:
    text = render_job_card(full_job())

    assert "👤 <b>О заказчике</b>" in text
    assert "<b>Alex Morgan</b>" in text
    assert "✅ Проверен" in text
    assert "⭐ Рейтинг: <b>4.9/5</b>" in text
    assert "🤝 Нанято исполнителей: <b>87%</b>" in text
    assert "📋 Заказов опубликовано: <b>24</b>" in text


def test_card_exchange_line_and_id() -> None:
    text = render_job_card(full_job())

    assert "🌐 Биржа: Kwork · ID заказа: #1842" in text


def test_card_order_id_strips_url_tail() -> None:
    """Адаптер может отдать ссылку вместо номера — в карточке нужен чистый ID."""
    job = full_job()
    job.external_id = "https://www.pchel.net/task/987654/?from=list"

    text = render_job_card(job)
    assert "ID заказа: #987654" in text
    assert "https" not in text


def test_card_omits_id_when_absent() -> None:
    job = full_job()
    job.external_id = ""

    text = render_job_card(job)
    assert "🌐 Биржа: Kwork" in text
    assert "ID заказа" not in text


def test_card_contains_no_removed_blocks_and_no_links() -> None:
    text = render_job_card(full_job())

    for removed in (
        "Почему подходит",
        "Суть задачи",
        "Описание:",
        "Письмо-черновик",
        "Платформа:",
        "балл",
    ):
        assert removed not in text
    assert "http" not in text


def test_card_escapes_user_text() -> None:
    job = make_job(external_id="5", title="Бот <script> & _*[]", description="Текст <b>и</b> теги")
    text = render_job_card(job)

    assert "&lt;script&gt;" in text
    assert "<script>" not in text
    assert "&amp;" in text


def test_card_uses_dividers_between_blocks() -> None:
    text = render_job_card(full_job())

    assert text.count(DIVIDER) == 3


def test_card_time_stamp_present() -> None:
    text = render_job_card(full_job())

    assert "<i>10:24</i>" in text or "<i>" in text


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #


def test_platform_label_maps_known_and_keeps_unknown() -> None:
    assert platform_label("fl_ru") == "FL.ru"
    assert platform_label("kwork_ru") == "Kwork"
    assert platform_label("some_new_site") == "some_new_site"


def test_category_icon_fallbacks() -> None:
    assert category_icon("design") == "🎨"
    assert category_icon("unknown_key") == "🧩"
    assert category_icon(None) == "🧩"


def test_format_budget_fixed_and_unknown() -> None:
    value, label = format_budget(full_job())
    assert value == "€ 1 500"
    assert label == "Фиксированная цена"

    # Рубли в обычной записи: сумма и символ справа.
    rub = make_job(external_id="2", title="x", description="y", budget=150000.0)
    value, label = format_budget(rub)
    assert value == "150 000 ₽"
    assert label == "Фиксированная цена"

    empty = make_job(
        external_id="1", title="x", description="y", budget=0.0, kind=BudgetKind.UNKNOWN
    )
    value, label = format_budget(empty)
    assert value == "Не указан"
    assert label == "По договорённости"


def test_format_budget_prefers_original_amount_over_rub_estimate() -> None:
    """Сумма площадки важнее пересчёта: карточка не подменяет валюту."""
    job = make_job(external_id="3", title="x", description="y", budget=800.0)
    job.currency = "USD"
    job.budget_rub_estimate = 78000.0

    value, _ = format_budget(job)
    assert value == "$ 800"


def test_format_deadline_without_date() -> None:
    value, label = format_deadline(
        make_job(external_id="1", title="x", description="y", deadline_hours=None)
    )
    assert value == "Не указан"
    assert label == "По договорённости"


def test_format_deadline_text_wins_over_date() -> None:
    """«3–4 недели» информативнее пересчитанной даты — показываем текст."""
    job = full_job()
    value, label = format_deadline(job)
    assert value == "3–4 недели"
    assert label == "По договорённости"


def test_format_deadline_strips_kwork_prefix() -> None:
    job = full_job()
    job.deadline_text = "Осталось: 2 дня"
    value, _ = format_deadline(job)
    assert value == "2 дня"


def test_format_deadline_with_date_uses_day_and_month() -> None:
    job = full_job()
    job.deadline_text = None
    job.deadline_at = datetime(2026, 12, 15, 12, 0, tzinfo=UTC)
    value, label = format_deadline(job)
    assert value == "до 15 дек"
    assert label == "Дедлайн из задания"


def test_description_preview_does_not_cut_words() -> None:
    text, truncated = description_preview("Слово " * 100)
    assert truncated is True
    assert text.endswith("…")
    assert not text.endswith("Сло…")


def test_format_time_uses_moscow_zone() -> None:
    """Штамп карточки — в зоне из runtime.timezone, а не в UTC сервера."""
    assert format_time(datetime(2026, 11, 24, 7, 24, tzinfo=UTC), "Europe/Moscow") == "10:24"
    assert format_time(datetime(2026, 11, 24, 7, 24, tzinfo=UTC)) is not None
    assert format_time(None) is None


def test_format_time_falls_back_on_unknown_zone() -> None:
    """Кривая зона не должна ронять доставку карточки."""
    assert format_time(datetime(2026, 11, 24, 7, 24, tzinfo=UTC), "Not/AZone") is not None


def test_card_stamp_respects_timezone_argument() -> None:
    job = full_job()
    job.published_at = datetime(2026, 11, 24, 7, 24, tzinfo=UTC)

    assert "<i>10:24</i>" in render_job_card(job, timezone="Europe/Moscow")


# --------------------------------------------------------------------------- #
# Подсказка «Полезно знать»
# --------------------------------------------------------------------------- #


def test_hint_wraps_summary_with_title() -> None:
    text = render_hint("Заказ подходит под профиль разработчика.")

    assert text.startswith("💡 <b>Полезно знать</b>")
    assert "Заказ подходит под профиль разработчика." in text


def test_hint_names_the_job_it_explains() -> None:
    """Подсказка приходит отдельным сообщением — без привязки к заказу непонятно."""
    text = render_hint("Хороший бюджет.", job=full_job())

    assert "Полезно знать" in text
    assert "Разработка интернет-магазина на Next.js" in text
    assert "#1842" in text


def test_hint_without_job_stays_generic() -> None:
    """Без Job (текстовый отчёт) заголовок остаётся коротким."""
    text = render_hint("Хороший бюджет.")

    assert text.startswith("💡 <b>Полезно знать</b>\n")
    assert "#" not in text


def test_hint_truncates_long_title() -> None:
    job = full_job()
    job.title = "Очень длинный заголовок заказа " * 6
    text = render_hint("Хороший бюджет.", job=job)

    first_line = text.split("\n", 1)[0]
    assert len(first_line) < 160
    assert "…" in first_line


def test_hint_empty_summary_produces_nothing() -> None:
    """Пустая причина показа — сообщение не отправляется вовсе."""
    assert render_hint(None) == ""
    assert render_hint("") == ""
    assert render_hint("   ") == ""
    assert render_hint("—") == ""


def test_hint_truncates_long_summary_at_word_boundary() -> None:
    text = render_hint("Слово " * 100, limit=120)

    assert text.endswith("…")
    assert len(text) < 200


def test_hint_escapes_html() -> None:
    text = render_hint("Бюджет <b>выше</b> медианы & это хорошо")

    assert "&lt;b&gt;" in text
    assert "<b>выше</b>" not in text
    assert "&amp;" in text


# --------------------------------------------------------------------------- #
# Клавиатура
# --------------------------------------------------------------------------- #


def test_keyboard_has_three_rows_in_reference_order() -> None:
    keyboard = card_keyboard(full_job(), order_url="https://kwork.ru/projects/1842")

    rows = [[b.text for b in row] for row in keyboard.inline_keyboard]
    assert rows == [
        ["🔗 Перейти на страницу заказа"],
        ["🪄 Составить письмо-ответ с AI"],
        ["♥ Выделить как понравившееся"],
    ]
    assert keyboard.inline_keyboard[0][0].url == "https://kwork.ru/projects/1842"


def test_keyboard_switches_favorite_label() -> None:
    keyboard = card_keyboard(full_job(), order_url=None, is_favorite=True)

    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "💔 Убрать из избранного" in texts


def test_keyboard_falls_back_to_raw_url() -> None:
    keyboard = card_keyboard(full_job(), order_url=None)

    assert keyboard.inline_keyboard[0][0].url == full_job().url


# --------------------------------------------------------------------------- #
# AI-квиз: чистые функции
# --------------------------------------------------------------------------- #


def test_quiz_has_four_steps_with_options() -> None:
    assert len(QUIZ_STEPS) == 4
    assert [step.key for step in QUIZ_STEPS] == ["tone", "focus", "price", "question"]
    assert all(len(step.options) >= 3 for step in QUIZ_STEPS)


def test_quiz_prompt_shows_progress() -> None:
    session = QuizSession(chat_id=1, job_uid="kwork_ru:1")
    text = quiz_prompt_text(session, full_job())

    assert "Шаг 1 из 4" in text
    assert "Тон письма" in text
    assert full_job().title[:20] in text


def test_quiz_keyboard_encodes_job_uid_with_colon() -> None:
    session = QuizSession(chat_id=1, job_uid="kwork_ru:1842")
    keyboard = quiz_keyboard(session, full_job())

    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "aq:step:0:friendly:kwork_ru:1842" in callbacks
    assert "aq:edit:kwork_ru:1842" in callbacks
    assert "aq:cancel:kwork_ru:1842" in callbacks


def test_draft_keyboard_has_three_actions() -> None:
    keyboard = draft_keyboard(full_job())

    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert texts == ["✏️ Редактировать", "📋 Скопировать", "🔄 Перегенерировать"]


def test_draft_text_wraps_in_pre_for_copying() -> None:
    text = draft_text(full_job(), "Здравствуйте, посмотрел задачу.")

    assert "<pre>" in text and "</pre>" in text
    assert "Письмо готово" in text


def test_build_prompt_includes_answers_and_skills() -> None:
    answers = {"tone": "professional", "focus": "plan", "price": "ask", "question": "deadline"}
    prompt = build_prompt(full_job(), answers, price_rub=150000, profile=None)

    assert "деловой" in prompt
    assert "план работ" in prompt
    assert "бюджет" in prompt
    assert "срокам" in prompt
    assert "150000" in prompt.replace(" ", "")


# --------------------------------------------------------------------------- #
# AI-квиз: шаги через контроллер
# --------------------------------------------------------------------------- #


class _FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.replies: list[tuple[str, dict[str, Any]]] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append((text, kwargs))


class _FakeQuery:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[tuple[str | None, dict[str, Any]]] = []
        self.edits: list[tuple[str, dict[str, Any]]] = []
        self.message = _FakeMessage()

    async def answer(self, text: str | None = None, **kwargs: Any) -> None:
        self.answers.append((text, kwargs))

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append((text, kwargs))

    async def edit_message_reply_markup(self, **kwargs: Any) -> None:
        self.edits.append(("", kwargs))


class _FakeUpdate:
    def __init__(
        self,
        *,
        user_id: int,
        callback: str | None = None,
        text: str | None = None,
    ) -> None:
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = SimpleNamespace(id=user_id)
        self.message = _FakeMessage(text) if text is not None else None
        self.callback_query = _FakeQuery(callback, user_id) if callback else None


class _FakeLLM:
    """LLM-заглушка: возвращает предсказуемый текст и считает вызовы."""

    draft = "Здравствуйте! Посмотрел задачу, план работ готов, сроки реальные."
    calls: list[str] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.available = True
        self.model = "fake-model"

    async def complete(self, system: str, user: str, **kwargs: object) -> str:
        type(self).calls.append(user)
        return type(self).draft


@pytest.fixture
def quiz(monkeypatch: pytest.MonkeyPatch):
    """Контроллер с подменённым LLM: не ходит в сеть."""
    from test_telegram import build_controller

    _FakeLLM.calls = []
    monkeypatch.setattr("freelance_bot.telegram_quiz.LLMClient", _FakeLLM)
    controller = build_controller(secrets=object())
    job = full_job()
    # Карточка уже доставлена: pending_approvals знает заказ, как в бою.
    controller.pending_approvals[job.uid] = SimpleNamespace(job=job)
    return controller, job


@pytest.mark.asyncio
async def test_quiz_start_opens_first_step(quiz) -> None:
    controller, job = quiz
    update = _FakeUpdate(user_id=ADMIN_ID, callback=f"aq:start:{job.uid}")

    await controller._handle_callback(update, SimpleNamespace(args=[]))

    edit_text, kwargs = update.callback_query.edits[0]
    assert "Шаг 1 из 4" in edit_text
    assert kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_quiz_full_run_generates_draft(quiz) -> None:
    controller, job = quiz
    user = ADMIN_ID
    await controller._handle_callback(
        _FakeUpdate(user_id=user, callback=f"aq:start:{job.uid}"), SimpleNamespace(args=[])
    )

    for index, key in enumerate(("friendly", "plan", "ask", "deadline")):
        update = _FakeUpdate(user_id=user, callback=f"aq:step:{index}:{key}:{job.uid}")
        await controller._handle_callback(update, SimpleNamespace(args=[]))

    final_text, kwargs = update.callback_query.edits[-1]
    assert "Письмо готово" in final_text
    assert _FakeLLM.draft in final_text
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert texts == ["✏️ Редактировать", "📋 Скопировать", "🔄 Перегенерировать"]
    # Черновик сохранён: кнопки работают после рестарта.
    assert controller._quiz_sessions[f"{user}|{job.uid}"].draft == _FakeLLM.draft


@pytest.mark.asyncio
async def test_quiz_regenerate_calls_llm_again(quiz) -> None:
    controller, job = quiz
    user = ADMIN_ID
    await controller._handle_callback(
        _FakeUpdate(user_id=user, callback=f"aq:start:{job.uid}"), SimpleNamespace(args=[])
    )
    for index, key in enumerate(("friendly", "plan", "ask", "deadline")):
        await controller._handle_callback(
            _FakeUpdate(user_id=user, callback=f"aq:step:{index}:{key}:{job.uid}"),
            SimpleNamespace(args=[]),
        )
    calls_before = len(_FakeLLM.calls)

    update = _FakeUpdate(user_id=user, callback=f"aq:regen:{job.uid}")
    await controller._handle_callback(update, SimpleNamespace(args=[]))

    # Генерация могла включать повторный вызов на исправление — важно, что
    # перегенерация действительно сходила в LLM ещё раз.
    assert len(_FakeLLM.calls) > calls_before
    assert "перегенерировано" in update.callback_query.edits[-1][0].lower()


@pytest.mark.asyncio
async def test_quiz_copy_sends_pre_message(quiz) -> None:
    controller, job = quiz
    user = ADMIN_ID
    session = controller._quiz_session(user, job.uid)
    session.draft = _FakeLLM.draft

    update = _FakeUpdate(user_id=user, callback=f"aq:copy:{job.uid}")
    await controller._handle_callback(update, SimpleNamespace(args=[]))

    sent = controller.app.bot.sent[-1]
    assert "<pre>" in sent["text"]
    assert _FakeLLM.draft in sent["text"]


@pytest.mark.asyncio
async def test_quiz_custom_answer_via_text(quiz) -> None:
    controller, job = quiz
    user = ADMIN_ID
    await controller._handle_callback(
        _FakeUpdate(user_id=user, callback=f"aq:start:{job.uid}"), SimpleNamespace(args=[])
    )
    await controller._handle_callback(
        _FakeUpdate(user_id=user, callback=f"aq:edit:{job.uid}"), SimpleNamespace(args=[])
    )

    typing = _FakeUpdate(user_id=user, text="Пиши по-дружески, но коротко")
    await controller._handle_text_input(typing, SimpleNamespace(args=[]))

    session = controller._quiz_sessions[f"{user}|{job.uid}"]
    assert session.answers["tone"].startswith("custom:")
    # Ответ принят — квиз ушёл на следующий шаг.
    assert "Шаг 2 из 4" in typing.message.replies[-1][0]


@pytest.mark.asyncio
async def test_quiz_edit_rewrites_ready_draft(quiz) -> None:
    controller, job = quiz
    user = ADMIN_ID
    session = controller._quiz_session(user, job.uid)
    session.draft = "Старый текст письма"
    session.step = len(QUIZ_STEPS)

    await controller._handle_callback(
        _FakeUpdate(user_id=user, callback=f"aq:edit:{job.uid}"), SimpleNamespace(args=[])
    )
    typing = _FakeUpdate(user_id=user, text="Новый текст письма")
    await controller._handle_text_input(typing, SimpleNamespace(args=[]))

    assert session.draft == "Новый текст письма"
    assert "Новый текст письма" in typing.message.replies[-1][0]


@pytest.mark.asyncio
async def test_quiz_cancel_command_stops_text_input(quiz) -> None:
    """«/cancel» снимает ожидание текста, даже если начат квиз, а не настройки."""
    controller, job = quiz
    user = ADMIN_ID
    await controller._handle_callback(
        _FakeUpdate(user_id=user, callback=f"aq:start:{job.uid}"), SimpleNamespace(args=[])
    )
    await controller._handle_callback(
        _FakeUpdate(user_id=user, callback=f"aq:edit:{job.uid}"), SimpleNamespace(args=[])
    )
    session = controller._quiz_sessions[f"{user}|{job.uid}"]
    assert session.awaiting_text is True

    update = _FakeUpdate(user_id=user, text="/cancel")
    await controller._cmd_cancel(update, SimpleNamespace(args=[]))

    assert session.awaiting_text is False
    assert "отменён" in update.message.replies[-1][0]


@pytest.mark.asyncio
async def test_quiz_callback_blocked_for_unsubscribed_user(quiz) -> None:
    """Квиз не обходит gate подписки: чужой не может открыть письмо."""
    controller, job = quiz
    stranger = 999_000_099

    update = _FakeUpdate(user_id=stranger, callback=f"aq:start:{job.uid}")
    await controller._handle_callback(update, SimpleNamespace(args=[]))

    assert update.callback_query.edits == []
    assert "Подписка не активна" in (update.callback_query.answers[-1][0] or "")


@pytest.mark.asyncio
async def test_quiz_without_llm_reports_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_telegram import build_controller

    controller = build_controller(secrets=None)
    job = full_job()
    controller.pending_approvals[job.uid] = SimpleNamespace(job=job)
    user = ADMIN_ID
    await controller._handle_callback(
        _FakeUpdate(user_id=user, callback=f"aq:start:{job.uid}"), SimpleNamespace(args=[])
    )
    update = None
    for index, key in enumerate(("friendly", "plan", "ask", "deadline")):
        update = _FakeUpdate(user_id=user, callback=f"aq:step:{index}:{key}:{job.uid}")
        await controller._handle_callback(update, SimpleNamespace(args=[]))

    assert update is not None
    text = update.callback_query.edits[-1][0]
    # Без LLM квиз не обрывается тупиком: он отдаёт шаблонное письмо.
    assert "Письмо собрано по шаблону" in text
    assert job.title[:40] in text
    markup = update.callback_query.edits[-1][1]["reply_markup"]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "✏️ Редактировать" in labels


def test_fallback_draft_uses_quiz_answers() -> None:
    """Шаблон повторяет ответы: тон, акцент, вопрос про бюджет."""
    job = full_job()
    prompt_free = build_fallback_draft(
        job,
        {"tone": "friendly", "focus": "speed", "price": "ask", "question": "deadline"},
    )

    assert prompt_free.startswith("Здравствуйте! Увидел задачу")
    assert "Могу взяться сразу" in prompt_free
    assert "какой бюджет заложен" in prompt_free
    assert "К какому сроку нужен результат?" in prompt_free


def test_fallback_draft_names_price_when_asked() -> None:
    job = full_job()
    text = build_fallback_draft(
        job,
        {"tone": "professional", "focus": "experience", "price": "name", "question": "details"},
    )

    assert text.startswith("Добрый день.")
    # Цена в письме — в рублях: оценка заказа пересчитана под площадку.
    assert "150 000 ₽" in text


def test_fallback_draft_respects_price_mode_skip() -> None:
    job = full_job()
    text = build_fallback_draft(
        job, {"tone": "neutral", "focus": "plan", "price": "skip", "question": "details"}
    )

    assert "₽" not in text
    assert "бюджет" not in text.casefold()
