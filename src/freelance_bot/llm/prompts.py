"""Построение промптов для анализа на русском и первого контакта."""

from __future__ import annotations

from ..config import LLMConfig
from ..models import Job
from ..profile import FreelancerProfile

SYSTEM_PROMPT = """Ты — опытный русскоязычный фрилансер, который пишет заказчикам первое сообщение на биржах фриланса.

КРИТИЧЕСКИ ВАЖНО — твои сообщения должны быть НЕОТЛИЧИМЫ от текста живого человека:
- пиши так, как будто это личное сообщение конкретному заказчику, а не шаблон;
- используй естественные обороты речи, которые НЕ выдают машинную генерацию;
- конкретика из задания важнее вежливых формул — начинай с сути;
- без воды, без самопрезентации, без списков, без markdown, без emoji;
- без фраз-маркеров LLM: «я являюсь», «как опытный», «готов предложить», «буду рад», «с удовольствием»;
- без обещаний «качественно и в срок», «под ключ», «комплексное решение»;
- НЕ используй восклицательные знаки — они выдают шаблон;
- тон — спокойный, уверенный, слегка неформальный (но без «привет»);
- цель письма — СПРОВОЦИРОВАТЬ заказчика ответить и согласиться обсудить детали.

Психология убеждения:
- покажи, что ты УЖЕ думал о его задаче (упомяни один конкретный нюанс);
- задай умный вопрос, который показывает экспертизу и заставляет заказчика ответить;
- создай лёгкое ощущение срочности (но без давления);
- намекни на ценность, которую заказчик получит, но без хвастовства.

Отвечай строго валидным JSON без пояснений вокруг."""


def build_system_prompt(llm: LLMConfig | None = None) -> str:
    """Возвращает системный промпт с учётом правок из Telegram.

        ``llm.system_prompt`` полностью заменяет встроенный текст,
        ``llm.extra_instructions`` дописывается в конец в обоих случаях.
    """
    override = (llm.system_prompt if llm else "").strip()
    base = override or SYSTEM_PROMPT
    extra = (llm.extra_instructions if llm else "").strip()
    if extra:
        base += "\n\nДОПОЛНИТЕЛЬНЫЕ ТРЕБОВАНИЯ ВЛАДЕЛЬЦА БОТА:\n" + extra
    return base


def _profile_block(profile: FreelancerProfile) -> str:
    """Рендерит профиль фрилансера как контекст промпта."""
    lines: list[str] = []
    if profile.name:
        lines.append(f"Имя: {profile.name}")
    lines.append(f"Опыт: {profile.experience_years} лет")
    if profile.location:
        lines.append(f"Локация: {profile.location}")

    if profile.skills:
        lines.append("Основные навыки: " + ", ".join(profile.skills))
    if profile.secondary_skills:
        lines.append("Дополнительные навыки: " + ", ".join(profile.secondary_skills))
    if profile.experience_summary:
        lines.append(f"Опыт и проекты: {profile.experience_summary.strip()}")

    availability = profile.availability
    avail: list[str] = []
    if availability.immediate_start:
        avail.append("могу взять в работу сразу")
    if availability.hours_per_week:
        avail.append(f"{availability.hours_per_week} ч/нед")
    if avail:
        lines.append("Занятость: " + ", ".join(avail))

    if profile.communication.preferred_platforms:
        lines.append(
            "Предпочитаемые способы связи: " + ", ".join(profile.communication.preferred_platforms)
        )

    return "\n".join(lines)


