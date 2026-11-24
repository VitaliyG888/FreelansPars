"""Панель управления Telegram: команды, защита админов, уведомления, одобрения.

Настоящие обработчики ``python-telegram-bot`` проверяются через крошечные
поддельные объекты ``Update``, поэтому набор доказывает связность без токена
бота и доступа к сети. Отдельная CLI-команда (``telegram-check``) выполняет
живую проверку ``getMe``, когда передан токен.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from html import escape as html_escape
from types import SimpleNamespace
from typing import Any

import pytest
from telegram.error import BadRequest

from conftest import make_job
from freelance_bot.config import AppConfig, CategoryFilterConfig, PlatformConfig
from freelance_bot.models import GeneratedMessage, JobAnalysis
from freelance_bot.profile import FreelancerProfile, PricingConfig
from freelance_bot.scoring.engine import ScoringEngine
from freelance_bot.storage import JobStore
from freelance_bot.telegram_bot import COMMAND_HELP, TelegramBotController, _chunk, _md
from freelance_bot.user_prefs import UserFilterPrefs

ADMIN_ID = 123456789


# --------------------------------------------------------------------------- #
# Заглушки
# --------------------------------------------------------------------------- #


class FakeMessage:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.replies: list[tuple[str, dict[str, Any]]] = []
        self.deleted = False

    # Методы используются с await в обработчиках — должны быть async.
    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append((text, kwargs))

    async def delete(self) -> None:
        self.deleted = True


class FakeQuery:
    def __init__(self, data: str, user_id: int = ADMIN_ID) -> None:
        self.data = data
        self.user_id = user_id
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[tuple[str | None, dict[str, Any]]] = []
        self.edits: list[tuple[str, dict[str, Any]]] = []
        # message.reply_text используется в send/copy handler.
        self.message: FakeMessage = FakeMessage()

    async def answer(self, text: str | None = None, **kwargs: Any) -> None:
        """Async-метод: handler использует await query.answer(...)."""
        self.answers.append((text, kwargs))

    async def edit_message_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append((text, kwargs))

    async def edit_message_reply_markup(self, **kwargs: Any) -> None:
        # reply_markup=None передаётся для удаления кнопок.
        self.edits.append(("", kwargs))


class FakeUpdate:
    def __init__(
        self,
        *,
        user_id: int = ADMIN_ID,
        chat_id: int | None = None,
        callback: str | None = None,
        with_message: bool = True,
    ) -> None:
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = SimpleNamespace(id=chat_id if chat_id is not None else user_id)
        self.message = FakeMessage() if with_message else None
        self.callback_query = FakeQuery(callback) if callback is not None else None


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.commands: list[Any] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)

    async def set_my_commands(self, commands: list[Any]) -> None:
        self.commands = list(commands)


class FakeUpdater:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start_polling(self, **kwargs: Any) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class FakeApplication:
    def __init__(self) -> None:
        self.handlers: list[Any] = []
        self.bot = FakeBot()
        self.updater = FakeUpdater()
        self.initialized = False
        self.started = False
        self.stopped = False

    def add_handler(self, handler: Any) -> None:
        self.handlers.append(handler)

    async def initialize(self) -> None:
        self.initialized = True

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def shutdown(self) -> None:
        return None


class FakeBuilder:
    def token(self, token: str) -> FakeBuilder:
        self._token = token
        return self

    def connect_timeout(self, value: float) -> FakeBuilder:
        return self

    def read_timeout(self, value: float) -> FakeBuilder:
        return self

    def write_timeout(self, value: float) -> FakeBuilder:
        return self

    def pool_timeout(self, value: float) -> FakeBuilder:
        return self

    def build(self) -> FakeApplication:
        return FakeApplication()


class FakeApplicationFactory:
    @staticmethod
    def builder() -> FakeBuilder:
        return FakeBuilder()


class FakeStore:
    def __init__(self) -> None:
        self.approved: list[int] = []
        self.rejected: list[int] = []
        self.bot_admins: set[int] = set()
        self.super_admins: set[int] = set()
        self.subscribers: dict[int, dict[str, Any]] = {}
        # Личные фильтры и избранное: минимальная in-memory имитация таблиц.
        self.filters: dict[int, UserFilterPrefs] = {}
        self.fav_map: dict[int, set[str]] = {}
        self.delivered: set[tuple[int, str]] = set()

    def list_bot_admins(self) -> set[int]:
        return set(self.bot_admins)

    def add_bot_admin(self, chat_id: int, added_by: int | None) -> bool:
        if chat_id in self.bot_admins:
            return False
        self.bot_admins.add(chat_id)
        return True

    def remove_bot_admin(self, chat_id: int) -> bool:
        if chat_id not in self.bot_admins:
            return False
        self.bot_admins.discard(chat_id)
        return True

    def list_super_admins(self) -> set[int]:
        return set(self.super_admins)

    def set_super_admin(
        self, chat_id: int, enabled: bool, added_by: int | None = None
    ) -> bool:
        if enabled:
            if chat_id in self.super_admins:
                return False
            self.super_admins.add(chat_id)
            self.bot_admins.add(chat_id)
            return True
        if chat_id not in self.super_admins:
            return False
        self.super_admins.discard(chat_id)
        return True

    def add_subscriber_request(
        self, chat_id: int, username: str | None, full_name: str | None
    ) -> bool:
        if chat_id in self.subscribers:
            return False
        self.subscribers[chat_id] = {
            "chat_id": chat_id,
            "username": username,
            "full_name": full_name,
            "status": "pending",
        }
        return True

    def get_subscriber(self, chat_id: int) -> dict[str, Any] | None:
        return self.subscribers.get(chat_id)

    def set_subscriber_status(
        self, chat_id: int, status: str, decided_by: int | None = None
    ) -> bool:
        if chat_id not in self.subscribers:
            return False
        self.subscribers[chat_id]["status"] = status
        self.subscribers[chat_id]["decided_by"] = decided_by
        return True

    def upsert_subscriber_status(
        self, chat_id: int, status: str, decided_by: int | None = None
    ) -> None:
        self.set_subscriber_status(chat_id, status, decided_by)
        if chat_id not in self.subscribers:
            self.subscribers[chat_id] = {
                "chat_id": chat_id,
                "username": None,
                "full_name": None,
                "status": status,
                "decided_by": decided_by,
            }

    def list_subscribers(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = [
            dict(row)
            for row in self.subscribers.values()
            if status is None or row["status"] == status
        ]
        return sorted(rows, key=lambda r: r["chat_id"])

    def approve(self, message_id: int) -> None:
        self.approved.append(message_id)

    def reject(self, message_id: int) -> None:
        self.rejected.append(message_id)

    def set_draft(self, uid: str, draft_text: str, card_text: str | None = None) -> None:
        # Заглушка: no-op, нужна чтобы notify_new_job не падал с FakeStore.
        pass

    def get_order(self, uid: str) -> dict | None:
        """Заглушка возвращает None — для реальных тестов используется JobStore."""
        return None

    def mark_sent_clicked(self, uid: str) -> bool:
        return True

    def mark_skipped(self, uid: str) -> bool:
        return True

    def log_action(
        self, user_id: int | None, job_uid: str, action: str, details: dict | None = None
    ) -> None:
        pass

    def stats_since(self, days: int = 7, min_score: float = 0.0) -> dict[str, int]:
        return {
            "total_jobs": 11,
            "total_new": 7,
            "total_eligible": 4,
            "total_messages": 3,
            "total_sent": 2,
            "total_rejected": 1,
        }

    # ------------------------------------------------- личные фильтры и избранное
    def get_user_filters(self, chat_id: int) -> UserFilterPrefs:
        return self.filters.get(chat_id) or UserFilterPrefs.default_for(chat_id)

    def set_user_filters(
        self,
        chat_id: int,
        *,
        platforms: list[str] | None = None,
        categories: list[str] | None = None,
        subcategories: dict[str, list[str]] | None = None,
        min_price_rub: float | None = None,
    ) -> UserFilterPrefs:
        base = self.get_user_filters(chat_id)
        prefs = UserFilterPrefs(
            chat_id=chat_id,
            platforms=base.platforms if platforms is None else sorted(set(platforms)),
            categories=base.categories if categories is None else list(categories),
            subcategories=base.subcategories if subcategories is None else {
                k: list(v) for k, v in subcategories.items() if v
            },
            min_price_rub=(
                base.min_price_rub if min_price_rub is None else max(0.0, float(min_price_rub))
            ),
        )
        self.filters[chat_id] = prefs
        return prefs

    def reset_user_filters(self, chat_id: int) -> None:
        self.filters.pop(chat_id, None)

    def all_user_filters(self) -> dict[int, UserFilterPrefs]:
        return dict(self.filters)

    def user_jobs(self, prefs: UserFilterPrefs, limit: int = 10) -> tuple[list[dict], int]:
        """Возвращает заранее положенные строки, отфильтрованные по prefs."""
        rows = getattr(self, "user_job_rows", [])
        matched = [
            row
            for row in rows
            if prefs.matches(
                platform=row["platform"],
                category_key=row["category_key"],
                subcategory=row["subcategory"],
                group=row["category_group"],
                budget_rub_estimate=row["budget_rub"],
            )
        ]
        return matched[:limit], len(matched)

    def add_favorite(self, user_id: int, job_uid: str) -> bool:
        favs = self.fav_map.setdefault(user_id, set())
        if job_uid in favs:
            return False
        favs.add(job_uid)
        return True

    def remove_favorite(self, user_id: int, job_uid: str) -> bool:
        favs = self.fav_map.get(user_id, set())
        if job_uid not in favs:
            return False
        favs.discard(job_uid)
        return True

    def is_favorite(self, user_id: int, job_uid: str) -> bool:
        return job_uid in self.fav_map.get(user_id, set())

    def favorites(self, user_id: int, limit: int = 50) -> list[dict]:
        favs = self.fav_map.get(user_id, set())
        return [dict(row) for row in getattr(self, "favorite_rows", []) if row["uid"] in favs][
            :limit
        ]

    def mark_delivered_to(self, chat_id: int, job_uid: str) -> None:
        self.delivered.add((chat_id, job_uid))

    def delivered_to(self, chat_id: int, job_uid: str) -> bool:
        return (chat_id, job_uid) in self.delivered


def build_config() -> AppConfig:
    return AppConfig(
        platforms={
            "fl_ru": PlatformConfig(listing_url="https://www.fl.ru/projects/"),
            "kwork_ru": PlatformConfig(
                listing_url="https://kwork.ru/projects", transport="browser", enabled=False
            ),
        },
        category_filter=CategoryFilterConfig(enabled=True, categories=["programming"]),
    )


def build_profile() -> FreelancerProfile:
    return FreelancerProfile(
        name="Иван",
        skills=["Python", "Telegram Bot API"],
        secondary_skills=["FastAPI"],
        experience_summary="Делал ботов и парсеры.",
        pricing=PricingConfig(strategy="markup", markup_percent=1.0, min_acceptable_rub=5000),
    )


def build_controller(*, store: Any = None, profile: FreelancerProfile | None = None):
    controller = TelegramBotController(
        token="123:test",
        admin_chat_ids=[ADMIN_ID],
        config=build_config(),
        profile=profile or build_profile(),
        store=store,
    )
    controller.app = FakeApplication()
    controller.is_running = True
    return controller


def ctx(*args: str) -> SimpleNamespace:
    return SimpleNamespace(args=list(args))


def scored_job(*, title: str = "Разработка Telegram-бота", external_id: str = "1"):
    job = make_job(
        external_id=external_id,
        title=title,
        description="Нужен Telegram-бот на Python с приёмом заявок и записью в Google Sheets.",
        budget=20000,
    )
    config = build_config()
    return ScoringEngine(config.scoring, config.red_flags).score(job)


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #


def test_markdown_escape_covers_telegram_metacharacters() -> None:
    assert _md("a_b*c[d]e\\f`") == "a\\_b\\*c\\[d\\]e\\\\f\\`"
    assert _md(None) == "None"


def test_chunk_keeps_every_word_and_respects_limit() -> None:
    text = " ".join(f"слово{i}" for i in range(2500))
    chunks = _chunk(text, limit=400)
    assert len(chunks) > 1
    assert all(len(chunk) <= 400 for chunk in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_short_message_is_not_split() -> None:
    assert _chunk("коротко") == ["коротко"]


# --------------------------------------------------------------------------- #
# Контроль доступа
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_non_admin_gets_refusal_on_start() -> None:
    controller = build_controller()
    update = FakeUpdate(user_id=42)

    await controller._cmd_start(update, ctx())

    assert len(update.message.replies) == 1
    assert "⛔" in update.message.replies[0][0]


@pytest.mark.asyncio
async def test_admin_is_allowed_via_chat_id_for_groups() -> None:
    controller = build_controller()
    update = FakeUpdate(user_id=999, chat_id=ADMIN_ID)

    await controller._cmd_status(update, ctx())

    assert update.message.replies
    assert "Статус" in update.message.replies[0][0]


@pytest.mark.asyncio
async def test_non_admin_callback_is_rejected_with_alert() -> None:
    controller = build_controller()
    update = FakeUpdate(user_id=42, callback="approve_fl_ru:1", with_message=False)

    await controller._handle_callback(update, ctx())

    assert update.callback_query.answers
    assert "⛔" in (update.callback_query.answers[0][0] or "")
    assert not update.callback_query.edits


@pytest.mark.asyncio
async def test_denied_access_logs_ids_for_allowlisting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ID отказника попадает в журнал: владелец добавит его в allow-list."""
    controller = build_controller()
    update = FakeUpdate(user_id=777, chat_id=777)

    with caplog.at_level(logging.WARNING, logger="freelance_bot.telegram_bot"):
        await controller._cmd_start(update, ctx())

    assert any(
        "user_id=777" in record.message and "chat_id=777" in record.message
        for record in caplog.records
    )


