"""Команды Telegram для полной настройки бота без правки YAML вручную.

Модуль подмешивается в :class:`freelance_bot.telegram_bot.TelegramBotController`.
Все изменения сразу применяются к объектам ``AppConfig``/``FreelancerProfile``,
которые конвейер читает на каждом цикле, и сохраняются в оверлей
``var/runtime_settings.yaml`` через :class:`~freelance_bot.settings_store.SettingsStore`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

if TYPE_CHECKING:
    from .config import AppConfig, Secrets
    from .profile import FreelancerProfile
    from .settings_store import SettingsStore

log = logging.getLogger(__name__)

MAX_PREVIEW = 1200

# Разделы, открытые любому допущенному пользователю: там только его личные
# данные и заказы. Всё, что меняет поведение бота для всех (конвейер, письма,
# профиль исполнителя, платформы), требует роли администратора.
SELF_SERVICE_TOPICS: frozenset[str] = frozenset(
    {
        "settings",
        "hub",
        "cat_filters",
        "platform_filters",
        "monitoring",
        "filters",
        "categories_user",
        "platforms_user",
    }
)


def _md(value: object) -> str:
    """Экранирование динамического текста для legacy-Markdown Telegram."""
    text = str(value)
    for char in ("\\", "_", "*", "[", "]", "`"):
        text = text.replace(char, "\\" + char)
    return text


def _parse_flag(value: str) -> bool | None:
    text = value.strip().lower()
    if text in {"on", "вкл", "включить", "включено", "1", "true", "да", "yes", "enable"}:
        return True
    if text in {"off", "выкл", "выключить", "выключено", "0", "false", "нет", "no", "disable"}:
        return False
    return None


def _split_items(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,;\n]", text) if item.strip()]


def _lookup(obj: Any, parts: list[str]) -> Any:
    current = obj
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(part)
            current = current[part]
        elif hasattr(current, part):
            current = getattr(current, part)
        else:
            raise KeyError(part)
    return current


def _assign(obj: Any, parts: list[str], value: Any) -> None:
    parent = _lookup(obj, parts[:-1])
    key = parts[-1]
    if isinstance(parent, dict):
        parent[key] = value
    else:
        setattr(parent, key, value)


def _coerce(value: str, current: Any) -> Any:
    """Приводит текст команды к типу текущего значения поля."""
    text = value.strip()
    if isinstance(current, bool):
        flag = _parse_flag(text)
        if flag is None:
            raise ValueError("ожидается on/off, true/false или 1/0")
        return flag
    if isinstance(current, int):
        return int(text.replace(" ", "").replace("_", ""))
    if isinstance(current, float):
        return float(text.replace(",", "."))
    if isinstance(current, list):
        if text.startswith("["):
            parsed = json.loads(text)
            if not isinstance(parsed, list):
                raise ValueError("ожидается JSON-массив")
            return parsed
        return _split_items(text)
    if isinstance(current, dict):
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("ожидается JSON-объект")
        return parsed
    return value


class SettingsCommandsMixin:
    """Набор ``/``-команд, меняющих конфиг, профиль, промпты и модели."""

    config: AppConfig
    profile: FreelancerProfile | None
    secrets: Secrets | None
    settings: SettingsStore | None
    interval_seconds: int
    profile_path: str
    _awaiting_input: str | None
    _awaiting_label: str

    # Реализации ниже предоставляет TelegramBotController; заглушки нужны
    # только для проверки типов миксина.
    async def _ensure_admin(self, update: Update) -> bool:  # pragma: no cover - abstract
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

    def _is_admin_update(self, update: Update) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    # Роль супер-админа и хук личных фильтров живут в других частях
    # контроллера (TelegramBotController и UserFiltersCommandsMixin); оба
    # стоят раньше этого mixin'а в MRO, поэтому заглушки не перекроют их.
    async def _ensure_super_admin(self, update: Update) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def _is_super_admin_update(self, update: Update) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    async def _deny_insufficient_rights(self, update: Update) -> None:  # pragma: no cover
        raise NotImplementedError

    async def _apply_user_filters_input(
        self, path: str, text: str, update: Update
    ) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    # ------------------------------------------------------------------ служебное
    def _persist_note(self) -> str:
        if self.settings is None:
            return ""
        try:
            self.settings.persist(self.config, self.profile)
        except OSError as exc:  # pragma: no cover - зависит от ФС
            log.exception("Failed to persist runtime settings")
            return f"\n⚠️ Не удалось сохранить на диск: {_md(exc)}"
        return "\n💾 Сохранено."

    def _profile_model(self) -> FreelancerProfile:
        if self.profile is None:
            from .profile import FreelancerProfile, load_profile

            try:
                self.profile = load_profile(self.profile_path)
            except Exception:  # noqa: BLE001 - битый профиль не должен ломать бота
                self.profile = FreelancerProfile()
        return self.profile

    # ------------------------------------------------------------------ /set, /get
    async def _cmd_set(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_super_admin(update) or not update.message:
            return
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text(
                "🛠 *Изменение любого параметра*\n\n"
                "`/set <путь> <значение>`\n"
                "`/set scoring.red_flag_penalty 0.3`\n"
                "`/set runtime.dry_run false`\n"
                "`/set profile.pricing.min_acceptable_rub 6000`\n"
                "`/set messaging.limits.fl_ru '{\"max_chars\": 2500, \"allow_links\": false}'`\n\n"
                "Списки задаются через запятую: `/set scoring.skill_match.skills python, fastapi`.\n"
                "Посмотреть значение: `/get <путь>`.",
                parse_mode="Markdown",
            )
            return

        path = args[0]
        raw_value = " ".join(args[1:])
        ok, message = self._apply_value(path, raw_value)
        await update.message.reply_text(
            message + (self._persist_note() if ok else ""), parse_mode="Markdown"
        )

    async def _cmd_get(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_super_admin(update) or not update.message:
            return
        args = context.args or []
        if not args:
            await update.message.reply_text(
                "Формат: `/get scoring.red_flag_penalty`", parse_mode="Markdown"
            )
            return
        path = args[0]
        try:
            target, parts = self._resolve_target(path)
            value = _lookup(target, parts)
        except (KeyError, AttributeError, IndexError):
            await update.message.reply_text(f"❌ Путь `{_md(path)}` не найден.", parse_mode="Markdown")
            return
        rendered = yaml.safe_dump(value, allow_unicode=True, sort_keys=False).strip()
        await update.message.reply_text(
            f"📄 `{_md(path)}`\n```\n{_md(rendered[:3500])}\n```",
            parse_mode="Markdown",
        )

    def _resolve_target(self, path: str) -> tuple[Any, list[str]]:
        clean = path.strip().strip(".")
        if clean.startswith("profile."):
            return self._profile_model(), clean[len("profile.") :].split(".")
        return self.config, clean.split(".")

    def _apply_value(self, path: str, raw_value: str) -> tuple[bool, str]:
        """Меняет значение по dotted-пути, проверяя модель целиком."""
        try:
            target, parts = self._resolve_target(path)
            if not parts or not parts[0]:
                return False, "❌ Пустой путь."
            current = _lookup(target, parts)
            value = _coerce(raw_value, current)
            old = current
            _assign(target, parts, value)
        except KeyError:
            return False, f"❌ Путь `{_md(path)}` не найден."
        except (ValueError, json.JSONDecodeError) as exc:
            return False, f"❌ Неверное значение: {_md(exc)}"

        try:
            if target is self.config:
                from .config import AppConfig

                AppConfig.model_validate(self.config.model_dump())
            else:
                from .profile import FreelancerProfile

                FreelancerProfile.model_validate(target.model_dump())
        except ValidationError as exc:
            _assign(target, parts, old)
            errors = exc.errors()
            detail = str(errors[0].get("msg")) if errors else "недопустимое значение"
            return False, f"❌ Модель отклонила значение: {_md(detail)}"

        rendered = yaml.safe_dump(value, allow_unicode=True, sort_keys=False).strip()
        return True, f"✅ `{_md(path)}` = `{_md(rendered[:200])}`"

    # ------------------------------------------------------------------ /settings
    # Разделы настроек в порядке показа. «Настройки» — хаб разделов, остальные
    # многоточечные записи открывают первый экран соответствующего раздела.
    # Записи с admin_only=True не показываются обычным подписчикам вовсе (они
    # не «серые» — их нет в клавиатуре), а их хендлеры дополнительно закрыты
    # проверкой _ensure_super_admin.
    SETTINGS_SECTIONS: tuple[tuple[str, str, str, bool], ...] = (
        ("main", "⚙️ Основные", "settings:main", True),
        ("admin", "🛠 Администрирование", "settings:admin", True),
        ("letters", "✉️ Письма", "settings:letters", True),
        ("profile", "👤 Профиль", "settings:profile", True),
        ("settings", "🔧 Настройки", "settings:hub", False),
        ("cat_filters", "📂 Фильтры категорий", "uf_menu", False),
        ("platform_filters", "🌐 Фильтры бирж", "uf_platforms", False),
        ("monitoring", "📡 Мониторинг заказов", "settings:monitoring", False),
    )

    async def _cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Открывает меню настроек: подписчику — только его разделы.

        Доступ к общим настройкам (конвейер, письма, профиль, платформы)
        проверяется _ensure_super_admin, поэтому обычный допущенный
        пользователь видит лишь «Настройки», «Фильтры категорий»,
        «Фильтры бирж» и «Мониторинг заказов».
        """
        if not await self._ensure_admin(update) or not update.message:
            return

        topic = context.args[0].lower() if context.args else ""
        if topic in {"all", "dump", "full", "yaml"}:
            if not await self._ensure_super_admin(update):
                return
            payload = {
                "config": self.config.model_dump(mode="json"),
                "profile": self._profile_model().model_dump(mode="json"),
            }
            text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
            await self._reply(update, f"⚙️ *Эффективные настройки*\n```\n{_md(text)}\n```", parse_mode="Markdown")
            return

        if topic:
            # Явно запрошенный раздел: админские темы закрыты проверкой прав.
            if not self._topic_allowed(topic, update):
                await self._deny_insufficient_rights(update)
                return
            text = self._settings_section(topic)
            if text is None:
                text = (
                    f"❌ Неизвестный раздел `{_md(topic)}`.\n"
                    "Доступно: runtime, scoring, categories, messaging, llm, platforms, "
                    "profile, skills, prompt, все — `/settings all`."
                )
            await self._reply(update, text, parse_mode="Markdown")
            return

        text, markup = self._settings_hub(update)
        await self._reply(update, text, parse_mode="Markdown", reply_markup=markup)

    def _topic_allowed(self, topic: str, update: Update) -> bool:
        """True, если раздел можно открыть этому пользователю.

        Админскими считаются темы конвейера и настроек бота: они меняют
        поведение для всех. Личные разделы доступны любому допущенному.
        """
        if topic in SELF_SERVICE_TOPICS:
            return True
        return self._is_super_admin_update(update)

    def _settings_hub(self, update: Update) -> tuple[str, InlineKeyboardMarkup]:
        """Строит текст и клавиатуру меню настроек по роли пользователя."""
        is_admin = self._is_super_admin_update(update)
        rows: list[list[InlineKeyboardButton]] = []
        for _key, label, callback, admin_only in self.SETTINGS_SECTIONS:
            if admin_only and not is_admin:
                continue
            rows.append([InlineKeyboardButton(label, callback_data=callback)])

        if is_admin:
            header = "⚙️ *Настройки*"
            note = "Полный доступ: все разделы бота."
        else:
            header = "⚙️ *Настройки*"
            note = (
                "Ваши разделы: заказы и личные фильтры. "
                "Общие настройки бота доступны администратору."
            )
        text = f"{header}\n\n{note}\n\nВыберите раздел:"
        return text, InlineKeyboardMarkup(rows)

    def _settings_overview(self) -> str:
        cfg = self.config
        profile = self._profile_model()
        cats = ", ".join(cfg.category_filter.categories) or "все"
        platforms = [name for name, item in cfg.platforms.items() if item.enabled]
        price = f"{profile.pricing.min_acceptable_rub:,.0f}".replace(",", " ")
        from .llm.client import resolve_provider

        if self.secrets is not None:
            provider = resolve_provider(cfg.llm, self.secrets)
            provider_label = f"{provider.name} / {provider.model}"
        else:
            provider_label = "—"
        return (
            "⚙️ *Настройки бота*\n\n"
            f"*Режим:* {'🧪 dry-run' if cfg.runtime.dry_run else '🚀 live'} | "
            f"одобрение: {'да' if cfg.messaging.require_human_approval else 'нет'}\n"
            f"*Интервал:* {self.interval_seconds} с (допустимо 600-3600)\n"
            f"*Приоритет:* штраф {cfg.scoring.red_flag_penalty:g}, "
            f"свежесть {cfg.scoring.freshness_half_life_hours:g} ч (порогов балла нет)\n"
            f"*Категории:* {'✅' if cfg.category_filter.enabled else '❌'} {_md(cats)}\n"
            f"*Платформы ({len(platforms)}):* {_md(', '.join(platforms)) or '—'}\n"
            f"*LLM:* {'✅' if cfg.llm.enabled else '❌'} {_md(provider_label)}\n"
            f"*Профиль:* навыков {len(profile.skills)}, цена от {price} ₽\n"
            "\n*Разделы:* `/settings runtime|scoring|categories|messaging|llm|platforms|profile|skills|prompt`\n"
            "*Полный дамп:* `/settings all`"
        )

    def _settings_section(self, topic: str) -> str | None:
        cfg = self.config
        # Составные разделы меню настроек: собираются из готовых блоков ниже,
        # чтобы один и тот же текст не дублировался в двух местах.
        if topic == "main":
            return (
                self._settings_section("runtime") or ""
            ) + "\n\n" + (self._settings_section("scoring") or "")
        if topic == "admin":
            return (
                "🛠 *Администрирование*\n\n"
                "*Платформы и категории сбора*\n"
                "/platforms — включить, выключить, добавить площадку\n"
                "/categories — общий фильтр сбора\n"
                "/interval — интервал циклов (600-3600 с)\n"
                "/score — штраф за красные флаги и свежесть\n"
                "/pause, /resume — пауза и возобновление сбора\n\n"
                "*Доступ и заявки*\n"
                "/users — кто имеет доступ и какие заявки ждут решения\n"
                "/adduser ID — выдать доступ\n"
                "/deluser ID — убрать доступ\n"
                "/set_admin ID — назначить администратора настроек\n"
                "/unset_admin ID — снять администратора настроек\n\n"
                "*Служебное*\n"
                "/set, /get — изменить и посмотреть параметр\n"
                "/reload — перечитать файлы, /reset — сбросить оверлей\n"
                "/export — выгрузить настройки в YAML\n"
                "/logs — последние ошибки конвейера"
            )
        if topic == "letters":
            return (
                "✉️ *Письма*\n\n"
                "*Генерация*\n"
                "/llm — модель, endpoint, ключ, temperature, провайдеры\n"
                "/prompt — системный промпт и дополнительные инструкции\n"
                "/style — тон, длина, эмодзи, запрещённые фразы\n\n"
                "*Отправка*\n"
                "/send — dry-run, одобрение, лимиты, пауза, письма за цикл\n"
                "/pricing — стратегия цены\n"
                "/markup N — коэффициент наценки\n"
                "/minprice N — минимальная цена письма\n\n"
                + (self._settings_section("llm") or "")
            )
        if topic == "hub":
            return self._settings_overview()
        if topic == "monitoring":
            return (
                "📡 *Мониторинг заказов*\n\n"
                "*Заказы по вашим фильтрам*\n"
                "/jobs [5|10|30] — последние заказы\n"
                "/favorites — избранные заказы (♥ на карточке)\n\n"
                "*Статистика*\n"
                "/status — статус сбора и последний цикл\n"
                "/stats — статистика за 7 дней\n\n"
                "*Что настроить, чтобы заказов было больше*\n"
                "/filters — рубрики, подкатегории, биржи, мин. цена\n"
                "Фильтры личные: расширение не влияет на других подписчиков."
            )
        if topic == "runtime":
            return (
                "⏱️ *Runtime*\n\n"
                f"dry\\_run: `{cfg.runtime.dry_run}`\n"
                f"интервал: `{self.interval_seconds}` с (границы 600-3600, меняет "
                "только админ командой `/interval`)\n"
                f"cycle\\_report: `{cfg.runtime.cycle_report}` — сводка «цикл "
                "завершён»; выключена, чтобы в чат приходили только заказы\n"
                f"max\\_jobs\\_per\\_platform: `{cfg.runtime.max_jobs_per_platform}`\n"
                f"timeout: `{cfg.runtime.request_timeout_seconds}` с\n\n"
                "Изменить: `/set runtime.dry_run false`, `/interval 600`, "
                "`/set runtime.cycle_report true`."
            )
        if topic == "scoring":
            return (
                "🎯 *Приоритет показа заказов*\n\n"
                f"red\\_flag\\_penalty: `{cfg.scoring.red_flag_penalty}`\n"
                f"свежесть (half-life): `{cfg.scoring.freshness_half_life_hours}` ч\n\n"
                "Порогов балла и совпадения навыков нет: показывается всё, что "
                "прошло фильтры. Штраф и старые заказы понижают приоритет — "
                "они уходят после свежих без штрафов.\n\n"
                "Команды (только админ): `/score penalty 0.35`, `/score freshness 18`."
            )
        if topic == "categories":
            cat = cfg.category_filter
            price = f"{cat.min_price_rub:,.0f} ₽".replace(",", " ") if cat.min_price_rub > 0 else "выключена"
            lines = [
                "📂 *Фильтр категорий*",
                "",
                f"Включён: `{cat.enabled}`",
                f"Категории: `{', '.join(cat.categories) or 'все'}`",
                f"Мин. цена: `{price}` (без цены — всегда показываются)",
                f"Мин. уверенность: `{cat.min_confidence}`",
                f"Неизвестные: `{cat.unknown_policy}`",
            ]
            if cat.subcategories:
                lines.append("Подкатегории:")
                for key, items in cat.subcategories.items():
                    lines.append(f"• {key}: {', '.join(items)}")
            lines.append(
                "\nКоманды: `/categories add programming`, `/categories rm design`, "
                "`/categories price 5000`, `/categories sub list`, `/categories conf 0.3`."
            )
            return "\n".join(lines)
        if topic == "messaging":
            msg = cfg.messaging
            lines = [
                "📨 *Режим отправки*",
                "",
                f"dry\\_run: `{cfg.runtime.dry_run}`",
                f"одобрение каждого письма: `{msg.require_human_approval}`",
                f"писем в час: `{msg.max_sends_per_hour}`",
                f"пауза между письмами: `{msg.min_seconds_between_sends}` с",
                f"писем за цикл: `{msg.max_messages_per_cycle}`",
                "",
                "*Лимиты площадок:*",
            ]
            for name, limit in msg.limits.items():
                lines.append(
                    f"• {name}: {limit.max_chars} симв., ссылки: {'да' if limit.allow_links else 'нет'}"
                )
            lines.append("\nКоманды: `/send dry off`, `/send limit 6`, `/send chars kwork_ru 1000`.")
            return "\n".join(lines)
        if topic == "llm":
            llm = cfg.llm
            from .llm.client import resolve_provider

            if self.secrets is not None:
                resolved = resolve_provider(llm, self.secrets)
                model = resolved.model
                base = resolved.base_url
                has_key = bool(resolved.api_key)
                provider_label = resolved.name
            else:
                model = base = "—"
                has_key = False
                provider_label = llm.active_provider or "default"
            prompt_state = "свой промпт" if llm.system_prompt.strip() else "встроенный"
            providers = ", ".join(sorted(llm.providers)) or "—"
            return (
                "🤖 *LLM и модель*\n\n"
                f"включён: `{llm.enabled}`\n"
                f"провайдер: `{_md(provider_label)}`\n"
                f"модель: `{_md(model)}`\n"
                f"endpoint: `{_md(base)}`\n"
                f"ключ: {'задан' if has_key else 'не задан'}\n"
                f"сохранённые провайдеры: {_md(providers)}\n"
                f"temperature: `{llm.temperature}`\n"
                f"max\\_tokens: `{llm.max_output_tokens}`\n"
                f"timeout: `{llm.timeout_seconds}` с\n"
                f"fallback: `{llm.fallback_enabled}`\n"
                f"промпт: `{prompt_state}`\n\n"
                "Команды: `/llm provider list`, `/llm provider add <имя> <url> <модель>`, "
                "`/llm provider use <имя>`, `/llm model <name>`, `/llm key <ключ>`, `/llm test`."
            )
        if topic == "platforms":
            return "🌐 *Платформы*\n\n" + self._platforms_text()
        if topic == "profile":
            profile = self._profile_model()
            return (
                "👤 *Профиль*\n\n"
                f"имя: `{_md(profile.name or '—')}`\n"
                f"опыт: `{profile.experience_years}` лет\n"
                f"локация: `{_md(profile.location)}`\n"
                f"стратегия цены: `{profile.pricing.strategy}`\n"
                f"наценка: `{profile.pricing.markup_percent}`\n"
                f"минимум: `{profile.pricing.min_acceptable_rub:.0f}` ₽\n"
                f"ставка: `{profile.pricing.hourly_rate_rub:.0f}` ₽/час\n"
                f"цена в письме: `{profile.pricing.include_price_in_message}`\n\n"
                "Команды: `/profile name Иван`, `/profile summary`, `/pricing`, `/markup 1.1`, `/minprice 6000`."
            )
        if topic == "skills":
            profile = self._profile_model()
            return (
                "🧩 *Навыки*\n\n"
                f"*Профиль (для писем):* {_md(', '.join(profile.skills)) or '—'}\n"
                f"*Дополнительно:* {_md(', '.join(profile.secondary_skills)) or '—'}\n\n"
                "Порогов совпадения навыков больше нет: навыки профиля нужны "
                "для генерации писем.\n"
                "Команды: `/skills add python,fastapi`, `/skills add2 react`."
            )
        if topic == "prompt":
            from .llm.prompts import SYSTEM_PROMPT, build_system_prompt

            llm = cfg.llm
            system = build_system_prompt(llm)
            state = "свой" if llm.system_prompt.strip() else "встроенный"
            return (
                f"✍️ *Промпты* (системный: {state})\n\n"
                f"*system\\_prompt:*\n```\n{_md(system[:MAX_PREVIEW])}\n```\n"
                f"*extra\\_instructions:* `{_md(llm.extra_instructions[:500]) or '—'}`\n\n"
                f"Встроенный промпт: {len(SYSTEM_PROMPT)} символов.\n"
                "Изменить: `/prompt system` (пришлите текст следующим сообщением), "
                "`/prompt extra ...`, `/prompt reset`."
            )
        return None

    # ------------------------------------------------------------------ /platforms
    def _platforms_text(self) -> str:
        from .adapters.registry import DEFAULT_LISTING_URLS

        lines: list[str] = []
        for name in sorted(self.config.platforms):
            cfg = self.config.platforms[name]
            status = "✅ включена" if cfg.enabled else "❌ выключена"
            lines.append(
                f"*{_md(name)}* — {status}\n"
                f"   транспорт: {_md(cfg.transport)}, URL: {_md(cfg.listing_url)}"
            )
        missing = [name for name in DEFAULT_LISTING_URLS if name not in self.config.platforms]
        if missing:
            lines.append("\n➕ *Доступны для добавления:* " + _md(", ".join(sorted(missing))))
        lines.append(
            "\nКоманды: `/platforms add <name>`, `/platforms rm <name>`, "
            "`/platforms url <name> <url>`, `/platforms on|off <name>`."
        )
        return "\n".join(lines)

    def _platforms_markup(self) -> InlineKeyboardMarkup:
        from .adapters.registry import DEFAULT_LISTING_URLS

        rows: list[list[InlineKeyboardButton]] = []
        for name in sorted(self.config.platforms):
            enabled = self.config.platforms[name].enabled
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{'✅' if enabled else '❌'} {name}",
                        callback_data=f"platform_toggle_{name}",
                    ),
                    InlineKeyboardButton("🗑", callback_data=f"platform_remove_{name}"),
                ]
            )
        for name in sorted(DEFAULT_LISTING_URLS):
            if name not in self.config.platforms:
                rows.append(
                    [InlineKeyboardButton(f"➕ {name}", callback_data=f"platform_add_{name}")]
                )
        return InlineKeyboardMarkup(rows)

    async def _cmd_platforms(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_super_admin(update) or not update.message:
            return

        args = context.args
        if not args:
            await self._reply(
                update,
                "🌐 *Платформы*\n\n" + self._platforms_text(),
                parse_mode="Markdown",
                reply_markup=self._platforms_markup(),
            )
            return

        action = args[0].lower()
        if action in {"add", "rm", "remove", "del", "delete"} and len(args) >= 2:
            name = args[1].strip()
            from .adapters.registry import ADAPTERS, DEFAULT_LISTING_URLS

            if name not in ADAPTERS:
                await update.message.reply_text(
                    f"❌ Неизвестная платформа `{_md(name)}`.\n"
                    f"Доступно: {_md(', '.join(sorted(ADAPTERS)))}",
                    parse_mode="Markdown",
                )
                return
            if action == "add":
                if name in self.config.platforms:
                    self.config.platforms[name].enabled = True
                    text = f"✅ Платформа *{_md(name)}* уже была настроена — включена."
                else:
                    from .config import PlatformConfig

                    cls = ADAPTERS[name]
                    transport = getattr(getattr(cls, "transport", "http"), "value", "http")
                    url = DEFAULT_LISTING_URLS.get(name) or f"https://{cls.domain}/"
                    self.config.platforms[name] = PlatformConfig(
                        enabled=True, transport=str(transport), listing_url=url
                    )
                    text = f"✅ Платформа *{_md(name)}* добавлена и включена.\nURL: {_md(url)}"
            else:
                if name not in self.config.platforms:
                    await update.message.reply_text(f"ℹ️ Платформы `{_md(name)}` нет в конфиге.")
                    return
                del self.config.platforms[name]
                text = (
                    f"🗑 Платформа *{_md(name)}* удалена из конфига.\n"
                    f"Вернуть: `/platforms add {_md(name)}`."
                )
            await update.message.reply_text(text + self._persist_note(), parse_mode="Markdown")
            return

        if action in {"on", "off"} and len(args) >= 2:
            name = args[1]
            if name not in self.config.platforms:
                await update.message.reply_text(f"❌ Платформа `{_md(name)}` не настроена.")
                return
            self.config.platforms[name].enabled = action == "on"
            state = "включена" if action == "on" else "выключена"
            await update.message.reply_text(
                f"✅ Платформа *{_md(name)}* {state}." + self._persist_note(),
                parse_mode="Markdown",
            )
            return

        if action == "url" and len(args) >= 3:
            name, url = args[1], args[2]
            if name not in self.config.platforms:
                await update.message.reply_text(f"❌ Платформа `{_md(name)}` не настроена.")
                return
            if not url.startswith(("http://", "https://")):
                await update.message.reply_text("❌ URL должен начинаться с http:// или https://")
                return
            self.config.platforms[name].listing_url = url
            await update.message.reply_text(
                f"✅ URL *{_md(name)}*: {_md(url)}" + self._persist_note(),
                parse_mode="Markdown",
            )
            return

        await update.message.reply_text(
            "Формат: `/platforms`, `/platforms add <name>`, `/platforms rm <name>`, "
            "`/platforms on|off <name>`, `/platforms url <name> <url>`.",
            parse_mode="Markdown",
        )

    def _platform_add(self, name: str) -> tuple[bool, str]:
        """Добавляет/включает площадку из реестра адаптеров."""
        from .adapters.registry import ADAPTERS, DEFAULT_LISTING_URLS

        if name not in ADAPTERS:
            return False, f"❌ Неизвестная платформа `{_md(name)}`."
        if name in self.config.platforms:
            self.config.platforms[name].enabled = True
            return True, f"✅ Платформа *{_md(name)}* включена."
        from .config import PlatformConfig

        cls = ADAPTERS[name]
        transport = getattr(getattr(cls, "transport", "http"), "value", "http")
        url = DEFAULT_LISTING_URLS.get(name) or f"https://{cls.domain}/"
        self.config.platforms[name] = PlatformConfig(
            enabled=True, transport=str(transport), listing_url=url
        )
        return True, f"✅ Платформа *{_md(name)}* добавлена и включена.\nURL: {_md(url)}"

    def _platform_remove(self, name: str) -> tuple[bool, str]:
        if name not in self.config.platforms:
            return False, f"ℹ️ Платформы `{_md(name)}` нет в конфиге."
        del self.config.platforms[name]
        return True, (
            f"🗑 Платформа *{_md(name)}* удалена из конфига.\n"
            f"Вернуть: `/platforms add {_md(name)}`."
        )

    def _platforms_view(self, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
        text = "🌐 *Платформы*\n\n" + self._platforms_text()
        if note:
            text += "\n\n" + note
        return text, self._platforms_markup()

    # ------------------------------------------------------------------ /categories
    def _category_mark(self, key: str) -> str:
        """Отметка состояния рубрики: вся выбрана / частично / не выбрана."""
        cat = self.config.category_filter
        if key not in (cat.categories or []):
            return "⬜"
        if cat.subcategories.get(key):
            return "🔸"
        return "✅"

    def _min_price_label(self) -> str:
        value = self.config.category_filter.min_price_rub
        if value <= 0:
            return "выключена"
        return f"{value:,.0f} ₽".replace(",", " ")

    def _categories_text(self) -> str:
        from .categories import CATEGORIES

        cat = self.config.category_filter
        lines = [
            "📂 *Детальная настройка заказов*",
            "",
            f"Фильтр: {'✅ включён' if cat.enabled else '❌ выключен'}",
            f"Мин. цена: `{self._min_price_label()}` (без цены — всегда показываются)",
            f"Мин. уверенность: `{cat.min_confidence}` | неизвестные: `{cat.unknown_policy}`",
            "",
            "Нажмите рубрику, чтобы выбрать категории:",
            "",
        ]
        for item in CATEGORIES:
            mark = self._category_mark(item.key)
            chosen = cat.subcategories.get(item.key)
            if item.key not in (cat.categories or []):
                detail = "не выбрана"
            elif chosen:
                detail = f"{len(chosen)} из {len(item.subcategories)}"
            else:
                detail = "вся рубрика"
            lines.append(f"{mark} {item.name} — {detail}")
        if cat.subcategories:
            lines.append("\n*Выбранные категории:*")
            for key, items in cat.subcategories.items():
                lines.append(f"• {_md(key)}: {_md(', '.join(items))}")
        lines.append(
            "\nКоманды: `/categories on|off`, `/categories price 5000`, `/categories add <key>`, "
            "`/categories rm <key>`, `/categories conf 0.3`, `/categories sub list|add|rm|clear`."
        )
        return "\n".join(lines)

    def _categories_markup(self) -> InlineKeyboardMarkup:
        from .categories import CATEGORIES

        cat = self.config.category_filter
        rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    f"Фильтр: {'✅ включён' if cat.enabled else '❌ выключен'}",
                    callback_data="category_filter_toggle",
                )
            ]
        ]
        for item in CATEGORIES:
            mark = self._category_mark(item.key)
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{mark} {item.name}", callback_data=f"cat_top_{item.key}"
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    f"💰 Мин. цена: {self._min_price_label()}", callback_data="cat_price"
                )
            ]
        )
        return InlineKeyboardMarkup(rows)

    def _categories_view(self, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
        text = self._categories_text()
        if note:
            text += "\n\n" + note
        return text, self._categories_markup()

    # ---------------------------------------------------------------- подменю рубрики
    def _category_sub_text(self, key: str) -> str | None:
        from .categories import CATEGORY_BY_KEY

        category = CATEGORY_BY_KEY.get(key)
        if category is None:
            return None
        cat = self.config.category_filter
        selected = cat.subcategories.get(key, [])
        if key not in (cat.categories or []):
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
            group_mark = self._group_mark(key, group.name)
            lines.append(f"{group_mark} *{_md(group.name)}*")
            for sub in group.items:
                mark = "✅" if sub.name in selected else "⬜"
                lines.append(f"  {mark} {_md(sub.name)}")
        lines.append(
            "\nТап по категории переключает её. Когда снята последняя подкатегория — "
            "рубрика выключается. «Вся рубрика» включает её целиком."
        )
        return "\n".join(lines)

    def _group_mark(self, key: str, group_name: str) -> str:
        from .categories import CATEGORY_BY_KEY

        category = CATEGORY_BY_KEY.get(key)
        if category is None:
            return "⬜"
        selected = self.config.category_filter.subcategories.get(key, [])
        names = [item.name for group in category.groups if group.name == group_name for item in group.items]
        chosen = [name for name in names if name in selected]
        if not chosen:
            return "⬜"
        if len(chosen) == len(names):
            return "✅"
        return "🔸"

    def _category_sub_markup(self, key: str) -> InlineKeyboardMarkup | None:
        from .categories import CATEGORY_BY_KEY

        category = CATEGORY_BY_KEY.get(key)
        if category is None:
            return None
        # Рубрика выбрана целиком (без ограничений) — единственное действие
        # с уровнем рубрики: выключить её. Иначе предлагаем выбрать целиком.
        if key in (self.config.category_filter.categories or []) and not (
            self.config.category_filter.subcategories.get(key)
        ):
            rows: list[list[InlineKeyboardButton]] = [
                [InlineKeyboardButton("⬜ Выключить рубрику", callback_data=f"cat_off_{key}")]
            ]
        else:
            rows = [
                [
                    InlineKeyboardButton(
                        "✅ Вся рубрика (снять ограничения)", callback_data=f"cat_all_{key}"
                    )
                ]
            ]
        for gi, group in enumerate(category.groups):
            group_mark = self._group_mark(key, group.name)
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{group_mark} {group.name}", callback_data=f"cat_grp_{key}_{gi}"
                    )
                ]
            )
            row: list[InlineKeyboardButton] = []
            for ii, item in enumerate(group.items):
                selected = item.name in self.config.category_filter.subcategories.get(key, [])
                mark = "✅" if selected else "⬜"
                label = f"{mark} {item.name}"
                # Telegram ограничивает подпись кнопки 64 символами.
                if len(label) > 64:
                    label = label[:63] + "…"
                row.append(InlineKeyboardButton(label, callback_data=f"cat_sub_{key}_{gi}_{ii}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
        rows.append([InlineKeyboardButton("⬅ Назад к рубрикам", callback_data="cat_menu")])
        return InlineKeyboardMarkup(rows)

    def _category_sub_view(self, key: str, note: str = "") -> tuple[str, InlineKeyboardMarkup] | None:
        text = self._category_sub_text(key)
        markup = self._category_sub_markup(key)
        if text is None or markup is None:
            return None
        if note:
            text += "\n\n" + note
        return text, markup

    def _category_sub_toggle(self, key: str, sub_name: str) -> None:
        """Переключает подкатегорию.

        Рубрика добавляется в выбор автоматически. Когда снята последняя
        подкатегория, рубрика выключается: иначе она осталась бы «выбрана
        целиком» и сбор неожиданно захватывал бы все заказы рубрики.
        """
        cat = self.config.category_filter
        current = list(cat.subcategories.get(key, []))
        if sub_name in current:
            current.remove(sub_name)
        else:
            if key not in (cat.categories or []):
                cat.categories = list(cat.categories or []) + [key]
            current.append(sub_name)
        if current:
            cat.subcategories = {**cat.subcategories, key: current}
        else:
            cat.subcategories = {k: v for k, v in cat.subcategories.items() if k != key}
            cat.categories = [c for c in (cat.categories or []) if c != key]

    def _category_group_toggle(self, key: str, group_name: str) -> None:
        """Переключает всю группу подкатегорий."""
        from .categories import CATEGORY_BY_KEY

        category = CATEGORY_BY_KEY.get(key)
        if category is None:
            return
        names = [
            item.name for group in category.groups if group.name == group_name for item in group.items
        ]
        current = list(self.config.category_filter.subcategories.get(key, []))
        all_selected = all(name in current for name in names)
        if all_selected:
            current = [name for name in current if name not in names]
        else:
            if key not in (self.config.category_filter.categories or []):
                self.config.category_filter.categories = (
                    list(self.config.category_filter.categories or []) + [key]
                )
            current = current + [name for name in names if name not in current]
        if current:
            self.config.category_filter.subcategories = {
                **self.config.category_filter.subcategories,
                key: current,
            }
        else:
            self.config.category_filter.subcategories = {
                k: v for k, v in self.config.category_filter.subcategories.items() if k != key
            }
            # Группа снята и подкатегорий не осталось — выключаем рубрику.
            self.config.category_filter.categories = [
                c for c in (self.config.category_filter.categories or []) if c != key
            ]

    def _category_sub_view_group(
        self, key: str, group_index: str
    ) -> tuple[str, InlineKeyboardMarkup] | None:
        """Переключает группу по индексу из callback_data и возвращает подменю."""
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
        self._category_group_toggle(key, category.groups[gi].name)
        return self._category_sub_view(key, self._persist_note())

    def _category_sub_view_item(
        self, key: str, group_index: str, item_index: str
    ) -> tuple[str, InlineKeyboardMarkup] | None:
        """Переключает категорию по индексам из callback_data и возвращает подменю."""
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
        self._category_sub_toggle(key, group.items[ii].name)
        return self._category_sub_view(key, self._persist_note())

    def _resolve_category(self, value: str):
        from .categories import find_category

        return find_category(value)

    async def _cmd_categories(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_super_admin(update) or not update.message:
            return

        cat = self.config.category_filter
        args = context.args
        if not args:
            await self._reply(
                update,
                self._categories_text(),
                parse_mode="Markdown",
                reply_markup=self._categories_markup(),
            )
            return

        action = args[0].lower()
        if action in {"on", "off"}:
            cat.enabled = action == "on"
            state = "включён" if cat.enabled else "выключен"
            await update.message.reply_text(
                f"✅ Фильтр категорий {state}." + self._persist_note()
            )
            return

        if action in {"add", "rm", "remove", "del"} and len(args) >= 2:
            changed: list[str] = []
            unknown: list[str] = []
            for raw in _split_items(" ".join(args[1:])):
                found = self._resolve_category(raw)
                if found is None:
                    unknown.append(raw)
                    continue
                current = list(cat.categories or [])
                if action == "add" and found.key not in current:
                    current.append(found.key)
                elif action != "add" and found.key in current:
                    current.remove(found.key)
                cat.categories = current
                changed.append(found.key)
            text = "✅ Категории: " + (_md(", ".join(changed)) or "—")
            if unknown:
                text += f"\n❌ Не найдены: {_md(', '.join(unknown))}"
            await update.message.reply_text(text + self._persist_note(), parse_mode="Markdown")
            return

        if action == "conf" and len(args) >= 2:
            try:
                value = float(args[1].replace(",", "."))
            except ValueError:
                await update.message.reply_text("❌ Нужно число 0..1, например `/categories conf 0.35`.")
                return
            if not 0 <= value <= 1:
                await update.message.reply_text("⚠️ Порог должен быть от 0 до 1.")
                return
            cat.min_confidence = value
            await update.message.reply_text(
                f"✅ Мин. уверенность: {value:g}." + self._persist_note()
            )
            return

        if action in {"price", "minprice"} and len(args) >= 2:
            try:
                value = float(args[1].replace(" ", "").replace(",", "."))
            except ValueError:
                await update.message.reply_text(
                    "❌ Нужно число, например `/categories price 5000`."
                )
                return
            if value < 0:
                await update.message.reply_text("⚠️ Цена не может быть отрицательной.")
                return
            cat.min_price_rub = value
            state = f"{value:,.0f} ₽".replace(",", " ") if value > 0 else "выключена"
            await update.message.reply_text(
                f"✅ Минимальная цена: *{state}*. Заказы без цены "
                "(по договорённости) показываются всегда." + self._persist_note(),
                parse_mode="Markdown",
            )
            return

        if action == "unknown" and len(args) >= 2:
            policy = args[1].lower()
            if policy not in {"include", "exclude"}:
                await update.message.reply_text("❌ Допустимо: `include` или `exclude`.")
                return
            cat.unknown_policy = policy
            await update.message.reply_text(
                f"✅ Неклассифицированные заказы: *{policy}*." + self._persist_note(),
                parse_mode="Markdown",
            )
            return

        if action == "sub":
            await self._categories_sub(update, args[1:])
            return

        if action in {"list", "show"}:
            await self._reply(update, self._categories_text(), parse_mode="Markdown")
            return

        await update.message.reply_text(
            "Формат: `/categories`, `/categories on|off`, `/categories price 5000`, "
            "`/categories add <key>`, `/categories rm <key>`, `/categories conf 0.35`, "
            "`/categories unknown include`, `/categories sub list|add|rm|clear`.",
            parse_mode="Markdown",
        )

    async def _categories_sub(self, update: Update, args: list[str]) -> None:
        from .categories import CATEGORIES

        cat = self.config.category_filter
        if not update.message:
            return
        if not args or args[0].lower() in {"list", "show"}:
            scope = args[1] if len(args) > 1 else ""
            lines = ["📂 *Подкатегории*", ""]
            for item in CATEGORIES:
                if scope and self._resolve_category(scope) is not item:
                    continue
                if not scope and cat.categories and item.key not in cat.categories:
                    continue
                selected = cat.subcategories.get(item.key, [])
                lines.append(f"*{_md(item.name)}* (`{item.key}`)")
                for sub in item.subcategories:
                    mark = "✅" if sub in selected else "•"
                    lines.append(f"  {mark} {sub}")
            await self._reply(update, "\n".join(lines), parse_mode="Markdown")
            return

        action = args[0].lower()
        if action == "clear":
            if len(args) >= 2:
                found = self._resolve_category(args[1])
                if found is None:
                    await update.message.reply_text("❌ Категория не найдена.")
                    return
                cat.subcategories.pop(found.key, None)
                text = f"✅ Ограничения подкатегорий для `{_md(found.key)}` сняты."
            else:
                cat.subcategories = {}
                text = "✅ Ограничения по подкатегориям сняты для всех категорий."
            await update.message.reply_text(text + self._persist_note(), parse_mode="Markdown")
            return

        if action in {"add", "rm", "remove", "del"}:
            if len(args) < 3:
                await update.message.reply_text(
                    "Формат: `/categories sub add programming парсер` и "
                    "`/categories sub rm programming парсер`."
                )
                return
            category = self._resolve_category(args[1])
            if category is None:
                await update.message.reply_text("❌ Категория не найдена.")
                return
            needle = " ".join(args[2:]).strip().casefold()
            matches = [sub for sub in category.subcategories if needle in sub.casefold()]
            if not matches:
                await update.message.reply_text(
                    f"❌ В категории `{_md(category.key)}` нет подкатегории «{_md(needle)}».\n"
                    f"Примеры: {_md(', '.join(category.subcategories[:12]))}",
                    parse_mode="Markdown",
                )
                return
            if len(matches) > 1:
                exact = [sub for sub in matches if sub.casefold() == needle]
                if len(exact) != 1:
                    await update.message.reply_text(
                        "Уточните подкатегорию:\n" + "\n".join(f"• {_md(sub)}" for sub in matches[:20])
                    )
                    return
                matches = exact
            sub_name = matches[0]
            current = list(cat.subcategories.get(category.key, []))
            if action == "add" and sub_name not in current:
                current.append(sub_name)
            elif action != "add" and sub_name in current:
                current.remove(sub_name)
            if current:
                cat.subcategories[category.key] = current
            else:
                cat.subcategories.pop(category.key, None)
            verb = "добавлена" if action == "add" else "убрана"
            await update.message.reply_text(
                f"✅ Подкатегория «{_md(sub_name)}» {verb} в `{_md(category.key)}`."
                + self._persist_note(),
                parse_mode="Markdown",
            )
            return

        await update.message.reply_text(
            "Формат: `/categories sub list [категория]`, `/categories sub add <категория> <название>`, "
            "`/categories sub rm <категория> <название>`, `/categories sub clear [категория]`."
        )

    # ------------------------------------------------------------------ /score
    async def _cmd_score(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Штраф красных флагов и свежесть — только администратор из .env.

        Порогов балла и совпадения навыков больше нет: скоринг задаёт лишь
        приоритет показа. Изменения влияют на всех подписчиков, поэтому
        команда закрыта для обычных пользователей.
        """
        if not await self._ensure_super_admin(update) or not update.message:
            return

        cfg = self.config.scoring
        args = context.args
        if not args or args[0].lower() in {"show", "list"}:
            await self._reply(update, self._settings_section("scoring") or "", parse_mode="Markdown")
            return

        action = args[0].lower()
        try:
            if action in {"penalty", "redflag"} and len(args) >= 2:
                value = float(args[1].replace(",", "."))
                if not 0 <= value <= 1:
                    raise ValueError("штраф должен быть 0..1")
                cfg.red_flag_penalty = value
                text = f"✅ Штраф за красные флаги: {value:g}."
            elif action == "freshness" and len(args) >= 2:
                value = float(args[1].replace(",", "."))
                if value <= 0:
                    raise ValueError("half-life должен быть > 0")
                cfg.freshness_half_life_hours = value
                text = f"✅ Свежесть (half-life): {value:g} ч."
            else:
                await update.message.reply_text(
                    "Формат: `/score`, `/score penalty 0.35`, `/score freshness 18`.\n\n"
                    "Заказы со штрафами и старые показываются последними; "
                    "порогов балла больше нет."
                )
                return
        except (ValueError, IndexError) as exc:
            await update.message.reply_text(f"❌ {exc}")
            return

        await update.message.reply_text(text + self._persist_note())

    # ------------------------------------------------------------------ /send
    def _send_text(self) -> str:
        msg = self.config.messaging
        return (
            "📨 *Режим отправки*\n\n"
            f"dry\\_run: `{self.config.runtime.dry_run}` "
            f"({'письма только пишутся в outbox' if self.config.runtime.dry_run else 'реальная отправка'})\n"
            f"одобрение каждого письма: `{msg.require_human_approval}`\n"
            f"писем в час: `{msg.max_sends_per_hour}`\n"
            f"пауза: `{msg.min_seconds_between_sends}` с\n"
            f"писем за цикл: `{msg.max_messages_per_cycle}`\n\n"
            "Команды: `/send dry on|off`, `/send approval on|off`, `/send limit 6`, "
            "`/send delay 240`, `/send drafts 20`, `/send chars kwork_ru 1000`, "
            "`/send links kwork_ru off`.\n"
            "⚠️ Выключение dry-run или одобрения требует добавки `confirm`."
        )

    def _send_markup(self) -> InlineKeyboardMarkup:
        msg = self.config.messaging
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"dry-run: {'✅' if self.config.runtime.dry_run else '❌'}",
                        callback_data="send_dry_toggle",
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"одобрение: {'✅' if msg.require_human_approval else '❌'}",
                        callback_data="send_approval_toggle",
                    )
                ],
            ]
        )

    async def _cmd_send(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_super_admin(update) or not update.message:
            return

        args = context.args
        if not args or args[0].lower() in {"show", "list"}:
            await self._reply(
                update, self._send_text(), parse_mode="Markdown", reply_markup=self._send_markup()
            )
            return

        action = args[0].lower()
        confirm = any(arg.lower() == "confirm" for arg in args)
        values = [arg for arg in args[1:] if arg.lower() != "confirm"]
        msg = self.config.messaging

        try:
            if action == "dry" and values:
                flag = _parse_flag(values[0])
                if flag is None:
                    raise ValueError("ожидается on/off")
                if (
                    flag is False
                    and not msg.require_human_approval
                    and not confirm
                ):
                    await update.message.reply_text(
                        "⛔ Одобрение писем выключено. Включите его или добавьте `confirm`, "
                        "если действительно хотите отправлять без проверки."
                    )
                    return
                self.config.runtime.dry_run = flag
                text = f"✅ dry\\_run = `{flag}`."
            elif action in {"approval", "approve"} and values:
                flag = _parse_flag(values[0])
                if flag is None:
                    raise ValueError("ожидается on/off")
                if flag is False and not self.config.runtime.dry_run and not confirm:
                    await update.message.reply_text(
                        "⛔ Бот в live-режиме. Добавьте `confirm`, чтобы отключить одобрение."
                    )
                    return
                msg.require_human_approval = flag
                text = f"✅ Одобрение писем = `{flag}`."
            elif action in {"limit", "hour"} and values:
                value = int(values[0])
                if not 0 <= value <= 60:
                    raise ValueError("лимит должен быть 0..60")
                msg.max_sends_per_hour = value
                text = f"✅ Писем в час: {value}."
            elif action in {"delay", "pause"} and values:
                value = int(values[0])
                if value < 0:
                    raise ValueError("пауза не может быть отрицательной")
                msg.min_seconds_between_sends = value
                text = f"✅ Пауза между письмами: {value} с."
            elif action in {"drafts", "cycle"} and values:
                value = int(values[0])
                if not 1 <= value <= 200:
                    raise ValueError("лимит должен быть 1..200")
                msg.max_messages_per_cycle = value
                text = f"✅ Писем за цикл: {value}."
            elif action == "chars" and len(values) >= 2:
                platform, value = values[0], int(values[1])
                if platform not in msg.limits:
                    from .config import MessageLimit

                    msg.limits[platform] = MessageLimit()
                msg.limits[platform].max_chars = value
                text = f"✅ Лимит {platform}: {value} символов."
            elif action == "links" and len(values) >= 2:
                platform = values[0]
                flag = _parse_flag(values[1])
                if flag is None:
                    raise ValueError("ожидается on/off")
                if platform not in msg.limits:
                    from .config import MessageLimit

                    msg.limits[platform] = MessageLimit()
                msg.limits[platform].allow_links = flag
                text = f"✅ Ссылки в {platform}: `{flag}`."
            else:
                await update.message.reply_text(
                    "Формат: `/send dry on|off`, `/send approval on|off`, `/send limit 6`, "
                    "`/send delay 240`, `/send drafts 20`, `/send chars <platform> <n>`, "
                    "`/send links <platform> on|off`."
                )
                return
        except (ValueError, IndexError) as exc:
            await update.message.reply_text(f"❌ {exc}")
            return

        await update.message.reply_text(text + self._persist_note(), parse_mode="Markdown")

    # ------------------------------------------------------------------ /llm
    async def _cmd_llm(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_super_admin(update) or not update.message:
            return

        cfg = self.config.llm
        args = context.args or []
        if not args or args[0].lower() in {"show", "list"}:
            await self._reply(update, self._settings_section("llm") or "", parse_mode="Markdown")
            return

        action = args[0].lower()
        values = args[1:]
        try:
            if action in {"provider", "providers"}:
                await self._cmd_llm_provider(update, context, values)
                return
            if action in {"on", "off"}:
                cfg.enabled = action == "on"
                text = f"✅ LLM {'включена' if cfg.enabled else 'выключена'}."
            elif action == "model" and values:
                model = values[0].strip()
                if not model:
                    raise ValueError("модель не может быть пустой")
                provider = cfg.active_llm_provider()
                if provider is not None:
                    provider.model = model
                    text = (
                        f"✅ Модель провайдера `{_md(cfg.active_provider)}`: `{_md(model)}`."
                    )
                else:
                    self._set_secret("LLM_MODEL", "llm_model", model)
                    text = f"✅ Модель: `{_md(model)}`."
            elif action in {"base", "url"} and values:
                url = values[0].rstrip("/")
                if not url.startswith(("http://", "https://")):
                    raise ValueError("URL должен начинаться с http(s)://")
                provider = cfg.active_llm_provider()
                if provider is not None:
                    provider.base_url = url
                    text = (
                        f"✅ Endpoint провайдера `{_md(cfg.active_provider)}`: `{_md(url)}`."
                    )
                else:
                    self._set_secret("LLM_BASE_URL", "llm_base_url", url)
                    text = f"✅ Endpoint: `{_md(url)}`."
            elif action == "key" and values:
                key = values[0]
                provider = cfg.active_llm_provider()
                if provider is not None:
                    self._set_provider_key(cfg.active_provider, provider, key)
                    label = f"провайдера `{_md(cfg.active_provider)}`"
                else:
                    self._set_secret("LLM_API_KEY", "llm_api_key", key)
                    label = "LLM"
                await self._delete_command_message(update, context)
                chat = update.effective_chat
                if chat is None:
                    return
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        f"✅ Ключ {label} сохранён в `.env`. "
                        "Значение в чат не выводится." + self._persist_note()
                    ),
                    parse_mode="Markdown",
                )
                return
            elif action in {"temp", "temperature"} and values:
                value = float(values[0].replace(",", "."))
                if not 0 <= value <= 2:
                    raise ValueError("temperature должна быть 0..2")
                cfg.temperature = value
                text = f"✅ temperature: {value:g}."
            elif action in {"tokens", "maxtokens"} and values:
                value = int(values[0])
                if not 1 <= value <= 32_000:
                    raise ValueError("лимит токенов должен быть 1..32000")
                cfg.max_output_tokens = value
                text = f"✅ max_tokens: {value}."
            elif action == "timeout" and values:
                value = int(values[0])
                if not 1 <= value <= 600:
                    raise ValueError("timeout должен быть 1..600 с")
                cfg.timeout_seconds = value
                text = f"✅ timeout: {value} с."
            elif action == "fallback" and values:
                flag = _parse_flag(values[0])
                if flag is None:
                    raise ValueError("ожидается on/off")
                cfg.fallback_enabled = flag
                text = f"✅ Fallback-письма: `{flag}`."
            elif action == "test":
                await self._llm_test(update)
                return
            else:
                await update.message.reply_text(
                    "Формат: `/llm`, `/llm on|off`, `/llm model <name>`, `/llm base <url>`, "
                    "`/llm key <ключ>`, `/llm temp 0.4`, `/llm tokens 900`, `/llm timeout 60`, "
                    "`/llm fallback on|off`, `/llm test`.\n\n"
                    "Кастомные провайдеры: `/llm provider list`, "
                    "`/llm provider add <имя> <url> <модель> [ключ]`, "
                    "`/llm provider use <имя|default>`."
                )
                return
        except (ValueError, IndexError) as exc:
            await update.message.reply_text(f"❌ {exc}")
            return

        await update.message.reply_text(text + self._persist_note(), parse_mode="Markdown")

    # ------------------------------------------------------------------ /llm provider
    @staticmethod
    def _provider_env_key(name: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper() or "CUSTOM"
        return f"LLM_PROVIDER_{slug}_API_KEY"

    @staticmethod
    def _valid_provider_name(name: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", name))

    def _set_provider_key(self, name: str, provider: Any, key: str) -> None:
        env_key = provider.api_key_env or self._provider_env_key(name)
        provider.api_key_env = env_key
        if self.settings is not None:
            self.settings.update_env({env_key: key})
        else:
            os.environ[env_key] = key

    def _remove_provider_key(self, provider: Any) -> None:
        if not provider.api_key_env:
            return
        if self.settings is not None:
            self.settings.remove_env([provider.api_key_env])
        else:
            os.environ.pop(provider.api_key_env, None)

    def _llm_providers_text(self) -> str:
        from .llm.client import resolve_provider

        llm = self.config.llm
        lines = ["🤖 *LLM-провайдеры*", ""]
        if self.secrets is not None:
            active = resolve_provider(llm, self.secrets)
            key_state = "задан" if active.api_key else "не задан"
            lines += [
                f"*Активный:* `{_md(active.name)}`",
                f"модель: `{_md(active.model)}`",
                f"endpoint: `{_md(active.base_url)}`",
                f"ключ: {key_state}",
                "",
            ]
        else:
            lines += [f"*Активный:* `{_md(llm.active_provider or 'default')}`", ""]

        if llm.providers:
            lines.append("*Сохранённые провайдеры:*")
            for name, provider in sorted(llm.providers.items()):
                mark = "✅" if name == llm.active_provider else "•"
                if provider.api_key_env:
                    has_key = bool(os.getenv(provider.api_key_env, ""))
                elif self.secrets is not None:
                    has_key = bool(self.secrets.llm_api_key)
                else:
                    has_key = False
                key_note = "ключ задан" if has_key else "ключа нет"
                if not provider.api_key_required:
                    key_note = "ключ не требуется"
                lines.append(
                    f"{mark} `{_md(name)}` — `{_md(provider.model)}`\n"
                    f"   {_md(provider.base_url)} · {key_note}"
                )
        else:
            lines.append("*Сохранённых провайдеров нет.*")

        lines += [
            "",
            "*Добавить свой OpenAI-совместимый провайдер:*",
            "`/llm provider add <имя> <url> <модель> [ключ]`",
            "`/llm provider use <имя>` · `/llm provider key <имя> <ключ>`",
            "`/llm provider nokey <имя> on|off` — для локального сервера без ключа",
            "`/llm provider header <имя> <Header> <value>` · `/llm provider rm <имя>`",
            "`/llm provider use default` — вернуться к `.env`",
        ]
        return "\n".join(lines)

    async def _cmd_llm_provider(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]
    ) -> None:
        if not update.message:
            return
        from .config import LLMProviderConfig

        llm = self.config.llm
        if not args or args[0].lower() in {"list", "show"}:
            if len(args) >= 2 and args[0].lower() == "show":
                await self._llm_provider_show(update, args[1])
                return
            await self._reply(update, self._llm_providers_text(), parse_mode="Markdown")
            return

        action = args[0].lower()
        values = args[1:]
        try:
            if action == "add":
                if len(values) < 3:
                    raise ValueError(
                        "формат: /llm provider add <имя> <url> <модель> [ключ]"
                    )
                name, url, model = values[0].strip(), values[1].strip(), values[2].strip()
                key = values[3] if len(values) > 3 else ""
                if not self._valid_provider_name(name):
                    raise ValueError(
                        "имя: латиница/цифры/дефис/подчёркивание, до 32 символов"
                    )
                if not url.startswith(("http://", "https://")):
                    raise ValueError("URL должен начинаться с http(s)://")
                if not model:
                    raise ValueError("модель не может быть пустой")

                existing = llm.providers.get(name)
                env_key = (
                    existing.api_key_env
                    if existing and existing.api_key_env
                    else self._provider_env_key(name)
                )
                llm.providers[name] = LLMProviderConfig(
                    base_url=url.rstrip("/"),
                    model=model,
                    api_key_env=env_key,
                )
                llm.active_provider = name
                if key:
                    self._set_provider_key(name, llm.providers[name], key)
                    await self._delete_command_message(update, context)
                    chat = update.effective_chat
                    if chat is None:
                        return
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=(
                            f"✅ Провайдер `{_md(name)}` добавлен и активен. "
                            "Ключ сохранён в `.env`, значение в чат не выводится."
                            + self._persist_note()
                        ),
                        parse_mode="Markdown",
                    )
                    return
                text = (
                    f"✅ Провайдер `{_md(name)}` добавлен и активен.\n"
                    f"Модель: `{_md(model)}`\n"
                    f"Endpoint: `{_md(url)}`\n"
                    f"Ключ: `/llm provider key {_md(name)} <ключ>` "
                    "(или `/llm provider nokey ... on` для локального сервера)."
                )

            elif action == "use":
                if not values:
                    raise ValueError("формат: /llm provider use <имя|default>")
                name = values[0].strip()
                if name.lower() in {"default", "env", "builtin", ".env", ""}:
                    llm.active_provider = ""
                    text = "✅ Активен встроенный провайдер из `.env`."
                elif name in llm.providers:
                    llm.active_provider = name
                    provider = llm.providers[name]
                    text = (
                        f"✅ Активен провайдер `{_md(name)}` "
                        f"(`{_md(provider.model)}`)."
                    )
                else:
                    raise ValueError(f"провайдер `{name}` не найден")

            elif action in {"rm", "remove", "del", "delete"}:
                if not values:
                    raise ValueError("формат: /llm provider rm <имя>")
                name = values[0].strip()
                target_provider = llm.providers.get(name)
                if target_provider is None:
                    raise ValueError(f"провайдер `{name}` не найден")
                self._remove_provider_key(target_provider)
                del llm.providers[name]
                if llm.active_provider == name:
                    llm.active_provider = ""
                text = (
                    f"🗑 Провайдер `{_md(name)}` удалён, ключ убран из `.env`."
                    + (" Активен встроенный провайдер." if not llm.active_provider else "")
                )

            elif action == "key":
                if len(values) < 2:
                    raise ValueError("формат: /llm provider key <имя> <ключ>")
                name, key = values[0].strip(), values[1]
                target_provider = llm.providers.get(name)
                if target_provider is None:
                    raise ValueError(f"провайдер `{name}` не найден")
                self._set_provider_key(name, target_provider, key)
                await self._delete_command_message(update, context)
                chat = update.effective_chat
                if chat is None:
                    return
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        f"✅ Ключ провайдера `{_md(name)}` сохранён в `.env`. "
                        "Значение в чат не выводится." + self._persist_note()
                    ),
                    parse_mode="Markdown",
                )
                return

            elif action == "nokey":
                if len(values) < 2:
                    raise ValueError("формат: /llm provider nokey <имя> on|off")
                name = values[0].strip()
                target_provider = llm.providers.get(name)
                if target_provider is None:
                    raise ValueError(f"провайдер `{name}` не найден")
                flag = _parse_flag(values[1])
                if flag is None:
                    raise ValueError("ожидается on/off")
                target_provider.api_key_required = not flag
                state = "не требуется" if flag else "требуется"
                text = f"✅ Для `{_md(name)}` ключ теперь {state}."

            elif action == "header":
                if len(values) < 3:
                    raise ValueError(
                        "формат: /llm provider header <имя> <Header> <value>, "
                        "для удаления value = -"
                    )
                name, header, header_value = (
                    values[0].strip(),
                    values[1].strip(),
                    " ".join(values[2:]),
                )
                target_provider = llm.providers.get(name)
                if target_provider is None:
                    raise ValueError(f"провайдер `{name}` не найден")
                if not re.fullmatch(r"[A-Za-z0-9-]+", header):
                    raise ValueError("имя заголовка: латиница, цифры и дефис")
                if header_value.strip() == "-":
                    target_provider.headers.pop(header, None)
                    text = f"🗑 Заголовок `{_md(header)}` убран у `{_md(name)}`."
                else:
                    target_provider.headers[header] = header_value
                    text = f"✅ Заголовок `{_md(header)}` задан для `{_md(name)}`."

            else:
                raise ValueError(
                    "формат: /llm provider list|show|add|use|rm|key|nokey|header"
                )
        except (ValueError, IndexError) as exc:
            await update.message.reply_text(f"❌ {exc}")
            return

        await update.message.reply_text(text + self._persist_note(), parse_mode="Markdown")

    async def _llm_provider_show(self, update: Update, name: str) -> None:
        if not update.message:
            return
        provider = self.config.llm.providers.get(name)
        if provider is None:
            await update.message.reply_text(f"❌ Провайдер `{_md(name)}` не найден.")
            return
        if provider.api_key_env:
            has_key = bool(os.getenv(provider.api_key_env, ""))
        elif self.secrets is not None:
            has_key = bool(self.secrets.llm_api_key)
        else:
            has_key = False
        headers = ", ".join(sorted(provider.headers)) or "—"
        await self._reply(
            update,
            f"🤖 *Провайдер `{_md(name)}`*\n\n"
            f"Endpoint: `{_md(provider.base_url)}`\n"
            f"Модель: `{_md(provider.model)}`\n"
            f"Ключ: {'задан' if has_key else 'не задан'}"
            f"{' (не требуется)' if not provider.api_key_required else ''}\n"
            f"Переменная ключа: `{_md(provider.api_key_env or '—')}`\n"
            f"Заголовки: {_md(headers)}\n"
            f"Активен: `{name == self.config.llm.active_provider}`",
            parse_mode="Markdown",
        )

    def _set_secret(self, env_key: str, attr: str, value: str) -> None:
        if self.secrets is not None:
            setattr(self.secrets, attr, value)
        if self.settings is not None:
            self.settings.update_env({env_key: value})

    async def _llm_test(self, update: Update) -> None:
        if not update.message:
            return
        if self.secrets is None:
            await update.message.reply_text("ℹ️ Секреты не подключены к контроллеру.")
            return
        from .llm.client import LLMClient, LLMError

        client = LLMClient(self.config.llm, self.secrets)
        if not client.available:
            await update.message.reply_text(
                "❌ LLM не настроена: проверьте `/llm show` — нужен ключ и enabled=true.\n"
                "Для локального сервера без ключа: `/llm provider nokey <имя> on`."
            )
            return
        await update.message.reply_text(
            f"⏳ Проверяю `{_md(client.provider.name)}` / `{_md(client.model)}`…",
            parse_mode="Markdown",
        )
        try:
            answer = await client.complete(
                "Отвечай одним словом.", "ping", max_tokens=8, temperature=0
            )
        except (LLMError, Exception) as exc:  # noqa: BLE001 - ошибка модели не должна падать
            await update.message.reply_text(f"❌ Модель ответила ошибкой: {_md(exc)}")
            return
        await update.message.reply_text(f"✅ Модель доступна. Ответ: {_md(answer[:100])}")

    async def _delete_command_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Удаляет сообщение с секретом, чтобы ключ не остался в истории чата."""
        if not update.message:
            return
        try:
            await update.message.delete()
        except Exception as exc:  # noqa: BLE001 - удаление может быть запрещено
            log.info("Could not delete secret message: %s", exc)

    # ------------------------------------------------------------------ /prompt
    async def _cmd_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_super_admin(update) or not update.message:
            return

        args = context.args
        if not args or args[0].lower() in {"show", "list"}:
            await self._reply(
                update, self._settings_section("prompt") or "", parse_mode="Markdown"
            )
            return

        action = args[0].lower()
        cfg = self.config.llm
        if action in {"system", "extra"} and len(args) > 1:
            value = " ".join(args[1:])
            if action == "system":
                cfg.system_prompt = value
            else:
                cfg.extra_instructions = value
            await update.message.reply_text(
                f"✅ Промпт `{action}` обновлён ({len(value)} символов)." + self._persist_note(),
                parse_mode="Markdown",
            )
            return

        if action in {"system", "extra"}:
            self._awaiting_input = f"llm.{'system_prompt' if action == 'system' else 'extra_instructions'}"
            self._awaiting_label = f"промпт {action}"
            await update.message.reply_text(
                f"✍️ Пришлите текст для `{action}` одним сообщением.\n"
                "Отмена: `/cancel`.\n"
                f"Текущий размер: {len(getattr(cfg, 'system_prompt' if action == 'system' else 'extra_instructions'))} символов.",
                parse_mode="Markdown",
            )
            return

        if action == "reset":
            what = args[1].lower() if len(args) > 1 else "all"
            if what in {"system", "all"}:
                cfg.system_prompt = ""
            if what in {"extra", "all"}:
                cfg.extra_instructions = ""
            await update.message.reply_text(
                f"✅ Промпт сброшен ({what})." + self._persist_note()
            )
            return

        await update.message.reply_text(
            "Формат: `/prompt show`, `/prompt system <текст>`, `/prompt extra <текст>`, "
            "`/prompt system` (бот попросит текст), `/prompt reset [system|extra|all]`."
        )

    # ------------------------------------------------------------------ /style
    async def _cmd_style(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_super_admin(update) or not update.message:
            return

        profile = self._profile_model()
        style = profile.message_style
        args = context.args
        if not args or args[0].lower() in {"show", "list"}:
            await self._reply(
                update,
                "🎨 *Стиль письма*\n\n"
                f"тон: `{style.tone}`\n"
                f"длина: `{style.length}`\n"
                f"эмодзи: `{style.max_emoji}`\n"
                f"портфолио: `{style.mention_portfolio}`\n"
                f"уточняющие вопросы: `{style.ask_questions}`\n"
                f"запрещённые фразы: `{_md(', '.join(style.banned_phrases)) or '—'}`\n\n"
                "Команды: `/style tone friendly`, `/style length short`, `/style emoji 0`, "
                "`/style portfolio on`, `/style banned add <фраза>`.",
                parse_mode="Markdown",
            )
            return

        action = args[0].lower()
        values = args[1:]
        try:
            if action == "tone" and values:
                if values[0] not in {"professional", "friendly", "casual"}:
                    raise ValueError("тон: professional, friendly или casual")
                style.tone = values[0]  # type: ignore[assignment]
                text = f"✅ Тон: `{values[0]}`."
            elif action == "length" and values:
                if values[0] not in {"short", "medium", "long"}:
                    raise ValueError("длина: short, medium или long")
                style.length = values[0]  # type: ignore[assignment]
                text = f"✅ Длина: `{values[0]}`."
            elif action == "emoji" and values:
                value = int(values[0])
                if not 0 <= value <= 5:
                    raise ValueError("эмодзи: 0..5")
                style.max_emoji = value
                text = f"✅ Эмодзи: {value}."
            elif action in {"portfolio", "questions"} and values:
                flag = _parse_flag(values[0])
                if flag is None:
                    raise ValueError("ожидается on/off")
                if action == "portfolio":
                    style.mention_portfolio = flag
                else:
                    style.ask_questions = flag
                text = f"✅ {action}: `{flag}`."
            elif action == "banned" and values:
                sub = values[0].lower()
                if sub in {"list", "show"}:
                    await update.message.reply_text(
                        "Запрещённые фразы:\n"
                        + ("\n".join(f"• {_md(p)}" for p in style.banned_phrases) or "—"),
                        parse_mode="Markdown",
                    )
                    return
                if len(values) < 2:
                    raise ValueError("укажите фразу")
                phrase = " ".join(values[1:])
                if sub == "add":
                    if phrase not in style.banned_phrases:
                        style.banned_phrases.append(phrase)
                    text = f"✅ Фраза добавлена: `{_md(phrase)}`."
                elif sub in {"rm", "remove", "del"}:
                    style.banned_phrases = [
                        item for item in style.banned_phrases if item.casefold() != phrase.casefold()
                    ]
                    text = f"✅ Фраза убрана: `{_md(phrase)}`."
                else:
                    raise ValueError("banned: list, add или rm")
            else:
                await update.message.reply_text(
                    "Формат: `/style`, `/style tone friendly`, `/style length short`, "
                    "`/style emoji 0`, `/style portfolio on`, `/style questions off`, "
                    "`/style banned add <фраза>`."
                )
                return
        except (ValueError, IndexError) as exc:
            await update.message.reply_text(f"❌ {exc}")
            return

        await update.message.reply_text(text + self._persist_note(), parse_mode="Markdown")

    # ------------------------------------------------------------------ /skills
    async def _cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_super_admin(update) or not update.message:
            return

        profile = self._profile_model()
        args = context.args
        if not args or args[0].lower() in {"show", "list"}:
            await self._reply(update, self._settings_section("skills") or "", parse_mode="Markdown")
            return

        action = args[0].lower()
        values = args[1:]
        if action in {"add", "rm", "remove", "del", "add2", "rm2"}:
            if not values:
                await update.message.reply_text("❌ Укажите навык(и) через запятую.")
                return
            items = _split_items(" ".join(values))
            if action in {"add", "add2"}:
                target = profile.skills if action == "add" else profile.secondary_skills
                for item in items:
                    if item.casefold() not in {existing.casefold() for existing in target}:
                        target.append(item)
                text = f"✅ Добавлено: {_md(', '.join(items))}."
            else:
                target = profile.skills if action == "rm" else profile.secondary_skills
                lowered = {item.casefold() for item in items}
                removed = [item for item in target if item.casefold() in lowered]
                if action == "rm":
                    profile.skills = [item for item in target if item.casefold() not in lowered]
                else:
                    profile.secondary_skills = [
                        item for item in target if item.casefold() not in lowered
                    ]
                text = f"🗑 Убрано: {_md(', '.join(removed)) or '—'}."
            await update.message.reply_text(text + self._persist_note(), parse_mode="Markdown")
            return

        if action in {"match", "exclude"}:
            # Термины скоринга (skills/exclude_keywords) задаются в config.yaml;
            # порог совпадения удалён, поэтому кнопки для них больше не нужны.
            await update.message.reply_text(
                "⚠️ Настройка совпадения навыков удалена: порогов больше нет. "
                "Термины для показа задаются в config.yaml."
            )
            return

        await update.message.reply_text(
            "Формат: `/skills`, `/skills add python,fastapi`, `/skills rm python`, "
            "`/skills add2 react`."
        )

    # ------------------------------------------------------------------ /profile
    async def _cmd_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_super_admin(update) or not update.message:
            return

        profile = self._profile_model()
        args = context.args
        if not args or args[0].lower() in {"show", "list"}:
            text = (
                "👤 *Профиль исполнителя*\n\n"
                f"Имя: {_md(profile.name or '—')}\n"
                f"Опыт: {profile.experience_years} лет\n"
                f"Локация: {_md(profile.location)}\n\n"
                "*Навыки:*\n" + _md(", ".join(profile.skills[:15])) + "\n"
            )
            if profile.secondary_skills:
                text += (
                    "\n*Дополнительно:*\n"
                    + _md(", ".join(profile.secondary_skills[:12]))
                    + "\n"
                )
            if profile.experience_summary:
                summary = profile.experience_summary.strip()
                text += f"\n*Опыт:*\n{_md(summary[:400])}\n"
            text += (
                "\nИзменить: `/profile name Иван`, `/profile years 5`, "
                "`/profile location Москва`, `/profile summary` (текст следующим сообщением)."
            )
            await self._reply(update, text, parse_mode="Markdown")
            return

        action = args[0].lower()
        values = args[1:]
        try:
            if action == "name" and values:
                profile.name = " ".join(values)
                text = f"✅ Имя: {_md(profile.name)}."
            elif action in {"years", "experience"} and values:
                value = int(values[0])
                if not 0 <= value <= 60:
                    raise ValueError("опыт должен быть 0..60 лет")
                profile.experience_years = value
                text = f"✅ Опыт: {value} лет."
            elif action == "location" and values:
                profile.location = " ".join(values)
                text = f"✅ Локация: {_md(profile.location)}."
            elif action == "summary" and values:
                profile.experience_summary = " ".join(values)
                text = f"✅ Опыт обновлён ({len(profile.experience_summary)} символов)."
            elif action == "summary":
                self._awaiting_input = "profile.experience_summary"
                self._awaiting_label = "описание опыта"
                await update.message.reply_text(
                    "✍️ Пришлите описание опыта одним сообщением. Отмена: `/cancel`."
                )
                return
            else:
                await update.message.reply_text(
                    "Формат: `/profile`, `/profile name Иван`, `/profile years 5`, "
                    "`/profile location Москва`, `/profile summary <текст>`."
                )
                return
        except (ValueError, IndexError) as exc:
            await update.message.reply_text(f"❌ {exc}")
            return

        await update.message.reply_text(text + self._persist_note(), parse_mode="Markdown")

    # ------------------------------------------------------------------ /reload, /reset, /export
    async def _cmd_reload(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_super_admin(update) or not update.message:
            return
        from .config import load_config
        from .profile import load_profile

        try:
            self.config = load_config()
            self.profile = load_profile(self.profile_path)
            if self.settings is not None:
                self.settings.refresh_baseline()
        except Exception as exc:  # noqa: BLE001 - показываем причину оператору
            await update.message.reply_text(f"❌ Не удалось перечитать файлы: {_md(exc)}")
            return
        await update.message.reply_text(
            "🔄 Настройки и профиль перечитаны с диска.\n"
            f"Платформ: {len(self.config.platforms)}, "
            f"категорий: {len(self.config.category_filter.categories)}."
        )

    async def _cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_super_admin(update) or not update.message:
            return
        if self.settings is None:
            await update.message.reply_text("ℹ️ Хранилище оверлея не подключено.")
            return

        args = context.args or []
        what = args[0].lower() if args else ""
        if what not in {"config", "profile", "all"} or "confirm" not in {
            arg.lower() for arg in args
        }:
            await update.message.reply_text(
                "⚠️ Сброс вернёт значения из config.yaml/profile.yaml.\n"
                "Команда: `/reset config|profile|all confirm`."
            )
            return

        section = None if what == "all" else what
        self.settings.reset(section)
        self.settings.refresh_baseline()
        from .config import load_config
        from .profile import load_profile

        self.config = load_config()
        self.profile = load_profile(self.profile_path)
        self.interval_seconds = self.config.runtime.poll_interval_seconds
        await update.message.reply_text(f"✅ Сброшено: {what}. Значения взяты из файлов.")

    async def _cmd_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_super_admin(update) or not update.message:
            return
        payload = {
            "config": self.config.model_dump(mode="json"),
            "profile": self._profile_model().model_dump(mode="json"),
        }
        text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        await self._reply(update, f"📤 *Экспорт настроек*\n```\n{_md(text)}\n```", parse_mode="Markdown")

    # ------------------------------------------------------------------ ввод текста
    async def _cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_admin(update) or not update.message:
            return
        if self._awaiting_input is None:
            await update.message.reply_text("Нечего отменять.")
            return
        self._awaiting_input = None
        await update.message.reply_text("❌ Ввод отменён.")

    async def _handle_text_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Принимает многострочный текст после /prompt system, /profile summary и т.п."""
        if self._awaiting_input is None or not update.message or not update.message.text:
            return
        if not self._is_admin_update(update):
            return
        path = self._awaiting_input
        text = update.message.text
        # Пути user_filters.* (мин. цена личного фильтра) пишут в SQLite
        # пользователя, а не в конфиг — обрабатываются отдельным хуком.
        if path.startswith("user_filters."):
            if not await self._apply_user_filters_input(path, text, update):
                self._awaiting_input = None
            return
        # Всё остальное пишет в общий конфиг/профиль: это уровень администратора.
        if not self._is_super_admin_update(update):
            self._awaiting_input = None
            await self._deny_insufficient_rights(update)
            return
        self._awaiting_input = None
        ok, message = self._apply_value(path, text)
        if not ok:
            await update.message.reply_text(message, parse_mode="Markdown")
            return
        preview = text.strip()
        if len(preview) > 300:
            preview = preview[:300] + "…"
        await update.message.reply_text(
            f"✅ {_md(getattr(self, '_awaiting_label', path))} сохранён "
            f"({len(text)} символов)." + self._persist_note()
            + f"\n\n{_md(preview)}",
            parse_mode="Markdown",
        )