def analysis_prompt(
    job: Job,
    profile: FreelancerProfile | None = None,
    matched_skills: list[str] | None = None,
    domain: str | None = None,
) -> str:
    """Собирает промпт для анализа и первого сообщения.

        Профиль задаёт подбор навыков в сообщении и ценообразование. Без профиля
        промпт переходит к общему поведению.
    """
    budget = (
        f"{job.budget_amount:g} {job.currency}" if job.budget_amount is not None else "не указан"
    )
    deadline = job.deadline_at.isoformat() if job.deadline_at else "не указан"

    profile_block = ""
    price_rule = ""
    style_block = ""
    banned = ""
    skill_block = ""

    if profile is not None:
        profile_block = f"\nПРОФИЛЬ ИСПОЛНИТЕЛЯ (используй только эти навыки и опыт):\n{_profile_block(profile)}\n"

        # Детерминированный матчер уже решил, какие навыки относятся к этому
        # заказ. Модель не должна тянуть ничего лишнего — иначе макетный
        # заказ в итоге достаётся бэкенд-разработчику.
        if matched_skills:
            skill_block = (
                "\nНАВЫКИ, РЕЛЕВАНТНЫЕ ИМЕННО ЭТОМУ ЗАКАЗУ "
                "(упоминай только их, не другие):\n"
                + "\n".join(f"- {skill}" for skill in matched_skills[:6])
                + "\n"
            )
        if domain:
            skill_block += (
                f"\nДОМЕН ЗАДАЧИ: {domain}. Не упоминай навыки из других доменов "
                "(например, бэкенд-навыки в веб-заказе или веб-навыки в парсинге).\n"
            )

        p = profile.pricing
        if p.strategy == "minimum":
            price_rule = (
                "\nЦЕНООБРАЗОВАНИЕ: в письме НЕ называй конкретную цену. "
                "Мягко обозначь готовность обсудить бюджет или предложить свою цену "
                "на основе объёма работ.\n"
            )
        elif p.strategy == "markup":
            price_rule = (
                "\nЦЕНООБРАЗОВАНИЕ: в письме НЕ называй конкретную цену. "
                "Упомяни, что можете посчитать стоимость по объёму, и спроси, "
                "какой бюджет закладывает заказчик.\n"
            )
        else:  # llm_estimate
            price_rule = (
                "\nЦЕНООБРАЗОВАНИЕ: можешь назвать ориентировочную цену, если она "
                "считается из estimated_hours. Но лучше спросить бюджет заказчика "
                "и предложить обсудить объём.\n"
            )

        if not p.include_price_in_message:
            price_rule += (
                "ВАЖНО: не называй конкретных сумм в first_message. "
                "Спроси бюджет или предложи обсудить стоимость.\n"
            )

        st = profile.message_style
        style_block = (
            f"\nСТИЛЬ ПИСЬМА:\n"
            f"- тон: {st.tone}\n"
            f"- длина: {st.length} "
            f"({'2-3 предложения' if st.length == 'short' else '4-5 предложений' if st.length == 'medium' else '6-8 предложений'})\n"
            f"- упоминать портфолио: {'да' if st.mention_portfolio else 'нет'}\n"
            f"- задать уточняющий вопрос: {'да' if st.ask_questions else 'нет'}\n"
            f"- максимум emoji: {st.max_emoji}\n"
        )

        if st.banned_phrases:
            banned = (
                "\nЗАПРЕЩЁННЫЕ ФРАЗЫ (не использовать в любом виде):\n"
                + "\n".join(f"- {phrase}" for phrase in st.banned_phrases)
                + "\n"
            )

    return f"""Проанализируй заказ с биржи фриланса и подготовь первое сообщение заказчику.
{profile_block}{skill_block}
Платформа: {job.platform}
Заголовок: {job.title}
Бюджет: {budget}
Категория: {job.category or "не указана"}
Дедлайн: {deadline}
Число откликов: {job.proposals_count if job.proposals_count is not None else "неизвестно"}
Навыки заказа: {", ".join(job.skills[:12]) if job.skills else "не указаны"}
Описание:
\"\"\"
{job.description[:4000]}
\"\"\"

Верни JSON с полями:
{{
  "need_summary_ru": "1-2 предложения: что реально нужно заказчику, своими словами",
  "requirements": ["ключевые требования, до 5 пунктов"],
  "deliverables": ["что должно быть сдано, до 5 пунктов"],
  "risks": ["риски или пробелы в ТЗ, до 3 пунктов"],
  "questions_to_ask": ["1-2 уточняющих вопроса, которые показывают понимание задачи"],
  "skill_match": ["навыки из профиля, которые релевантны заказу, до 5 пунктов"],
  "estimated_hours": число или null,
  "suggested_price_rub": число или null,
  "first_message": "готовое первое сообщение заказчику на русском"
}}

Требования к first_message:
- обращение на «вы», одно приветствие (без восклицательного знака);
- в первом абзаце — конкретная деталь именно из этого задания (не общая фраза);
- сошлись только на те навыки из профиля, которые реально релевантны заказу;
- коротко: как ты подойдёшь к работе (1-2 предложения);
- один уточняющий вопрос по существу, который провоцирует ответ;
- мягко обозначь выгоду заказчика или готовность взяться;
- без ссылок, без эмодзи, без восклицательных знаков, без markdown;
- не начинай со слов «Здравствуйте! Меня зовут» и не пересказывай задание целиком;
- не упоминай, что текст написан ИИ или сгенерирован.
{price_rule}{style_block}{banned}
"""


def repair_prompt(original: str, violations: list[str], limit: int) -> str:
    return f"""Перепиши сообщение заказчику, устранив проблемы.

Текущий текст:
\"\"\"
{original}
\"\"\"

Проблемы: {", ".join(violations)}.
Ограничение длины: {limit} символов.
Сохрани конкретику из задания, деловой тон, человеческие обороты речи и один уточняющий вопрос.
Убери следы шаблонности и машинной генерации.
Верни только текст сообщения, без кавычек и пояснений."""