class ParseErrorBot(FakeBot):
    """Бот, отклоняющий разметку: имитация несбалансированных *_ в карточке."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts: list[str | None] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.attempts.append(kwargs.get("parse_mode"))
        if kwargs.get("parse_mode") is not None:
            raise BadRequest(
                "Can't parse entities: can't find end of the entity starting at byte offset 274"
            )
        await super().send_message(**kwargs)


class ChatNotFoundBot(FakeBot):
    """Бот с не-парсинговой ошибкой: фолбэк не должен маскировать её."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def send_message(self, **kwargs: Any) -> None:
        self.attempts += 1
        raise BadRequest("Chat not found")


@pytest.mark.asyncio
async def test_send_to_admins_falls_back_to_plain_text_on_parse_error() -> None:
    """Карточка с битой разметкой доставляется повтором без parse_mode."""
    controller = build_controller()
    controller.app.bot = ParseErrorBot()

    delivered = await controller._send_to_admins("✅ *Цикл завершён* ошибка *не закрыта")

    assert delivered is True
    assert controller.app.bot.attempts == ["Markdown", None]


@pytest.mark.asyncio
async def test_send_to_admins_fallback_strips_html_tags() -> None:
    """Фолбэк без parse_mode не показывает HTML-теги как текст."""
    controller = build_controller()
    controller.app.bot = ParseErrorBot()

    await controller._send_to_admins("🎯 <b>Заголовок</b> и <a href=\"https://x.ru\">ссылка</a>")

    sent = controller.app.bot.sent[-1]
    assert "<b>" not in sent["text"]
    assert "<a href=" not in sent["text"]
    assert "Заголовок" in sent["text"]
    assert "ссылка" in sent["text"]


@pytest.mark.asyncio
async def test_send_to_admins_does_not_retry_other_errors() -> None:
    """Ошибки кроме парсинга разметки не порождают повторных отправок."""
    controller = build_controller()
    controller.app.bot = ChatNotFoundBot()

    delivered = await controller._send_to_admins("текст")

    assert delivered is False
    assert controller.app.bot.attempts == 1


# --------------------------------------------------------------------------- #
# Управление доступом: /users, /adduser, /deluser
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_adduser_grants_access_and_persists(tmp_path: Any) -> None:
    """Добавленный админ сразу допущен и переживает рестарт (живёт в базе)."""
    store = JobStore(tmp_path / "admins.db")
    controller = build_controller(store=store)
    update = FakeUpdate(user_id=ADMIN_ID)

    await controller._cmd_adduser(update, ctx("555000111"))

    assert "✅" in update.message.replies[0][0]
    assert 555000111 in controller.admin_chat_ids
    assert store.list_bot_admins() == {555000111}

    # «Перезапуск»: свежий контроллер с той же базой видит добавленного.
    fresh = build_controller(store=JobStore(tmp_path / "admins.db"))
    assert 555000111 in fresh.admin_chat_ids
    newcomer = FakeUpdate(user_id=555000111)
    await fresh._cmd_status(newcomer, ctx())
    assert newcomer.message.replies
    assert "⛔" not in newcomer.message.replies[0][0]


@pytest.mark.asyncio
async def test_only_admin_can_add_users(tmp_path: Any) -> None:
    """Посторонний не может ни добавлять, ни убирать, ни смотреть список."""
    store = JobStore(tmp_path / "admins.db")
    controller = build_controller(store=store)
    stranger = FakeUpdate(user_id=42)

    await controller._cmd_adduser(stranger, ctx("42"))
    await controller._cmd_deluser(stranger, ctx(str(ADMIN_ID)))
    await controller._cmd_users(stranger, ctx())

    assert store.list_bot_admins() == set()
    assert controller.admin_chat_ids == {ADMIN_ID}
    # Все три ответа — отказ в доступе.
    for reply, _ in stranger.message.replies:
        assert "⛔" in reply


@pytest.mark.asyncio
async def test_adduser_rejects_bad_input_and_duplicates(tmp_path: Any) -> None:
    store = JobStore(tmp_path / "admins.db")
    controller = build_controller(store=store)
    update = FakeUpdate(user_id=ADMIN_ID)

    await controller._cmd_adduser(update, ctx())
    assert "Использование" in update.message.replies[0][0]

    await controller._cmd_adduser(update, ctx("abc"))
    assert "Использование" in update.message.replies[1][0]

    # Существующий админ (из .env) не дублируется в базу.
    await controller._cmd_adduser(update, ctx(str(ADMIN_ID)))
    assert "уже имеет доступ" in update.message.replies[2][0]
    assert store.list_bot_admins() == set()

    # Повторное добавление того же ID.
    store.add_bot_admin(555000111, added_by=ADMIN_ID)
    await controller._cmd_adduser(update, ctx("555000111"))
    assert "уже имеет доступ" in update.message.replies[3][0]


@pytest.mark.asyncio
async def test_deluser_protects_env_admins(tmp_path: Any) -> None:
    """Админы из .env постоянны; добавленные убираются корректно."""
    store = JobStore(tmp_path / "admins.db")
    controller = build_controller(store=store)
    update = FakeUpdate(user_id=ADMIN_ID)

    await controller._cmd_deluser(update, ctx(str(ADMIN_ID)))
    assert ".env" in update.message.replies[0][0]
    assert ADMIN_ID in controller.admin_chat_ids

    await controller._cmd_adduser(update, ctx("555000111"))
    await controller._cmd_adduser(update, ctx("555000222"))
    await controller._cmd_deluser(update, ctx("555000111"))
    assert 555000111 not in controller.admin_chat_ids
    assert 555000222 in controller.admin_chat_ids
    assert store.list_bot_admins() == {555000222}

    # Убранного нельзя убрать снова.
    await controller._cmd_deluser(update, ctx("555000111"))
    assert "нет в списке" in update.message.replies[4][0]


@pytest.mark.asyncio
async def test_deluser_refuses_last_admin_without_env() -> None:
    """Без .env-админов единственного админа в базе убрать нельзя."""
    store = FakeStore()
    store.bot_admins.add(555000111)
    controller = TelegramBotController(
        token="123:test", admin_chat_ids=[], config=build_config(), store=store
    )
    controller.app = FakeApplication()
    controller.is_running = True
    update = FakeUpdate(user_id=555000111)

    await controller._cmd_deluser(update, ctx("555000111"))

    assert "последнего администратора" in update.message.replies[0][0]
    assert controller.admin_chat_ids == {555000111}


