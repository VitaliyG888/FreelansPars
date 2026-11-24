"""CLI-команды для таксономии категорий и классификации."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

console = Console()


def categories_cmd(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Показывает таксономию категорий в стиле Kwork, используемую для фильтрации.
    """
    from .categories import CATEGORIES, category_tree_lines

    console.print(f"[bold]{len(CATEGORIES)} categories[/bold]")
    for line in category_tree_lines(include_urls=verbose):
        console.print(line)


def classify_cmd(
    path: Annotated[Path, typer.Argument(exists=True)],
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Классифицирует заказы из JSON-файла (офлайн-отладка)."""
    from .categories import classify_job
    from .models import Job

    raw = json.loads(path.read_text(encoding="utf-8"))
    jobs = [Job.model_validate(item) for item in raw]
    table = Table(title="Classification")
    table.add_column("#", width=3)
    table.add_column("Category")
    table.add_column("Confidence", justify="right")
    table.add_column("Group", overflow="fold")
    table.add_column("Subcategory", overflow="fold")
    if verbose:
        table.add_column("Matched")
    table.add_column("Title", overflow="fold")
    for i, job in enumerate(jobs, 1):
        result = classify_job(job)
        row = [
            str(i),
            result.category_key or "—",
            f"{result.confidence:.2f}",
            result.group or "—",
            result.subcategory or "—",
        ]
        if verbose:
            row.append(result.matched or "—")
        row.append(job.title[:50])
        table.add_row(*row)
    console.print(table)
