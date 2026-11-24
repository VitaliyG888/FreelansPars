"""Тестирует функции генерации."""
from __future__ import annotations

from freelance_bot.kwork_taxonomy import TAXONOMY


def make_key(sub_name: str) -> str:
    key = sub_name.lower()
    key = key.replace(" ", "_")
    key = key.replace("-", "_")
    key = key.replace("(", "").replace(")", "")
    key = key.replace(",", "").replace("'", "")
    key = key.strip()
    return key


def make_triggers(sub_name: str) -> str:
    return ", ".join(sub_name.lower().split())


def make_approach(sub_name: str) -> str:
    return f"Создам/напишу/проведу: {sub_name}. Учтем специфику задачи и требования заказчика."


def make_deliverables(sub_name: str) -> str:
    return f"Готовая работа по задаче: {sub_name}. Файлы в требуемом формате, инструкция по использованию."


# Тест с первой подкатегорией из дизайна
cat_data = TAXONOMY["design"]
group_name, subs = list(cat_data["groups"].items())[0]
sub_name, url = subs[0]
print(f"sub_name: {sub_name}")
print(f"make_key: {make_key(sub_name)}")
print(f"make_triggers: {make_triggers(sub_name)}")
print(f"make_approach: {make_approach(sub_name)}")
print(f"make_deliverables: {make_deliverables(sub_name)}")