@pytest.mark.asyncio
async def test_users_lists_sources(tmp_path: Any) -> None:
    store = JobStore(tmp_path / "admins.db")
    store.add_bot_admin(555000111, added_by=ADMIN_ID)
    controller = build_controller(store=store)
    update = FakeUpdate(user_id=ADMIN_ID)

    await controller._cmd_users(update, ctx())

    text = update.message.replies[0][0]
    assert str(ADMIN_ID) in text and ".env" in text
    assert "555000111" in text and "adduser" in text


@pytest.mark.asyncio
async def test_new_admin_receives_cards_immediately(tmp_path: Any) -> None:
    """После /adduser карточки уходят и новому чату — без рестарта."""
    store = JobStore(tmp_path / "admins.db")
    controller = build_controller(store=store)
    controller.app.bot = ParseErrorBot()
    update = FakeUpdate(user_id=ADMIN_ID)

    await controller._cmd_adduser(update, ctx("555000111"))
    await controller._send_to_admins("тест")

    chat_ids = {call["chat_id"] for call in controller.app.bot.sent}
    assert chat_ids == {ADMIN_ID, 555000111}


@pytest.mark.asyncio
async def test_denied_start_shows_own_id() -> None:
    """Без базы отказ показывает ID и кнопку «📩 Подписаться»."""
    controller = build_controller()
    update = FakeUpdate(user_id=777)

    await controller._cmd_start(update, ctx())

    reply = update.message.replies[0][0]
    markup = update.message.replies[0][1]["reply_markup"]
    assert "777" in reply
    assert "Подписаться" in reply
    assert markup.inline_keyboard[0][0].callback_data == "sub:request"


