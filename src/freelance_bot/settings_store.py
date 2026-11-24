"""Сохранение настроек, изменённых командами Telegram.

Базовые файлы (``config.yaml`` и ``profile.yaml``) остаются источником
значений по умолчанию. Всё, что оператор поменял через бота, пишется в
отдельный оверлей ``var/runtime_settings.yaml``:

.. code-block:: yaml

    config:
      runtime: {dry_run: false}
      scoring: {min_skill_match: 0.3}
    profile:
      skills: [Python, FastAPI]

При старте оверлей накладывается поверх базовых файлов. Такой подход не
переписывает пользовательский YAML (комментарии и порядок ключей целы) и
позволяет одной командой ``/reset`` вернуться к файловым значениям.
"""

from __future__ import annotations

import contextlib
import copy
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .config import AppConfig, load_config, overrides_path
from .profile import FreelancerProfile, load_profile

#: Значение «разницы нет» — нужно, чтобы отличить отсутствие изменений от
#: законного пустого словаря/списка.
_NO_DIFF = object()

_ENV_LINE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")


def diff(base: Any, current: Any) -> Any:
    """Возвращает только отличающиеся части *current* относительно *base*."""
    if isinstance(base, dict) and isinstance(current, dict):
        out: dict[str, Any] = {}
        for key, value in current.items():
            if key not in base:
                out[key] = copy.deepcopy(value)
                continue
            nested = diff(base[key], value)
            if nested is not _NO_DIFF:
                out[key] = nested
        return out if out else _NO_DIFF
    if isinstance(base, list) and isinstance(current, list):
        return _NO_DIFF if base == current else copy.deepcopy(current)
    return _NO_DIFF if base == current else copy.deepcopy(current)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _yaml_text(data: Any) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)


class SettingsStore:
    """Хранит разницу между текущими настройками и базовыми YAML-файлами."""

    def __init__(
        self,
        *,
        config_path: str | None = None,
        profile_path: str = "profile.yaml",
        env_path: str | Path = ".env",
        path: str | Path | None = None,
    ) -> None:
        self.path = Path(path) if path else overrides_path()
        self.config_path = config_path
        self.profile_path = profile_path
        self.env_path = Path(env_path)
        self.baseline_config = load_config(config_path, with_overrides=False)
        self.baseline_profile = load_profile(profile_path, with_overrides=False)

    # ------------------------------------------------------------------ запись
    def build_payload(self, config: AppConfig, profile: FreelancerProfile | None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        config_diff = diff(
            self.baseline_config.model_dump(mode="json"),
            config.model_dump(mode="json"),
        )
        if config_diff is not _NO_DIFF:
            payload["config"] = config_diff
        if profile is not None:
            profile_diff = diff(
                self.baseline_profile.model_dump(mode="json"),
                profile.model_dump(mode="json"),
            )
            if profile_diff is not _NO_DIFF:
                payload["profile"] = profile_diff
        return payload

    def persist(self, config: AppConfig, profile: FreelancerProfile | None) -> None:
        """Перезаписывает оверлей текущими отличиями от базовых файлов."""
        payload = self.build_payload(config, profile)
        if not payload:
            self.clear()
            return
        payload["meta"] = {
            "version": 1,
            "profile_path": str(Path(self.profile_path).resolve()),
        }
        _atomic_write(self.path, _yaml_text(payload))

    # ------------------------------------------------------------------ чтение
    def payload(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {}
        return data if isinstance(data, dict) else {}

    def clear(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    def reset(self, section: str | None = None) -> None:
        """Удаляет одну секцию оверлея (``config``/``profile``) или весь файл."""
        if section is None:
            self.clear()
            return
        payload = self.payload()
        payload.pop(section, None)
        if payload:
            _atomic_write(self.path, _yaml_text(payload))
        else:
            self.clear()

    def refresh_baseline(self) -> None:
        """Перечитывает базовые файлы (после ручной правки config/profile)."""
        self.baseline_config = load_config(self.config_path, with_overrides=False)
        self.baseline_profile = load_profile(self.profile_path, with_overrides=False)

    # ------------------------------------------------------------------ секреты
    def update_env(self, values: dict[str, str]) -> None:
        """Точечно обновляет ключи в ``.env``, сохраняя остальные строки."""
        if not values:
            return
        lines: list[str] = []
        if self.env_path.is_file():
            lines = self.env_path.read_text(encoding="utf-8").splitlines()

        seen: set[str] = set()
        for index, line in enumerate(lines):
            match = _ENV_LINE.match(line.strip())
            if not match:
                continue
            key = match.group("key")
            if key in values:
                lines[index] = f"{key}={_env_value(values[key])}"
                seen.add(key)

        for key, value in values.items():
            if key not in seen:
                lines.append(f"{key}={_env_value(value)}")

        _atomic_write(self.env_path, "\n".join(lines).rstrip() + "\n")

        # Текущий процесс должен увидеть новые значения без рестарта.
        for key, value in values.items():
            os.environ[key] = value

    def remove_env(self, keys: list[str]) -> None:
        """Удаляет ключи из ``.env`` и из окружения текущего процесса."""
        if not keys:
            return
        targets = set(keys)
        if self.env_path.is_file():
            lines = self.env_path.read_text(encoding="utf-8").splitlines()
            kept = [
                line
                for line in lines
                if not (
                    (match := _ENV_LINE.match(line.strip()))
                    and match.group("key") in targets
                )
            ]
            if len(kept) != len(lines):
                _atomic_write(self.env_path, "\n".join(kept).rstrip() + "\n")
        for key in targets:
            os.environ.pop(key, None)


def _env_value(value: str) -> str:
    """Кавычит значение, если без этого .env прочитается неверно."""
    text = str(value)
    if not text:
        return ""
    if re.search(r"[\s#\"'`]", text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text
