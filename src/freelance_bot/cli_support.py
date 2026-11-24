"""Помощники CLI (вынесены отдельно, чтобы их можно было покрыть юнит-тестами).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from rich.table import Table

from .config import AppConfig, Secrets
from .models import ScoredJob

TRANSPORT_LABELS = {"http": "HTTP (curl_cffi)", "browser": "Browser (playwright)"}


def format_scored_table(scored: list[ScoredJob], limit: int = 25) -> Table:
    table = Table(title="Top orders", show_lines=False)
    table.add_column("#", width=3)
    table.add_column("Score", justify="right", width=6)
    table.add_column("Platform", width=16)
    table.add_column("Title", overflow="fold", max_width=46)
    table.add_column("Budget", justify="right", width=12)
    table.add_column("Resp", justify="right", width=5)
    table.add_column("Почему", overflow="fold", max_width=48)
    for i, item in enumerate(scored[:limit], 1):
        job = item.job
        budget = (
            f"{job.budget_rub_estimate:,.0f} ₽".replace(",", " ")
            if job.budget_rub_estimate
            else "—"
        )
        table.add_row(
            str(i),
            f"{item.score.total:.1f}",
            job.platform,
            job.title[:80],
            budget,
            str(job.proposals_count) if job.proposals_count is not None else "—",
            item.score.summary_ru,
        )
    return table


def platform_overview(config: AppConfig) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for name, cfg in config.platforms.items():
        status = cfg.notes or ("enabled" if cfg.enabled else "disabled")
        if not cfg.enabled:
            status = "disabled — " + status
        rows.append(
            {
                "platform": name,
                "enabled": "yes" if cfg.enabled else "no",
                "transport": TRANSPORT_LABELS.get(cfg.transport, cfg.transport),
                "status": status,
            }
        )
    for name, auth_cfg in config.authenticated_only.items():
        rows.append(
            {
                "platform": name,
                "enabled": "no",
                "transport": "—",
                "status": "authenticated-only: " + auth_cfg.reason,
            }
        )
    return rows


def preflight_checks(config: AppConfig, secrets: Secrets) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []

    def add(check: str, level: str, detail: str) -> None:
        results.append({"check": check, "level": level, "detail": detail})

    add(
        "python",
        "ok" if __import__("sys").version_info >= (3, 11) else "fail",
        __import__("sys").version.split()[0],
    )

    for module, label, required in (
        ("curl_cffi", "curl_cffi (TLS impersonation)", True),
        ("bs4", "beautifulsoup4 (parsing)", True),
        ("pydantic", "pydantic (config/models)", True),
        ("playwright", "playwright (browser fallback)", False),
    ):
        found = importlib.util.find_spec(module) is not None
        level = "ok" if found else ("fail" if required else "warn")
        add(label, level, "installed" if found else "missing")

    add(
        "dry_run",
        "ok" if config.runtime.dry_run else "warn",
        "enabled (no messages leave the machine)" if config.runtime.dry_run else "LIVE mode",
    )
    from .llm.client import resolve_provider

    provider = resolve_provider(config.llm, secrets)
    llm_ready = config.llm.enabled and provider.configured
    add(
        "llm",
        "ok" if llm_ready else "warn",
        f"provider={provider.name} model={provider.model}"
        if llm_ready
        else "no key — deterministic fallback will be used",
    )
    add(
        "proxies",
        "ok" if secrets.proxies else "warn",
        f"{len(secrets.proxies)} configured"
        if secrets.proxies
        else "none (datacenter IP; Avito will block)",
    )
    if config.captcha.enabled:
        has_key = bool(secrets.capsolver_api_key or secrets.twocaptcha_api_key)
        add(
            "captcha solver",
            "ok" if has_key else "warn",
            f"provider={config.captcha.provider}"
            if has_key
            else "no solver key; challenges will be reported, not solved",
        )
    add(
        "database",
        "ok" if Path(secrets.freelance_bot_db).parent.exists() or True else "warn",
        str(Path(secrets.freelance_bot_db).resolve()),
    )
    add(
        "outbox",
        "ok",
        str(Path(secrets.freelance_bot_outbox).resolve()),
    )
    return results
