"""Точка входа CLI."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .adapters.registry import ADAPTERS
from .categories import CategoryFilter

# Команды категорий определены в cli_categories.py, но регистрируются здесь.
from .cli_categories import categories_cmd, classify_cmd  # noqa: F401
from .cli_profile import profile_cmd  # noqa: F401
from .cli_support import format_scored_table, platform_overview, preflight_checks
from .cli_telegram import telegram_check_cmd, telegram_cmd  # noqa: F401
from .config import get_config, get_secrets
from .pipeline import run_cycle, run_trial
from .storage import JobStore

app = typer.Typer(
    add_completion=False, help="Russian freelance marketplace bot (dry-run by default)."
)
app.command("categories")(categories_cmd)
app.command("classify")(classify_cmd)
app.command("telegram")(telegram_cmd)
app.command("telegram-check")(telegram_check_cmd)
app.command("profile")(profile_cmd)
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(console=console, rich_tracebacks=False, show_path=False)],
    )
    for noisy in ("curl_cffi", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@app.command()
def run(
    platform: Annotated[list[str] | None, typer.Option("--platform", "-p")] = None,
    live: Annotated[
        bool, typer.Option("--live", help="Disable dry-run (still requires approval)")
    ] = False,
    browser: Annotated[bool, typer.Option("--browser/--no-browser")] = True,
    messages: Annotated[bool, typer.Option("--messages/--no-messages")] = True,
    config_path: Annotated[str | None, typer.Option("--config")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Запускает один цикл сбора + скоринга + рассылки."""
    _setup_logging(verbose)
    config = get_config()
    if config_path:
        from .config import load_config

        config = load_config(config_path)
    if live:
        config.runtime.dry_run = False
        console.print("[yellow]LIVE mode: messages still need per-message approval.[/yellow]")

    secrets = get_secrets()
    report = asyncio.run(
        run_cycle(
            config,
            secrets,
            platform_names=platform,
            with_browser=browser,
            generate_messages=messages,
        )
    )
    _render_report(report)


