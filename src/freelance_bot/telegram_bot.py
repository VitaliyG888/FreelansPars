"""Telegram-бот для панели управления, уведомлений и одобрения сообщений.

Архитектура:
- Бот работает в отдельной задаче asyncio рядом с основным конвейером
- Каждый подписчик получает заказы по своим личным фильтрам: категории,
  подкатегории, биржи, минимальная цена (/filters)
- Порогов балла и совпадения навыков нет: показывается всё, что прошло
  фильтры; штраф красных флагов и свежесть задаются только администратором
  и влияют лишь на порядок (штрафные и старые заказы — последними)
- Пользователь запрашивает последние 5/10/30 заказов (/jobs) и помечает
  понравившиеся (/favorites, кнопка ♥ на карточке)

Команды пользователя:

/start          - приветствие; для новых пользователей - заявка на подписку
/menu           - список всех команд по группам
/help [команда] - справка по всем командам или подробно по одной
/status         - текущий статус конвейера (работает/остановлен, статистика)
/filters        - личные фильтры: категории, подкатегории, биржи, мин. цена
/jobs [5|10|30] - последние заказы по вашим фильтрам
/favorites      - избранные заказы (кнопка ♥ на карточке)
/stats          - статистика (собрано заказов, отправлено сообщений)

Команды администратора:

/platforms      - включить/выключить площадки
/categories     - общий фильтр категорий сбора
/interval       - интервал цикла (только админ, 900-3600 секунд)
/score          - штраф за красные флаги и свежесть (только админ)
/pause /resume  - приостановка и возобновление сбора
/llm /prompt /style /profile /skills - письма и профиль
/users /adduser /deluser - доступ к боту
/set /get /reload /reset /export - конфигурация
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape as html_escape
from html import unescape as html_unescape
from typing import TYPE_CHECKING, Any

from telegram import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import MAX_POLL_INTERVAL_SECONDS, MIN_POLL_INTERVAL_SECONDS
from .telegram_quiz import QuizCommandsMixin, QuizSession
from .telegram_settings import SELF_SERVICE_TOPICS, SettingsCommandsMixin, _parse_flag
from .telegram_user_filters import UserFiltersCommandsMixin

if TYPE_CHECKING:
    from .config import AppConfig, Secrets
    from .models import Job
    from .profile import FreelancerProfile
    from .settings_store import SettingsStore
    from .storage import JobStore

log = logging.getLogger(__name__)

# Telegram отклоняет сообщения длиннее 4096 символов. Оставляем запас для
# экранирование форматирования, добавляемое после сборки текста.
MAX_TELEGRAM_CHARS = 4000


def _md(value: object) -> str:
    """Экранирует динамический текст для legacy-режима Markdown в Telegram.

        Заголовки заказов, ТЗ и сгенерированные письма — свободный текст; непарный
        ``*``/``_``/``[`` заставит Telegram отклонить всё сообщение.
    """
    text = str(value)
    for char in ("\\", "_", "*", "[", "]", "`"):
        text = text.replace(char, "\\" + char)
    return text


def _format_budget(value: float | None) -> str | None:
    """Бюджет в рублях для карточки заказа; None, если сумма неизвестна."""
    if not value:
        return None
    return f"{value:,.0f} ₽".replace(",", " ")


_HTML_TAG_RE = re.compile(r"<[^>]*>")


def _plain_text(text: str) -> str:
    """Превращает HTML-текст в читаемый plain text.

        Убирает теги и раскодирует сущности. Нужен для запасной отправки без
        parse_mode: иначе при сбое разметки Telegram получил бы сообщение с
        видимыми ``<b>``/``<a href>`` вместо обычного текста.
    """
    return html_unescape(_HTML_TAG_RE.sub("", text))


def _chunk(text: str, limit: int = MAX_TELEGRAM_CHARS) -> list[str]:
    """Разбивает длинное сообщение по границе строки/пробела, не теряя текст."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunk = remaining[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip("\n ")
    if remaining:
        chunks.append(remaining)
    return chunks


@dataclass(frozen=True)
class CommandHelp:
    """Одна команда для /menu и /help."""

    name: str
    usage: str
    summary: str
    group: str
    examples: tuple[str, ...] = ()


# Единый источник для /menu и /help: новый обработчик достаточно добавить
# сюда, чтобы он появился в обеих командах.
COMMAND_HELP: tuple[CommandHelp, ...] = (
    CommandHelp(
        "start",
        "/start",
        "Приветствие; новый пользователь отправляет заявку на подписку.",
        "Основное",
    ),
    CommandHelp("menu", "/menu", "Список всех команд по группам.", "Основное"),
    CommandHelp(
        "help",
        "/help [команда]",
        "Справка по всем командам или подробно по одной.",
        "Основное",
        ("/help", "/help jobs"),
    ),
    CommandHelp(
        "users",
        "/users",
        "Кто имеет доступ к боту и какие заявки на подписку ожидают одобрения.",
        "Доступ",
    ),
    CommandHelp(
        "adduser",
        "/adduser ID",
        "Выдать доступ пользователю по его Telegram ID; переживает рестарт.",
        "Доступ",
        ("/adduser 123456789",),
    ),
    CommandHelp(
        "deluser",
        "/deluser ID",
        "Убрать добавленного пользователя; админы из .env убираются только правкой .env.",
        "Доступ",
        ("/deluser 123456789",),
    ),
    CommandHelp(
        "set_admin",
        "/set_admin ID",
        "Назначить администратора настроек: откроет разделы конвейера, писем и профиля.",
        "Доступ",
        ("/set_admin 123456789",),
    ),
    CommandHelp(
        "unset_admin",
        "/unset_admin ID",
        "Снять роль администратора настроек; доступ к заказам при этом сохраняется.",
        "Доступ",
        ("/unset_admin 123456789",),
    ),
    CommandHelp(
        "status",
        "/status",
        "Статус сбора и показатели последнего цикла.",
        "Мониторинг",
    ),
    CommandHelp(
        "jobs",
        "/jobs [5|10|30]",
        "Последние заказы по вашим фильтрам: свежие первыми, со штрафами — в конце.",
        "Мониторинг",
        ("/jobs", "/jobs 30"),
    ),
    CommandHelp(
        "favorites",
        "/favorites",
        "Избранные заказы, отмеченные кнопкой ♥ на карточке.",
        "Мониторинг",
        ("/favorites",),
    ),
    CommandHelp(
        "stats",
        "/stats",
        "Статистика за 7 дней: заказы, письма, отправки, отказы.",
        "Мониторинг",
    ),
    CommandHelp("logs", "/logs", "Последние ошибки конвейера.", "Мониторинг"),
    CommandHelp(
        "filters",
        "/filters",
        "Личные фильтры: рубрики и категории заказов, биржи, минимальная цена.",
        "Мои фильтры",
        (
            "/filters",
            "/filters price 5000",
            "/filters platforms",
            "/filters reset",
        ),
    ),
    CommandHelp(
        "platforms",
        "/platforms",
        "Список площадок и включение/выключение; добавление, удаление, смена URL.",
        "Администрирование",
        (
            "/platforms",
            "/platforms off avito_ru",
            "/platforms add kwork_ru",
            "/platforms url fl_ru https://www.fl.ru/projects/",
        ),
    ),
    CommandHelp(
        "categories",
        "/categories",
        "Общий фильтр сбора (админ): рубрики Kwork, категории внутри рубрики, "
        "минимальная цена. Личные фильтры пользователей — в /filters.",
        "Администрирование",
        (
            "/categories",
            "/categories off",
            "/categories add programming",
            "/categories price 5000",
            "/categories sub list",
        ),
    ),
    CommandHelp(
        "score",
        "/score",
        "Настройки приоритета (только админ): штраф за красные флаги и "
        "свежесть заказа. Заказы со штрафами и старые показываются последними.",
        "Администрирование",
        (
            "/score",
            "/score penalty 0.35",
            "/score freshness 18",
        ),
    ),
    CommandHelp(
        "interval",
        "/interval N",
        "Интервал между проходами в секундах (только админ), от 900 до 3600.",
        "Администрирование",
        ("/interval 900",),
    ),
    CommandHelp("pause", "/pause", "Приостановить сбор заказов.", "Администрирование"),
    CommandHelp("resume", "/resume", "Возобновить сбор заказов.", "Администрирование"),
    CommandHelp(
        "skills",
        "/skills",
        "Навыки профиля для писем: основные и дополнительные.",
        "Администрирование",
        (
            "/skills",
            "/skills add python,fastapi",
            "/skills rm python",
        ),
    ),
    CommandHelp(
        "send",
        "/send",
        "Режим отправки: dry-run, одобрение, лимиты, пауза, письма за цикл "
        "и ограничения площадок.",
        "Администрирование",
        (
            "/send",
            "/send dry on",
            "/send approval on",
            "/send limit 6",
            "/send delay 240",
            "/send drafts 20",
            "/send chars fl_ru 2000",
            "/send links fl_ru off",
        ),
    ),
    CommandHelp(
        "pricing",
        "/pricing [стратегия]",
        "Стратегия цены: minimum, markup или llm; без аргумента — кнопки.",
        "Администрирование",
        ("/pricing", "/pricing markup"),
    ),
    CommandHelp(
        "markup",
        "/markup N",
        "Коэффициент наценки: 1.15 или 15 (проценты).",
        "Администрирование",
        ("/markup 1.15", "/markup 15"),
    ),
    CommandHelp(
        "minprice",
        "/minprice N",
        "Минимальная приемлемая цена письма в рублях.",
        "Администрирование",
        ("/minprice 8000",),
    ),
    CommandHelp(
        "llm",
        "/llm",
        "LLM: включение, модель, endpoint, ключ, temperature, токены, "
        "timeout, fallback, проверка и кастомные провайдеры.",
        "Письма и профиль",
        (
            "/llm",
            "/llm model gpt-4o-mini",
            "/llm key <ключ>",
            "/llm temp 0.4",
            "/llm test",
            "/llm provider list",
            "/llm provider add local http://127.0.0.1:8080/v1 model",
        ),
    ),
    CommandHelp(
        "prompt",
        "/prompt",
        "Системный промпт и дополнительные инструкции; текст можно прислать "
        "следующим сообщением.",
        "Письма и профиль",
        (
            "/prompt",
            "/prompt system",
            "/prompt extra <текст>",
            "/prompt reset all",
        ),
    ),
    CommandHelp(
        "style",
        "/style",
        "Стиль письма: тон, длина, эмодзи, портфолио, уточняющие вопросы "
        "и запрещённые фразы.",
        "Письма и профиль",
        (
            "/style",
            "/style tone friendly",
            "/style length short",
            "/style emoji 0",
            "/style banned add <фраза>",
        ),
    ),
    CommandHelp(
        "profile",
        "/profile",
        "Имя, опыт, локация и саммари профиля.",
        "Письма и профиль",
        (
            "/profile",
            "/profile name Иван",
            "/profile years 5",
            "/profile location Москва",
            "/profile summary",
        ),
    ),
    CommandHelp(
        "settings",
        "/settings [all]",
        "Эффективные настройки; all — полный дамп в YAML.",
        "Настройки",
        ("/settings", "/settings all"),
    ),
    CommandHelp(
        "set",
        "/set <путь> <значение>",
        "Изменить любой параметр по пути (только админ).",
        "Настройки",
        (
            "/set runtime.dry_run false",
            "/set scoring.red_flag_penalty 0.3",
        ),
    ),
    CommandHelp(
        "get",
        "/get <путь>",
        "Показать значение параметра.",
        "Настройки",
        ("/get scoring.red_flag_penalty",),
    ),
    CommandHelp(
        "reload",
        "/reload",
        "Перечитать config.yaml и profile.yaml.",
        "Настройки",
    ),
    CommandHelp(
        "reset",
        "/reset <config|profile|all> confirm",
        "Сбросить оверлей настроек к значениям из файлов.",
        "Настройки",
        ("/reset config confirm", "/reset all confirm"),
    ),
    CommandHelp(
        "export",
        "/export",
        "Выгрузить эффективные настройки в YAML.",
        "Настройки",
    ),
    CommandHelp(
        "cancel",
        "/cancel",
        "Отменить ожидание многострочного ввода.",
        "Настройки",
    ),
)

COMMAND_HELP_BY_NAME = {item.name: item for item in COMMAND_HELP}

# Статусы подписки в таблице bot_subscribers. Требование — pending / active /
# rejected. Историческое значение 'approved' (старые базы) приводится к 'active'
# миграцией в storage.py, поэтому в коде сравнение идёт только с active.
SUB_PENDING = "pending"
SUB_ACTIVE = "active"
SUB_REJECTED = "rejected"
# Обратная совместимость для кода и тестов, которые ещё пишут «approved».
SUB_APPROVED = SUB_ACTIVE

# Тексты, которые получает пользователь по решению админа.
MSG_SUBSCRIBED = "Вы подписаны"
MSG_REJECTED = "Заявка отклонена"
MSG_PENDING = "Заявка на подписку ожидает решения администратора."

# Кнопки меню подписки.
SUBSCRIBE_LABEL = "📩 Подписаться"
SUBSCRIBE_CALLBACK = "sub:request"
SUBSCRIBE_HINT = "Если хотите попробовать снова — нажмите «📩 Подписаться» под сообщением."

# Кнопки под полем ввода. Метка кнопки совпадает с командой, поэтому нажатие
# отправляет обычное сообщение «/menu» или «/help» и не требует отдельного
# обработчика текста. Клавиатура постоянная — остаётся видимой после ответа.
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["/menu", "/help"]],
    resize_keyboard=True,
    is_persistent=True,
)


