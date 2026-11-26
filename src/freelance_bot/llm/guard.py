"""Контроль качества сообщений: лимиты площадок, признаки ИИ, ссылки, конкретика.

Работает для каждого сгенерированного сообщения, включая вывод LLM. Не прошедшее
сообщение никогда не ставится в очередь: его один раз исправляют или отбрасывают с причинами.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from ..config import MessageLimit, StyleConfig
from ..domains import wrong_domain_note
from ..models import Job
from ..text import lowercase_sentence_starts

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

# Письмо должно прямо говорить о готовности взяться именно за этот заказ.
# Без этого отклик читается как рассуждение о теме, а не как предложение
# работы, и заказчик не понимает, будет ли исполнитель что-то делать.
READY_PHRASES = (
    "готов взяться",
    "готова взяться",
    "готов заняться",
    "готова заняться",
    "берусь",
    "возьмусь",
    "могу взяться",
    "готов сделать",
    "готова сделать",
    "готов выполнить",
    "готова выполнить",
)

# Сломанное обращение: «Привет, вы.» — модель смешивает приветствие с
# подлежащим. Такое письмо выдаёт генерацию с первого слова.
BROKEN_GREETING_RE = re.compile(
    r"^\s*(?:привет|здравствуйте|добрый день|добрый вечер)\s*,\s*(?:вы|ты|вас)\b",
    re.I,
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


def _is_job_quote(fragment: str, job: Job) -> bool:
    """Является ли фрагмент дословным куском из заказа.

    Заказчики пишут со строчной буквы и без точек: «… у меня есть флешка, но
    BIOS не пропускает». Когда такое предложение попадает в письмо, проверка
    «маленькая буква после точки» ругалась на текст самого клиента.
    """
    needle = " ".join(fragment.casefold().split())
    if len(needle) < 8:
        return False
    haystack = " ".join(f"{job.title} {job.description}".casefold().split())
    return needle in haystack


def _foreign_link(message: str, job: Job) -> bool:
    """Есть ли в письме ссылка или контакт, которых не было в заказе.

    Заказчик сам пишет адрес страницы («продвинуть giprint.ru/pakety»), и
    модель называет его в письме — это не контакт исполнителя. Без такой
    оговорки годное письмо блокировалось как «содержит ссылку», и заказ
    уходил в запасной текст.
    """
    job_text = f"{job.title} {job.description} {job.url}".casefold()
    for match in LINK_RE.finditer(message):
        found = match.group(0).casefold()
        if found not in job_text:
            return True
    return False


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
    # Слова заказчика — не признак шаблона. Если заказ называется «Сайт под
    # ключ», письмо обязано назвать его так же, иначе непонятно, о чём оно.
    # Раньше такой заголовок сам себя блокировал: письмо не проходило
    # проверку и заказ уходил в чат без текста.
    job_words = f"{job.title} {job.description}".lower()

    def from_job(phrase: str) -> bool:
        return bool(phrase) and phrase in job_words

    if not text:
        return ["пустое сообщение"]

    if len(text) > limit.max_chars:
        violations.append(f"длина {len(text)} > лимита {limit.max_chars}")

    if not limit.allow_links and _foreign_link(text, job):
        violations.append("содержит ссылку или внешний контакт")

    for phrase in style.banned_phrases:
        if phrase.lower() in lowered and not from_job(phrase.lower()):
            violations.append(f"шаблонная фраза: «{phrase}»")
    for phrase in extra_banned:
        banned = phrase.strip().lower()
        if banned and banned in lowered and not from_job(banned):
            violations.append(f"запрещённая фраза из профиля: «{phrase.strip()}»")
    for phrase in AI_TELLS:
        if phrase in lowered and not from_job(phrase):
            violations.append(f"признак машинного текста: «{phrase}»")
    for phrase in TEMPLATE_BODY_PHRASES:
        if phrase in lowered and not from_job(phrase):
            violations.append(f"шаблонная формулировка: «{phrase}»")
    for phrase in FILLER_PHRASES:
        if phrase in lowered and not from_job(phrase):
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

    if BROKEN_GREETING_RE.match(text):
        violations.append("сломанное обращение после приветствия")

    # Предложение, начатое с маленькой буквы после точки («… клиентов. готов
    # взяться за этот заказ»), — явный след генерации и небрежности.
    # Исключения: сокращения («т. к.», «см.») и дословные куски из заказа —
    # заказчики сами пишут со строчной буквы, и виноват в этом не бот.
    for tail in lowercase_sentence_starts(text):
        if _is_job_quote(tail, job):
            continue
        violations.append("предложение начинается с маленькой буквы")
        break

    # Прямое «готов взяться» — требование промпта; модель иногда отвечает
    # рассуждением о теме, и такое письмо не читается как отклик.
    if not any(phrase in lowered for phrase in READY_PHRASES):
        violations.append("нет прямого согласия взяться за заказ")
    # «...это именно то, что я готов взяться.» — оборванная конструкция:
    # после «взяться» должно идти «за». Двоеточие и запятая нормальны
    # («готов взяться: сделаю…»), поэтому ловим только конец предложения.
    if re.search(r"(?:взяться|заняться)\s*[.!?]", lowered):
        violations.append("оборванная фраза «готов взяться» без «за»")

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

    # Письмо должно быть про эту работу. Заказ на сайт с текстом про
    # Telegram-бота — не стилистическая придирка, а прямое несоответствие:
    # заказчик видит, что отклик не про его задачу.
    domain_note = wrong_domain_note(text, job)
    if domain_note:
        violations.append(domain_note)

    return violations
