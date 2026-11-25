"""CLI-команда запуска бота с интеграцией Telegram."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from datetime import UTC, datetime
from typing import Annotated

import typer
from rich.console import Console

from .config import clamp_poll_interval, get_config, get_secrets
from .pipeline import run_cycle
from .profile import load_profile
from .settings_store import SettingsStore
from .storage import JobStore

log = logging.getLogger(__name__)
console = Console()


def _setup_telegram_logging(verbose: bool = False) -> None:
    """Включает рабочие логи для долгоживущего бота.

        ``run``/``trial`` настраивают логирование в ``cli.py``; у команды ``telegram``
        своя точка входа, поэтому она должна делать то же самое, иначе прогресс
        цикла/адаптеров молча теряется корневым логгером.
    """
    if logging.getLogger().handlers:
        return
    from rich.logging import RichHandler

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=console, rich_tracebacks=False, show_path=False)],
    )
    for noisy in ("curl_cffi", "urllib3", "asyncio", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def run_with_telegram(
    interval_seconds: int | None,
    platforms: list[str] | None = None,
    profile_path: str = "profile.yaml",
    verbose: bool = False,
) -> None:
    """Запускает непрерывный цикл с интеграцией Telegram-бота."""
    _setup_telegram_logging(verbose)
    config = get_config()
    secrets = get_secrets()

    if not secrets.telegram_bot_token:
        console.print("[red]❌ TELEGRAM_BOT_TOKEN not configured in .env[/red]")
        raise typer.Exit(1)

    if not secrets.telegram_admins:
        console.print("[red]❌ TELEGRAM_ADMIN_CHAT_IDS not configured in .env[/red]")
        raise typer.Exit(1)

    # Импорт здесь, чтобы не грузить зависимости Telegram без необходимости
    from .telegram_bot import TelegramBotController

    profile = load_profile(profile_path)
    store = JobStore(secrets.freelance_bot_db)
    settings = SettingsStore(
        config_path=os.getenv("FREELANCE_BOT_CONFIG"),
        profile_path=profile_path,
    )
    telegram_bot = TelegramBotController(
        token=secrets.telegram_bot_token,
        admin_chat_ids=secrets.telegram_admins,
        config=config,
        profile=profile,
        store=store,
        secrets=secrets,
        settings=settings,
        profile_path=profile_path,
    )
    if interval_seconds is not None:
        # CLI-флаг обрезается в те же границы, что и команда /interval:
        # частые проходы вредят площадкам, редкие — задерживают доставку.
        telegram_bot.interval_seconds = clamp_poll_interval(interval_seconds)

    shutdown_event = asyncio.Event()

    # Обработчики сигналов для корректного завершения
    def handle_signal(sig: int, frame: object) -> None:
        log.info("Received signal %s, shutting down...", sig)
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        # Запускаем Telegram-бота
        await telegram_bot.start()
        console.print("[green]✅ Telegram bot started[/green]")
        console.print(
            f"[cyan]Профиль:[/cyan] {profile_path} | "
            f"[cyan]интервал:[/cyan] {telegram_bot.interval_seconds} с | "
            f"[cyan]dry-run:[/cyan] {telegram_bot.config.runtime.dry_run}"
        )

        cycle_count = 0
        while not shutdown_event.is_set():
            if telegram_bot.pipeline_paused:
                log.info("Pipeline paused, waiting...")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(shutdown_event.wait(), timeout=10)
                continue

            cycle_count += 1
            log.info("Starting cycle #%d", cycle_count)

            try:
                report = await run_cycle(
                    telegram_bot.config,
                    secrets,
                    platform_names=platforms,
                    store=store,
                    telegram_bot=telegram_bot,
                    profile=telegram_bot.profile,
                )
                log.info(
                    "Cycle #%d complete: %d jobs, %d new, %d eligible, %d messages",
                    cycle_count,
                    report.jobs_scored,
                    report.jobs_new,
                    report.jobs_eligible,
                    report.messages_generated,
                )
            except Exception as e:
                log.exception("Cycle #%d failed", cycle_count)
                await telegram_bot.notify_error(f"Цикл #{cycle_count} упал: {e!s}")

            # Ждём следующего цикла. Интервал может меняться во время работы
            # через команду Telegram /interval, поэтому читается на каждой
            # итерации и обрезается в границы 600-3600.
            current_interval = clamp_poll_interval(telegram_bot.interval_seconds)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(shutdown_event.wait(), timeout=current_interval)

    finally:
        console.print("[yellow]⏹️ Stopping Telegram bot...[/yellow]")
        await telegram_bot.stop()
        store.close()
        console.print("[green]✅ Shutdown complete[/green]")


async def check_telegram(
    token: str, admin_chat_ids: list[int], *, send_test: bool = False
) -> None:
    """Проверяет токен через getMe и при необходимости доставляет тестовое сообщение.
    """
    from telegram import Bot
    from telegram.error import TelegramError
    from telegram.request import HTTPXRequest

    request = HTTPXRequest(
        connect_timeout=30, read_timeout=30, write_timeout=30, pool_timeout=10
    )
    try:
        async with Bot(token, request=request) as bot:
            me = await bot.get_me()
            console.print(
                f"[green]✅ Токен валиден:[/green] @{me.username} "
                f"(id={me.id}, name={me.first_name!r})"
            )
            if not send_test:
                console.print(
                    "[cyan]Режим проверки:[/cyan] getMe API отвечает. "
                    "Добавьте --send-test, чтобы отправить сообщение админам."
                )
                return
            if not admin_chat_ids:
                console.print(
                    "[red]❌ Нет admin chat id: укажите TELEGRAM_ADMIN_CHAT_IDS "
                    "или передайте --chat-id[/red]"
                )
                raise typer.Exit(1)
            from .telegram_bot import _md

            text = (
                "✅ *freelance-bot* подключён.\n\n"
                f"Бот: @{_md(me.username)}\n"
                f"Проверка выполнена: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )
            for chat_id in admin_chat_ids:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
                console.print(f"[green]✅ Тестовое сообщение отправлено:[/green] {chat_id}")
    except TelegramError as e:
        console.print(f"[red]❌ Telegram API: {e}[/red]")
        raise typer.Exit(1) from e


def telegram_check_cmd(
    token: Annotated[
        str | None, typer.Option("--token", help="Bot token (defaults to .env)")
    ] = None,
    chat_id: Annotated[
        int | None, typer.Option("--chat-id", help="Admin chat id for the test message")
    ] = None,
    send_test: Annotated[
        bool, typer.Option("--send-test", help="Send a test message to the admins")
    ] = False,
) -> None:
    """Проверяет токен Telegram и при необходимости отправляет тестовое сообщение.

        Это смоук-тест без polling: он вызывает getMe, доказывая, что токен валиден
        и сетевой путь к api.telegram.org работает до запуска долгоживущего бота.
    """
    secrets = get_secrets()
    resolved_token = (token or secrets.telegram_bot_token).strip()
    if not resolved_token:
        console.print(
            "[red]❌ TELEGRAM_BOT_TOKEN не задан.[/red]\n"
            "Создайте бота через @BotFather и добавьте токен в .env:\n"
            "  TELEGRAM_BOT_TOKEN=123456789:ABCdef...\n"
            "  TELEGRAM_ADMIN_CHAT_IDS=123456789"
        )
        raise typer.Exit(1)

    admins = [chat_id] if chat_id is not None else secrets.telegram_admins
    try:
        asyncio.run(check_telegram(resolved_token, admins, send_test=send_test))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")


def telegram_cmd(
    interval: Annotated[
        int | None, typer.Option("--interval", "-i", help="Cycle interval in seconds")
    ] = None,
    platforms: Annotated[
        list[str] | None, typer.Option("--platform", "-p", help="Specific platforms")
    ] = None,
    profile_path: Annotated[
        str, typer.Option("--profile", help="Path to freelancer profile YAML")
    ] = "profile.yaml",
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Запускает бота с панелью управления Telegram и уведомлениями.

        Запускает Telegram-бота и непрерывные циклы: каждый подписчик получает
        заказы по своим личным фильтрам (/filters). Интервал ограничен
        600-3600 секундами и меняется только администратором.

        Примеры:
            python -m freelance_bot.cli telegram
            python -m freelance_bot.cli telegram --interval 600
            python -m freelance_bot.cli telegram --profile my_profile.yaml
    """
    try:
        asyncio.run(run_with_telegram(interval, platforms, profile_path, verbose))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        sys.exit(0)