def _grouped_help() -> dict[str, list[CommandHelp]]:
    """Группирует команды в порядке объявления."""
    groups: dict[str, list[CommandHelp]] = {}
    for item in COMMAND_HELP:
        groups.setdefault(item.group, []).append(item)
    return groups


@dataclass
class TelegramNotification:
    """Уведомление о заказе, отправляемое в Telegram."""

    job: Job
    score_total: float
    score_summary: str
    message_text: str | None
    approved: bool | None  # None = ожидает, True = одобрено, False = отклонено
    message_id: int | None = None


class TelegramBotController(
    QuizCommandsMixin, UserFiltersCommandsMixin, SettingsCommandsMixin
):
    """Telegram-бот для управления во время работы и уведомлений."""

    def __init__(
        self,
        token: str,
        admin_chat_ids: list[int],
        config: AppConfig,
        profile: FreelancerProfile | None = None,
        store: JobStore | None = None,
        secrets: Secrets | None = None,
        settings: SettingsStore | None = None,
        profile_path: str = "profile.yaml",
    ) -> None:
        self.token = token
        self.admin_chat_ids = set(admin_chat_ids)
        # Админы из .env постоянны: через /adduser//deluser меняются только
        # те, кто добавлен в базу; после рестарта список восстанавливается.
        self.env_admin_ids = set(admin_chat_ids)
        if store is not None:
            self.admin_chat_ids |= store.list_bot_admins()
        # Роль «администратор настроек»: .env-админы плюс назначенные /set_admin.
        # Остальные допущенные видят только заказы и личные фильтры.
        self.super_admin_ids: set[int] = set(self.env_admin_ids)
        if store is not None:
            self.super_admin_ids |= store.list_super_admins()
        self.config = config
        self.profile = profile
        self.store = store
        self.secrets = secrets
        self.settings = settings
        self.profile_path = profile_path
        self.app: Application | None = None
        self.is_running = False
        self.pipeline_paused = False
        self.pending_approvals: dict[str, TelegramNotification] = {}
        self.stats: dict[str, int] = defaultdict(int)
        self.last_errors: list[tuple[datetime, str]] = []
        self.interval_seconds = config.runtime.poll_interval_seconds
        # Ожидание многострочного ввода (/prompt system, /profile summary).
        self._awaiting_input: str | None = None
        self._awaiting_label: str = ""
        # Сессии AI-квиза: короткий диалог в памяти, черновик — в SQLite.
        self._quiz_sessions: dict[str, QuizSession] = {}

    async def start(self) -> None:
        """Запускает Telegram-бота."""
        # Telegram бывает медленным в некоторых сетях; стандартные 5 с в PTB —
        # настолько малым, что первый getMe на холодном соединении сорвётся.
        self.app = (
            Application.builder()
            .token(self.token)
            .connect_timeout(30)
            .read_timeout(30)
            .write_timeout(30)
            .pool_timeout(10)
            .build()
        )

        # Регистрируем обработчики команд
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("menu", self._cmd_menu))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("settings", self._cmd_settings))
        self.app.add_handler(CommandHandler("platforms", self._cmd_platforms))
        self.app.add_handler(CommandHandler("categories", self._cmd_categories))
        self.app.add_handler(CommandHandler("filters", self._cmd_filters))
        self.app.add_handler(CommandHandler("interval", self._cmd_interval))
        self.app.add_handler(CommandHandler("pause", self._cmd_pause))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CommandHandler("stats", self._cmd_stats))
        self.app.add_handler(CommandHandler("jobs", self._cmd_jobs))
        self.app.add_handler(CommandHandler("favorites", self._cmd_favorites))
        self.app.add_handler(CommandHandler("logs", self._cmd_logs))
        self.app.add_handler(CommandHandler("pricing", self._cmd_pricing))
        self.app.add_handler(CommandHandler("markup", self._cmd_markup))
        self.app.add_handler(CommandHandler("minprice", self._cmd_minprice))
        self.app.add_handler(CommandHandler("profile", self._cmd_profile))
        self.app.add_handler(CommandHandler("send", self._cmd_send))
        self.app.add_handler(CommandHandler("score", self._cmd_score))
        self.app.add_handler(CommandHandler("llm", self._cmd_llm))
        self.app.add_handler(CommandHandler("prompt", self._cmd_prompt))
        self.app.add_handler(CommandHandler("style", self._cmd_style))
        self.app.add_handler(CommandHandler("skills", self._cmd_skills))
        self.app.add_handler(CommandHandler("set", self._cmd_set))
        self.app.add_handler(CommandHandler("get", self._cmd_get))
        self.app.add_handler(CommandHandler("reload", self._cmd_reload))
        self.app.add_handler(CommandHandler("reset", self._cmd_reset))
        self.app.add_handler(CommandHandler("export", self._cmd_export))
        self.app.add_handler(CommandHandler("cancel", self._cmd_cancel))
        self.app.add_handler(CommandHandler("users", self._cmd_users))
        self.app.add_handler(CommandHandler("adduser", self._cmd_adduser))
        self.app.add_handler(CommandHandler("deluser", self._cmd_deluser))
        self.app.add_handler(CommandHandler("set_admin", self._cmd_set_admin))
        self.app.add_handler(CommandHandler("unset_admin", self._cmd_unset_admin))

        # Обработчик callback-запросов от inline-кнопок
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))
        # Свободный текст принимается только в режиме ожидания ввода.
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text_input)
        )

        await self.app.initialize()
        await self._register_bot_commands()
        await self.app.start()
        if self.app.updater:
            await self.app.updater.start_polling(drop_pending_updates=True)
        self.is_running = True
        log.info("Telegram bot started, polling for updates")

    async def stop(self) -> None:
        """Останавливает Telegram-бота."""
        if self.app:
            if self.app.updater:
                await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        self.is_running = False
        log.info("Telegram bot stopped")

    async def _register_bot_commands(self) -> None:
        """Публикует список команд в меню Telegram (кнопка «/» и «Меню»).

        Ошибка регистрации не должна мешать запуску бота: сама панель и
        кнопки /menu, /help работают и без неё.
        """
        if not self.app:
            return
        try:
            await self.app.bot.set_my_commands(
                [BotCommand(item.name, item.summary[:240]) for item in COMMAND_HELP]
            )
        except Exception as exc:  # noqa: BLE001 - косметика, не повод падать
            log.warning("Failed to register Telegram command menu: %s", exc)

    def _is_admin(self, chat_id: int) -> bool:
        """Проверяет, разрешено ли пользователю или чату управлять ботом."""
        return chat_id in self.admin_chat_ids

    def _is_super_admin(self, chat_id: int | None) -> bool:
        """Администратор настроек: из .env или назначенный через /set_admin.

        Подписчики, добавленные через заявку или /adduser, имеют доступ к
        панели и личным фильтрам, но не к настройкам конвейера.
        """
        return chat_id is not None and chat_id in self.super_admin_ids

    def _is_admin_update(self, update: Update) -> bool:
        """True, если отправитель или чат в allow-list админов."""
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        return (user_id is not None and self._is_admin(user_id)) or (
            chat_id is not None and self._is_admin(chat_id)
        )

    def _update_ids(self, update: Update) -> tuple[int | None, int | None]:
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        return user_id, chat_id

    def _is_subscribed_update(self, update: Update) -> bool:
        """True, если у отправителя активная подписка (или он админ).

        Проверяются оба ID: в личке они совпадают, в группе подписка
        привязана к чату, поэтому решает тот, у кого статус активный.
        """
        user_id, chat_id = self._update_ids(update)
        for candidate in (user_id, chat_id):
            if candidate is not None and self.subscription_status(candidate) == SUB_ACTIVE:
                return True
        return False

    def _super_admin_id(self, update: Update) -> int | None:
        """ID отправителя, если это администратор из .env, иначе None."""
        user_id = update.effective_user.id if update.effective_user else None
        if user_id is not None and self._is_super_admin(user_id):
            return user_id
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is not None and self._is_super_admin(chat_id):
            return chat_id
        return None

    def _is_super_admin_update(self, update: Update) -> bool:
        return self._super_admin_id(update) is not None

    async def _ensure_super_admin(self, update: Update) -> bool:
        """Возвращает True только для администратора настроек, иначе отвечает."""
        if self._is_super_admin_update(update):
            return True
        # Отказ тоже пишем в журнал: владельцу полезно видеть, кто пытался
        # открыть админский раздел.
        log.warning(
            "Недостаточно прав (админский раздел): user_id=%s chat_id=%s",
            update.effective_user.id if update.effective_user else None,
            update.effective_chat.id if update.effective_chat else None,
        )
        await self._deny_insufficient_rights(update)
        return False

    async def _deny_insufficient_rights(self, update: Update) -> None:
        """Единый ответ на попытку открыть раздел не своего уровня."""
        text = (
            "⛔ Недостаточно прав.\n\n"
            "Этот раздел доступен только администратору бота. "
            "Вам открыты заказы и личные фильтры: /jobs, /favorites, /filters."
        )
        if update.message:
            await update.message.reply_text(text)
            return
        query = update.callback_query
        if query is not None:
            await query.answer("⛔ Недостаточно прав.", show_alert=True)

    def audience_chat_ids(self) -> set[int]:
        """Все чаты, которые могут получать заказы: активные подписки.

        Админы из .env входят всегда. Подписчики попадают сюда только при
        статусе active: пока заявка pending или отклонена, карточки не идут.
        """
        chat_ids = set(self.admin_chat_ids)
        if self.store is not None:
            for row in self.store.list_subscribers(SUB_ACTIVE):
                chat_ids.add(int(row["chat_id"]))
            # Совместимость со старыми базами до миграции статусов.
            for row in self.store.list_subscribers("approved"):
                chat_ids.add(int(row["chat_id"]))
        return chat_ids

    async def _ensure_admin(self, update: Update) -> bool:
        """Пускает только при активной подписке, иначе объясняет как её получить.

        Это единственный gate: команды, callback'и и карточки проверяют одно и
        то же условие. Пользователь без записи в bot_subscribers и без
        одобренной заявки видит кнопку «📩 Подписаться».
        """
        if self._is_admin_update(update):
            return True
        # ID отказника пишем в журнал и показываем ему самому: заявка на
        # подписку отправляется через /start без @userinfobot.
        user_id, chat_id = self._update_ids(update)
        log.warning("Доступ запрещён: user_id=%s chat_id=%s", user_id, chat_id)
        own = user_id if user_id is not None else chat_id
        status = self.subscription_status(own) if own is not None else None
        if status == SUB_PENDING:
            text = (
                "⏳ Ваша заявка на рассмотрении.\n"
                f"Ваш ID: {own}. Доступ откроется после одобрения администратором."
            )
        elif status == SUB_REJECTED:
            text = (
                "❌ Заявка отклонена, доступа нет.\n"
                "Если это ошибка — отправьте заявку снова кнопкой «📩 Подписаться»."
            )
        else:
            hint = (
                f"Ваш ID: {own}. Нажмите «📩 Подписаться», чтобы отправить заявку."
                if own is not None
                else ""
            )
            text = "⛔ Подписка не активна." + (f"\n{hint}" if hint else "")
        if update.message:
            await update.message.reply_text(
                text, reply_markup=self._subscribe_keyboard()
            )
        else:
            query = update.callback_query
            if query is not None:
                await query.answer(text, show_alert=True)
        return False

    @staticmethod
    def _parse_error(exc: Exception) -> bool:
        """True, если Telegram не смог разобрать разметку сообщения.

        Тексты карточек приходят от LLM и могут содержать несбалансированные
        ``*``/``_``: Telegram отклоняет всё сообщение («Can't parse entities»).
        Такое лечится только повторной отправкой без разметки.
        """
        return isinstance(exc, BadRequest) and "parse entities" in str(exc).lower()

    async def _reply(
        self,
        update: Update,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
    ) -> None:
        """Отвечает с автоматическим разбиением; клавиатура крепится к последнему фрагменту.
        """
        if not update.message:
            return
        parts = _chunk(text)
        for index, part in enumerate(parts):
            markup = reply_markup if index == len(parts) - 1 else None
            try:
                await update.message.reply_text(part, parse_mode=parse_mode, reply_markup=markup)
            except BadRequest as e:
                if not self._parse_error(e):
                    raise
                # Несбалансированная разметка не должна терять ответ целиком:
                # повторяем текстом без тегов, чтобы Telegram не показал их
                # как обычный текст.
                log.warning("Разметка ответа некорректна, повтор без parse_mode: %s", e)
                await update.message.reply_text(_plain_text(part), reply_markup=markup)

    async def _send_to_admins(
        self, text: str, *, reply_markup: InlineKeyboardMarkup | None = None
    ) -> bool:
        """Отправляет сообщение (при необходимости разбитое) всем настроенным админам.

        Возвращает True, если хотя бы один чат принял сообщение.
        """
        if not self.app or not self.is_running:
            return False
        delivered = False
        parts = _chunk(text)
        for chat_id in sorted(self.admin_chat_ids):
            for index, part in enumerate(parts):
                try:
                    await self._bot_send(
                        chat_id=chat_id,
                        text=part,
                        parse_mode="Markdown",
                        reply_markup=reply_markup if index == len(parts) - 1 else None,
                        disable_web_page_preview=True,
                    )
                    delivered = True
                except Exception as e:  # noqa: BLE001 - one bad chat must not stop the rest
                    if not self._parse_error(e):
                        log.error("Failed to send Telegram message to %s: %s", chat_id, e)
                        continue
                    # Карточка заказа важнее форматирования: повторяем текстом
                    # без тегов, иначе заказ вообще не дойдёт до Telegram,
                    # а с сырым HTML — придёт с видимыми тегами.
                    log.warning("Разметка карточки некорректна, повтор без parse_mode: %s", e)
                    try:
                        await self._bot_send(
                            chat_id=chat_id,
                            text=_plain_text(part),
                            reply_markup=reply_markup if index == len(parts) - 1 else None,
                            disable_web_page_preview=True,
                        )
                        delivered = True
                    except Exception as e2:  # noqa: BLE001
                        log.error("Failed to send Telegram message to %s: %s", chat_id, e2)
        return delivered

    async def _send_to_super_admins(
        self, text: str, *, reply_markup: InlineKeyboardMarkup | None = None
    ) -> bool:
        """Отправляет сообщение только администраторам настроек.

        Нужно для заявок на подписку: решать, кого пускать, должен админ, а
        не любой подписчик — иначе персональные данные заявителя расходятся
        по всем чатам.
        """
        if not self.app or not self.is_running:
            return False
        delivered = False
        parts = _chunk(text)
        for chat_id in sorted(self.super_admin_ids):
            for index, part in enumerate(parts):
                try:
                    await self._bot_send(
                        chat_id=chat_id,
                        text=part,
                        parse_mode="Markdown",
                        reply_markup=reply_markup if index == len(parts) - 1 else None,
                        disable_web_page_preview=True,
                    )
                    delivered = True
                except Exception as e:  # noqa: BLE001 - один чат не должен ломать остальные
                    if not self._parse_error(e):
                        log.error("Failed to send Telegram message to %s: %s", chat_id, e)
                        continue
                    try:
                        await self._bot_send(
                            chat_id=chat_id,
                            text=_plain_text(part),
                            reply_markup=reply_markup if index == len(parts) - 1 else None,
                            disable_web_page_preview=True,
                        )
                        delivered = True
                    except Exception as e2:  # noqa: BLE001
                        log.error("Failed to send Telegram message to %s: %s", chat_id, e2)
        return delivered

    async def _bot_send(self, **kwargs: Any) -> None:
        """Отправка с ретраями на rate limit (экспоненциальная задержка)."""
        if not self.app:
            raise RuntimeError("Telegram application is not initialized")
        retries = 3
        for attempt in range(retries):
            try:
                await self.app.bot.send_message(**kwargs)
                return
            except RetryAfter as e:
                if attempt == retries - 1:
                    raise
                # retry_after может быть int (секунды) или timedelta (PTB >= 20).
                retry_after = e.retry_after
                if isinstance(retry_after, timedelta):
                    wait = retry_after.total_seconds()
                else:
                    wait = float(retry_after or 0)
                delay = max(1.0, 2.0**attempt) + wait
                log.warning("Telegram rate limit, retrying in %.1fs", delay)
                await asyncio.sleep(delay)

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду /start по статусу подписки.

        active (и админы) — приветствие с тем набором команд, который им
        доступен; нет записи — заявка уходит админам с кнопками решения;
        pending — напоминание, что заявка на рассмотрении; rejected —
        сообщение об отказе с кнопкой «📩 Подписаться» для повторной попытки.
        """
        user_id, chat_id = self._update_ids(update)
        owner = chat_id if chat_id is not None else user_id
        if owner is not None and self.subscription_status(owner) == SUB_ACTIVE:
            await self._send_welcome(update)
            return
        await self._handle_subscription_request(update)

    async def _send_welcome(self, update: Update) -> None:
        """Приветствие панели управления для пользователя с доступом."""
        is_admin = self._is_super_admin_update(update)
        parts = [
            "🤖 *Freelance Bot*",
            "",
            "Заказы приходят по вашим личным фильтрам. Порогов балла нет: "
            "показывается всё, что прошло фильтр.",
            "",
            "*Мои заказы:*",
            "/jobs — последние 5/10/30 заказов по вашим фильтрам",
            "/favorites — избранные заказы (♥ на карточке)",
            "/status — статус и последний цикл",
            "/stats — статистика за 7 дней",
            "",
            "*Мои фильтры:*",
            "/filters — категории, подкатегории, биржи, мин. цена",
            "",
            "*Настройки:*",
            "/settings — разделы настроек по вашим правам",
            "/menu — все команды, /help — подробная справка",
        ]
        if is_admin:
            parts += [
                "",
                "*Администрирование:*",
                "/platforms — платформы: список, добавление, удаление",
                "/categories — общий фильтр сбора",
                "/score — штраф за красные флаги и свежесть",
                "/interval — интервал циклов (900-3600 с)",
                "/users, /adduser, /deluser, /set_admin — доступ",
                "",
                "*Письма и профиль:*",
                "/llm — модель, endpoint, ключ, temperature, провайдеры",
                "/prompt — системный промпт и доп. инструкции",
                "/style — тон, длина, эмодзи, запрещённые фразы",
                "/profile — имя, опыт, локация, саммари",
                "/send, /pricing, /markup, /minprice — отправка и цена",
                "",
                "*Служебное:*",
                "/set, /get — изменить/посмотреть параметр",
                "/pause, /resume — пауза сбора",
                "/reload — перечитать файлы, /reset — сбросить оверлей",
                "/export — выгрузить эффективные настройки",
            ]
        parts += ["", "Кнопки /menu и /help всегда доступны под полем ввода."]
        text = "\n".join(parts)
        await self._reply(
            update, text, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD
        )
        await self._reply(
            update, text, parse_mode="Markdown", reply_markup=MAIN_KEYBOARD
        )

    # ------------------------------------------------------------------ подписка
    def subscription_status(self, chat_id: int) -> str | None:
        """Статус подписки чата: pending/active/rejected или None.

        Админы из .env считаются активными всегда — их доступ не зависит от
        заявки. Это единственное место, где решается вопрос «пускать или нет»,
        поэтому gate одинаков для команд, callback'ов и карточек.
        """
        if self._is_admin(chat_id):
            return SUB_ACTIVE
        if self.store is None:
            return None
        row = self.store.get_subscriber(chat_id)
        if row is None:
            return None
        status = str(row.get("status") or "")
        # 'approved' — историческое имя активной подписки в старых базах.
        if status in {SUB_ACTIVE, "approved"}:
            return SUB_ACTIVE
        if status == SUB_REJECTED:
            return SUB_REJECTED
        return SUB_PENDING

    async def _handle_subscription_request(self, update: Update) -> None:
        """Проводит нового пользователя через заявку на подписку.

        Первый /start создаёт заявку и уведомляет админов; повторные показывают
        её статус. Доступ выдаётся только после одобрения кнопкой админа.
        """
        if not update.message:
            return
        user = update.effective_user
        chat = update.effective_chat
        # Заявка привязывается к чату: в личке это ID пользователя, в группе —
        # ID группы (соответствует семантике /adduser и гарантирует доставку).
        chat_id = chat.id if chat is not None else (user.id if user is not None else None)
        if chat_id is None:
            return

        if self.store is None:
            # Без базы хранить заявку негде — остаётся прежнее поведение.
            await self._ensure_admin(update)
            return

        username = getattr(user, "username", None) if user is not None else None
        first = getattr(user, "first_name", "") or "" if user is not None else ""
        last = getattr(user, "last_name", "") or "" if user is not None else ""
        full_name = " ".join(part for part in (first, last) if part) or None

        status = self.subscription_status(chat_id)
        if status is None:
            self.store.add_subscriber_request(chat_id, username, full_name)
            await self._notify_subscription_request(chat_id, username, full_name)
            log.info("subscription requested: chat_id=%s username=%s", chat_id, username)
            await self._reply(
                update,
                "📨 Заявка на подписку отправлена администратору.\n"
                f"Ваш ID: {chat_id}. После одобрения вы получите доступ к боту.",
                reply_markup=self._subscribe_keyboard(),
            )
            return

        if status == SUB_PENDING:
            await self._reply(
                update,
                "⏳ Ваша заявка уже отправлена и ожидает одобрения администратора.\n"
                f"Ваш ID: {chat_id}.",
                reply_markup=self._subscribe_keyboard(),
            )
        elif status == SUB_ACTIVE:
            # Самовосстановление: подписка активна, но записи в bot_admins нет
            # (например, база пересоздана). Выдаём доступ снова.
            if self.store.add_bot_admin(chat_id, added_by=None):
                self.admin_chat_ids.add(chat_id)
            await self._send_welcome(update)
        else:
            await self._reply(
                update,
                "❌ Ваша заявка была отклонена.\n"
                "Если это ошибка — нажмите «📩 Подписаться», чтобы отправить заявку снова.",
                reply_markup=self._subscribe_keyboard(),
            )

    def _subscribe_keyboard(self) -> InlineKeyboardMarkup:
        """Кнопка «📩 Подписаться» для тех, у кого ещё нет активной подписки."""
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(SUBSCRIBE_LABEL, callback_data=SUBSCRIBE_CALLBACK)]]
        )

    async def _handle_subscribe_callback(self, update: Update, query: CallbackQuery) -> None:
        """Обрабатывает кнопку «📩 Подписаться».

        Повторная заявка после отказа допустима: админ мог отклонить случайно,
        а человек — исправить ситуацию. Пока заявка на рассмотрении, повторное
        нажатие не создаёт дубликатов и не дёргает админов.
        """
        chat = update.effective_chat
        user = update.effective_user
        chat_id = chat.id if chat is not None else (user.id if user is not None else None)
        if chat_id is None:
            await query.answer("⚠️ Не удалось определить чат.", show_alert=True)
            return
        if self.store is None:
            await query.answer("🗄️ База недоступна.", show_alert=True)
            return

        status = self.subscription_status(chat_id)
        if status == SUB_ACTIVE:
            await query.answer("✅ У вас уже есть активная подписка.")
            return
        if status == SUB_PENDING:
            await query.answer("⏳ Заявка уже на рассмотрении.", show_alert=True)
            return

        username = getattr(user, "username", None) if user is not None else None
        first = getattr(user, "first_name", "") or "" if user is not None else ""
        last = getattr(user, "last_name", "") or "" if user is not None else ""
        full_name = " ".join(part for part in (first, last) if part) or None

        # После отказа запись уже есть: возвращаем её в pending, чтобы админ
        # увидел повторную заявку, а история решения не потерялась.
        self.store.upsert_subscriber_status(chat_id, SUB_PENDING)
        self.store.add_subscriber_request(chat_id, username, full_name)
        await self._notify_subscription_request(chat_id, username, full_name)
        log.info("subscription re-requested: chat_id=%s", chat_id)
        await query.answer("📨 Заявка отправлена администратору.")
        try:
            await query.edit_message_text(
                "📨 Заявка на подписку отправлена администратору.\n"
                f"Ваш ID: {chat_id}. После одобрения вы получите доступ к боту."
            )
        except TelegramError as exc:  # pragma: no cover - сообщение могло устареть
            log.debug("cannot edit subscribe message: %s", exc)

    async def _notify_subscription_request(
        self, user_id: int, username: str | None, full_name: str | None
    ) -> None:
        """Отправляет админам карточку заявки с кнопками одобрения/отклонения."""
        name = _md(full_name or "—")
        handle = f"@{_md(username)}" if username else "username скрыт"
        text = (
            "📨 *Новая заявка на подписку*\n\n"
            f"Пользователь: {name}\n"
            f"Username: {handle}\n"
            f"ID: `{user_id}`"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Одобрить", callback_data=f"sub_approve:{user_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ Отклонить", callback_data=f"sub_reject:{user_id}"
                    ),
                ]
            ]
        )
        await self._send_to_super_admins(text, reply_markup=keyboard)

    async def _notify_user(self, chat_id: int, text: str) -> None:
        """Прямое сообщение пользователю с обработкой ошибок доставки."""
        if not self.app or not self.is_running:
            return
        try:
            await self._bot_send(chat_id=chat_id, text=text)
        except Exception as e:  # noqa: BLE001 - недоступный чат не ломает обработчик
            log.error("Failed to deliver message to %s: %s", chat_id, e)

    async def _cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает /menu: компактный список команд по группам."""
        if not await self._ensure_admin(update):
            return

        if not update.message:
            return

        lines = ["🤖 <b>Команды бота</b>", ""]
        for group, items in _grouped_help().items():
            lines.append(f"<b>{html_escape(group)}</b>")
            lines.append(
                " · ".join(
                    f"<code>{html_escape(item.usage.split()[0])}</code>" for item in items
                )
            )
            lines.append("")
        lines.append(
            "Подробная справка: <code>/help</code> или "
            "<code>/help команда</code>, например <code>/help jobs</code>."
        )
        await self._reply(
            update, "\n".join(lines), parse_mode="HTML", reply_markup=MAIN_KEYBOARD
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает /help [команда]: справка по всем или одной команде."""
        if not await self._ensure_admin(update):
            return

        if not update.message:
            return

        args = context.args or []
        if args:
            name = args[0].strip().lower().lstrip("/").split("@", 1)[0]
            item = COMMAND_HELP_BY_NAME.get(name)
            if item is None:
                await update.message.reply_text(
                    f"❓ Команда <code>{html_escape(name or args[0])}</code> не найдена. "
                    "Список команд: /menu",
                    parse_mode="HTML",
                    reply_markup=MAIN_KEYBOARD,
                )
                return

            lines = [
                f"📖 <b>{html_escape(item.usage)}</b>",
                "",
                html_escape(item.summary),
            ]
            if item.examples:
                lines += ["", "<b>Примеры</b>"]
                lines += [f"<code>{html_escape(example)}</code>" for example in item.examples]
            lines += ["", f"<b>Группа:</b> {html_escape(item.group)}"]
            await self._reply(
                update, "\n".join(lines), parse_mode="HTML", reply_markup=MAIN_KEYBOARD
            )
            return

        lines = ["📖 <b>Справка по командам</b>", ""]
        for group, items in _grouped_help().items():
            lines.append(f"<b>{html_escape(group)}</b>")
            for item in items:
                lines.append(
                    f"<code>{html_escape(item.usage)}</code> — {html_escape(item.summary)}"
                )
            lines.append("")
        lines.append(
            "Подробно по одной команде: <code>/help команда</code>, например "
            "<code>/help jobs</code>."
        )
        await self._reply(
            update, "\n".join(lines), parse_mode="HTML", reply_markup=MAIN_KEYBOARD
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду /status."""
        if not await self._ensure_admin(update):
            return

        if not update.message:
            return

        status_icon = "✅" if not self.pipeline_paused else "⏸️"
        status_text = "запущен" if not self.pipeline_paused else "приостановлен"

        text = (
            f"{status_icon} *Статус:* {status_text}\n"
            f"📊 *Последний цикл:*\n"
            f"  • Собрано заказов: {self.stats.get('jobs_collected', 0)}\n"
            f"  • Новых: {self.stats.get('jobs_new', 0)}\n"
            f"  • Подходящих: {self.stats.get('jobs_eligible', 0)}\n"
            f"  • Сообщений создано: {self.stats.get('messages_generated', 0)}\n"
            f"  • Ожидают отправки: {len(self.pending_approvals)}\n"
        )
        await self._reply(update, text, parse_mode="Markdown")

    async def _cmd_interval(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду /interval — доступна только администратору.

        Границы фиксированы: 900..3600 секунд (15-60 минут). Частые проходы
        вредят площадкам (антибот-защита), редкие — задерживают доставку.
        """
        if not await self._ensure_super_admin(update):
            return

        if not update.message:
            return

        if not context.args:
            await update.message.reply_text(
                "⏱️ Укажите интервал в секундах.\n\n"
                f"Пример: `/interval {MIN_POLL_INTERVAL_SECONDS}` (15 минут)\n"
                f"Допустимо: {MIN_POLL_INTERVAL_SECONDS}-{MAX_POLL_INTERVAL_SECONDS} секунд "
                "(настройка администратора).",
                parse_mode="Markdown",
            )
            return

        try:
            interval = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Неверный формат. Укажите число секунд.")
            return
        if interval < MIN_POLL_INTERVAL_SECONDS:
            await update.message.reply_text(
                f"⚠️ Минимальный интервал: {MIN_POLL_INTERVAL_SECONDS} секунд."
            )
            return
        if interval > MAX_POLL_INTERVAL_SECONDS:
            await update.message.reply_text(
                f"⚠️ Максимальный интервал: {MAX_POLL_INTERVAL_SECONDS} секунд (1 час)."
            )
            return

        self.interval_seconds = interval
        self.config.runtime.poll_interval_seconds = interval
        await update.message.reply_text(
            f"✅ Интервал изменён на {interval} секунд ({interval // 60} мин)."
            + self._persist_note()
        )

    async def _cmd_pricing(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает /pricing — выбор стратегии цены."""
        if not await self._ensure_admin(update):
            return

        if not update.message:
            return

        profile = self._profile_model()
        pricing = profile.pricing

        # Без аргументов — показать клавиатуру; с аргументами — изменить поле.
        if context.args:
            action = context.args[0].lower()
            values = context.args[1:]
            if action in {"rate", "hour"} and values:
                try:
                    value = float(values[0].replace(",", "."))
                except ValueError:
                    await update.message.reply_text(
                        "❌ Нужно число, например `/pricing rate 2500`."
                    )
                    return
                if value <= 0:
                    await update.message.reply_text("⚠️ Ставка должна быть больше нуля.")
                    return
                pricing.hourly_rate_rub = value
                await update.message.reply_text(
                    f"✅ Ставка: {value:,.0f} ₽/час.".replace(",", " ") + self._persist_note(),
                    parse_mode="Markdown",
                )
                return
            if action in {"price", "include"} and values:
                flag = _parse_flag(values[0])
                if flag is None:
                    await update.message.reply_text("❌ Ожидается on/off.")
                    return
                pricing.include_price_in_message = flag
                await update.message.reply_text(
                    f"✅ Цена в письме: {flag}." + self._persist_note()
                )
                return

        labels = {
            "minimum": "Минимум (бюджет заказчика −5%, не ниже порога)",
            "markup": "Наценка (бюджет заказчика × коэффициент)",
            "llm_estimate": "Оценка LLM (часы × ставка)",
        }
        current = pricing.strategy
        keyboard = [
            [
                InlineKeyboardButton(
                    ("✅ " if current == key else "") + label,
                    callback_data=f"pricing_strategy_{key}",
                )
            ]
            for key, label in labels.items()
        ]
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"Порог: {pricing.min_acceptable_rub:,.0f} ₽".replace(",", " "),
                    callback_data="pricing_min_ask",
                )
            ]
        )

        text = (
            "💰 *Стратегия ценообразования*\n\n"
            f"Текущая: *{labels.get(current, current)}*\n\n"
            f"Коэффициент наценки: {pricing.markup_percent}\n"
            f"Ставка: {pricing.hourly_rate_rub:,.0f} ₽/час\n".replace(",", " ")
            + f"Минимальная цена: {pricing.min_acceptable_rub:,.0f} ₽\n".replace(",", " ")
            + f"Цена в письме: {'да' if pricing.include_price_in_message else 'нет'}\n\n"
            + "Выберите стратегию:"
        )
        await self._reply(
            update,
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _cmd_markup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает /markup N — задаёт множитель наценки."""
        if not await self._ensure_admin(update):
            return

        if not update.message:
            return

        from .profile import load_profile

        profile = self.profile or load_profile()
        self.profile = profile

        if not context.args:
            await update.message.reply_text(
                f"Текущий коэффициент: *{profile.pricing.markup_percent}*\n\n"
                "Формат: `/markup 1.15` (бюджет × 1.15)\n"
                "или `/markup 15` (то же, что 1.15)",
                parse_mode="Markdown",
            )
            return

        try:
            raw = float(context.args[0].replace(",", "."))
        except ValueError:
            await update.message.reply_text("❌ Нужно число. Пример: `/markup 1.15`")
            return

        # Принимаем и множитель (1.15), и процент (15 -> 1.15).
        multiplier = 1 + raw / 100 if raw > 10 else raw
        if multiplier <= 0 or multiplier > 5:
            await update.message.reply_text("⚠️ Коэффициент должен быть от 0.01 до 5.")
            return

        profile.pricing.markup_percent = multiplier
        await update.message.reply_text(
            f"✅ Коэффициент наценки: *{multiplier:g}* "
            f"(бюджет {profile.pricing.markup_percent * 100:.0f}%)"
            + self._persist_note(),
            parse_mode="Markdown",
        )

    async def _cmd_minprice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает /minprice N — задаёт минимальную допустимую цену."""
        if not await self._ensure_admin(update):
            return

        if not update.message:
            return

        from .profile import load_profile

        profile = self.profile or load_profile()
        self.profile = profile

        if not context.args:
            await update.message.reply_text(
                f"Текущий минимум: *{profile.pricing.min_acceptable_rub:,.0f} ₽*\n\n"
                "Формат: `/minprice 8000`".replace(",", " "),
                parse_mode="Markdown",
            )
            return

        try:
            value = float(context.args[0].replace(" ", "").replace(",", ""))
        except ValueError:
            await update.message.reply_text("❌ Нужно число. Пример: `/minprice 8000`")
            return

        if value < 0:
            await update.message.reply_text("⚠️ Минимум не может быть отрицательным.")
            return

        profile.pricing.min_acceptable_rub = value
        await update.message.reply_text(
            f"✅ Минимальная цена: *{value:,.0f} ₽*".replace(",", " ") + self._persist_note(),
            parse_mode="Markdown",
        )

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду /pause."""
        if not await self._ensure_admin(update):
            return

        if not update.message:
            return

        self.pipeline_paused = True
        await update.message.reply_text("⏸️ Сбор заказов приостановлен.")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду /resume."""
        if not await self._ensure_admin(update):
            return

        if not update.message:
            return

        self.pipeline_paused = False
        await update.message.reply_text("▶️ Сбор заказов возобновлён.")

    async def _cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду /stats."""
        if not await self._ensure_admin(update):
            return

        if not update.message:
            return

        if self.store is not None:
            try:
                self.stats.update(self.store.stats_since(days=7))
            except Exception:  # noqa: BLE001 - stats must never break the bot
                log.exception("Failed to query statistics")

        text = (
            "📊 *Статистика за 7 дней*\n\n"
            f"• Заказов собрано: {self.stats.get('total_jobs', 0)}\n"
            f"• Новых: {self.stats.get('total_new', 0)}\n"
            f"• Подходящих: {self.stats.get('total_eligible', 0)}\n"
            f"• Сообщений создано: {self.stats.get('total_messages', 0)}\n"
            f"• Отправлено: {self.stats.get('total_sent', 0)}\n"
            f"• Отклонено: {self.stats.get('total_rejected', 0)}\n"
        )
        await self._reply(update, text, parse_mode="Markdown")

    # Допустимые размеры выборки для /jobs — только 5, 10 или 30.
    JOB_LIMITS: tuple[int, ...] = (5, 10, 30)

    async def _cmd_jobs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает /jobs [5|10|30]: последние заказы по личным фильтрам.

        Приоритет выдачи: свежие без штрафов — первыми, заказы со штрафом
        красных флагов и старые — последними (приоритет задаёт админ).
        """
        if not await self._ensure_admin(update):
            return

        if not update.message:
            return

        limit = 10
        args = context.args or []
        if args:
            try:
                limit = int(args[0])
            except (TypeError, ValueError):
                await update.message.reply_text(
                    "Использование: `/jobs [5|10|30]` — например, `/jobs 30`.",
                    parse_mode="Markdown",
                )
                return
            if limit not in self.JOB_LIMITS:
                await update.message.reply_text(
                    "⚠️ Доступны только выборки: /jobs 5, /jobs 10 или /jobs 30."
                )
                return

        if self.store is None:
            await update.message.reply_text("🗄️ База заказов недоступна.")
            return

        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is None:
            return

        try:
            prefs = self.store.get_user_filters(chat_id)
            jobs, total = self.store.user_jobs(prefs, limit)
        except Exception:  # noqa: BLE001 - команда не должна ломать бота
            log.exception("Failed to query user jobs")
            await update.message.reply_text("⚠️ Не удалось прочитать базу заказов.")
            return

        if not jobs:
            await update.message.reply_text(
                "🎯 Подходящих по вашим фильтрам заказов пока нет. "
                "Список появится после ближайшего прохода."
            )
            return

        lines = [
            "🎯 <b>Последние заказы по вашим фильтрам</b>",
            f"Всего подходящих: {total} · показаны {len(jobs)}",
            "",
        ]
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        for index, row in enumerate(jobs, start=1):
            title = html_escape(str(row["title"]), quote=False)
            url = html_escape(str(row["order_url"] or row["url"]), quote=True)
            platform = html_escape(str(row["platform"]), quote=False)
            status = "✅ показан" if row["notified_at"] else "🆕 новый"
            score = float(row["score"] or 0)
            penalty = float(row["penalty"] or 0)
            lines.append(f'{index}. <a href="{url}">{title}</a>')
            meta = f"   ⭐ {score:.1f} · {platform} · {status}"
            if penalty > 0:
                meta += " · ⚠️ штраф"
            budget = _format_budget(row["budget_rub"])
            if budget:
                meta += f" · {budget}"
            lines.append(meta)
            # Кнопка избранного под каждым заказом.
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        "💔 Убрать из избранного"
                        if self.store.is_favorite(chat_id, str(row["uid"]))
                        else "♥ Выделить как понравившееся",
                        callback_data=(
                            f"unfav:{row['uid']}"
                            if self.store.is_favorite(chat_id, str(row["uid"]))
                            else f"fav:{row['uid']}"
                        ),
                    )
                ]
            )
            lines.append("")

        keyboard = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None
        await self._reply(update, "\n".join(lines), parse_mode="HTML", reply_markup=keyboard)

    async def _cmd_favorites(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает /favorites: избранные заказы пользователя."""
        if not await self._ensure_admin(update):
            return

        if not update.message:
            return
        if self.store is None:
            await update.message.reply_text("🗄️ База заказов недоступна.")
            return

        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is None:
            return

        try:
            rows = self.store.favorites(chat_id)
        except Exception:  # noqa: BLE001 - команда не должна ломать бота
            log.exception("Failed to query favorites")
            await update.message.reply_text("⚠️ Не удалось прочитать базу заказов.")
            return

        if not rows:
            await update.message.reply_text(
                "♥ В избранном пусто. Нажмите «♥ Выделить как понравившееся» "
                "под заказом — он появится здесь."
            )
            return

        lines = ["♥ <b>Избранные заказы</b>", ""]
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        for index, row in enumerate(rows, start=1):
            title = html_escape(str(row["title"] or "заказ удалён"), quote=False)
            uid = str(row["uid"])
            if row["url"]:
                url = html_escape(str(row["order_url"] or row["url"]), quote=True)
                lines.append(f'{index}. <a href="{url}">{title}</a>')
            else:
                lines.append(f"{index}. {title}")
            meta = f"   {html_escape(str(row['platform'] or ''), quote=False)}"
            budget = _format_budget(row["budget_rub"])
            if budget:
                meta += f" · {budget}"
            lines.append(meta)
            keyboard_rows.append(
                [InlineKeyboardButton(f"💔 Убрать {index}", callback_data=f"unfav:{uid}")]
            )
            lines.append("")

        await self._reply(
            update,
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None,
        )

    async def _cmd_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду /logs."""
        if not await self._ensure_admin(update):
            return

        if not update.message:
            return

        if not self.last_errors:
            await update.message.reply_text("✅ Ошибок не обнаружено.")
            return

        text = "⚠️ *Последние ошибки:*\n\n"
        for ts, error in self.last_errors[-10:]:
            text += f"• {ts.strftime('%H:%M:%S')} — {_md(error[:200])}\n"

        await self._reply(update, text, parse_mode="Markdown")

    # ------------------------------------------------------------- меню настроек
    async def _handle_settings_callback(
        self, update: Update, query: CallbackQuery, data: str
    ) -> None:
        """Открывает раздел меню настроек, проверяя уровень доступа.

        Callback приходит из клавиатуры /settings. Админские разделы закрыты
        ещё и здесь: подписчик мог сохранить callback из старого сообщения
        или собрать его вручную — одного отсутствия кнопки мало.
        """
        section = data.split(":", 1)[1] if ":" in data else ""
        chat_id = update.effective_chat.id if update.effective_chat else None
        if section in {"cat_filters", "uf_menu", "platform_filters", "uf_platforms"}:
            # Личные фильтры: свой экран и своя клавиатура, доступны всем.
            if self.store is None or chat_id is None:
                await query.answer("🗄️ База недоступна.", show_alert=True)
                return
            prefs = self.store.get_user_filters(chat_id)
            if section in {"platform_filters", "uf_platforms"}:
                text, markup = self._uf_platforms_view(prefs)
            else:
                text, markup = self._uf_view(prefs)
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
            return

        if section not in {"main", "admin", "letters", "profile", "hub", "monitoring"}:
            await query.answer("Неизвестный раздел.", show_alert=True)
            return
        if section not in SELF_SERVICE_TOPICS and not self._is_super_admin_update(update):
            await self._deny_insufficient_rights(update)
            return

        if section == "hub":
            # Возврат в меню: клавиатура строится заново по текущей роли.
            text, markup = self._settings_hub(update)
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
            return

        body = self._settings_section(section)
        if body is None:
            await query.answer("Раздел пуст.", show_alert=True)
            return
        # Кнопка «⬅ Назад» возвращает к меню с учётом роли.
        back = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅ Назад", callback_data="settings:hub")]]
        )
        await query.edit_message_text(body, parse_mode="Markdown", reply_markup=back)

    # ------------------------------------------------------------------ доступ
    async def _cmd_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду /users: доступ и заявки на подписку."""
        if not await self._ensure_admin(update):
            return
        db_admins = self.store.list_bot_admins() if self.store else set()
        lines = ["👥 <b>Доступ к боту</b>", ""]
        for chat_id in sorted(self.admin_chat_ids):
            source = "база (/adduser)" if chat_id in db_admins else ".env (постоянный)"
            role = (
                "🛠 администратор настроек"
                if chat_id in self.super_admin_ids
                else "👤 подписчик"
            )
            lines.append(
                f"• <code>{chat_id}</code> — {html_escape(source)}, {html_escape(role)}"
            )
        if not lines[2:]:
            lines.append("(список пуст)")

        pending = self.store.list_subscribers(SUB_PENDING) if self.store else []
        if pending:
            lines += ["", "📨 <b>Заявки на подписку (ожидают одобрения)</b>", ""]
            for row in pending:
                who = html_escape(str(row["full_name"] or "—"), quote=False)
                username = str(row["username"]) if row["username"] else None
                handle = f"@{html_escape(username, quote=True)}" if username else "—"
                lines.append(f"• {who} ({handle}) — <code>{row['chat_id']}</code>")
            lines.append(
                "Одобрить или отклонить — кнопками в карточке заявки "
                "(приходят админам автоматически) или вручную: /adduser ID."
            )

        lines += [
            "",
            "Добавить: <code>/adduser ID</code>, убрать: <code>/deluser ID</code>. "
            "Заявку новый пользователь отправляет командой /start.",
        ]
        await self._reply(update, "\n".join(lines), parse_mode="HTML")

    async def _cmd_adduser(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду /adduser ID — выдаёт доступ новому чату."""
        if not await self._ensure_admin(update):
            return
        raw = (context.args or [""])[0]
        if not raw.lstrip("-").isdigit():
            await self._reply(
                update,
                "Использование: <code>/adduser ID</code> — Telegram ID пользователя "
                "(для групп ID отрицательный).",
                parse_mode="HTML",
            )
            return
        if self.store is None:
            await self._reply(update, "База недоступна: добавить пользователя нельзя.")
            return
        chat_id = int(raw)
        added_by = update.effective_user.id if update.effective_user else None
        if chat_id in self.admin_chat_ids:
            # Уже админ (из .env или добавленный ранее): в базу не дублируем.
            await self._reply(
                update, f"Пользователь <code>{chat_id}</code> уже имеет доступ.",
                parse_mode="HTML",
            )
            return
        if self.store.add_bot_admin(chat_id, added_by=added_by):
            # Живое обновление: карточки сразу уходят и новому чату, без рестарта.
            self.admin_chat_ids.add(chat_id)
            log.info("user %s added admin %s", added_by, chat_id)
            await self._reply(
                update,
                f"✅ Пользователь <code>{chat_id}</code> добавлен — карточки и панель "
                "ему доступны сразу.",
                parse_mode="HTML",
            )
        else:
            await self._reply(
                update, f"Пользователь <code>{chat_id}</code> уже имеет доступ.", parse_mode="HTML"
            )

    async def _cmd_deluser(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду /deluser ID — убирает доступ."""
        if not await self._ensure_admin(update):
            return
        raw = (context.args or [""])[0]
        if not raw.lstrip("-").isdigit():
            await self._reply(update, "Использование: <code>/deluser ID</code>", parse_mode="HTML")
            return
        chat_id = int(raw)
        if chat_id in self.env_admin_ids:
            await self._reply(
                update,
                f"<code>{chat_id}</code> задан в .env (TELEGRAM_ADMIN_CHAT_IDS) — убирается "
                "только правкой .env на сервере.",
                parse_mode="HTML",
            )
            return
        if self.store is None or chat_id not in self.admin_chat_ids:
            await self._reply(
                update, f"Пользователя <code>{chat_id}</code> нет в списке доступа.",
                parse_mode="HTML",
            )
            return
        if len(self.admin_chat_ids) == 1:
            # Единственный админ остался без .env-админов: запрет спасает от
            # полного закрытия доступа к панели.
            await self._reply(
                update, "⛔ Нельзя убрать последнего администратора.", parse_mode="HTML"
            )
            return
        self.store.remove_bot_admin(chat_id)
        # Синхронизируем подписку: без этого повторный /start создал бы новую
        # заявку или восстановил доступ мимо решения админа.
        self.store.upsert_subscriber_status(chat_id, SUB_REJECTED)
        self.admin_chat_ids.discard(chat_id)
        log.info(
            "user %s removed admin %s",
            update.effective_user.id if update.effective_user else None,
            chat_id,
        )
        await self._reply(
            update, f"✅ Пользователь <code>{chat_id}</code> убран из доступа.", parse_mode="HTML"
        )

    async def _cmd_set_admin(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Назначает администратора настроек (/set_admin ID).

        Роль даёт доступ к разделам, которые меняют поведение бота для всех:
        конвейер, письма, профиль, платформы. Назначать может только
        действующий администратор настроек.
        """
        await self._change_super_admin(update, context, enabled=True)

    async def _cmd_unset_admin(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Снимает роль администратора настроек (/unset_admin ID)."""
        await self._change_super_admin(update, context, enabled=False)

    async def _change_super_admin(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, *, enabled: bool
    ) -> None:
        """Общая реализация /set_admin и /unset_admin."""
        if not await self._ensure_super_admin(update):
            return
        command = "/set_admin" if enabled else "/unset_admin"
        raw = (context.args or [""])[0]
        if not raw.lstrip("-").isdigit():
            await self._reply(
                update,
                f"Использование: <code>{command} ID</code> — Telegram ID пользователя "
                "или группы (для групп ID отрицательный).",
                parse_mode="HTML",
            )
            return
        chat_id = int(raw)
        if self.store is None:
            await self._reply(
                update, "База недоступна: роль администратора негде сохранить."
            )
            return
        if chat_id in self.env_admin_ids:
            await self._reply(
                update,
                f"<code>{chat_id}</code> задан в .env — он администратор постоянно. "
                "Роль снимается только правкой .env на сервере.",
                parse_mode="HTML",
            )
            return
        if not enabled and chat_id == self._super_admin_id(update):
            # Защита от самоблокировки: без роли не останется, кому управлять.
            await self._reply(
                update,
                "⛔ Нельзя снять роль с самого себя. Попросите другого "
                "администратора или используйте ID из .env.",
                parse_mode="HTML",
            )
            return

        actor = self._super_admin_id(update)
        changed = self.store.set_super_admin(chat_id, enabled, added_by=actor)
        if enabled:
            # Назначенный сразу получает и доступ к боту: иначе он не увидит
            # ни карточек, ни ответов на команды.
            self.store.add_bot_admin(chat_id, added_by=actor)
            self.admin_chat_ids.add(chat_id)
            self.super_admin_ids.add(chat_id)
        else:
            self.super_admin_ids.discard(chat_id)
            # Доступ к заказам сохраняем: снимается только роль настроек.
        log.info("super admin %s: %s by %s", "granted" if enabled else "revoked", chat_id, actor)
        if changed:
            status = "назначен администратором настроек" if enabled else (
                "больше не администратор настроек"
            )
            await self._reply(
                update, f"✅ <code>{chat_id}</code> {status}.", parse_mode="HTML"
            )
        else:
            state = "уже администратор настроек" if enabled else (
                "и так не администратор настроек"
            )
            await self._reply(update, f"ℹ️ <code>{chat_id}</code> {state}.", parse_mode="HTML")

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает callback от inline-кнопок."""
        query = update.callback_query
        if not query or not query.data:
            return

        # Кнопка «📩 Подписаться» — единственный callback, доступный без
        # активной подписки: иначе новый пользователь не смог бы её получить.
        if query.data == SUBSCRIBE_CALLBACK:
            await self._handle_subscribe_callback(update, query)
            return

        if not self._is_subscribed_update(update):
            own = update.effective_user.id if update.effective_user else None
            chat_id = update.effective_chat.id if update.effective_chat else None
            log.warning(
                "Доступ запрещён (callback): user_id=%s chat_id=%s", own, chat_id
            )
            status = self.subscription_status(own or chat_id or 0)
            if status == SUB_PENDING:
                alert = "⏳ Ваша заявка на рассмотрении."
            elif status == SUB_REJECTED:
                alert = "❌ Заявка отклонена. Отправьте новую кнопкой «📩 Подписаться»."
            else:
                alert = "⛔ Подписка не активна. Нажмите «📩 Подписаться»."
            if own is not None:
                alert += f" Ваш ID: {own}"
            await query.answer(alert, show_alert=True)
            return

        # ------------------------------------------------------------------ AI-квиз
        # До общего answer(): каждый шаг квиза сам отвечает на callback своим
        # сообщением («Отменено», «Генерирую заново»), а повторный ответ
        # Telegram считает ошибкой.
        if (query.data or "").startswith("aq:"):
            await self._handle_quiz_callback(update, query, query.data or "")
            return

        await query.answer()

        data = query.data

        # ------------------------------------------------------------------ площадки
        if data.startswith("platform_toggle_"):
            platform = data.replace("platform_toggle_", "")
            if platform in self.config.platforms:
                cfg = self.config.platforms[platform]
                cfg.enabled = not cfg.enabled
                note = self._persist_note().strip()
                text, markup = self._platforms_view(note)
                await query.edit_message_text(
                    text, parse_mode="Markdown", reply_markup=markup
                )

        elif data.startswith("platform_add_"):
            platform = data.replace("platform_add_", "")
            _ok, note = self._platform_add(platform)
            text, markup = self._platforms_view(note + self._persist_note())
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)

        elif data.startswith("platform_remove_"):
            platform = data.replace("platform_remove_", "")
            _ok, note = self._platform_remove(platform)
            text, markup = self._platforms_view(note + self._persist_note())
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)

        # ---------------------------------------------------------------- категории
        elif data == "category_filter_toggle":
            self.config.category_filter.enabled = not self.config.category_filter.enabled
            await query.edit_message_text(
                self._categories_text() + self._persist_note(),
                parse_mode="Markdown",
                reply_markup=self._categories_markup(),
            )

        elif data.startswith("category_toggle_"):
            cat_key = data.replace("category_toggle_", "")
            current = list(self.config.category_filter.categories or [])
            if cat_key in current:
                current.remove(cat_key)
            else:
                current.append(cat_key)
            self.config.category_filter.categories = current
            await query.edit_message_text(
                self._categories_text() + self._persist_note(),
                parse_mode="Markdown",
                reply_markup=self._categories_markup(),
            )

        # ------------------------------------------------ навигация по рубрикам
        elif data == "cat_menu":
            text, markup = self._categories_view()
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)

        elif data.startswith("cat_top_"):
            cat_key = data[len("cat_top_"):]
            view = self._category_sub_view(cat_key)
            if view is None:
                await query.answer("Рубрика не найдена.", show_alert=True)
                return
            await query.edit_message_text(view[0], parse_mode="Markdown", reply_markup=view[1])

        elif data.startswith("cat_all_"):
            cat_key = data[len("cat_all_"):]
            cat = self.config.category_filter
            # Вся рубрика: снимаем ограничения по категориям и включаем её.
            cat.subcategories = {k: v for k, v in cat.subcategories.items() if k != cat_key}
            if cat_key not in (cat.categories or []):
                cat.categories = list(cat.categories or []) + [cat_key]
            view = self._category_sub_view(cat_key, self._persist_note())
            if view is None:
                await query.answer("Рубрика не найдена.", show_alert=True)
                return
            await query.edit_message_text(view[0], parse_mode="Markdown", reply_markup=view[1])

        elif data.startswith("cat_off_"):
            cat_key = data[len("cat_off_"):]
            cat = self.config.category_filter
            # Выключить рубрику: убираем её и её подкатегории из выбора.
            cat.categories = [c for c in (cat.categories or []) if c != cat_key]
            cat.subcategories = {k: v for k, v in cat.subcategories.items() if k != cat_key}
            view = self._category_sub_view(cat_key, self._persist_note())
            if view is None:
                await query.answer("Рубрика не найдена.", show_alert=True)
                return
            await query.edit_message_text(view[0], parse_mode="Markdown", reply_markup=view[1])

        elif data.startswith("cat_grp_"):
            rest = data[len("cat_grp_"):]
            # rpartition даёт (до, "_", после): ключ — голова (в нём есть
            # подчёркивания: writing_translations, audio_video), индекс — хвост.
            key, _, group_index = rest.rpartition("_")
            view = self._category_sub_view_group(key, group_index)
            if view is None:
                await query.answer("Группа не найдена.", show_alert=True)
                return
            await query.edit_message_text(view[0], parse_mode="Markdown", reply_markup=view[1])

        elif data.startswith("cat_sub_"):
            rest = data[len("cat_sub_"):]
            # Два индекса справа: cat_sub_<key>_<группа>_<категория>.
            # rpartition даёт (до, "_", после): индексы — в хвосте.
            rest2, _, item_index = rest.rpartition("_")
            key, _, group_index = rest2.rpartition("_")
            view = self._category_sub_view_item(key, group_index, item_index)
            if view is None:
                await query.answer("Категория не найдена.", show_alert=True)
                return
            await query.edit_message_text(view[0], parse_mode="Markdown", reply_markup=view[1])

        elif data == "cat_price":
            self._awaiting_input = "category_filter.min_price_rub"
            self._awaiting_label = "минимальная цена"
            chat = update.effective_chat
            if chat is not None:
                await self._bot_send(
                    chat_id=chat.id,
                    text=(
                        "💰 Пришлите минимальную цену числом, например `5000`.\n"
                        "`0` — выключить фильтр цены.\n\n"
                        "Заказы без цены (по договорённости) показываются всегда.\n"
                        "Отмена: /cancel"
                    ),
                    parse_mode="Markdown",
                )

        elif data.startswith("cat_"):
            # Знакомый префикс с неизвестным хвостом — отдаём понятную ошибку.
            await query.answer("Неизвестное действие меню категорий.", show_alert=True)

        # ------------------------------------------------ личные фильтры и избранное
        elif data.startswith("uf_"):
            handled = await self._handle_uf_callback(update, query, data)
            if not handled:
                # Префикс знакомый, хвост нет — понятная ошибка вместо «unknown».
                await query.answer("Неизвестное действие личных фильтров.", show_alert=True)

        # ------------------------------------------------------------ меню настроек
        elif data.startswith("settings:"):
            await self._handle_settings_callback(update, query, data)

        elif data.startswith(("fav:", "unfav:")):
            await self._handle_favorite_callback(update, query, data)

        # ---------------------------------------------------------------- режим отправки
        elif data in {"send_dry_toggle", "send_approval_toggle"}:
            msg = self.config.messaging
            if data == "send_dry_toggle":
                target = not self.config.runtime.dry_run
                if target is False and not msg.require_human_approval:
                    await query.answer(
                        "Сначала включите одобрение писем или используйте /send dry off confirm",
                        show_alert=True,
                    )
                    return
                self.config.runtime.dry_run = target
            else:
                target = not msg.require_human_approval
                if target is False and not self.config.runtime.dry_run:
                    await query.answer(
                        "Бот в live-режиме. Используйте /send approval off confirm",
                        show_alert=True,
                    )
                    return
                msg.require_human_approval = target
            await query.edit_message_text(
                self._send_text() + self._persist_note(),
                parse_mode="Markdown",
                reply_markup=self._send_markup(),
            )

        # ---------------------------------------------------------------- стратегия цены
        elif data.startswith("pricing_strategy_"):
            strategy = data.replace("pricing_strategy_", "")
            if strategy not in ("minimum", "markup", "llm_estimate"):
                return
            if self.profile is None:
                self.profile = self._profile_model()
            self.profile.pricing.strategy = strategy  # type: ignore[assignment]
            labels = {
                "minimum": "Минимум",
                "markup": "Наценка",
                "llm_estimate": "Оценка LLM",
            }
            await query.edit_message_text(
                f"Стратегия цены: *{labels[strategy]}*." + self._persist_note(),
                parse_mode="Markdown",
            )

        # ------------------------------------------------------------------ заявки на подписку
        elif data.startswith(("sub_approve:", "sub_reject:")):
            action, _, raw_id = data.partition(":")
            try:
                target_id = int(raw_id)
            except ValueError:
                await query.answer("⚠️ Неверный формат заявки.", show_alert=True)
                log.warning("malformed subscription callback: %s", data)
                return
            if self.store is None:
                await query.answer("База недоступна: заявку обработать нельзя.", show_alert=True)
                return

            subscriber = self.store.get_subscriber(target_id)
            if subscriber is None:
                await query.answer("Заявка не найдена.", show_alert=True)
                return
            status = str(subscriber.get("status") or "")
            if status not in {SUB_PENDING, "rejected"} or target_id in self.admin_chat_ids:
                await query.answer("Заявка уже обработана.", show_alert=True)
                return

            admin_id = query.from_user.id
            name = subscriber.get("full_name") or "пользователь"
            if action == "sub_approve":
                self.store.set_subscriber_status(
                    target_id, SUB_ACTIVE, decided_by=admin_id
                )
                # Доступ живёт в bot_admins: переживает рестарт и сразу
                # добавляет пользователя к получателям карточек.
                self.store.add_bot_admin(target_id, added_by=admin_id)
                self.admin_chat_ids.add(target_id)
                log.info("admin %s approved subscription %s", admin_id, target_id)
                # Карточка заявки правится на месте: кнопки исчезают, в тексте
                # остаётся решение. Иначе админ видит «висящие» кнопки и жмёт
                # их повторно.
                await self._edit_subscription_decision(
                    query, str(name), target_id, approved=True
                )
                await self._notify_user(target_id, MSG_SUBSCRIBED + "\n\n" + SUBSCRIBE_HINT)
            else:
                self.store.set_subscriber_status(
                    target_id, SUB_REJECTED, decided_by=admin_id
                )
                log.info("admin %s rejected subscription %s", admin_id, target_id)
                await self._edit_subscription_decision(
                    query, str(name), target_id, approved=False
                )
                await self._notify_user(target_id, MSG_REJECTED + "\n\n" + SUBSCRIBE_HINT)

        # ----------------------------------------------------------------- карточки заказов
        # Битые callback_data: проверяем префикс, чтобы отдать понятный ответ.
        elif data.startswith(("send:", "copy:", "skip:")):
            # Новый формат: "send:fl_ru:1" — split слева, action НЕ содержит "_".
            action, job_uid = data.split(":", 1)

            if not job_uid:
                await query.answer(
                    "⚠️ Неверный формат команды. Попробуйте ещё раз.", show_alert=True
                )
                log.warning("malformed callback data: %s from user %s", data, query.from_user.id)
                return

            # Состояние читаем из БД — переживает рестарт бота.
            # В тестах с FakeStore fallback на pending_approvals (in-memory).
            order = None
            if self.store:
                order = self.store.get_order(job_uid)
            if order is None:
                pending = self.pending_approvals.get(job_uid)
                if pending is not None:
                    order = {
                        "uid": job_uid,
                        "platform": pending.job.platform,
                        "external_id": pending.job.external_id,
                        "url": pending.job.url,
                        "order_url": pending.job.order_url or pending.job.url,
                        "status": "shown",
                        "draft_text": pending.message_text or "",
                    }
                else:
                    await query.answer(
                        "⚠️ Заказ не найден. Возможно, он уже снят.", show_alert=True
                    )
                    log.warning("order not found: %s", job_uid)
                    return

            user_id = query.from_user.id
            draft_text = order.get("draft_text") or ""
            chat_id = update.effective_chat.id if update.effective_chat else None

            async def _reply(text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
                """Отвечает в чат карточки через bot.send_message (mypy-безопасно)."""
                if self.app is None or chat_id is None:
                    return False
                try:
                    await self._bot_send(
                        chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup
                    )
                    return True
                except TelegramError as e:
                    log.error("failed to deliver draft for %s: %s", job_uid, e)
                    return False

            if action == "send":
                # «Отправить»: прислать письмо для копирования + пометить карточку.
                if draft_text:
                    # Кнопка в новом сообщении не нужна: <pre> копируется тапом.
                    for chunk in _chunk(draft_text):
                        await _reply(
                            f"📝 <b>Письмо для заказа</b>\n\n<pre>{html_escape(chunk)}</pre>"
                        )
                if self.store:
                    self.store.mark_sent_clicked(job_uid)
                    self.store.log_action(
                        user_id, job_uid, "sent_clicked", {"url": order.get("order_url")}
                    )
                log.info("user %s sent_clicked %s", user_id, job_uid)
                await query.edit_message_reply_markup(reply_markup=None)
                await _reply("✅ Открыто для ответа. Вставьте письмо в форму отклика.")

            elif action == "copy":
                # «Копировать»: прислать письмо в <pre> — Telegram показывает
                # кнопку «копировать» на поддерживаемых клиентах.
                if not draft_text:
                    await query.answer("⚠️ Текст письма недоступен.", show_alert=True)
                    log.warning("copy empty draft for %s", job_uid)
                    return
                for chunk in _chunk(draft_text):
                    escaped = html_escape(chunk)
                    copy_view = f"📋 <b>Скопируйте текст ниже</b>\n\n<pre>{escaped}</pre>"
                    await _reply(copy_view)
                if self.store:
                    self.store.log_action(user_id, job_uid, "copy", {})
                log.info("user %s copied draft for %s", user_id, job_uid)

            elif action == "skip":
                # «Пропустить»: убираем кнопки, фиксируем статус.
                if self.store:
                    self.store.mark_skipped(job_uid)
                    self.store.log_action(user_id, job_uid, "skipped", {})
                log.info("user %s skipped %s", user_id, job_uid)
                await query.edit_message_reply_markup(reply_markup=None)

        # ------------------------------------------------------------------ устаревшие
        # Поддержка старых callback_data от уже отправленных карточек.
        elif data.startswith(("approve_", "reject_")):
            await query.answer(
                "⚠️ Эта карточка обновлена. Используйте новые кнопки.",
                show_alert=True,
            )

        else:
            await query.answer("⚠️ Неизвестная команда.", show_alert=True)
            log.warning("unknown callback data: %s", data)

    async def _edit_subscription_decision(
        self, query: CallbackQuery, name: str, target_id: int, *, approved: bool
    ) -> None:
        """Правит карточку заявки после решения админа.

        Кнопки убираются: повторное нажатие уже не пройдёт, но «мёртвая»
        клавиатура выглядит как приглашение нажать ещё раз.
        """
        decision = "✅ Одобрено" if approved else "❌ Отклонено"
        text = (
            f"📨 *Заявка на подписку* — {decision}\n\n"
            f"Пользователь: {_md(name)}\n"
            f"ID: `{target_id}`"
        )
        try:
            await query.edit_message_text(text, parse_mode="Markdown")
        except TelegramError as exc:
            # Сообщение могло быть удалено или уже изменено другим админом.
            log.debug("cannot edit subscription card for %s: %s", target_id, exc)

    async def notify_cycle_complete(
        self, jobs_collected: int, jobs_new: int, jobs_eligible: int, messages_generated: int
    ) -> None:
        """Отправляет сводку о завершении цикла.

        ``jobs_eligible`` здесь — число новых подходящих заказов, реально
        показанных в этом проходе. Если новых нет, сводка не отправляется:
        фоновые проходы без изменений не должны засорять чат.
        """
        self.stats["jobs_collected"] = jobs_collected
        self.stats["jobs_new"] = jobs_new
        self.stats["jobs_eligible"] = jobs_eligible
        self.stats["messages_generated"] = messages_generated

        if jobs_eligible <= 0:
            return
        if not self.app or not self.is_running:
            return

        text = (
            "✅ *Цикл завершён*\n\n"
            f"• Собрано заказов: {jobs_collected}\n"
            f"• Новых: {jobs_new}\n"
            f"• Новых подходящих: {jobs_eligible}\n"
            f"• Сообщений создано: {messages_generated}\n"
        )

        await self._send_to_admins(text)

    async def notify_error(self, error_message: str) -> None:
        """Отправляет уведомление об ошибке."""
        self.last_errors.append((datetime.now(UTC), error_message))
        if len(self.last_errors) > 50:
            self.last_errors = self.last_errors[-50:]

        if not self.app or not self.is_running:
            return

        text = f"⚠️ *Ошибка в pipeline:*\n\n{_md(error_message)}"

        await self._send_to_admins(text)


async def run_telegram_bot(
    token: str, admin_chat_ids: list[int], config: AppConfig
) -> TelegramBotController:
    """Запускает Telegram-бота и возвращает экземпляр контроллера."""
    controller = TelegramBotController(token, admin_chat_ids, config)
    await controller.start()
    return controller
