"""Личные фильтры подписчика: команды, меню и персональная доставка карточек.

Mixin для :class:`freelance_bot.telegram_bot.TelegramBotController`. Пользователь
настраивает свои рубрики, подкатегории, биржи и минимальную цену (/filters),
получает только те заказы, что прошли его фильтр, и помечает понравившиеся
кнопкой ♥. Состояние фильтров живёт в SQLite (таблица user_filters), поэтому
переживает рестарт бота.

Полномочия: /filters и кнопки избранного доступны каждому одобренному
подписчику (_ensure_admin), а не только администратору из .env — это личные
настройки пользователя, не затрагивающие общий конвейер.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

from .card_render import (
    FAV_OFF_LABEL,
    FAV_ON_LABEL,
    card_keyboard,
    render_hint,
    render_job_card,
)
from .categories import classify_job
from .models import ScoredJob

if TYPE_CHECKING:
    from .config import AppConfig
    from .models import JobAnalysis
    from .storage import JobStore
    from .user_prefs import UserFilterPrefs

log = logging.getLogger(__name__)

# Путь ожидаемого ввода (_awaiting_input) для мин. цены личного фильтра.
# Обрабатывается в _apply_user_filters_input, а не в общей _apply_value,
# которая пишет только в конфиг/профиль.
UF_PRICE_INPUT = "user_filters.min_price_rub"


def _md(value: object) -> str:
    """Экранирование динамического текста для legacy-Markdown Telegram."""
    text = str(value)
    for char in ("\\", "_", "*", "[", "]", "`"):
        text = text.replace(char, "\\" + char)
    return text


def _price_label(value: float) -> str:
    if value > 0:
        return f"{value:,.0f} ₽".replace(",", " ")
    return "любая"


class UserFiltersCommandsMixin:
    """Личные фильтры, избранное и адресная доставка карточек заказов."""

    # Атрибуты предоставляет TelegramBotController; аннотации нужны mypy.
    config: AppConfig
    store: JobStore | None
    is_running: bool
    pending_approvals: dict[str, Any]  # dict[str, TelegramNotification]
    _awaiting_input: str | None
    _awaiting_label: str
    app: Application | None

    # Реализация в SettingsCommandsMixin (стоит позже в MRO). Объявляем
    # только для mypy: блок не исполняется, поэтому заглушка не перекроет
    # настоящий метод — в отличие от обычного метода-заглушки.
    if TYPE_CHECKING:  # pragma: no cover

        def _persist_note(self) -> str: ...

    # Реализации предоставляет TelegramBotController; заглушки — только mypy.
    # ВАЖНО: заглушать методы, реализованные в SettingsCommandsMixin
    # (_persist_note и др.), нельзя — этот mixin стоит раньше в MRO и
    # перекрыл бы их заглушкой.
    async def _ensure_admin(self, update: Update) -> bool:  # pragma: no cover
        raise NotImplementedError

    async def _reply(  # pragma: no cover
        self,
        update: Update,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        raise NotImplementedError

    async def _bot_send(self, **kwargs: Any) -> None:  # pragma: no cover
        raise NotImplementedError

    # ------------------------------------------------------------------ метки меню
    def _uf_category_mark(self, prefs: UserFilterPrefs, key: str) -> str:
        """Отметка рубрики в меню: выбрана целиком / частично / нет."""
        from .categories import CATEGORY_BY_KEY

        if key not in prefs.categories or key not in CATEGORY_BY_KEY:
            return "⬜"
        if prefs.subcategories.get(key):
            return "🔸"
        return "✅"

    def _uf_group_mark(self, prefs: UserFilterPrefs, key: str, group_name: str) -> str:
        from .categories import CATEGORY_BY_KEY

        category = CATEGORY_BY_KEY.get(key)
        if category is None:
            return "⬜"
        selected = prefs.subcategories.get(key, [])
        names = [
            item.name
            for group in category.groups
            if group.name == group_name
            for item in group.items
        ]
        chosen = [name for name in names if name in selected]
        if not chosen:
            return "⬜"
        if len(chosen) == len(names):
            return "✅"
        return "🔸"

    # ------------------------------------------------------------------ виды меню
    def _uf_text(self, prefs: UserFilterPrefs) -> str:
        """Карточка /filters: сводка личных фильтров и список рубрик."""
        from .categories import CATEGORIES, CATEGORY_BY_KEY

        lines = [
            "🔧 *Мои фильтры заказов*",
            "",
            "Пустой фильтр — показывать всё, что собирает бот. Порогов балла нет.",
            f"🏛 Биржи: {_md(prefs.describe_platforms())}",
            f"💰 Мин. цена: {_md(_price_label(prefs.min_price_rub))}"
            " (заказы без цены — всегда)",
            "",
            "Нажмите рубрику, чтобы выбрать категории:",
            "",
        ]
        for item in CATEGORIES:
            mark = self._uf_category_mark(prefs, item.key)
            if item.key not in prefs.categories:
                detail = "не выбрана"
            elif prefs.subcategories.get(item.key):
                detail = f"{len(prefs.subcategories[item.key])} из {len(item.subcategories)}"
            else:
                detail = "вся рубрика"
            lines.append(f"{mark} {item.name} — {detail}")
        if prefs.subcategories:
            lines.append("\n*Выбранные категории:*")
            for key, items in prefs.subcategories.items():
                name = CATEGORY_BY_KEY[key].name if key in CATEGORY_BY_KEY else key
                lines.append(f"• {_md(name)}: {_md(', '.join(items))}")
        lines.append(
            "\nКоманды: `/filters platforms`, `/filters price 5000`, `/filters reset`."
        )
        return "\n".join(lines)

    def _uf_markup(self, prefs: UserFilterPrefs) -> InlineKeyboardMarkup:
        from .categories import CATEGORIES

        rows: list[list[InlineKeyboardButton]] = []
        for item in CATEGORIES:
            mark = self._uf_category_mark(prefs, item.key)
            rows.append(
                [InlineKeyboardButton(f"{mark} {item.name}", callback_data=f"uf_top_{item.key}")]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    f"🏛 Биржи: {self._uf_platform_label(prefs)}",
                    callback_data="uf_platforms",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    f"💰 Мин. цена: {_price_label(prefs.min_price_rub)}",
                    callback_data="uf_price",
                )
            ]
        )
        rows.append(
            [InlineKeyboardButton("♻️ Сбросить всё", callback_data="uf_reset")]
        )
        return InlineKeyboardMarkup(rows)

    def _uf_view(
        self, prefs: UserFilterPrefs, note: str = ""
    ) -> tuple[str, InlineKeyboardMarkup]:
        text = self._uf_text(prefs)
        if note:
            text += "\n\n" + note
        return text, self._uf_markup(prefs)

    def _uf_platform_label(self, prefs: UserFilterPrefs) -> str:
        """Подпись кнопки бирж: пустой выбор = все."""
        if not prefs.platforms:
            return "все"
        return ", ".join(prefs.platforms)

    def _uf_platforms_text(self, prefs: UserFilterPrefs) -> str:
        lines = [
            "🏛 *Мои биржи*",
            "",
            "Пустой выбор — заказы со всех площадок, которые собирает бот.",
            "",
        ]
        for name, cfg in self.config.platforms.items():
            mark = "✅" if name in prefs.platforms else "⬜"
            state = "" if cfg.enabled else " · ⏸ площадка выключена админом"
            lines.append(f"{mark} {_md(name)}{state}")
        lines.append(
            "\nТап по бирже переключает её. Выбор влияет только на ваши "
            "заказы: другие подписчики получают всё по своим фильтрам."
        )
        return "\n".join(lines)

    def _uf_platforms_markup(self, prefs: UserFilterPrefs) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        for name in self.config.platforms:
            mark = "✅" if name in prefs.platforms else "⬜"
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{mark} {name}", callback_data=f"uf_platform_{name}"
                    )
                ]
            )
        rows.append([InlineKeyboardButton("⬅ Назад к фильтрам", callback_data="uf_menu")])
        return InlineKeyboardMarkup(rows)

    def _uf_platforms_view(
        self, prefs: UserFilterPrefs, note: str = ""
    ) -> tuple[str, InlineKeyboardMarkup]:
        text = self._uf_platforms_text(prefs)
        if note:
            text += "\n\n" + note
        return text, self._uf_platforms_markup(prefs)

    # ---------------------------------------------------------------- подменю рубрики
    def _uf_sub_text(self, prefs: UserFilterPrefs, key: str) -> str | None:
        """Карточка рубрики личного фильтра: список групп и категорий."""
        from .categories import CATEGORY_BY_KEY

        category = CATEGORY_BY_KEY.get(key)
        if category is None:
            return None
        selected = prefs.subcategories.get(key, [])
        if key not in prefs.categories:
            state = "⬜ рубрика не выбрана"
        elif selected:
            state = f"🔸 выбрано {len(selected)} из {len(category.subcategories)}"
        else:
            state = "✅ вся рубрика (без ограничений)"
        lines = [
            f"📂 *Рубрика «{_md(category.name)}»*",
            "",
            state,
            "",
        ]
        for group in category.groups:
            group_mark = self._uf_group_mark(prefs, key, group.name)
            lines.append(f"{group_mark} *{_md(group.name)}*")
            for sub in group.items:
                mark = "✅" if sub.name in selected else "⬜"
                lines.append(f"  {mark} {_md(sub.name)}")
        lines.append(
            "\nТап по категории переключает её. Когда снята последняя подкатегория — "
            "рубрика выключается. «Вся рубрика» включает её целиком."
        )
        return "\n".join(lines)

    def _uf_sub_markup(self, prefs: UserFilterPrefs, key: str) -> InlineKeyboardMarkup | None:
        from .categories import CATEGORY_BY_KEY

        category = CATEGORY_BY_KEY.get(key)
        if category is None:
            return None
        # Рубрика выбрана целиком (без ограничений) — единственное действие
        # с уровнем рубрики: выключить её. Иначе предлагаем выбрать целиком.
        if key in prefs.categories and not prefs.subcategories.get(key):
            rows: list[list[InlineKeyboardButton]] = [
                [InlineKeyboardButton("⬜ Выключить рубрику", callback_data=f"uf_off_{key}")]
            ]
        else:
            rows = [
                [
                    InlineKeyboardButton(
                        "✅ Вся рубрика (снять ограничения)", callback_data=f"uf_all_{key}"
                    )
                ]
            ]
        for gi, group in enumerate(category.groups):
            group_mark = self._uf_group_mark(prefs, key, group.name)
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{group_mark} {group.name}", callback_data=f"uf_grp_{key}_{gi}"
                    )
                ]
            )
            row: list[InlineKeyboardButton] = []
            for ii, item in enumerate(group.items):
                selected = item.name in prefs.subcategories.get(key, [])
                mark = "✅" if selected else "⬜"
                label = f"{mark} {item.name}"
                # Telegram ограничивает подпись кнопки 64 символами.
                if len(label) > 64:
                    label = label[:63] + "…"
                row.append(InlineKeyboardButton(label, callback_data=f"uf_sub_{key}_{gi}_{ii}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
        rows.append([InlineKeyboardButton("⬅ Назад к фильтрам", callback_data="uf_menu")])
        return InlineKeyboardMarkup(rows)

    def _uf_sub_view(
        self, prefs: UserFilterPrefs, key: str, note: str = ""
    ) -> tuple[str, InlineKeyboardMarkup] | None:
        text = self._uf_sub_text(prefs, key)
        markup = self._uf_sub_markup(prefs, key)
        if text is None or markup is None:
            return None
        if note:
            text += "\n\n" + note
        return text, markup

    # ------------------------------------------------------------------ переключатели
    def _uf_toggle_sub(
        self, prefs: UserFilterPrefs, key: str, sub_name: str
    ) -> tuple[list[str], dict[str, list[str]]]:
        """Переключает подкатегорию.

        Рубрика добавляется в выбор автоматически. Когда снята последняя
        подкатегория, рубрика выключается: иначе она осталась бы «выбрана
        целиком» и фильтр неожиданно пропускал бы все заказы рубрики.
        """
        categories = list(prefs.categories)
        subcats: dict[str, list[str]] = {k: list(v) for k, v in prefs.subcategories.items()}
        current = subcats.get(key, [])
        if sub_name in current:
            current.remove(sub_name)
        else:
            if key not in categories:
                categories.append(key)
            current.append(sub_name)
        if current:
            subcats[key] = current
        else:
            subcats.pop(key, None)
            categories = [c for c in categories if c != key]
        return categories, subcats

    def _uf_toggle_group(
        self, prefs: UserFilterPrefs, key: str, group_name: str
    ) -> tuple[list[str], dict[str, list[str]]] | None:
        """Переключает всю группу подкатегорий; None — группа не найдена."""
        from .categories import CATEGORY_BY_KEY

        category = CATEGORY_BY_KEY.get(key)
        if category is None:
            return None
        names = [
            item.name
            for group in category.groups
            if group.name == group_name
            for item in group.items
        ]
        categories = list(prefs.categories)
        subcats: dict[str, list[str]] = {k: list(v) for k, v in prefs.subcategories.items()}
        current = subcats.get(key, [])
        if names and all(name in current for name in names):
            current = [name for name in current if name not in names]
        else:
            if key not in categories:
                categories.append(key)
            current = current + [name for name in names if name not in current]
        if current:
            subcats[key] = current
        else:
            # Группа снята и подкатегорий в рубрике не осталось — выключаем её.
            subcats.pop(key, None)
            categories = [c for c in categories if c != key]
        return categories, subcats

    def _uf_save(
        self,
        chat_id: int,
        prefs: UserFilterPrefs,
        *,
        categories: list[str],
        subcategories: dict[str, list[str]],
    ) -> UserFilterPrefs:
        """Сохраняет рубрики/подкатегории и возвращает свежий снимок."""
        assert self.store is not None
        return self.store.set_user_filters(
            chat_id, categories=categories, subcategories=subcategories
        )

    # ------------------------------------------------------------------ /filters
    async def _cmd_filters(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает /filters: личные фильтры подписчика.

        Доступна каждому одобренному пользователю (_ensure_admin): это
        персональные настройки, общий конвейер они не трогают.
        """
        if not await self._ensure_admin(update) or not update.message:
            return
        if self.store is None:
            await self._reply(update, "🗄️ База недоступна: фильтры нельзя изменить.")
            return

        chat = update.effective_chat
        if chat is None:
            return
        chat_id = chat.id
        args = context.args
        if not args:
            prefs = self.store.get_user_filters(chat_id)
            text, markup = self._uf_view(prefs)
            await self._reply(update, text, parse_mode="Markdown", reply_markup=markup)
            return

        action = args[0].lower()
        if action == "platforms":
            prefs = self.store.get_user_filters(chat_id)
            text, markup = self._uf_platforms_view(prefs)
            await self._reply(update, text, parse_mode="Markdown", reply_markup=markup)
            return

        if action == "reset":
            # Полный сброс: снова показывается всё, что собирает бот.
            self.store.reset_user_filters(chat_id)
            prefs = self.store.get_user_filters(chat_id)
            text, markup = self._uf_view(prefs, "♻️ Фильтры сброшены: снова показывается всё.")
            await self._reply(update, text, parse_mode="Markdown", reply_markup=markup)
            return

        if action in {"price", "minprice"} and len(args) >= 2:
            try:
                value = float(args[1].replace(" ", "").replace(",", "."))
            except ValueError:
                await update.message.reply_text(
                    "❌ Нужно число, например `/filters price 5000`."
                )
                return
            if value < 0:
                await update.message.reply_text("⚠️ Цена не может быть отрицательной.")
                return
            prefs = self.store.set_user_filters(chat_id, min_price_rub=value)
            await update.message.reply_text(
                f"✅ Мин. цена личного фильтра: {_price_label(prefs.min_price_rub)}."
            )
            return

        await update.message.reply_text(
            "Формат: `/filters`, `/filters platforms`, `/filters price 5000`, `/filters reset`."
        )

    # ------------------------------------------------------------- callback'и фильтров
    async def _handle_uf_callback(
        self, update: Update, query: CallbackQuery, data: str
    ) -> bool:
        """Маршрутизирует callback'и меню личных фильтров (префикс ``uf_``).

        Возвращает True, если data обработан; False — префикс знакомый,
        хвост неизвестен (вызывающая сторона отдаст понятную ошибку).
        """
        chat = update.effective_chat
        if chat is None or self.store is None:
            await query.answer("База недоступна: фильтры нельзя изменить.", show_alert=True)
            return True
        chat_id = chat.id
        store = self.store

        if data == "uf_menu":
            prefs = store.get_user_filters(chat_id)
            text, markup = self._uf_view(prefs, self._persist_note())
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
            return True

        if data.startswith("uf_top_"):
            key = data[len("uf_top_"):]
            prefs = store.get_user_filters(chat_id)
            view = self._uf_sub_view(prefs, key)
            if view is None:
                await query.answer("Рубрика не найдена.", show_alert=True)
                return True
            await query.edit_message_text(view[0], parse_mode="Markdown", reply_markup=view[1])
            return True

        if data.startswith("uf_all_"):
            key = data[len("uf_all_"):]
            prefs = store.get_user_filters(chat_id)
            # Вся рубрика: снимаем подкатегории и включаем рубрику целиком.
            categories = [c for c in prefs.categories if c != key]
            if key not in categories:
                categories.append(key)
            subcats = {k: v for k, v in prefs.subcategories.items() if k != key}
            prefs = self._uf_save(chat_id, prefs, categories=categories, subcategories=subcats)
            view = self._uf_sub_view(prefs, key, self._persist_note())
            if view is None:
                await query.answer("Рубрика не найдена.", show_alert=True)
                return True
            await query.edit_message_text(view[0], parse_mode="Markdown", reply_markup=view[1])
            return True

        if data.startswith("uf_off_"):
            key = data[len("uf_off_"):]
            prefs = store.get_user_filters(chat_id)
            # Выключить рубрику: убираем её и её подкатегории из выбора.
            categories = [c for c in prefs.categories if c != key]
            subcats = {k: v for k, v in prefs.subcategories.items() if k != key}
            prefs = self._uf_save(chat_id, prefs, categories=categories, subcategories=subcats)
            view = self._uf_sub_view(prefs, key, self._persist_note())
            if view is None:
                await query.answer("Рубрика не найдена.", show_alert=True)
                return True
            await query.edit_message_text(view[0], parse_mode="Markdown", reply_markup=view[1])
            return True

        if data.startswith("uf_grp_"):
            # rpartition: ключ содержит подчёркивания (audio_video), индекс — хвост.
            rest = data[len("uf_grp_"):]
            key, _, group_index = rest.rpartition("_")
            prefs = store.get_user_filters(chat_id)
            group_name = self._uf_group_name(key, group_index)
            if group_name is None:
                await query.answer("Группа не найдена.", show_alert=True)
                return True
            changed = self._uf_toggle_group(prefs, key, group_name)
            if changed is None:
                await query.answer("Группа не найдена.", show_alert=True)
                return True
            categories, subcats = changed
            prefs = self._uf_save(chat_id, prefs, categories=categories, subcategories=subcats)
            view = self._uf_sub_view(prefs, key, self._persist_note())
            if view is None:
                await query.answer("Рубрика не найдена.", show_alert=True)
                return True
            await query.edit_message_text(view[0], parse_mode="Markdown", reply_markup=view[1])
            return True

        if data.startswith("uf_sub_"):
            # Два индекса справа: uf_sub_<key>_<группа>_<категория>.
            rest = data[len("uf_sub_"):]
            rest2, _, item_index = rest.rpartition("_")
            key, _, group_index = rest2.rpartition("_")
            prefs = store.get_user_filters(chat_id)
            sub_name = self._uf_item_name(key, group_index, item_index)
            if sub_name is None:
                await query.answer("Категория не найдена.", show_alert=True)
                return True
            categories, subcats = self._uf_toggle_sub(prefs, key, sub_name)
            prefs = self._uf_save(chat_id, prefs, categories=categories, subcategories=subcats)
            view = self._uf_sub_view(prefs, key, self._persist_note())
            if view is None:
                await query.answer("Рубрика не найдена.", show_alert=True)
                return True
            await query.edit_message_text(view[0], parse_mode="Markdown", reply_markup=view[1])
            return True

        if data.startswith("uf_platform_"):
            name = data[len("uf_platform_"):]
            if name not in self.config.platforms:
                await query.answer("Площадка не найдена.", show_alert=True)
                return True
            prefs = store.get_user_filters(chat_id)
            current = list(prefs.platforms)
            if name in current:
                current.remove(name)
            else:
                current.append(name)
            prefs = store.set_user_filters(chat_id, platforms=current)
            text, markup = self._uf_platforms_view(prefs, self._persist_note())
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
            return True

        if data == "uf_platforms":
            prefs = store.get_user_filters(chat_id)
            text, markup = self._uf_platforms_view(prefs)
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
            return True

        if data == "uf_price":
            # Ввод цены перехватывает _handle_text_input по пути UF_PRICE_INPUT.
            self._awaiting_input = UF_PRICE_INPUT
            self._awaiting_label = "мин. цена личного фильтра"
            await self._bot_send(
                chat_id=chat_id,
                text=(
                    "💰 Пришлите минимальную цену числом, например `5000`.\n"
                    "`0` — показывать заказы с любой ценой.\n\n"
                    "Заказы без цены (по договорённости) приходят всегда.\n"
                    "Отмена: /cancel"
                ),
                parse_mode="Markdown",
            )
            return True

        if data == "uf_reset":
            store.reset_user_filters(chat_id)
            prefs = store.get_user_filters(chat_id)
            text, markup = self._uf_view(
                prefs, "♻️ Фильтры сброшены: снова показывается всё."
            )
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
            return True

        return False

    # ------------------------------------------------------------- имена таксономии
    def _uf_group_name(self, key: str, group_index: str) -> str | None:
        """Имя группы по индексу callback_data; None — не найдена."""
        from .categories import CATEGORY_BY_KEY

        category = CATEGORY_BY_KEY.get(key)
        if category is None:
            return None
        try:
            gi = int(group_index)
        except ValueError:
            return None
        if not 0 <= gi < len(category.groups):
            return None
        return category.groups[gi].name

    def _uf_item_name(self, key: str, group_index: str, item_index: str) -> str | None:
        """Имя категории по индексам callback_data; None — не найдена."""
        from .categories import CATEGORY_BY_KEY

        category = CATEGORY_BY_KEY.get(key)
        if category is None:
            return None
        try:
            gi, ii = int(group_index), int(item_index)
        except ValueError:
            return None
        if not 0 <= gi < len(category.groups):
            return None
        group = category.groups[gi]
        if not 0 <= ii < len(group.items):
            return None
        return group.items[ii].name

    # ------------------------------------------------------------- ввод цены фильтра
    async def _apply_user_filters_input(
        self, path: str, text: str, update: Update
    ) -> bool:
        """Обрабатывает ввод цены личного фильтра после кнопки «Мин. цена».

        Возвращает True, если путь относится к личным фильтрам и обработан;
        False — путь неизвестен (его обработает общая _apply_value).
        """
        if path != UF_PRICE_INPUT or self.store is None:
            return False
        if update.message is None:
            return True
        try:
            value = float(text.strip().replace(" ", "").replace(",", "."))
        except ValueError:
            await update.message.reply_text(
                "❌ Нужно число, например `5000`. Попробуйте ещё раз: кнопка "
                "«💰 Мин. цена» в /filters.",
                parse_mode="Markdown",
            )
            return True
        if value < 0:
            await update.message.reply_text("⚠️ Цена не может быть отрицательной.")
            return True
        chat_id = update.effective_chat.id if update.effective_chat else 0
        prefs = self.store.set_user_filters(chat_id, min_price_rub=value)
        await update.message.reply_text(
            f"✅ Мин. цена личного фильтра: {_price_label(prefs.min_price_rub)}. "
            "Заказы дешевле порога приходить не будут."
        )
        return True

    # ------------------------------------------------------------------ избранное
    async def _handle_favorite_callback(
        self, update: Update, query: CallbackQuery, data: str
    ) -> None:
        """Обрабатывает кнопки избранного «♥ Выделить…» / «💔 Убрать…».

        Формат: ``fav:<uid>`` / ``unfav:<uid>``. Избранное личное: ключ —
        чат, как и у фильтров. После переключения меняем подпись кнопки
        в самом сообщении, чтобы состояние было видно без повторного /jobs.
        """
        action, _, job_uid = data.partition(":")
        chat = update.effective_chat
        if not job_uid or chat is None:
            await query.answer("⚠️ Неверный формат команды.", show_alert=True)
            return
        if self.store is None:
            await query.answer("🗄️ База заказов недоступна.", show_alert=True)
            return

        user_id = chat.id
        if action == "fav":
            added = self.store.add_favorite(user_id, job_uid)
            popup = "♥ Добавлено в избранное" if added else "Уже в избранном ♥"
        elif action == "unfav":
            removed = self.store.remove_favorite(user_id, job_uid)
            popup = "💔 Убрано из избранного" if removed else "Нет в избранном"
        else:
            await query.answer("⚠️ Неизвестное действие.", show_alert=True)
            return

        actor = update.effective_user.id if update.effective_user else user_id
        try:
            self.store.log_action(actor, job_uid, action, {})
        except Exception:  # noqa: BLE001 - журнал не должен ломать кнопку
            log.debug("log_action failed for %s", job_uid, exc_info=True)

        await query.answer(popup)
        await self._flip_favorite_button(query, data)

    async def _flip_favorite_button(self, query: CallbackQuery, data: str) -> None:
        """Переворачивает кнопку избранного в сообщении, откуда пришёл callback.

        Правим только саму кнопку: «Перейти на страницу заказа» и «Составить
        письмо-ответ» остаются нетронутыми. Старые сообщения Telegram править
        уже нельзя — ошибка уходит в журнал, состояние в базе верное.
        """
        action, _, job_uid = data.partition(":")
        new_action = "unfav" if action == "fav" else "fav"
        # fav добавил заказ в избранное → кнопка предлагает убрать, и наоборот.
        new_label = FAV_ON_LABEL if action == "fav" else FAV_OFF_LABEL
        message = query.message
        markup = getattr(message, "reply_markup", None) if message is not None else None
        rows = getattr(markup, "inline_keyboard", None) if markup is not None else None
        if not rows:
            return
        changed = False
        new_rows: list[list[InlineKeyboardButton]] = []
        for row in rows:
            new_row: list[InlineKeyboardButton] = []
            for btn in row:
                if btn.callback_data == data:
                    changed = True
                    new_row.append(
                        InlineKeyboardButton(
                            new_label, callback_data=f"{new_action}:{job_uid}"
                        )
                    )
                else:
                    new_row.append(btn)
            new_rows.append(new_row)
        if not changed:
            return
        try:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_rows))
        except TelegramError as e:
            log.debug("cannot flip favorite button for %s: %s", job_uid, e)

    # ------------------------------------------------------------- доставка карточек
    async def deliver_job(
        self,
        chat_ids: Iterable[int],
        scored: ScoredJob,
        message_text: str | None = None,
        *,
        price: float | None = None,
        strategy: str | None = None,
        analysis: JobAnalysis | None = None,
        message_id: int | None = None,
    ) -> list[int]:
        """Отправляет карточку заказа каждому чату из ``chat_ids``.

        Конвейер вызывает метод один раз на заказ, передав список чатов,
        чьим личным фильтрам заказ соответствует. Возвращает список чатов,
        куда карточка реально ушла: пустой список означает «бот не работает»,
        и конвейер повторит доставку в следующем проходе.

        Вёрстка карточки — в :mod:`card_render`; кнопки эталона:
          ряд 1 — [🔗 Перейти на страницу заказа]
          ряд 2 — [🪄 Составить письмо-ответ с AI]
          ряд 3 — [♥ Выделить как понравившееся] / [💔 Убрать из избранного]

        Письмо в карточку больше не вставляется: его собирает AI-квиз по
        ответам пользователя, поэтому черновик остаётся в базе до запроса.
        ``price``, ``strategy`` и ``analysis`` сохранены в сигнатуре ради
        конвейера: они больше не влияют на вёрстку, но их передача не должна
        требовать правок в ``pipeline``.
        """
        # Отложенный импорт: telegram_bot импортирует этот модуль (mixin),
        # поэтому общий код карточек недоступен на уровне импорта.
        from .telegram_bot import TelegramNotification, _chunk, _plain_text

        if not self.app or not self.is_running or not chat_ids:
            return []

        job = scored.job

        # order_url валидирован order_links.safe_order_url; без него кнопка
        # ведёт по исходному URL, но карточку всё равно отправляем.
        order_url = job.order_url or job.url
        if job.order_url is None:
            log.warning(
                "order_url missing for %s (%s), fallback: %s",
                job.uid, job.platform, job.url,
            )

        # Отрасль для шапки карточки: классификация детерминированная и
        # дешёвая, поэтому считается здесь, не расширяя сигнатуру pipeline.
        classification = classify_job(job)
        text = render_job_card(
            job,
            category_key=classification.category_key,
            category_name=classification.category_name,
            timezone=self.config.runtime.timezone,
        )

        if message_text and self.store:
            # Черновик живёт в БД: кнопки AI-квиза работают после рестарта.
            self.store.set_draft(job.uid, message_text)
        if message_text:
            self.pending_approvals[job.uid] = TelegramNotification(
                job=job,
                score_total=scored.score.total,
                score_summary=scored.score.summary_ru,
                message_text=message_text,
                approved=None,
                message_id=message_id,
            )
        else:
            log.info("deliver_job for %s: черновика нет, письмо по кнопке AI", job.uid)

        delivered: list[int] = []
        # Подсказка «Полезно знать» — второе сообщение эталона: в карточке
        # причин показа нет, но знать, почему заказ прислали, полезно.
        hint = render_hint(scored.score.summary_ru, job=job)
        for chat_id in chat_ids:
            # Клавиатура зависит от чата: состояние ♥ у каждого своё, поэтому
            # собирается внутри цикла, а не один раз на заказ.
            is_favorite = bool(self.store and self.store.is_favorite(chat_id, job.uid))
            keyboard = card_keyboard(
                job,
                order_url=order_url,
                is_favorite=is_favorite,
                has_draft=bool(message_text),
            )
            # Клавиатура крепится к последнему фрагменту, как в _reply.
            chunks = _chunk(text)
            ok = True
            for index, chunk in enumerate(chunks):
                markup = keyboard if index == len(chunks) - 1 else None
                try:
                    await self._bot_send(
                        chat_id=chat_id, text=chunk, parse_mode="HTML", reply_markup=markup
                    )
                except TelegramError as e:
                    if "parse entities" not in str(e).lower():
                        log.error("failed to deliver card %s to %s: %s", job.uid, chat_id, e)
                        ok = False
                        break
                    # Битая разметка (например, тег разрезан границей chunks)
                    # не должна терять заказ: повторяем чистым текстом без тегов.
                    log.warning(
                        "card %s markup rejected, resending plain text: %s", job.uid, e
                    )
                    try:
                        await self._bot_send(
                            chat_id=chat_id,
                            text=_plain_text(chunk),
                            reply_markup=markup,
                        )
                    except TelegramError as e2:
                        log.error(
                            "failed to deliver plain card %s to %s: %s", job.uid, chat_id, e2
                        )
                        ok = False
                        break
            if ok:
                delivered.append(chat_id)
                if hint:
                    try:
                        await self._bot_send(
                            chat_id=chat_id, text=hint, parse_mode="HTML"
                        )
                    except TelegramError as e:
                        # Подсказка вторична: её сбой не отменяет доставку заказа.
                        log.debug("cannot send hint for %s to %s: %s", job.uid, chat_id, e)
        return delivered

