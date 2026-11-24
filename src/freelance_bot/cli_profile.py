"""CLI-команда для просмотра профиля фрилансера, используемого при генерации сообщений.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

console = Console()


def profile_cmd(
    path: Annotated[
        str, typer.Option("--path", "-p", help="Path to the profile YAML")
    ] = "profile.yaml",
) -> None:
    """Показывает профиль фрилансера, навыки и стратегию цены.

        Пример:
            python -m freelance_bot.cli profile
            python -m freelance_bot.cli profile --path my_profile.yaml
    """
    from .profile import load_profile

    p = Path(path)
    if not p.exists():
        console.print(f"[red]Файл профиля не найден: {path}[/red]")
        console.print("Скопируйте profile.yaml и заполните свои навыки.")
        raise typer.Exit(1)

    profile = load_profile(p)

    table = Table(title=f"Профиль исполнителя ({path})")
    table.add_column("Параметр")
    table.add_column("Значение", overflow="fold")

    table.add_row("Имя", profile.name or "—")
    table.add_row("Опыт", f"{profile.experience_years} лет")
    table.add_row("Локация", profile.location)
    table.add_row("Основные навыки", ", ".join(profile.skills) or "—")
    table.add_row("Дополнительные навыки", ", ".join(profile.secondary_skills) or "—")
    table.add_row(
        "Стратегия цены",
        {
            "minimum": "минимум (бюджет −5%, не ниже порога)",
            "markup": "наценка (бюджет × коэффициент)",
            "llm_estimate": "оценка LLM (часы × ставка)",
        }.get(profile.pricing.strategy, profile.pricing.strategy),
    )
    table.add_row("Коэффициент наценки", str(profile.pricing.markup_percent))
    table.add_row("Ставка", f"{profile.pricing.hourly_rate_rub} ₽/час")
    table.add_row("Минимальная цена", f"{profile.pricing.min_acceptable_rub} ₽")
    table.add_row("Цена в письме", "да" if profile.pricing.include_price_in_message else "нет")
    table.add_row("Тон", profile.message_style.tone)
    table.add_row("Длина", profile.message_style.length)
    table.add_row("Упоминать портфолио", "да" if profile.message_style.mention_portfolio else "нет")
    table.add_row("Задавать вопросы", "да" if profile.message_style.ask_questions else "нет")
    table.add_row("Занятость", f"{profile.availability.hours_per_week} ч/нед")
    table.add_row("Старт", "сразу" if profile.availability.immediate_start else "по договорённости")

    console.print(table)

    if profile.experience_summary:
        console.print("\n[bold]Опыт и проекты:[/bold]")
        console.print(profile.experience_summary.strip())

    if profile.message_style.banned_phrases:
        console.print("\n[bold]Запрещённые фразы:[/bold]")
        for phrase in profile.message_style.banned_phrases:
            console.print(f"  • {phrase}")
