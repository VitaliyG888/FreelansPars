"""Базовые типы реестра пакетов навыков.

Пакет навыка описывает подход к одной подкатегории Kwork: какие слова её
запускают, что фрилансер реально сделает и что будет сдано.
Пакеты позволяют боту отвечать на заказы в *любой* из 181 подкатегории Kwork,
а не только в тех нескольких, для которых подходы были написаны вручную.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SkillPack:
    """Одна подкатегория Kwork, упакованная как переиспользуемый навык ответа.
    """

    key: str
    name: str
    group: str
    category: str
    triggers: str
    approach: str
    deliverables: str = ""
    domains: tuple[str, ...] = field(default_factory=tuple)

    @property
    def trigger_terms(self) -> tuple[str, ...]:
        """Слова-триггеры через запятую в виде кортежа."""
        return tuple(t.strip() for t in self.triggers.split(",") if t.strip())

    @property
    def deliverable_items(self) -> tuple[str, ...]:
        """Результаты через запятую в виде кортежа."""
        return tuple(t.strip() for t in self.deliverables.split(",") if t.strip())

    def label(self) -> str:
        """Человекочитаемая метка для интерфейса Telegram."""
        return f"{self.group} → {self.name}"


# --------------------------------------------------------------------------- #
# Вспомогательные конструкторы
# --------------------------------------------------------------------------- #


def P(  # noqa: N802 - short constructor used by the data modules
    key: str,
    name: str,
    group: str,
    category: str,
    triggers: str,
    approach: str,
    deliverables: str = "",
    domains: tuple[str, ...] = (),
) -> SkillPack:
    """Собирает SkillPack."""
    return SkillPack(
        key=key,
        name=name,
        group=group,
        category=category,
        triggers=triggers,
        approach=approach,
        deliverables=deliverables,
        domains=domains,
    )
