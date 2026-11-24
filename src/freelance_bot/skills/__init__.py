"""Реестр пакетов навыков для всех подкатегорий Kwork.

Модуль экспортирует ``ALL_PACKS`` — плоский кортеж объектов :class:`SkillPack`,
по одному на каждую из 181 подкатегории Kwork в семи категориях Kwork.
"""

from __future__ import annotations

import importlib

from .base import SkillPack

# --------------------------------------------------------------------------- #
# Явное сопоставление имени модуля с атрибутом, где лежат пакеты.
# --------------------------------------------------------------------------- #

_PACK_SPECS = [
    ("design_packs", "design_skill_packs"),
    ("programming_packs", "programming_skill_packs"),
    ("writing_translations_packs", "writing_translations_skill_packs"),
    ("seo_packs", "seo_skill_packs"),
    ("promotion_packs", "promotion_skill_packs"),
    ("audio_video_packs", "audio_video_skill_packs"),
    ("business_packs", "business_skill_packs"),
]


def _import_mod(name: str):
    """Импортирует модуль пакетов навыков и возвращает его."""
    return importlib.import_module(f"freelance_bot.skills.{name}")


def _ensure_tuple(x) -> tuple:
    """Возвращает *x* кортежем, будь то кортеж, список или один SkillPack."""
    from .base import SkillPack
    if isinstance(x, (tuple, list)):
        return tuple(x)
    if isinstance(x, SkillPack):
        return (x,)
    return ()


def load_alls_packs() -> tuple[SkillPack, ...]:
    """Импортирует все модули пакетов и возвращает плоский кортеж всех экземпляров ``SkillPack``.
    """
    all_packs: list[SkillPack] = []
    for mod_name, attr_name in _PACK_SPECS:
        mod = _import_mod(mod_name)
        attr = getattr(mod, attr_name, None)
        if attr is not None:
            packs = _ensure_tuple(attr)
            all_packs.extend(packs)
    return tuple(all_packs)


# --------------------------------------------------------------------------- #
# Глобальное состояние (заполняется один раз при импорте)
# --------------------------------------------------------------------------- #

_ALL_PACKS: tuple[SkillPack, ...] = load_alls_packs()

#:  Кортеж всех ``SkillPack`` для всех 181 подкатегории Kwork.
ALL_PACKS: tuple[SkillPack, ...] = _ALL_PACKS

#:  Сопоставление канонического ключа (как в таксономии Kwork) с
#   соответствующего ``SkillPack``.  Разложено для поиска за O(1), когда боту нужно
#   получить пакет по ключу.
KEY_TO_PACK: dict[str, SkillPack] = {pack.key: pack for pack in ALL_PACKS}

#:  Сопоставление названия категории с пакетами этой категории.
_grouped: dict[str, list[SkillPack]] = {}
for _pack in ALL_PACKS:
    _grouped.setdefault(_pack.category, []).append(_pack)
CATEGORY_MAP: dict[str, tuple[SkillPack, ...]] = {
    key: tuple(value) for key, value in _grouped.items()
}
del _grouped, _pack

# --------------------------------------------------------------------------- #
# Помощник: получить пакет по каноническому ключу
# --------------------------------------------------------------------------- #


def get_pack(key: str) -> SkillPack | None:
    """Возвращает ``SkillPack`` с заданным каноническим ключом или ``None``."""
    return KEY_TO_PACK.get(key)