@app.command()
def trial(
    minutes: Annotated[int, typer.Option("--minutes", "-m")] = 30,
    interval: Annotated[int, typer.Option("--interval", "-i")] = 600,
    platform: Annotated[list[str] | None, typer.Option("--platform", "-p")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Ограниченный живой наблюдательный запуск. Всегда dry-run."""
    _setup_logging(verbose)
    config = get_config()
    reports = asyncio.run(
        run_trial(
            config,
            get_secrets(),
            minutes=minutes,
            interval_seconds=interval,
            platform_names=platform,
        )
    )
    console.print(f"[bold]{len(reports)} cycles completed[/bold]")
    for r in reports:
        _render_report(r, compact=True)


@app.command("platforms")
def platforms_cmd() -> None:
    """Показывает настроенные площадки, транспорт и задокументированные исключения.
    """
    config = get_config()
    overview = platform_overview(config)
    table = Table(title="Platform coverage")
    table.add_column("Platform")
    table.add_column("Enabled")
    table.add_column("Transport")
    table.add_column("Status / reason")
    for row in overview:
        table.add_row(row["platform"], row["enabled"], row["transport"], row["status"])
    console.print(table)


@app.command("parse-fixture")
def parse_fixture(
    platform: Annotated[str, typer.Argument()],
    path: Annotated[Path, typer.Argument(exists=True, readable=True)],
) -> None:
    """Разбирает сохранённый HTML-файл адаптером площадки (офлайн, без сети)."""
    config = get_config()
    cls = ADAPTERS.get(platform)
    if cls is None:
        console.print(f"[red]unknown platform {platform!r}; known: {', '.join(ADAPTERS)}[/red]")
        raise typer.Exit(2)
    cfg = config.platforms.get(platform)
    if cfg is None:
        from .config import PlatformConfig

        cfg = PlatformConfig(listing_url=f"https://{cls.domain}/")
    adapter = cls(cfg)
    jobs = adapter.parse(path.read_text(encoding="utf-8", errors="replace"), cfg.listing_url)
    console.print(f"[bold]{len(jobs)} jobs parsed[/bold]")
    table = Table()
    table.add_column("#", width=3)
    table.add_column("ID")
    table.add_column("Title", overflow="fold")
    table.add_column("Budget")
    table.add_column("Proposals")
    for i, job in enumerate(jobs[:25], 1):
        table.add_row(
            str(i),
            job.external_id,
            job.title[:70],
            job.budget_text or "—",
            str(job.proposals_count) if job.proposals_count is not None else "—",
        )
    console.print(table)
    if len(jobs) > 25:
        console.print(f"... and {len(jobs) - 25} more")


@app.command()
def score(
    path: Annotated[Path, typer.Argument(exists=True)],
    top: Annotated[int, typer.Option("--top", "-n")] = 10,
) -> None:
    """Скоринг JSON-файла нормализованных заказов (офлайн-отладка)."""
    config = get_config()
    from .models import Job
    from .scoring.engine import ScoringEngine

    raw = json.loads(path.read_text(encoding="utf-8"))
    jobs = [Job.model_validate(item) for item in raw]
    engine = ScoringEngine(config.scoring, config.red_flags)
    ranked = engine.rank(jobs, top_n=top)
    console.print(format_scored_table(ranked))


@app.command()
def reclassify(
    limit: Annotated[int, typer.Option("--limit", help="0 — все заказы")] = 0,
) -> None:
    """Пересчитывает рубрики сохранённых заказов по текущему классификатору.

    После правок классификации старые записи хранят прежний результат, и
    заказы без рубрики не попадают в /jobs и доставку. Команда заново
    классифицирует payload и обновляет признак «подходящий» по общему
    фильтру категорий из config.yaml.
    """
    _setup_logging(False)
    config = get_config()
    store = JobStore(get_secrets().freelance_bot_db)
    try:
        cat_cfg = config.category_filter
        cat_filter = CategoryFilter(
            enabled=cat_cfg.enabled,
            categories=tuple(cat_cfg.categories),
            subcategories={
                k: tuple(v) for k, v in (cat_cfg.subcategories or {}).items()
            }
            if cat_cfg.subcategories
            else None,
            min_confidence=cat_cfg.min_confidence,
            unknown_policy=cat_cfg.unknown_policy,
            min_price_rub=cat_cfg.min_price_rub,
        )
        stats = store.reclassify_jobs(limit=limit, cat_filter=cat_filter)
    finally:
        store.close()
    console.print(
        f"проверено: {stats['checked']} · с рубрикой: {stats['classified']} · "
        f"без рубрики: {stats['unknown']} · подходящих: {stats['eligible']}"
    )


@app.command()
def outbox(
    limit: Annotated[int, typer.Option("--limit")] = 30,
) -> None:
    """Список сгенерированных сообщений, ожидающих проверки."""
    store = JobStore(get_secrets().freelance_bot_db)
    try:
        rows = store.pending_messages(limit)
        if not rows:
            console.print("No pending messages.")
            return
        table = Table(title="Pending messages")
        table.add_column("ID", width=5)
        table.add_column("Platform")
        table.add_column("Job")
        table.add_column("Problems")
        for row in rows:
            violations = json.loads(row["violations"] or "[]")
            table.add_row(
                str(row["id"]),
                row["platform"],
                row["job_uid"],
                "; ".join(violations) if violations else "—",
            )
        console.print(table)
    finally:
        store.close()


@app.command()
def approve(message_id: Annotated[int, typer.Argument()]) -> None:
    """Помечает сообщение в очереди как одобренное оператором."""
    store = JobStore(get_secrets().freelance_bot_db)
    try:
        store.approve(message_id)
        console.print(f"message {message_id} approved")
    finally:
        store.close()


@app.command()
def preflight() -> None:
    """Проверяет зависимости, конфиг и окружение перед живым запуском."""
    _setup_logging(False)
    config = get_config()
    secrets = get_secrets()
    results = preflight_checks(config, secrets)
    table = Table(title="Preflight")
    table.add_column("Check")
    table.add_column("Result")
    table.add_column("Detail", overflow="fold")
    for r in results:
        colour = {"ok": "green", "warn": "yellow", "fail": "red"}[r["level"]]
        table.add_row(r["check"], f"[{colour}]{r['level']}[/{colour}]", r["detail"])
    console.print(table)
    if any(r["level"] == "fail" for r in results):
        raise typer.Exit(1)


@app.command()
def version() -> None:
    console.print(f"freelance-bot {__version__}")


def _render_report(report, compact: bool = False) -> None:
    table = Table(title="Platform results")
    table.add_column("Platform")
    table.add_column("Outcome")
    table.add_column("Jobs", justify="right")
    table.add_column("ms", justify="right")
    table.add_column("Challenge")
    for p in report.platforms:
        colour = (
            "green"
            if p.outcome.value == "ok"
            else "yellow"
            if p.outcome.value
            in (
                "empty",
                "disabled",
            )
            else "red"
        )
        table.add_row(
            p.platform,
            f"[{colour}]{p.outcome.value}[/{colour}]",
            str(p.jobs_found),
            str(p.duration_ms),
            p.challenge or "—",
        )
    console.print(table)

    if report.top and not compact:
        console.print(format_scored_table(report.top))

    console.print(
        Panel(
            f"new={report.jobs_new}  scored={report.jobs_scored}  "
            f"eligible={report.jobs_eligible}  "
            f"messages={report.messages_generated}  sent={report.messages_sent}  "
            f"rejected={report.messages_rejected}  "
            f"dry_run={report.dry_run}",
            title="Cycle summary",
        )
    )


if __name__ == "__main__":
    sys.exit(app())
