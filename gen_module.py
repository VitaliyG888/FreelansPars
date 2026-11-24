"""Генерирует содержимое для 7 модулей пакетов навыков."""

from __future__ import annotations

from freelance_bot.kwork_taxonomy import TAXONOMY
from freelance_bot.skills.base import P


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


# Сопоставляем имена модулей с названиями категорий и именами атрибутов
MODULE_SPECS = [
    ("design_packs", "design", "design_skill_packs"),
    ("programming_packs", "programming", "programming_skill_packs"),
    ("writing_translations_packs", "writing_translations", "writing_translations_skill_packs"),
    ("seo_packs", "seo", "seo_skill_packs"),
    ("promotion_packs", "promotion", "promotion_skill_packs"),
    ("audio_video_packs", "audio_video", "audio_video_skill_packs"),
    ("business_packs", "business", "business_skill_packs"),
]


def generate_module(module_name: str, category: str, attr_name: str) -> str:
    """Генерирует содержимое одного модуля пакетов."""
    cat_data = TAXONOMY[category]
    packs = []
    for group_name, subs in cat_data["groups"].items():
        for sub_name, url in subs:
            key = make_key(sub_name)
            pack = P(
                key=key,
                name=sub_name,
                group=group_name,
                category=category,
                triggers=make_triggers(sub_name),
                approach=make_approach(sub_name),
                deliverables=make_deliverables(sub_name),
            )
            packs.append(pack)

    # Генерируем код Python
    lines = [f"{attr_name} = ["]
    for p in packs:
        # Используем строки напрямую; это уже корректные Python-строки Unicode
        lines.append(f'    P(')
        lines.append(f'        key="{p.key}",')
        lines.append(f'        name="{p.name}",')
        lines.append(f'        group="{p.group}",')
        lines.append(f'        category="{p.category}",')
        lines.append(f'        triggers="{p.triggers}",')
        lines.append(f'        approach="{p.approach}",')
        lines.append(f'        deliverables="{p.deliverables}",')
        lines.append(f'    ),')
    lines.append("]")
    return "\n".join(lines)


if __name__ == "__main__":
    for module_name, category, attr_name in MODULE_SPECS:
        content = generate_module(module_name, category, attr_name)
        print(f"\\n=== {module_name} ===")
        print(content)