"""Контроль качества сообщений: лимиты площадок, признаки ИИ, ссылки, конкретика.

Работает для каждого сгенерированного сообщения, включая вывод LLM. Не прошедшее
сообщение никогда не ставится в очередь: его один раз исправляют или отбрасывают с причинами.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from ..config import MessageLimit, StyleConfig
from ..models import Job

LINK_RE = re.compile(
    r"(https?://|www\.|t\.me/|@[a-z0-9_]{4,}\.(ru|com)|\b[a-z0-9-]+\.(ru|com|net|io)\b)", re.I
)
EMOJI_RE = re.compile("[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff]")
CYRILLIC_RE = re.compile(r"[а-яё]", re.I)
LATIN_RE = re.compile(r"[a-z]", re.I)
TOKEN_RE = re.compile(r"[а-яёa-z0-9]{5,}", re.I)

STOPWORDS = {
    "нужно",
    "надо",
    "чтобы",
    "который",
    "которая",
    "также",
    "очень",
    "можно",
    "будет",
    "есть",
    "этого",
    "этому",
    "после",
    "перед",
    "более",
    "менее",
    "сделать",
    "нужен",
    "нужна",
    "требуется",
    "работа",
    "заказ",
    "проект",
    "задача",
    "если",
    "когда",
    "только",
    "всего",
    "через",
    "между",
    "сайта",
}

GENERIC_OPENERS = (
    "здравствуйте! меня зовут",
    "добрый день! меня зовут",
    "приветствую! меня зовут",
    "я профессиональный",
    "я опытный специалист",
    "готов выполнить вашу задачу",
    "качественно и в срок",
)

# Признаки, по которым текст читается как сгенерированный по-русски.
AI_TELLS = (
    "в современном мире",
    "в заключение",
    "таким образом",
    "стоит отметить",
    "важно отметить",
    "динамично развивающ",
    "комплексное решение",
    "индивидуальный подход",
    "широкий спектр",
    "гибкая система",
    "надеюсь, это сообщение",
    "я уверен, что",
    "как опытный",
    # Дополнительные маркеры, которые часто выдают чат-модели.
    "я являюсь",
    "буду рад сотрудничеству",
    "с удовольствием возьмусь",
    "готов предложить",
    "предлагаю следующие шаги",
    "мой подход к проекту",
    "позвольте мне",
    "вы можете рассчитывать",
    "полностью готов",
    "всё под ключ",
    "под ключ",
    "даю гарантию",
    "работаю над проектами уже",
    "мои услуги",
    "предоставляю",
)

# Фразы синтаксически нормальны, но звучат шаблонно в начале
# тело сообщения.
TEMPLATE_BODY_PHRASES = (
    "мой опыт и навыки позволяют",
    "мой опыт работы позволяет",
    "я хорошо понимаю",
    "задача мне знакома",
    "готов обсудить детали",
    "готов обсудить вашу задачу",
    "буду рад обсудить",
    "с удовольствием обсужу",
)

# Слова, которые редко должны встречаться в коротком первом сообщении.
FILLER_PHRASES = (
    "в наши дни",
    "на сегодняшний день",
    "в текущих реалиях",
    "в рамках поставленной задачи",
    "с учётом всех требований",
    "в полном объёме",
    "на все 100%",
    "в максимально короткие сроки",
    "в кратчайшие сроки",
)


def significant_tokens(text: str) -> set[str]:
    tokens = {t.lower() for t in TOKEN_RE.findall(text)}
    return {t for t in tokens if t not in STOPWORDS}


def specificity(message: str, job: Job) -> float:
    """Доля значимых токенов заказа, повторно использованных в сообщении.

        Знаменатель — заголовок плюс начало ТЗ, а не всё тело: первое сообщение
        не может упомянуть каждый токен ТЗ на 4000 символов, и оценка по всему
        тексту наказывала бы длинные подробные объявления.
    """
    salient = f"{job.title} {job.description[:400]}"
    job_tokens = significant_tokens(salient)
    if not job_tokens:
        return 1.0
    msg_tokens = significant_tokens(message)
    if not msg_tokens:
        return 0.0
    # Похожее на основу сравнение: 5 символов префикса, чтобы терпеть словоформы.
    job_stems = {t[:5] for t in job_tokens}
    msg_stems = {t[:5] for t in msg_tokens}
    return len(job_stems & msg_stems) / len(job_stems)


def cyrillic_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if CYRILLIC_RE.match(c)) / len(letters)


def check(
    message: str,
    job: Job,
    limit: MessageLimit,
    style: StyleConfig,
    extra_banned: Sequence[str] = (),
) -> list[str]:
    """Возвращает список человекочитаемых нарушений (пусто — проверка пройдена).
    """
    violations: list[str] = []
    text = message.strip()
    lowered = text.lower()

    if not text:
        return ["пустое сообщение"]

    if len(text) > limit.max_chars:
        violations.append(f"длина {len(text)} > лимита {limit.max_chars}")

    if not limit.allow_links and LINK_RE.search(text):
        violations.append("содержит ссылку или внешний контакт")

    for phrase in style.banned_phrases:
        if phrase.lower() in lowered:
            violations.append(f"шаблонная фраза: «{phrase}»")
    for phrase in extra_banned:
        if phrase.strip() and phrase.strip().lower() in lowered:
            violations.append(f"запрещённая фраза из профиля: «{phrase.strip()}»")
    for phrase in AI_TELLS:
        if phrase in lowered:
            violations.append(f"признак машинного текста: «{phrase}»")
    for phrase in TEMPLATE_BODY_PHRASES:
        if phrase in lowered:
            violations.append(f"шаблонная формулировка: «{phrase}»")
    for phrase in FILLER_PHRASES:
        if phrase in lowered:
            violations.append(f"вода в тексте: «{phrase}»")

    if EMOJI_RE.search(text) and len(EMOJI_RE.findall(text)) > style.max_emoji:
        violations.append("слишком много эмодзи")

    if text.count("!") > style.max_exclamation:
        violations.append("слишком много восклицательных знаков")

    if cyrillic_ratio(text) < 0.55:
        violations.append("недостаточно русского текста")

    for opener in GENERIC_OPENERS:
        if lowered.startswith(opener):
            violations.append(f"шаблонное начало: «{opener}»")
            break

    overlap = specificity(text, job)
    if overlap < style.min_specificity_overlap:
        violations.append(
            "мало конкретики из задания "
            f"(совпадение {overlap:.0%} < {style.min_specificity_overlap:.0%})"
        )

    # Один вопросительный знак уместен; стена вопросов — это не первое сообщение.
    if text.count("?") > 2:
        violations.append("слишком много вопросов")

    if re.search(r"^\s*[-*•]\s", text, re.M):
        violations.append("маркированный список в первом сообщении")

    # Первое сообщение с нумерованным планом читается как сгенерированное ТЗ.
    if len(re.findall(r"(?:^|\s)\d[.)]\s", text, re.M)) >= 3:
        violations.append("нумерованный список в первом сообщении")

    # Злоупотребление длинными тире — сильный признак русской LLM.
    if text.count("—") >= 4:
        violations.append("слишком много длинных тире")

    return violations
