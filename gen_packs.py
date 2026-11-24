"""Генерирует SkillPack для всех 181 подкатегории таксономии Kwork."""

from __future__ import annotations

from freelance_bot.kwork_taxonomy import TAXONOMY
from freelance_bot.skills.base import P


def make_key(sub_name: str) -> str:
    """Создаёт канонический ключ из названия подкатегории."""
    key = sub_name.lower()
    key = key.replace(" ", "_")
    key = key.replace("-", "_")
    key = key.replace("(", "").replace(")", "")
    key = key.replace(",", "").replace("'", "")
    key = key.strip()
    return key


def make_triggers(sub_name: str) -> str:
    """Создаёт триггеры через запятую из названия подкатегории."""
    words = sub_name.lower().split()
    return ", ".join(words)


def make_approach(sub_name: str) -> str:
    """Создаёт человекоподобную формулировку подхода."""
    return f"Создам/напишу/проведу: {sub_name}. Учтем специфику задачи и требования заказчика."


def make_deliverables(sub_name: str) -> str:
    """Создаёт описание результатов."""
    return f"Готовая работа по задаче: {sub_name}. Файлы в требуемом формате, инструкция по использованию."


def generate_all_packs() -> dict[str, tuple]:
    """Генерирует кортежи SkillPack для всех 181 подкатегории таксономии,
    сгруппированные по категориям.
    """
    result = {cat: [] for cat in TAXONOMY}

    for cat_name, cat_data in TAXONOMY.items():
        for group_name, subs in cat_data["groups"].items():
            for sub_name, url in subs:
                key = make_key(sub_name)
                pack = P(
                    key=key,
                    name=sub_name,
                    group=group_name,
                    category=cat_name,
                    triggers=make_triggers(sub_name),
                    approach=make_approach(sub_name),
                    deliverables=make_deliverables(sub_name),
                )
                result[cat_name].append(pack)

    # Преобразуем списки в кортежи
    return {cat: tuple(packs) for cat, packs in result.items()}


if __name__ == "__main__":
    packs = generate_all_packs()
    total = sum(len(v) for v in packs.values())
    print(f"Total packs generated: {total}")
    for cat_name, cat_packs in packs.items():
        print(f"  {cat_name}: {len(cat_packs)} packs")