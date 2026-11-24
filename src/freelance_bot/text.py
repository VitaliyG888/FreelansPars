"""Небольшие помощники сопоставления текста, общие для скоринга, генерации и защит.

Сопоставление подстрок намеренно не используется для коротких терминов. Без
границ слова навык ``бот`` совпадает с ``работа`` и ``обработка``, что молча
поднимает несвязанные заказы на верх очереди.
"""

from __future__ import annotations

import re
from functools import lru_cache

_WORD_CHARS = "0-9a-zа-яё"

_CONTACT_RE = re.compile(
    r"(?i)(?:"
    r"(?:телеграм|telegram|whatsapp|ватсап|viber|вайбер|skype|скайп)\s*[:@]\s*\S+"
    r"|(?:https?://)?t\.me/\S+"
    r"|@\w{4,}"
    r"|[\w.+-]+@[\w-]+\.[\w.]+"
    r")"
)


def strip_contact_handles(text: str) -> str:
    """Удаляет ники мессенджеров и ссылки, чтобы они не выглядели как совпадение навыка.
    """
    return re.sub(r"\s+", " ", _CONTACT_RE.sub(" ", text)).strip()


@lru_cache(maxsize=4096)
def _term_pattern(needle: str) -> re.Pattern[str]:
    if " " not in needle and len(needle) >= 5 and re.search(r"[а-яё]", needle):
        needle = needle[:-1]
        suffix = r"[а-яё]{0,3}"
    else:
        suffix = ""
    return re.compile(rf"(?<![{_WORD_CHARS}]){re.escape(needle)}{suffix}(?![{_WORD_CHARS}])")


def contains_term(text: str, term: str) -> bool:
    """Возвращает True, если *term* встречается как отдельное слово или фраза.

        Русские термины сопоставляются по основе, поэтому ``парсинг`` находит и
        ``парсинга``, а ``автоматизация`` — ``автоматизации``. Короткие термины
        остаются точными: совпадение ``бот`` внутри ``работа`` было исходным
        источником ложных срабатываний.
    """
    needle = term.strip().lower()
    if not needle:
        return False
    return _term_pattern(needle).search(text.lower()) is not None


def matching_terms(text: str, terms: list[str]) -> list[str]:
    """Возвращает все термины из *terms*, найденные в *text*, сохраняя порядок ввода.
    """
    return [term for term in terms if contains_term(text, term)]