# --------------------------------------------------------------------------- #
# Подписка: заявка, одобрение, отклонение
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_start_new_user_creates_subscription_request() -> None:
    """Первый /start нового пользователя создаёт заявку и уведомляет админа."""
    store = FakeStore()
    controller = build_controller(store=store)
    update = FakeUpdate(user_id=777)

    await controller._cmd_start(update, ctx())

    # Заявка сохранена со статусом pending.
    assert store.subscribers[777]["status"] == "pending"
    # Админ получил карточку заявки с кнопками одобрения/отклонения.
    sent = controller.app.bot.sent[0]
    assert "заявка" in sent["text"].lower()
    callbacks = [
        button.callback_data
        for row in sent["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "sub_approve:777" in callbacks
    assert "sub_reject:777" in callbacks
    # Пользователь получил подтверждение со своим ID.
    assert "777" in update.message.replies[0][0]


@pytest.mark.asyncio
async def test_start_pending_user_does_not_duplicate_request() -> None:
    """Повторный /start не создаёт вторую заявку и не дёргает админов."""
    store = FakeStore()
    controller = build_controller(store=store)
    await controller._cmd_start(FakeUpdate(user_id=777), ctx())
    sent_after_first = len(controller.app.bot.sent)

    second = FakeUpdate(user_id=777)
    await controller._cmd_start(second, ctx())

    assert len(controller.app.bot.sent) == sent_after_first
    assert "ожидает" in second.message.replies[0][0]


@pytest.mark.asyncio
async def test_subscription_approve_grants_access() -> None:
    """Одобрение шлёт «Вы подписаны» и открывает доступ (в базе)."""
    store = FakeStore()
    controller = build_controller(store=store)
    await controller._cmd_start(FakeUpdate(user_id=777), ctx())

    approve = FakeUpdate(user_id=ADMIN_ID, callback="sub_approve:777", with_message=False)
    await controller._handle_callback(approve, ctx())

    # Пользователю ушло точное сообщение «Вы подписаны».
    user_messages = [m for m in controller.app.bot.sent if m["chat_id"] == 777]
    assert user_messages and user_messages[-1]["text"].startswith("Вы подписаны")
    # Доступ выдан сразу и записан в базу (переживает рестарт).
    assert 777 in controller.admin_chat_ids
    assert 777 in store.bot_admins
    assert store.subscribers[777]["status"] == "active"


@pytest.mark.asyncio
async def test_subscription_reject_denies_access() -> None:
    """Отклонение шлёт «Заявка отклонена», доступ не выдаётся."""
    store = FakeStore()
    controller = build_controller(store=store)
    await controller._cmd_start(FakeUpdate(user_id=777), ctx())

    reject = FakeUpdate(user_id=ADMIN_ID, callback="sub_reject:777", with_message=False)
    await controller._handle_callback(reject, ctx())

    user_messages = [m for m in controller.app.bot.sent if m["chat_id"] == 777]
    assert user_messages and user_messages[-1]["text"].startswith("Заявка отклонена")
    assert 777 not in controller.admin_chat_ids
    assert 777 not in store.bot_admins
    assert store.subscribers[777]["status"] == "rejected"

    # Повторный /start отклонённого не создаёт новую заявку.
    again = FakeUpdate(user_id=777)
    await controller._cmd_start(again, ctx())
    assert "заявка была отклонена" in again.message.replies[0][0]
    assert 777 not in controller.admin_chat_ids


@pytest.mark.asyncio
async def test_subscription_decision_is_idempotent() -> None:
    """Повторное нажатие кнопки не меняет уже принятого решения."""
    store = FakeStore()
    controller = build_controller(store=store)
    await controller._cmd_start(FakeUpdate(user_id=777), ctx())

    first = FakeUpdate(user_id=ADMIN_ID, callback="sub_approve:777", with_message=False)
    await controller._handle_callback(first, ctx())
    second = FakeUpdate(user_id=ADMIN_ID, callback="sub_reject:777", with_message=False)
    await controller._handle_callback(second, ctx())

    alerts = [a for a in second.callback_query.answers if a[0]]
    assert any("уже обработана" in (a[0] or "") for a in alerts)
    assert 777 in controller.admin_chat_ids
    assert store.subscribers[777]["status"] == "active"


@pytest.mark.asyncio
async def test_subscription_callbacks_require_admin() -> None:
    """Посторонний не может одобрять или отклонять заявки."""
    store = FakeStore()
    controller = build_controller(store=store)
    await controller._cmd_start(FakeUpdate(user_id=777), ctx())

    stranger = FakeUpdate(user_id=42, callback="sub_approve:777", with_message=False)
    await controller._handle_callback(stranger, ctx())

    assert 777 not in controller.admin_chat_ids
    assert "⛔" in (stranger.callback_query.answers[0][0] or "")


@pytest.mark.asyncio
async def test_approved_subscription_survives_restart(tmp_path: Any) -> None:
    """Одобренная подписка работает после перезапуска бота (реальная база)."""
    store = JobStore(tmp_path / "subs.db")
    try:
        controller = build_controller(store=store)
        await controller._cmd_start(FakeUpdate(user_id=777), ctx())

        approve = FakeUpdate(
            user_id=ADMIN_ID, callback="sub_approve:777", with_message=False
        )
        await controller._handle_callback(approve, ctx())

        fresh = build_controller(store=JobStore(tmp_path / "subs.db"))
        newcomer = FakeUpdate(user_id=777)
        await fresh._cmd_status(newcomer, ctx())
        assert newcomer.message.replies
        assert "⛔" not in newcomer.message.replies[0][0]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_deluser_marks_subscription_rejected(tmp_path: Any) -> None:
    """После /deluser повторный /start не восстанавливает доступ."""
    store = JobStore(tmp_path / "subs.db")
    try:
        controller = build_controller(store=store)
        admin = FakeUpdate(user_id=ADMIN_ID)
        await controller._cmd_adduser(admin, ctx("555000111"))
        await controller._cmd_deluser(admin, ctx("555000111"))

        again = FakeUpdate(user_id=555000111)
        await controller._cmd_start(again, ctx())
        assert "заявка была отклонена" in again.message.replies[0][0]
        assert 555000111 not in controller.admin_chat_ids
    finally:
        store.close()


@pytest.mark.asyncio
async def test_users_lists_pending_requests(tmp_path: Any) -> None:
    """/users показывает ожидающие одобрения заявки."""
    store = FakeStore()
    store.add_subscriber_request(777, "newcomer", "Новый Пользователь")
    controller = build_controller(store=store)
    update = FakeUpdate(user_id=ADMIN_ID)

    await controller._cmd_users(update, ctx())

    text = update.message.replies[0][0]
    assert "777" in text and "новый" in text.lower()
    assert "Заявки" in text


@pytest.mark.asyncio
async def test_subscribe_button_creates_request() -> None:
    """Кнопка «📩 Подписаться» отправляет заявку без команды /start."""
    store = FakeStore()
    controller = build_controller(store=store)
    update = FakeUpdate(user_id=777, callback="sub:request", with_message=False)

    await controller._handle_callback(update, ctx())

    assert store.subscribers[777]["status"] == "pending"
    assert update.callback_query.answers
    # Админ получил карточку заявки с кнопками решения.
    callbacks = [
        button.callback_data
        for call in controller.app.bot.sent
        for row in call["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "sub_approve:777" in callbacks and "sub_reject:777" in callbacks


@pytest.mark.asyncio
async def test_subscribe_button_allows_retry_after_rejection() -> None:
    """Отклонённый пользователь может отправить заявку повторно."""
    store = FakeStore()
    controller = build_controller(store=store)
    await controller._cmd_start(FakeUpdate(user_id=777), ctx())
    reject = FakeUpdate(user_id=ADMIN_ID, callback="sub_reject:777", with_message=False)
    await controller._handle_callback(reject, ctx())
    sent_before = len(controller.app.bot.sent)

    retry = FakeUpdate(user_id=777, callback="sub:request", with_message=False)
    await controller._handle_callback(retry, ctx())

    assert store.subscribers[777]["status"] == "pending"
    # Админ снова получил карточку заявки.
    assert len(controller.app.bot.sent) > sent_before
    assert 777 not in controller.admin_chat_ids


@pytest.mark.asyncio
async def test_subscribe_button_is_idempotent_while_pending() -> None:
    """Пока заявка на рассмотрении, повторное нажатие не создаёт новую."""
    store = FakeStore()
    controller = build_controller(store=store)
    await controller._cmd_start(FakeUpdate(user_id=777), ctx())
    sent_before = len(controller.app.bot.sent)

    again = FakeUpdate(user_id=777, callback="sub:request", with_message=False)
    await controller._handle_callback(again, ctx())

    assert len(controller.app.bot.sent) == sent_before
    alerts = [answer[0] for answer in again.callback_query.answers if answer[0]]
    assert any("рассмотрении" in (alert or "") for alert in alerts)


@pytest.mark.asyncio
async def test_pending_user_cannot_call_commands(tmp_path: Any) -> None:
    """Gate по подписке: pending не пройдёт ни в команду, ни в callback."""
    store = JobStore(tmp_path / "gate.db")
    try:
        store.add_subscriber_request(555000111, "waiting", "Ожидающий")
        controller = build_controller(store=store)

        by_command = FakeUpdate(user_id=555000111)
        await controller._cmd_jobs(by_command, ctx())
        reply, kwargs = by_command.message.replies[0]
        assert "рассмотрении" in reply
        assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "sub:request"

        by_callback = FakeUpdate(
            user_id=555000111, callback="settings:hub", with_message=False
        )
        await controller._handle_callback(by_callback, ctx())
        alerts = [a[0] for a in by_callback.callback_query.answers if a[0]]
        assert any("рассмотрении" in (alert or "") for alert in alerts)
        assert not by_callback.callback_query.edits
    finally:
        store.close()


@pytest.mark.asyncio
async def test_active_subscriber_passes_gate(tmp_path: Any) -> None:
    """active проходит gate: команды и callback'и работают."""
    store = JobStore(tmp_path / "gate2.db")
    try:
        store.add_subscriber_request(555000111, "ok", "Активный")
        store.set_subscriber_status(555000111, "pending")
        store.set_subscriber_status(555000111, "active")
        controller = build_controller(store=store)
        controller.store.add_bot_admin(555000111, added_by=None)
        controller.admin_chat_ids.add(555000111)

        update = FakeUpdate(user_id=555000111)
        await controller._cmd_filters(update, ctx())
        assert "⛔" not in update.message.replies[0][0]
        assert "Подписка не активна" not in update.message.replies[0][0]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_admin_decision_edits_card_and_removes_buttons() -> None:
    """После решения карточка заявки правится: кнопки исчезают."""
    store = FakeStore()
    controller = build_controller(store=store)
    await controller._cmd_start(FakeUpdate(user_id=777), ctx())

    approve = FakeUpdate(user_id=ADMIN_ID, callback="sub_approve:777", with_message=False)
    await controller._handle_callback(approve, ctx())

    assert approve.callback_query.edits
    edited_text = approve.callback_query.edits[0][0]
    assert "Одобрено" in edited_text
    assert "777" in edited_text


@pytest.mark.asyncio
async def test_subscription_request_goes_to_super_admins_only(tmp_path: Any) -> None:
    """Заявка уходит администраторам настроек, а не всем подписчикам."""
    store = JobStore(tmp_path / "gate3.db")
    try:
        store.add_bot_admin(555000111, added_by=ADMIN_ID)
        controller = build_controller(store=store)
        await controller._cmd_start(FakeUpdate(user_id=777), ctx())

        recipients = {call["chat_id"] for call in controller.app.bot.sent}
        assert ADMIN_ID in recipients
        assert 555000111 not in recipients
    finally:
        store.close()


@pytest.mark.asyncio
async def test_audience_excludes_non_active_subscribers(tmp_path: Any) -> None:
    """Карточки заказов получают только активные подписки и админы."""
    store = JobStore(tmp_path / "gate4.db")
    try:
        for chat_id, status in ((111, "active"), (222, "pending"), (333, "rejected")):
            store.add_subscriber_request(chat_id, None, None)
            store.set_subscriber_status(chat_id, status)
        controller = build_controller(store=store)

        assert controller.audience_chat_ids() == {ADMIN_ID, 111}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_legacy_approved_status_is_migrated_to_active(tmp_path: Any) -> None:
    """Статус 'approved' из старых баз читается и мигрируется как active."""
    store = JobStore(tmp_path / "legacy.db")
    try:
        store.add_subscriber_request(555000111, None, None)
        # Эмулируем запись старой версии, минуя API нового статуса.
        store._conn.execute(
            "UPDATE bot_subscribers SET status = 'approved' WHERE chat_id = ?",
            (555000111,),
        )
        store._conn.commit()
        controller = build_controller(store=store)
        assert controller.subscription_status(555000111) == "active"

        # Повторное открытие базы приводит статус к новому имени.
        store.close()
        reopened = JobStore(tmp_path / "legacy.db")
        try:
            assert reopened.get_subscriber(555000111)["status"] == "active"
        finally:
            reopened.close()
    finally:
        pass


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_status_shows_last_cycle_and_pending_count() -> None:
    controller = build_controller()
    controller.stats.update({"jobs_collected": 12, "jobs_new": 3, "jobs_eligible": 2})
    update = FakeUpdate()

    await controller._cmd_status(update, ctx())

    text = update.message.replies[0][0]
    assert "запущен" in text
    assert "12" in text and "3" in text


@pytest.mark.asyncio
async def test_settings_hub_shows_all_sections_to_admin() -> None:
    """Админ видит в меню настроек все разделы, включая общие."""
    controller = build_controller()
    controller.interval_seconds = 300
    update = FakeUpdate()

    await controller._cmd_settings(update, ctx())

    text = update.message.replies[0][0]
    markup = update.message.replies[0][1]["reply_markup"]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "⚙️ Основные" in labels
    assert "🛠 Администрирование" in labels
    assert "✉️ Письма" in labels
    assert "👤 Профиль" in labels
    assert "🔧 Настройки" in labels
    assert "📂 Фильтры категорий" in labels
    assert "🌐 Фильтры бирж" in labels
    assert "📡 Мониторинг заказов" in labels
    assert "Выберите раздел" in text


@pytest.mark.asyncio
async def test_settings_hub_hides_admin_sections_from_subscriber(tmp_path: Any) -> None:
    """Подписчику доступны только три личных раздела — остальных нет вовсе."""
    store = JobStore(tmp_path / "hub.db")
    try:
        store.add_bot_admin(555000111, added_by=ADMIN_ID)
        controller = build_controller(store=store)
        update = FakeUpdate(user_id=555000111)

        await controller._cmd_settings(update, ctx())

        markup = update.message.replies[0][1]["reply_markup"]
        labels = [button.text for row in markup.inline_keyboard for button in row]
        assert labels == [
            "🔧 Настройки",
            "📂 Фильтры категорий",
            "🌐 Фильтры бирж",
            "📡 Мониторинг заказов",
        ]
        callbacks = [
            button.callback_data for row in markup.inline_keyboard for button in row
        ]
        assert "settings:admin" not in callbacks
        assert "settings:letters" not in callbacks
    finally:
        store.close()


@pytest.mark.asyncio
async def test_subscriber_cannot_open_admin_sections(tmp_path: Any) -> None:
    """Прямой запрос админского раздела и callback дают «Недостаточно прав»."""
    store = JobStore(tmp_path / "hub2.db")
    try:
        store.add_bot_admin(555000111, added_by=ADMIN_ID)
        controller = build_controller(store=store)

        by_command = FakeUpdate(user_id=555000111)
        await controller._cmd_settings(by_command, ctx("admin"))
        assert "Недостаточно прав" in by_command.message.replies[0][0]

        by_callback = FakeUpdate(user_id=555000111, callback="settings:main", with_message=False)
        await controller._handle_callback(by_callback, ctx())
        alerts = [answer[0] for answer in by_callback.callback_query.answers if answer[0]]
        assert any("Недостаточно прав" in (alert or "") for alert in alerts)
        assert not by_callback.callback_query.edits
    finally:
        store.close()


@pytest.mark.asyncio
async def test_subscriber_opens_monitoring_and_filters_sections(tmp_path: Any) -> None:
    """Личные разделы открываются подписчику и через команду, и по кнопке."""
    store = JobStore(tmp_path / "hub3.db")
    try:
        store.add_bot_admin(555000111, added_by=ADMIN_ID)
        controller = build_controller(store=store)

        monitoring = FakeUpdate(user_id=555000111)
        await controller._cmd_settings(monitoring, ctx("monitoring"))
        assert "Мониторинг заказов" in monitoring.message.replies[0][0]

        filters = FakeUpdate(user_id=555000111, callback="settings:cat_filters", with_message=False)
        await controller._handle_callback(filters, ctx())
        assert filters.callback_query.edits
        assert "Мои фильтры" in filters.callback_query.edits[0][0]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_set_admin_grants_settings_access_and_persists(tmp_path: Any) -> None:
    """/set_admin открывает разделы настроек и переживает рестарт."""
    store = JobStore(tmp_path / "admins2.db")
    try:
        store.add_bot_admin(555000111, added_by=ADMIN_ID)
        controller = build_controller(store=store)
        update = FakeUpdate(user_id=ADMIN_ID)

        await controller._cmd_set_admin(update, ctx("555000111"))

        assert "назначен администратором настроек" in update.message.replies[0][0]
        assert 555000111 in controller.super_admin_ids
        assert 555000111 in controller.admin_chat_ids
        assert store.list_super_admins() == {555000111}

        # Права реально работают: админский раздел открывается.
        admin_update = FakeUpdate(user_id=555000111)
        await controller._cmd_settings(admin_update, ctx("admin"))
        assert "Недостаточно прав" not in admin_update.message.replies[0][0]
        assert "Администрирование" in admin_update.message.replies[0][0]

        fresh = build_controller(store=JobStore(tmp_path / "admins2.db"))
        assert 555000111 in fresh.super_admin_ids
    finally:
        store.close()


@pytest.mark.asyncio
async def test_unset_admin_keeps_order_access(tmp_path: Any) -> None:
    """/unset_admin снимает роль настроек, но не доступ к заказам."""
    store = JobStore(tmp_path / "admins3.db")
    try:
        store.add_bot_admin(555000111, added_by=ADMIN_ID)
        controller = build_controller(store=store)
        admin = FakeUpdate(user_id=ADMIN_ID)
        await controller._cmd_set_admin(admin, ctx("555000111"))
        await controller._cmd_unset_admin(admin, ctx("555000111"))

        assert 555000111 not in controller.super_admin_ids
        assert 555000111 in controller.admin_chat_ids

        update = FakeUpdate(user_id=555000111)
        await controller._cmd_jobs(update, ctx())
        assert "⛔" not in update.message.replies[0][0]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_only_super_admin_can_assign_roles(tmp_path: Any) -> None:
    """Обычный подписчик не может выдать себе или другому роль администратора."""
    store = JobStore(tmp_path / "admins4.db")
    try:
        store.add_bot_admin(555000111, added_by=ADMIN_ID)
        controller = build_controller(store=store)
        update = FakeUpdate(user_id=555000111)

        await controller._cmd_set_admin(update, ctx("555000111"))

        assert "Недостаточно прав" in update.message.replies[0][0]
        assert store.list_super_admins() == set()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_set_admin_rejects_bad_input_and_env_admins(tmp_path: Any) -> None:
    """/set_admin проверяет формат ID и не трогает администраторов из .env."""
    store = JobStore(tmp_path / "admins5.db")
    try:
        controller = build_controller(store=store)
        update = FakeUpdate(user_id=ADMIN_ID)

        await controller._cmd_set_admin(update, ctx("abc"))
        assert "Использование" in update.message.replies[0][0]

        await controller._cmd_set_admin(update, ctx(str(ADMIN_ID)))
        assert ".env" in update.message.replies[1][0]
        assert store.list_super_admins() == set()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_admin_cannot_revoke_own_role(tmp_path: Any) -> None:
    """Самоблокировка запрещена: иначе управлять настройками станет некому."""
    store = JobStore(tmp_path / "admins6.db")
    try:
        controller = build_controller(store=store)
        update = FakeUpdate(user_id=ADMIN_ID)

        await controller._cmd_unset_admin(update, ctx(str(ADMIN_ID)))

        assert ".env" in update.message.replies[0][0]
        assert ADMIN_ID in controller.super_admin_ids
    finally:
        store.close()


@pytest.mark.asyncio
async def test_admin_commands_require_super_admin_rights(tmp_path: Any) -> None:
    """Команды конвейера закрыты для обычного подписчика."""
    store = JobStore(tmp_path / "admins7.db")
    try:
        store.add_bot_admin(555000111, added_by=ADMIN_ID)
        controller = build_controller(store=store)
        update = FakeUpdate(user_id=555000111)

        for call in (
            lambda: controller._cmd_platforms(update, ctx()),
            lambda: controller._cmd_categories(update, ctx()),
            lambda: controller._cmd_score(update, ctx()),
            lambda: controller._cmd_send(update, ctx()),
            lambda: controller._cmd_set(update, ctx("runtime.dry_run", "false")),
            lambda: controller._cmd_reload(update, ctx()),
        ):
            await call()

        denials = [text for text, _ in update.message.replies if "Недостаточно прав" in text]
        assert len(denials) == 6
        # Настройки не изменились.
        assert controller.config.runtime.dry_run is True
    finally:
        store.close()


@pytest.mark.asyncio
async def test_interval_accepts_bounds_and_rejects_out_of_range() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_interval(update, ctx("1800"))
    assert controller.interval_seconds == 1800

    await controller._cmd_interval(update, ctx("30"))
    await controller._cmd_interval(update, ctx("4000"))
    await controller._cmd_interval(update, ctx("abc"))

    replies = [text for text, _ in update.message.replies]
    assert any("Минимальный" in text for text in replies)
    assert any("Максимальный" in text for text in replies)
    assert any("Неверный формат" in text for text in replies)
    assert controller.interval_seconds == 1800


@pytest.mark.asyncio
async def test_interval_min_and_max_are_inclusive() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_interval(update, ctx("900"))
    assert controller.interval_seconds == 900
    await controller._cmd_interval(update, ctx("3600"))
    assert controller.interval_seconds == 3600


@pytest.mark.asyncio
async def test_interval_is_super_admin_only() -> None:
    """Обычный подписчик не меняет интервал: это общая настройка конвейера."""
    controller = build_controller()
    update = FakeUpdate(user_id=42)

    await controller._cmd_interval(update, ctx("1800"))

    assert controller.interval_seconds == 900
    assert any("только администратору" in text for text, _ in update.message.replies)


@pytest.mark.asyncio
async def test_score_penalty_and_freshness_are_admin_settings() -> None:
    """/score задает только приоритет (штраф/свежесть); порогов больше нет."""
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_score(update, ctx("penalty", "0.4"))
    assert controller.config.scoring.red_flag_penalty == 0.4

    await controller._cmd_score(update, ctx("freshness", "24"))
    assert controller.config.scoring.freshness_half_life_hours == 24

    await controller._cmd_score(update, ctx("penalty", "2"))
    assert controller.config.scoring.red_flag_penalty == 0.4
    assert any("0..1" in text for text, _ in update.message.replies)

    # Старые подкоманды порогов больше не принимаются.
    await controller._cmd_score(update, ctx("min", "40"))
    await controller._cmd_score(update, ctx("skill", "0.3"))
    await controller._cmd_score(update, ctx("top", "5"))
    replies = [text for text, _ in update.message.replies]
    assert any("penalty" in text for text in replies)


@pytest.mark.asyncio
async def test_score_is_super_admin_only() -> None:
    """Обычный подписчик не трогает штраф: влияет на порядок у всех."""
    controller = build_controller()
    update = FakeUpdate(user_id=42)

    await controller._cmd_score(update, ctx("penalty", "0.5"))

    assert controller.config.scoring.red_flag_penalty == 0.35
    assert any("только администратору" in text for text, _ in update.message.replies)


@pytest.mark.asyncio
async def test_markup_accepts_percentage_and_multiplier() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_markup(update, ctx("15"))
    assert controller.profile.pricing.markup_percent == pytest.approx(1.15)

    await controller._cmd_markup(update, ctx("1.3"))
    assert controller.profile.pricing.markup_percent == pytest.approx(1.3)

    await controller._cmd_markup(update, ctx("500"))
    assert controller.profile.pricing.markup_percent == pytest.approx(1.3)


@pytest.mark.asyncio
async def test_minprice_updates_profile() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_minprice(update, ctx("8 000"))
    assert controller.profile.pricing.min_acceptable_rub == 8000

    await controller._cmd_minprice(update, ctx("-5"))
    assert controller.profile.pricing.min_acceptable_rub == 8000


@pytest.mark.asyncio
async def test_pricing_command_builds_strategy_keyboard() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_pricing(update, ctx())

    text, kwargs = update.message.replies[0]
    assert "Стратегия" in text
    assert "markup" in str(kwargs["reply_markup"])


@pytest.mark.asyncio
async def test_pricing_callback_switches_strategy() -> None:
    controller = build_controller()
    update = FakeUpdate(callback="pricing_strategy_minimum", with_message=False)

    await controller._handle_callback(update, ctx())

    assert controller.profile.pricing.strategy == "minimum"
    assert "Минимум" in update.callback_query.edits[0][0]


@pytest.mark.asyncio
async def test_platforms_command_and_toggle() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_platforms(update, ctx())
    _text, kwargs = update.message.replies[0]
    assert kwargs["reply_markup"] is not None

    toggle = FakeUpdate(callback="platform_toggle_kwork_ru", with_message=False)
    await controller._handle_callback(toggle, ctx())
    assert controller.config.platforms["kwork_ru"].enabled is True
    assert "включена" in toggle.callback_query.edits[0][0]


@pytest.mark.asyncio
async def test_categories_command_and_callbacks() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_categories(update, ctx())
    assert update.message.replies

    toggle = FakeUpdate(callback="category_filter_toggle", with_message=False)
    await controller._handle_callback(toggle, ctx())
    assert controller.config.category_filter.enabled is False

    add = FakeUpdate(callback="category_toggle_design", with_message=False)
    await controller._handle_callback(add, ctx())
    assert "design" in controller.config.category_filter.categories


@pytest.mark.asyncio
async def test_categories_add_by_russian_name_and_subcategory() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_categories(update, ctx("add", "Дизайн"))
    assert "design" in controller.config.category_filter.categories

    await controller._cmd_categories(update, ctx("sub", "add", "programming", "парсер"))
    subs = controller.config.category_filter.subcategories
    assert any("парс" in item.casefold() for item in subs["programming"])

    await controller._cmd_categories(update, ctx("rm", "design"))
    assert "design" not in controller.config.category_filter.categories


@pytest.mark.asyncio
async def test_categories_menu_navigation_rubric_by_rubric() -> None:
    """Рубрики в меню предоставляются по очереди: сначала список, потом подменю."""
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_categories(update, ctx())
    _text, kwargs = update.message.replies[0]
    markup = kwargs["reply_markup"]
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    # Все семь рубрик Kwork с индивидуальными кнопками.
    assert "cat_top_programming" in callbacks
    assert "cat_top_design" in callbacks
    assert "cat_top_writing_translations" in callbacks
    assert "cat_top_seo" in callbacks
    assert "cat_top_promotion" in callbacks
    assert "cat_top_audio_video" in callbacks
    assert "cat_top_business" in callbacks
    assert "cat_price" in callbacks

    # Открытие подменю рубрики.
    top = FakeUpdate(callback="cat_top_programming", with_message=False)
    await controller._handle_callback(top, ctx())
    sub_text, sub_kwargs = top.callback_query.edits[0]
    assert "Разработка и IT" in sub_text
    sub_callbacks = [
        btn.callback_data
        for row in sub_kwargs["reply_markup"].inline_keyboard
        for btn in row
    ]
    assert any(c.startswith("cat_sub_programming_") for c in sub_callbacks)
    assert "cat_menu" in sub_callbacks

    # Возврат к списку рубрик.
    back = FakeUpdate(callback="cat_menu", with_message=False)
    await controller._handle_callback(back, ctx())
    assert "Детальная настройка" in back.callback_query.edits[0][0]


@pytest.mark.asyncio
async def test_categories_subcategory_toggle_and_group() -> None:
    controller = build_controller()
    cat = controller.config.category_filter

    # Находим кнопку подкатегории «Парсеры» в подменю programming.
    markup = controller._category_sub_markup("programming")
    assert markup is not None
    parser_btn = next(
        btn
        for row in markup.inline_keyboard
        for btn in row
        if btn.text and "Парсеры" in btn.text
    )

    # Переключение категории автоматически включает рубрику.
    toggle = FakeUpdate(callback=parser_btn.callback_data, with_message=False)
    await controller._handle_callback(toggle, ctx())
    assert "programming" in cat.categories
    assert "Парсеры" in cat.subcategories["programming"]
    assert "✅" in toggle.callback_query.edits[0][0]

    # Повторный клик снимает выбор: подкатегорий не осталось —
    # рубрика выключается (иначе она осталась бы «выбрана целиком»).
    untoggle = FakeUpdate(callback=parser_btn.callback_data, with_message=False)
    await controller._handle_callback(untoggle, ctx())
    assert "Парсеры" not in cat.subcategories.get("programming", [])
    assert "programming" not in cat.categories

    # Группа целиком: кнопка «Верстка».
    grp_btn = next(
        btn
        for row in markup.inline_keyboard
        for btn in row
        if btn.text and btn.text.endswith("Верстка")
    )
    grp = FakeUpdate(callback=grp_btn.callback_data, with_message=False)
    await controller._handle_callback(grp, ctx())
    assert "Верстка по макету" in cat.subcategories["programming"]
    assert "Доработка и адаптация верстки" in cat.subcategories["programming"]

    # «Вся рубрика» снимает ограничения, рубрика остаётся выбранной.
    all_btn = FakeUpdate(callback="cat_all_programming", with_message=False)
    await controller._handle_callback(all_btn, ctx())
    assert "programming" not in cat.subcategories
    assert "programming" in cat.categories

    # Рубрика выбрана целиком: кнопка превращается в «Выключить рубрику».
    sub_markup = controller._category_sub_markup("programming")
    assert sub_markup is not None
    first = sub_markup.inline_keyboard[0][0]
    assert first.text == "⬜ Выключить рубрику"
    assert first.callback_data == "cat_off_programming"

    # Кнопка выключает рубрику: ни рубрики, ни подкатегорий в выборе нет.
    off_btn = FakeUpdate(callback="cat_off_programming", with_message=False)
    await controller._handle_callback(off_btn, ctx())
    assert "programming" not in cat.categories
    assert "programming" not in cat.subcategories


@pytest.mark.asyncio
async def test_categories_min_price_command_and_callback() -> None:
    controller = build_controller()
    update = FakeUpdate()

    # Команда.
    await controller._cmd_categories(update, ctx("price", "5000"))
    assert controller.config.category_filter.min_price_rub == 5000

    # Кнопка меню ставит режим ожидания числа и присылает подсказку.
    price_btn = FakeUpdate(callback="cat_price", with_message=False)
    await controller._handle_callback(price_btn, ctx())
    assert controller._awaiting_input == "category_filter.min_price_rub"
    assert any("минимальную цену" in m["text"] for m in controller.app.bot.sent)

    # Ввод текстом применяет значение (0 — выключить).
    text_update = FakeUpdate()
    text_update.message = FakeMessage(text="0")
    controller._awaiting_input = "category_filter.min_price_rub"
    await controller._handle_text_input(text_update, ctx())
    assert controller.config.category_filter.min_price_rub == 0
    assert controller._awaiting_input is None


@pytest.mark.asyncio
async def test_categories_callback_with_bad_data_alerts() -> None:
    controller = build_controller()

    bad = FakeUpdate(callback="cat_top_nonexistent", with_message=False)
    await controller._handle_callback(bad, ctx())
    alerts = [a for a in bad.callback_query.answers if a[0]]
    assert alerts and "не найдена" in alerts[0][0]

    garbage = FakeUpdate(callback="cat_sub_programming_99_99", with_message=False)
    await controller._handle_callback(garbage, ctx())
    alerts = [a for a in garbage.callback_query.answers if a[0]]
    assert alerts and "не найдена" in alerts[0][0]


@pytest.mark.asyncio
async def test_platforms_add_remove_and_url() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_platforms(update, ctx("add", "pchel_net"))
    assert controller.config.platforms["pchel_net"].enabled is True
    assert controller.config.platforms["pchel_net"].listing_url.startswith("https://")

    await controller._cmd_platforms(update, ctx("off", "pchel_net"))
    assert controller.config.platforms["pchel_net"].enabled is False

    await controller._cmd_platforms(update, ctx("rm", "kwork_ru"))
    assert "kwork_ru" not in controller.config.platforms

    await controller._cmd_platforms(update, ctx("add", "unknown_platform"))
    assert any("Неизвестная" in text for text, _ in update.message.replies)


@pytest.mark.asyncio
async def test_set_get_roundtrip_for_arbitrary_paths() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_set(update, ctx("scoring.red_flag_penalty", "0.25"))
    assert controller.config.scoring.red_flag_penalty == 0.25

    await controller._cmd_set(update, ctx("runtime.dry_run", "false"))
    assert controller.config.runtime.dry_run is False

    await controller._cmd_set(update, ctx("profile.pricing.min_acceptable_rub", "7000"))
    assert controller.profile.pricing.min_acceptable_rub == 7000

    await controller._cmd_set(update, ctx("runtime.dry_run", "maybe"))
    assert controller.config.runtime.dry_run is False
    assert any("Неверное значение" in text for text, _ in update.message.replies)

    await controller._cmd_get(update, ctx("scoring.red_flag_penalty"))
    assert "0.25" in update.message.replies[-1][0]


@pytest.mark.asyncio
async def test_send_requires_confirm_for_live_mode() -> None:
    controller = build_controller()
    controller.config.messaging.require_human_approval = False
    update = FakeUpdate()

    await controller._cmd_send(update, ctx("dry", "off"))
    assert controller.config.runtime.dry_run is True
    assert any("confirm" in text for text, _ in update.message.replies)

    await controller._cmd_send(update, ctx("dry", "off", "confirm"))
    assert controller.config.runtime.dry_run is False


@pytest.mark.asyncio
async def test_prompt_command_changes_system_prompt() -> None:
    from freelance_bot.llm.prompts import build_system_prompt

    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_prompt(update, ctx("system", "Ты пишешь очень кратко."))
    assert controller.config.llm.system_prompt == "Ты пишешь очень кратко."
    assert build_system_prompt(controller.config.llm).startswith("Ты пишешь очень кратко.")

    await controller._cmd_prompt(update, ctx("extra", "Без эмодзи."))
    prompt = build_system_prompt(controller.config.llm)
    assert "ДОПОЛНИТЕЛЬНЫЕ ТРЕБОВАНИЯ" in prompt
    assert "Без эмодзи." in prompt

    await controller._cmd_prompt(update, ctx("reset", "all"))
    assert controller.config.llm.system_prompt == ""
    assert controller.config.llm.extra_instructions == ""


@pytest.mark.asyncio
async def test_llm_custom_provider_add_use_model_and_remove() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_llm(
        update,
        ctx("provider", "add", "openrouter", "https://openrouter.ai/api/v1", "deepseek/chat"),
    )
    provider = controller.config.llm.providers["openrouter"]
    assert controller.config.llm.active_provider == "openrouter"
    assert provider.base_url == "https://openrouter.ai/api/v1"
    assert provider.model == "deepseek/chat"
    assert provider.api_key_env == "LLM_PROVIDER_OPENROUTER_API_KEY"

    await controller._cmd_llm(update, ctx("model", "deepseek/deepseek-chat"))
    assert provider.model == "deepseek/deepseek-chat"

    await controller._cmd_llm(update, ctx("provider", "use", "default"))
    assert controller.config.llm.active_provider == ""

    await controller._cmd_llm(update, ctx("provider", "show", "openrouter"))
    assert any("openrouter" in text for text, _ in update.message.replies)

    await controller._cmd_llm(update, ctx("provider", "rm", "openrouter"))
    assert "openrouter" not in controller.config.llm.providers


@pytest.mark.asyncio
async def test_llm_provider_rejects_bad_name_and_url() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_llm(
        update, ctx("provider", "add", "bad name", "https://example.com/v1", "model")
    )
    assert not controller.config.llm.providers

    await controller._cmd_llm(
        update, ctx("provider", "add", "local", "ftp://example.com", "model")
    )
    assert not controller.config.llm.providers
    assert any("URL" in text for text, _ in update.message.replies)


@pytest.mark.asyncio
async def test_pause_and_resume_flip_the_pipeline_flag() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_pause(update, ctx())
    assert controller.pipeline_paused is True
    await controller._cmd_resume(update, ctx())
    assert controller.pipeline_paused is False


@pytest.mark.asyncio
async def test_stats_reads_the_database() -> None:
    controller = build_controller(store=FakeStore())
    update = FakeUpdate()

    await controller._cmd_stats(update, ctx())

    text = update.message.replies[0][0]
    assert "11" in text and "7" in text and "4" in text
    assert "3" in text and "2" in text and "1" in text


@pytest.mark.asyncio
async def test_stats_without_store_uses_zero_counters() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_stats(update, ctx())

    assert "0" in update.message.replies[0][0]


@pytest.mark.asyncio
async def test_jobs_lists_eligible_with_show_status(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    try:
        shown = scored_job(title="Первый заказ", external_id="1")
        fresh = scored_job(title="Второй заказ", external_id="2")
        store.save_scored(shown, eligible=True)
        store.save_scored(fresh, eligible=True)
        store.mark_notified(shown.job.uid)

        controller = build_controller(store=store)
        update = FakeUpdate()

        await controller._cmd_jobs(update, ctx("5"))

        text, kwargs = update.message.replies[0]
        assert "Последние заказы по вашим фильтрам" in text
        assert "Первый заказ" in text and "Второй заказ" in text
        assert "✅ показан" in text and "🆕 новый" in text
        assert "https://example.test/1" in text
        assert kwargs["parse_mode"] == "HTML"
        # Каждый заказ снабжён кнопкой избранного.
        markup = kwargs["reply_markup"]
        buttons = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert any(str(data).startswith("fav:") for data in buttons)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_jobs_honors_personal_filters(tmp_path) -> None:
    """Личный фильтр по бирже скрывает заказы чужих площадок."""
    store = JobStore(tmp_path / "jobs.db")
    try:
        shown = scored_job(title="Заказ с fl_ru", external_id="1")
        store.save_scored(shown, eligible=True)

        controller = build_controller(store=store)
        update = FakeUpdate()
        controller.store.set_user_filters(ADMIN_ID, platforms=["kwork_ru"])

        await controller._cmd_jobs(update, ctx("5"))

        text = update.message.replies[0][0]
        assert "Заказ с fl_ru" not in text
        assert "Подходящих по вашим фильтрам заказов пока нет" in text
    finally:
        store.close()


@pytest.mark.asyncio
async def test_jobs_rejects_invalid_limit(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    try:
        controller = build_controller(store=store)
        update = FakeUpdate()

        await controller._cmd_jobs(update, ctx("abc"))

        assert "Использование" in update.message.replies[0][0]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_menu_lists_all_command_groups() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_menu(update, ctx())

    text, kwargs = update.message.replies[0]
    for group in ("Основное", "Мониторинг", "Мои фильтры", "Администрирование", "Настройки"):
        assert group in text
    for command in ("/start", "/menu", "/help", "/jobs", "/filters", "/llm", "/reset"):
        assert command in text
    assert kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["_cmd_start", "_cmd_menu", "_cmd_help"])
async def test_menu_and_help_attach_persistent_keyboard(command: str) -> None:
    controller = build_controller()
    update = FakeUpdate()

    await getattr(controller, command)(update, ctx())

    markup = update.message.replies[-1][1]["reply_markup"]
    assert [[button.text for button in row] for row in markup.keyboard] == [
        ["/menu", "/help"]
    ]
    assert markup.resize_keyboard is True
    assert markup.is_persistent is True


@pytest.mark.asyncio
async def test_help_describes_every_command() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_help(update, ctx())

    text = "".join(reply for reply, _ in update.message.replies)
    assert "Справка по командам" in text
    assert "/jobs [5|10|30]" in text
    assert "Последние заказы по вашим фильтрам" in text
    assert "/help команда" in text
    # /menu и /help остаются синхронными: описание есть у каждой команды.
    for item in COMMAND_HELP:
        assert html_escape(item.usage) in text
        assert html_escape(item.summary) in text


@pytest.mark.asyncio
async def test_help_for_single_command_shows_examples() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_help(update, ctx("/jobs"))

    text = update.message.replies[0][0]
    assert "/jobs [5|10|30]" in text
    assert "/jobs 30" in text
    assert "Мониторинг" in text


@pytest.mark.asyncio
async def test_help_unknown_command_suggests_menu() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_help(update, ctx("nope"))

    text = update.message.replies[0][0]
    assert "не найдена" in text
    assert "/menu" in text


@pytest.mark.asyncio
async def test_profile_command_formats_and_escapes() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_profile(update, ctx())

    text = update.message.replies[0][0]
    assert "Python" in text
    assert "Telegram Bot API" in text


@pytest.mark.asyncio
async def test_logs_empty_and_error_listing() -> None:
    controller = build_controller()
    update = FakeUpdate()

    await controller._cmd_logs(update, ctx())
    assert "Ошибок не обнаружено" in update.message.replies[0][0]

    controller.last_errors.append((datetime.now(UTC), "fl_ru: bad_markup *"))
    await controller._cmd_logs(update, ctx())
    text = update.message.replies[-1][0]
    assert "bad\\_markup" in text
    assert "\\*" in text


# --------------------------------------------------------------------------- #
# Уведомления и одобрения
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_deliver_job_sends_analysis_price_and_buttons() -> None:
    """Карточка в HTML содержит анализ, цену, черновик и кнопки с ♥."""
    controller = build_controller()
    scored = scored_job(title="Бот со спецсимволами _*[]")
    analysis = JobAnalysis(
        need_summary_ru="Нужен бот с заявками и таблицами.",
        skill_match=["Python", "Telegram Bot API"],
        requirements=["Python"],
    )

    delivered = await controller.deliver_job(
        [ADMIN_ID],
        scored,
        "Здравствуйте! Сделаю бота на Python.",
        price=23000,
        strategy="markup",
        analysis=analysis,
        message_id=42,
    )

    assert delivered == [ADMIN_ID]
    assert controller.app.bot.sent
    sent = controller.app.bot.sent[0]
    assert sent["chat_id"] == ADMIN_ID
    # Теги именно рендерятся разметкой, а не приходят текстом.
    assert sent["parse_mode"] == "HTML"
    assert "<b>Цена:</b>" in sent["text"]
    assert "<b>Суть задачи:</b>" in sent["text"]
    assert sent["reply_markup"] is not None
    # В HTML нет экранированных метасимволов Markdown.
    assert "\\*" not in sent["text"]

    # Кнопки действия + избранное.
    keyboard = sent["reply_markup"]
    btn_texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert any("Отправить" in t for t in btn_texts)
    assert any("Пропустить" in t for t in btn_texts)
    assert any("Скопировать письмо" in t for t in btn_texts)
    assert any("Открыть заказ" in t for t in btn_texts)
    assert any("Понравилось" in t for t in btn_texts)

    pending = controller.pending_approvals[scored.job.uid]
    assert pending.approved is None
    assert pending.message_id == 42


@pytest.mark.asyncio
async def test_deliver_job_without_letter_keeps_favorite_button() -> None:
    """Без письма карточка всё равно показывается с кнопкой ♥."""
    controller = build_controller()

    delivered = await controller.deliver_job([ADMIN_ID], scored_job(), None)

    assert delivered == [ADMIN_ID]
    sent = controller.app.bot.sent[0]
    assert sent["reply_markup"] is not None
    keyboard = sent["reply_markup"]
    assert [b.text for row in keyboard.inline_keyboard for b in row] == ["♥ Понравилось"]
    assert "Письмо ещё не готово" in sent["text"]
    assert not controller.pending_approvals


@pytest.mark.asyncio
async def test_deliver_job_falls_back_to_plain_text_on_parse_error() -> None:
    """Битая разметка карточки: повтор чистым текстом, заказ не теряется."""
    controller = build_controller()
    controller.app.bot = ParseErrorBot()
    scored = scored_job()

    delivered = await controller.deliver_job([ADMIN_ID], scored, "Черновик письма")

    assert delivered == [ADMIN_ID]
    sent = controller.app.bot.sent[-1]
    assert sent.get("parse_mode") is None
    assert "<b>" not in sent["text"]
    assert "Новый заказ" in sent["text"]
    assert sent["reply_markup"] is not None


@pytest.mark.asyncio
async def test_send_callback_delivers_draft_and_edits_card(tmp_path) -> None:
    """Нажатие «Отправить» присылает письмо и обновляет карточку."""
    from freelance_bot.storage import JobStore

    store = JobStore(tmp_path / "send.db")
    try:
        controller = build_controller(store=store)
        scored = scored_job()
        await controller.deliver_job(
            [ADMIN_ID], scored, "Текст письма для отправки", message_id=7
        )

        update = FakeUpdate(callback=f"send:{scored.job.uid}", with_message=False)
        await controller._handle_callback(update, ctx())

        # Письмо отправлено через bot.send_message: sent[0] — карточка,
        # sent[1] — черновик, sent[2] — пометка «Открыто для ответа».
        assert len(controller.app.bot.sent) == 3
        sent_draft = controller.app.bot.sent[1]
        assert "<pre>" in sent_draft["text"]
        assert "Текст письма для отправки" in sent_draft["text"]

        # Второе сообщение — пометка об отправке.
        assert "✅ Открыто для ответа" in controller.app.bot.sent[2]["text"]

        # В БД записан статус.
        from freelance_bot.models import OrderStatus

        assert store.status_of(scored.job.uid) == OrderStatus.SENT_CLICKED
    finally:
        store.close()


@pytest.mark.asyncio
async def test_skip_callback_removes_buttons(tmp_path) -> None:
    """Нажатие «Пропустить» убирает кнопки и помечает заказ skipped."""
    from freelance_bot.storage import JobStore

    store = JobStore(tmp_path / "skip.db")
    try:
        controller = build_controller(store=store)
        scored = scored_job()
        await controller.deliver_job([ADMIN_ID], scored, "Черновик", message_id=9)

        update = FakeUpdate(callback=f"skip:{scored.job.uid}", with_message=False)
        await controller._handle_callback(update, ctx())

        # Кнопки убраны.
        assert update.callback_query.edits[-1][1].get("reply_markup") is None or (
            update.callback_query.edits[-1][1].get("reply_markup") is not None
            and update.callback_query.edits[-1][1]["reply_markup"].inline_keyboard == []
        )

        from freelance_bot.models import OrderStatus

        assert store.status_of(scored.job.uid) == OrderStatus.SKIPPED
    finally:
        store.close()


@pytest.mark.asyncio
async def test_copy_callback_delivers_draft(tmp_path) -> None:
    """«Копировать» присылает письмо в <pre> без изменения карточки."""
    from freelance_bot.storage import JobStore

    store = JobStore(tmp_path / "copy.db")
    try:
        controller = build_controller(store=store)
        scored = scored_job()
        await controller.deliver_job([ADMIN_ID], scored, "Копируемый текст", message_id=10)

        update = FakeUpdate(callback=f"copy:{scored.job.uid}", with_message=False)
        await controller._handle_callback(update, ctx())

        # Копирование присылает письмо в <pre> через bot.send_message.
        sent_draft = controller.app.bot.sent[-1]
        assert "<pre>" in sent_draft["text"]
        assert "Копируемый текст" in sent_draft["text"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_old_approve_callback_shows_migration_alert(tmp_path) -> None:
    """Старые approve/reject-колбеки отвечают предупреждением о миграции."""
    from freelance_bot.storage import JobStore

    store = JobStore(tmp_path / "old.db")
    try:
        controller = build_controller(store=store)
        scored = scored_job()
        await controller.deliver_job([ADMIN_ID], scored, "Текст", message_id=11)

        update = FakeUpdate(callback=f"approve_{scored.job.uid}", with_message=False)
        await controller._handle_callback(update, ctx())

        # Первый answer — пустой (query.answer()), алерт — последний.
        alerts = [a for a in update.callback_query.answers if a[0]]
        assert alerts, "должен прийти алерт о миграции"
        assert "новые кнопки" in (alerts[0][0] or "")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_cycle_complete_updates_stats_and_notifies() -> None:
    controller = build_controller()

    await controller.notify_cycle_complete(10, 4, 2, 1)

    assert controller.stats["jobs_collected"] == 10
    assert controller.stats["messages_generated"] == 1
    assert "Цикл завершён" in controller.app.bot.sent[0]["text"]


@pytest.mark.asyncio
async def test_empty_cycle_updates_stats_without_message() -> None:
    controller = build_controller()

    await controller.notify_cycle_complete(50, 0, 0, 0)

    assert controller.stats["jobs_collected"] == 50
    assert controller.stats["jobs_eligible"] == 0
    assert controller.app.bot.sent == []


@pytest.mark.asyncio
async def test_error_notification_is_stored_and_escaped() -> None:
    controller = build_controller()

    await controller.notify_error("kwork_ru: timeout * 2")

    assert controller.last_errors
    sent = controller.app.bot.sent[0]["text"]
    assert "Ошибка" in sent
    assert "\\*" in sent


# --------------------------------------------------------------------------- #
# Жизненный цикл
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_start_registers_handlers_and_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import freelance_bot.telegram_bot as tb

    monkeypatch.setattr(tb, "Application", FakeApplicationFactory)
    controller = TelegramBotController(
        token="123:test",
        admin_chat_ids=[ADMIN_ID],
        config=build_config(),
        profile=build_profile(),
    )

    await controller.start()

    assert controller.is_running is True
    assert len(controller.app.handlers) == 38
    assert controller.app.initialized and controller.app.started
    assert controller.app.updater.started is True
    # В меню Telegram (кнопка «/») есть все команды, включая /menu и /help.
    registered = {command.command for command in controller.app.bot.commands}
    assert {"menu", "help", "jobs", "status"} <= registered
    assert len(registered) == len(COMMAND_HELP)

    await controller.stop()

    assert controller.is_running is False
    assert controller.app.stopped is True
    assert controller.app.updater.stopped is True


def test_notifications_are_ignored_before_start() -> None:
    controller = build_controller()
    controller.is_running = False

    import asyncio

    asyncio.run(controller.notify_error("ignored"))

    assert controller.last_errors
    assert not controller.app.bot.sent


# --------------------------------------------------------------------------- #
# Счётчики хранилища для /stats
# --------------------------------------------------------------------------- #


def test_store_stats_since_counts_recent_rows(tmp_path) -> None:
    store = JobStore(tmp_path / "bot.db")
    try:
        job = make_job(external_id="77")
        store.upsert_job(job, score=50, eligible=True)
        message_id = store.save_message(
            GeneratedMessage(platform="fl_ru", job_uid=job.uid, text="Письмо")
        )
        store.approve(message_id)
        store.mark_sent(message_id)
        store.reject(store.save_message(GeneratedMessage(platform="fl_ru", job_uid="x", text="2")))

        stats = store.stats_since(days=7, min_score=0)

        assert stats["total_jobs"] == 1
        assert stats["total_eligible"] == 1
        assert stats["total_messages"] == 2
        assert stats["total_sent"] == 1
        assert stats["total_rejected"] == 1
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# Личные фильтры (/filters) и избранное (♥)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_filters_menu_shows_and_allows_everything_by_default() -> None:
    """/filters без аргументов показывает меню; пустой фильтр = всё."""
    controller = build_controller(store=FakeStore())
    update = FakeUpdate()

    await controller._cmd_filters(update, ctx())

    text, kwargs = update.message.replies[0]
    assert "Мои фильтры заказов" in text
    assert "Биржи: все" in text
    assert kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_filters_platform_toggle_saves_to_store() -> None:
    """Кнопка биржи переключает выбор и пишет его в user_filters."""
    controller = build_controller(store=FakeStore())
    update = FakeUpdate(callback="uf_platform_fl_ru", with_message=False)

    await controller._handle_callback(update, ctx())

    assert controller.store.get_user_filters(ADMIN_ID).platforms == ["fl_ru"]
    # Повторный тап снимает выбор.
    await controller._handle_callback(update, ctx())
    assert controller.store.get_user_filters(ADMIN_ID).platforms == []


@pytest.mark.asyncio
async def test_filters_rubric_toggle_and_reset() -> None:
    """Подкатегории рубрики включают рубрику; /filters reset всё сбрасывает."""
    controller = build_controller(store=FakeStore())

    update = FakeUpdate(callback="uf_sub_programming_0_0", with_message=False)
    await controller._handle_callback(update, ctx())
    prefs = controller.store.get_user_filters(ADMIN_ID)
    assert "programming" in prefs.categories
    assert prefs.subcategories["programming"]

    # Повторный тап по последней (единственной) подкатегории выключает рубрику.
    await controller._handle_callback(update, ctx())
    prefs = controller.store.get_user_filters(ADMIN_ID)
    assert "programming" not in prefs.categories
    assert prefs.subcategories.get("programming") is None

    # «Вся рубрика» включает рубрику целиком без ограничений.
    all_btn = FakeUpdate(callback="uf_all_programming", with_message=False)
    await controller._handle_callback(all_btn, ctx())
    prefs = controller.store.get_user_filters(ADMIN_ID)
    assert prefs.subcategories.get("programming") is None
    assert "programming" in prefs.categories

    # Выбранная целиком рубрика выключается кнопкой «Выключить рубрику».
    prefs = controller.store.get_user_filters(ADMIN_ID)
    sub_markup = controller._uf_sub_markup(prefs, "programming")
    assert sub_markup is not None
    first = sub_markup.inline_keyboard[0][0]
    assert first.text == "⬜ Выключить рубрику"
    assert first.callback_data == "uf_off_programming"

    off_btn = FakeUpdate(callback="uf_off_programming", with_message=False)
    await controller._handle_callback(off_btn, ctx())
    prefs = controller.store.get_user_filters(ADMIN_ID)
    assert "programming" not in prefs.categories
    assert prefs.subcategories.get("programming") is None

    reset = FakeUpdate(callback="uf_reset", with_message=False)
    await controller._handle_callback(reset, ctx())
    assert controller.store.get_user_filters(ADMIN_ID).categories == []


@pytest.mark.asyncio
async def test_filters_price_via_command_and_text_input() -> None:
    """Цена задаётся командой и вводом текста после кнопки «Мин. цена»."""
    controller = build_controller(store=FakeStore())
    update = FakeUpdate()

    await controller._cmd_filters(update, ctx("price", "5000"))
    assert controller.store.get_user_filters(ADMIN_ID).min_price_rub == 5000

    # Ввод текста: кнопка uf_price ставит ожидание, _handle_text_input пишет в базу.
    button = FakeUpdate(callback="uf_price", with_message=False)
    await controller._handle_callback(button, ctx())
    assert controller._awaiting_input == "user_filters.min_price_rub"

    typing = FakeUpdate()
    typing.message.text = "7000"
    await controller._handle_text_input(typing, ctx())
    assert controller.store.get_user_filters(ADMIN_ID).min_price_rub == 7000
    # _price_label форматирует цену с разделителем тысяч: «7 000 ₽».
    assert any("7 000" in text for text, _ in typing.message.replies)


@pytest.mark.asyncio
async def test_favorite_callback_toggles_and_updates_button() -> None:
    """fav добавляет заказ в избранное, unfav убирает; кнопка переворачивается."""
    controller = build_controller(store=FakeStore())
    card = FakeUpdate(
        callback="fav:fl_ru:1", with_message=False
    )
    # Имитируем клавиатуру карточки: кнопка ♥ в последнем ряду.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    card.callback_query.message.reply_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("♥ Понравилось", callback_data="fav:fl_ru:1")]]
    )

    await controller._handle_callback(card, ctx())

    assert controller.store.is_favorite(ADMIN_ID, "fl_ru:1") is True
    assert "избранное" in (card.callback_query.answers[-1][0] or "")
    # Кнопка перевернулась в «убрать».
    markup = card.callback_query.edits[-1][1]["reply_markup"]
    assert markup.inline_keyboard[0][0].text == "💔 Убрать"
    assert markup.inline_keyboard[0][0].callback_data == "unfav:fl_ru:1"

    back = FakeUpdate(callback="unfav:fl_ru:1", with_message=False)
    await controller._handle_callback(back, ctx())
    assert controller.store.is_favorite(ADMIN_ID, "fl_ru:1") is False


@pytest.mark.asyncio
async def test_favorites_command_lists_saved_jobs(tmp_path) -> None:
    """/favorites показывает избранные заказы; пусто — подсказка."""
    store = JobStore(tmp_path / "fav.db")
    try:
        controller = build_controller(store=store)
        scored = scored_job()
        store.save_scored(scored, eligible=True)
        store.add_favorite(ADMIN_ID, scored.job.uid)

        update = FakeUpdate()
        await controller._cmd_favorites(update, ctx())
        text = update.message.replies[0][0]
        assert "Избранные заказы" in text
        assert scored.job.title in text

        store.remove_favorite(ADMIN_ID, scored.job.uid)
        empty = FakeUpdate()
        await controller._cmd_favorites(empty, ctx())
        assert "В избранном пусто" in empty.message.replies[0][0]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_jobs_rejects_limits_outside_5_10_30(tmp_path) -> None:
    """/jobs принимает только 5, 10 или 30."""
    store = JobStore(tmp_path / "limits.db")
    try:
        controller = build_controller(store=store)
        update = FakeUpdate()

        await controller._cmd_jobs(update, ctx("20"))

        assert any("5, /jobs 10 или /jobs 30" in text for text, _ in update.message.replies)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_unknown_uf_callback_answers_alert() -> None:
    """Знакомый префикс uf_ с битым хвостом отвечает понятной ошибкой."""
    controller = build_controller(store=FakeStore())
    update = FakeUpdate(callback="uf_top_nonexistent", with_message=False)

    await controller._handle_callback(update, ctx())

    assert any("Рубрика не найдена" in (a[0] or "") for a in update.callback_query.answers)
