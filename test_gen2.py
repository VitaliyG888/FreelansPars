"""Тестирует генерацию и запись модулей пакетов."""
import importlib
from pathlib import Path


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


MODULE_SPECS = [
    ("design_packs", "design", "design_skill_packs"),
    ("programming_packs", "programming", "programming_skill_packs"),
    ("writing_translations_packs", "writing_translations", "writing_translations_skill_packs"),
    ("seo_packs", "seo", "seo_skill_packs"),
    ("promotion_packs", "promotion", "promotion_skill_packs"),
    ("audio_video_packs", "audio_video", "audio_video_skill_packs"),
    ("business_packs", "business", "business_skill_packs"),
]


def generate_packs(cat_name: str) -> list:
    mod = importlib.import_module("freelance_bot.kwork_taxonomy")
    cat_data = mod.TAXONOMY[cat_name]
    packs = []
    for group_name, subs in cat_data["groups"].items():
        for sub_name, url in subs:
            key = make_key(sub_name)
            packs.append({
                "key": key,
                "name": sub_name,
                "group": group_name,
                "category": cat_name,
                "triggers": make_triggers(sub_name),
                "approach": f"Создам/напишу/проведу: {sub_name}. Учтем специфику задачи и требования заказчика.",
                "deliverables": f"Готовая работа по задаче: {sub_name}. Файлы в требуемом формате, инструкция по использованию.",
            })
    return packs


def write_module_file(module_name: str, attr_name: str, packs: list) -> None:
    import_line = "from .base import P, SkillPack\n\n"
    lines = [import_line, f"{attr_name} = ["]
    for p in packs:
        lines.append("    P(")
        lines.append(f'        key="{p["key"]}",')
        lines.append(f'        name="{p["name"]}",')
        lines.append(f'        group="{p["group"]}",')
        lines.append(f'        category="{p["category"]}",')
        lines.append(f'        triggers="{p["triggers"]}",')
        lines.append(f'        approach="{p["approach"]}",')
        lines.append(f'        deliverables="{p["deliverables"]}",')
        lines.append("    ),")
    lines.append("]")
    content = "\n".join(lines) + "\n"
    file_path = Path("src/freelance_bot/skills") / f"{module_name}.py"
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)


if __name__ == "__main__":
    total = 0
    for module_name, category, attr_name in MODULE_SPECS:
        packs = generate_packs(category)
        write_module_file(module_name, attr_name, packs)
        print(f"Wrote {len(packs)} packs to {module_name}.py")
        total += len(packs)
    print(f"\nTotal packs: {total}")
    print(f"Expected: 181")