"""Оверлей настроек: точечное сохранение, загрузка и сброс."""

from __future__ import annotations

from pathlib import Path

import yaml

from freelance_bot.config import load_config
from freelance_bot.profile import load_profile
from freelance_bot.settings_store import SettingsStore, diff


def _write_base(tmp_path: Path) -> tuple[Path, Path]:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "runtime:\n"
        "  dry_run: true\n"
        "scoring:\n"
        "  red_flag_penalty: 0.35\n"
        "platforms: {}\n",
        encoding="utf-8",
    )
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "name: Иван\nskills:\n  - Python\npricing:\n  strategy: markup\n",
        encoding="utf-8",
    )
    return config_path, profile_path


def test_diff_keeps_only_changed_paths() -> None:
    base = {"a": 1, "nested": {"x": 1, "y": 2}, "items": [1, 2]}
    current = {"a": 1, "nested": {"x": 5, "y": 2}, "items": [1, 2, 3]}
    assert diff(base, current) == {"nested": {"x": 5}, "items": [1, 2, 3]}


def test_persist_writes_only_overrides_and_reloads(tmp_path: Path, monkeypatch) -> None:
    config_path, profile_path = _write_base(tmp_path)
    overrides = tmp_path / "var" / "runtime_settings.yaml"
    monkeypatch.setenv("FREELANCE_BOT_CONFIG", str(config_path))
    monkeypatch.setenv("FREELANCE_BOT_OVERRIDES", str(overrides))

    store = SettingsStore(
        config_path=str(config_path), profile_path=str(profile_path), path=overrides
    )
    config = load_config(str(config_path))
    profile = load_profile(profile_path)
    config.runtime.dry_run = False
    config.scoring.red_flag_penalty = 0.25
    profile.skills.append("FastAPI")

    store.persist(config, profile)

    payload = yaml.safe_load(overrides.read_text(encoding="utf-8"))
    assert payload["config"] == {
        "runtime": {"dry_run": False},
        "scoring": {"red_flag_penalty": 0.25},
    }
    assert payload["profile"] == {"skills": ["Python", "FastAPI"]}
    assert payload["meta"]["version"] == 1

    loaded = load_config()
    assert loaded.runtime.dry_run is False
    assert loaded.scoring.red_flag_penalty == 0.25

    loaded_profile = load_profile(profile_path)
    assert loaded_profile.skills == ["Python", "FastAPI"]


def test_reset_restores_file_values(tmp_path: Path, monkeypatch) -> None:
    config_path, profile_path = _write_base(tmp_path)
    overrides = tmp_path / "runtime_settings.yaml"
    monkeypatch.setenv("FREELANCE_BOT_CONFIG", str(config_path))
    monkeypatch.setenv("FREELANCE_BOT_OVERRIDES", str(overrides))

    store = SettingsStore(
        config_path=str(config_path), profile_path=str(profile_path), path=overrides
    )
    config = load_config(str(config_path))
    config.runtime.dry_run = False
    store.persist(config, load_profile(profile_path))
    assert overrides.is_file()

    store.reset("config")
    assert not overrides.is_file() or "config" not in yaml.safe_load(
        overrides.read_text(encoding="utf-8")
    )
    assert load_config().runtime.dry_run is True


def test_env_update_preserves_comments_and_other_keys(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# secrets\nTELEGRAM_BOT_TOKEN=abc\nLLM_MODEL=old\nRU_PROXY_URLS=\n",
        encoding="utf-8",
    )
    store = SettingsStore(path=tmp_path / "overrides.yaml", env_path=env_path)
    store.update_env({"LLM_MODEL": "gpt-4o-mini", "LLM_API_KEY": "sk-test"})

    text = env_path.read_text(encoding="utf-8")
    assert "# secrets" in text
    assert "TELEGRAM_BOT_TOKEN=abc" in text
    assert "LLM_MODEL=gpt-4o-mini" in text
    assert "LLM_API_KEY=sk-test" in text


def test_env_remove_deletes_only_named_keys(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "TELEGRAM_BOT_TOKEN=abc\nLLM_PROVIDER_OLD_API_KEY=secret\nRU_PROXY_URLS=\n",
        encoding="utf-8",
    )
    store = SettingsStore(path=tmp_path / "overrides.yaml", env_path=env_path)
    store.remove_env(["LLM_PROVIDER_OLD_API_KEY"])

    text = env_path.read_text(encoding="utf-8")
    assert "LLM_PROVIDER_OLD_API_KEY" not in text
    assert "TELEGRAM_BOT_TOKEN=abc" in text
    assert "RU_PROXY_URLS=" in text
