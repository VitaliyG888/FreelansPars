"""Загрузка конфигурации, значения по умолчанию и работа с секретами."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from freelance_bot.config import (
    AppConfig,
    LLMConfig,
    LLMProviderConfig,
    Secrets,
    load_config,
)
from freelance_bot.llm.client import resolve_provider


def test_shipped_example_config_is_valid() -> None:
    cfg = load_config("config.example.yaml")
    assert cfg.runtime.dry_run is True
    assert cfg.scoring.weights.budget > 0
    assert cfg.platforms["fl_ru"].enabled is True
    assert cfg.platforms["fl_ru"].relay_fallback is True
    assert cfg.platforms["avito_ru"].enabled is True
    assert cfg.platforms["avito_ru"].requires_proxy is True
    assert cfg.network.relay.enabled is True
    assert cfg.network.browser.challenge_wait_ms == 20000
    assert "workzilla" in cfg.authenticated_only
    assert cfg.scoring.market_median_rub["default"] == 8000


def test_kwork_proxy_and_schedule_config() -> None:
    """kwork ходит через прокси (sticky) и опрашивается 2 раза в час."""
    cfg = load_config("config.example.yaml")
    kwork = cfg.platforms["kwork_ru"]
    assert kwork.proxy_strategy == "sticky"
    assert kwork.poll_interval_seconds == 1800
    # Остальные площадки по умолчанию наследуют глобальную стратегию (none),
    # то есть ходят напрямую, как и раньше.
    assert cfg.platforms["fl_ru"].proxy_strategy == ""
    assert cfg.network.proxy_strategy == "none"


def test_platform_proxy_strategy_rejects_unknown_value() -> None:
    from freelance_bot.config import PlatformConfig

    with pytest.raises(ValidationError):
        PlatformConfig(listing_url="https://x.test/", proxy_strategy="bogus")


def test_missing_config_falls_back_to_safe_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "nope.yaml")
    assert cfg.runtime.dry_run is True
    assert cfg.network.respect_robots_txt is True
    assert cfg.messaging.require_human_approval is True


def test_defaults_are_safe() -> None:
    cfg = AppConfig()
    assert cfg.runtime.dry_run is True
    assert cfg.captcha.enabled is True
    assert cfg.messaging.require_human_approval is True


def test_platform_enable_filter() -> None:
    cfg = load_config("config.example.yaml")
    enabled = cfg.enabled_platforms()
    assert "fl_ru" in enabled
    # Avito включён, но сам отключается на лету без RU-прокси (requires_proxy).
    assert "avito_ru" in enabled
    assert enabled["avito_ru"].requires_proxy is True


def test_rate_for_uses_domain_override() -> None:
    cfg = load_config("config.example.yaml")
    assert cfg.network.rate_for("kwork.ru").requests_per_minute == 3
    assert cfg.network.rate_for("unknown.test").requests_per_minute == 4


def test_median_lookup_falls_back_to_default() -> None:
    cfg = load_config("config.example.yaml")
    assert cfg.scoring.median_for("Разработка и IT") == 15000
    assert cfg.scoring.median_for("Неизвестная категория") == 8000
    assert cfg.scoring.median_for(None) == 8000


def test_proxy_parsing_strips_and_splits() -> None:
    s = Secrets(ru_proxy_urls=" http://a:1 , http://b:2 ,, ")
    assert s.proxies == ["http://a:1", "http://b:2"]


def test_empty_proxy_list() -> None:
    assert Secrets(ru_proxy_urls="").proxies == []


def test_message_limits_lookup() -> None:
    cfg = load_config("config.example.yaml")
    assert cfg.messaging.limit_for("kwork_ru").max_chars == 1000
    assert cfg.messaging.limit_for("nonexistent").max_chars == 2000
    assert cfg.messaging.limit_for("fl_ru").allow_links is False


# --------------------------------------------------------------------------- #
# Именованные LLM-провайдеры
# --------------------------------------------------------------------------- #


def test_example_config_has_no_custom_providers() -> None:
    cfg = load_config("config.example.yaml")
    assert cfg.llm.active_provider == ""
    assert cfg.llm.providers == {}


def test_provider_uses_its_own_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER_OPENROUTER_API_KEY", "custom-key")
    cfg = LLMConfig(
        active_provider="openrouter",
        providers={
            "openrouter": LLMProviderConfig(
                base_url="https://openrouter.ai/api/v1/",
                model="deepseek/deepseek-chat",
                api_key_env="LLM_PROVIDER_OPENROUTER_API_KEY",
                headers={"HTTP-Referer": "https://example.com"},
            )
        },
    )
    secrets = Secrets(_env_file=None, llm_api_key="", llm_base_url="", llm_model="")
    provider = resolve_provider(cfg, secrets)
    assert provider.name == "openrouter"
    assert provider.base_url == "https://openrouter.ai/api/v1"
    assert provider.model == "deepseek/deepseek-chat"
    assert provider.api_key == "custom-key"
    assert provider.configured is True
    assert provider.headers["HTTP-Referer"] == "https://example.com"


def test_provider_without_key_for_local_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER_LOCAL_API_KEY", raising=False)
    cfg = LLMConfig(
        active_provider="local",
        providers={
            "local": LLMProviderConfig(
                base_url="http://127.0.0.1:11434/v1",
                model="qwen2.5",
                api_key_env="LLM_PROVIDER_LOCAL_API_KEY",
                api_key_required=False,
            )
        },
    )
    secrets = Secrets(_env_file=None, llm_api_key="", llm_base_url="", llm_model="")
    provider = resolve_provider(cfg, secrets)
    assert provider.api_key == ""
    assert provider.configured is True


def test_default_provider_still_requires_key() -> None:
    cfg = LLMConfig()
    secrets = Secrets(_env_file=None, llm_api_key="", llm_base_url="", llm_model="")
    provider = resolve_provider(cfg, secrets)
    assert provider.name.startswith("default")
    assert provider.configured is False


def test_provider_rejects_bad_url() -> None:
    with pytest.raises(ValidationError):
        LLMProviderConfig(base_url="ftp://example.com", model="m")
