"""Ссылки на карточки заказов: шаблоны бирж и строгая валидация.

Единственное место, где описаны домены площадок и шаблоны URL. Чтобы добавить
новую биржу, достаточно:

1. добавить адаптер в ``adapters/registry.py``;
2. добавить запись в :data:`EXCHANGE_URL_PATTERNS` и :data:`ALLOWED_HOSTS`.

Шаблон используется только как запасной вариант. Основной источник ссылки —
URL, который адаптер извлёк со страницы листинга: у части площадок адрес
карточки содержит slug и не собирается из одного ``external_id``.

Валидация намеренно консервативная: только ``https``, только точные хосты из
whitelist, без учётных данных в URL, без нестандартных портов. Это защищает
карточку от подмены ссылки и от попадания в Telegram ``javascript:``/``http:``
адресов, которые Bot API всё равно отклонит.
"""

from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit

#: Шаблоны карточек. ``None`` означает, что адрес нельзя восстановить по id:
#: используется только URL, извлечённый адаптером со страницы.
EXCHANGE_URL_PATTERNS: dict[str, str | None] = {
    "kwork_ru": "https://kwork.ru/projects/{external_id}",
    "fl_ru": "https://www.fl.ru/projects/{external_id}/",
    "freelance_ru": "https://freelance.ru/task/view/{external_id}",
    "freelancejob_ru": "https://freelancejob.ru/vacancy/{external_id}/",
    # Ниже адрес карточки содержит slug, поэтому шаблон не применяется.
    "pchel_net": None,
    "weblancer_net": None,
    "freelancehunt_com": None,
    "avito_ru": None,
}

#: Точные хосты, которым разрешено быть ссылкой на заказ. Поддомены, кроме
#: явно перечисленных ``www``, не принимаются.
ALLOWED_HOSTS: dict[str, frozenset[str]] = {
    "kwork_ru": frozenset({"kwork.ru", "www.kwork.ru"}),
    "fl_ru": frozenset({"fl.ru", "www.fl.ru"}),
    "freelance_ru": frozenset({"freelance.ru", "www.freelance.ru"}),
    "freelancejob_ru": frozenset({"freelancejob.ru", "www.freelancejob.ru"}),
    "pchel_net": frozenset({"pchel.net", "www.pchel.net"}),
    "weblancer_net": frozenset({"weblancer.net", "www.weblancer.net"}),
    "freelancehunt_com": frozenset({"freelancehunt.com", "www.freelancehunt.com"}),
    "avito_ru": frozenset({"avito.ru", "www.avito.ru"}),
}

_ALLOWED_PORTS = {None, 443}


def allowed_hosts(platform: str) -> frozenset[str]:
    """Возвращает whitelist хостов площадки (пустой, если площадка неизвестна)."""
    return ALLOWED_HOSTS.get(platform, frozenset())


def safe_order_url(url: str | None, platform: str | None) -> str | None:
    """Проверяет и нормализует ссылку на заказ.

    Возвращает ``https``-URL без fragment, если он безопасен и принадлежит
    домену площадки, иначе ``None``. Функция никогда не бросает исключений:
    битые данные из БД не должны ломать карточку.
    """
    if not url or not platform:
        return None
    hosts = ALLOWED_HOSTS.get(platform)
    if not hosts:
        return None

    raw = str(url).strip()
    if not raw or any(char.isspace() for char in raw):
        return None

    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        # Некорректный порт/скобки в netloc — trafilatura/urllib уже не спасут.
        return None

    if parts.scheme.lower() != "https":
        return None
    if parts.username or parts.password:
        # https://kwork.ru@evil.example/ должен быть отвергнут.
        return None
    if port not in _ALLOWED_PORTS:
        return None

    host = (parts.hostname or "").lower().rstrip(".")
    if host not in hosts:
        return None
    if not parts.path or parts.path == "/":
        return None

    return urlunsplit(("https", host, parts.path, parts.query, ""))


def template_order_url(platform: str, external_id: str) -> str | None:
    """Собирает ссылку по шаблону площадки, если он определён."""
    pattern = EXCHANGE_URL_PATTERNS.get(platform)
    if not pattern:
        return None
    try:
        return pattern.format(external_id=quote(str(external_id), safe=""))
    except (KeyError, ValueError):
        return None


def resolve_order_url(
    platform: str,
    external_id: str,
    scanned_url: str | None = None,
) -> str | None:
    """Возвращает безопасную ссылку на заказ.

    Приоритет: URL со страницы (он точный, включая slug) → шаблон площадки.
    """
    from_scanned = safe_order_url(scanned_url, platform)
    if from_scanned is not None:
        return from_scanned
    return safe_order_url(template_order_url(platform, external_id), platform)
