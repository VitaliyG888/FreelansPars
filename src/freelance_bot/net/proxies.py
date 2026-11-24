"""Пул прокси с ротацией, остыванием и учётом здоровья каждого прокси."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit


@dataclass
class ProxyState:
    url: str
    failures: int = 0
    successes: int = 0
    cooldown_until: float = 0.0
    last_used: float = 0.0

    @property
    def available(self) -> bool:
        return time.monotonic() >= self.cooldown_until

    def redact(self) -> str:
        """Никогда не логируем учётные данные."""
        if "@" in self.url:
            scheme, rest = self.url.split("://", 1) if "://" in self.url else ("http", self.url)
            return f"{scheme}://***@{rest.rsplit('@', 1)[-1]}"
        return self.url


@dataclass
class ProxyPool:
    urls: list[str] = field(default_factory=list)
    strategy: str = "round_robin"
    cooldown_seconds: float = 300.0
    _states: dict[str, ProxyState] = field(default_factory=dict)
    _cursor: int = 0
    # Закрепление домена за прокси (strategy="sticky"): один домен ходит
    # через один IP, поэтому куки прогрева и TLS-сессия согласованы с ним.
    _assigned: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for url in self.urls:
            self._states.setdefault(url, ProxyState(url=url))

    def __len__(self) -> int:
        return len(self.urls)

    @property
    def enabled(self) -> bool:
        return bool(self.urls)

    def acquire(self, domain: str | None = None, strategy: str | None = None) -> ProxyState | None:
        """Возвращает рабочий прокси согласно стратегии или None.

        Стратегии: ``round_robin`` — следующий прокси на каждый вызов,
        домен игнорируется; ``sticky`` — домен закрепляется за одним
        прокси, смена только когда он остыл после ошибки; ``none`` —
        всегда прямой запрос, пул не используется.
        """
        if not self.urls:
            return None
        mode = strategy or self.strategy
        if mode == "none":
            return None
        if mode == "sticky" and domain:
            return self._acquire_sticky(domain)
        return self._acquire_round_robin()

    def _acquire_round_robin(self) -> ProxyState | None:
        n = len(self.urls)
        for offset in range(n):
            idx = (self._cursor + offset) % n
            state = self._states[self.urls[idx]]
            if state.available:
                self._cursor = (idx + 1) % n
                state.last_used = time.monotonic()
                return state
        return None

    def _acquire_sticky(self, domain: str) -> ProxyState | None:
        """Отдаёт закреплённый за доменом прокси, пока он доступен."""
        assigned = self._assigned.get(domain)
        if assigned:
            state = self._states.get(assigned)
            if state and state.available:
                state.last_used = time.monotonic()
                return state
        # Закреплённый остыл или его нет: берём следующий доступный.
        fresh = self._acquire_round_robin()
        if fresh is not None:
            self._assigned[domain] = fresh.url
            return fresh
        # Все прокси остыли: лучше попытаться через прежний закреплённый,
        # чем не ходить вовсе — остывание лишь эвристика.
        if assigned:
            fallback = self._states.get(assigned)
            if fallback is not None:
                fallback.last_used = time.monotonic()
                return fallback
        return None

    def report_success(self, url: str) -> None:
        state = self._states.get(url)
        if state:
            state.successes += 1
            state.failures = 0

    def report_failure(self, url: str, *, cooldown: bool = True) -> None:
        state = self._states.get(url)
        if not state:
            return
        state.failures += 1
        if cooldown:
            backoff = min(self.cooldown_seconds * (2 ** (state.failures - 1)), 3600)
            state.cooldown_until = time.monotonic() + backoff

    def healthy_snapshot(self) -> list[dict[str, object]]:
        return [
            {
                "proxy": s.redact(),
                "available": s.available,
                "failures": s.failures,
                "successes": s.successes,
            }
            for s in self._states.values()
        ]

    def reset(self) -> None:
        for state in self._states.values():
            state.failures = 0
            state.cooldown_until = 0.0


def playwright_proxy(url: str) -> dict[str, str]:
    """Преобразует URL прокси в формат Playwright, отделяя учётные данные.

    Playwright не принимает ``user:pass@host`` внутри ``server``: логин и пароль
    должны быть отдельными ключами.
    """
    parts = urlsplit(url)
    if not parts.hostname:
        return {"server": url}
    server = f"{parts.scheme or 'http'}://{parts.hostname}"
    if parts.port:
        server += f":{parts.port}"
    proxy: dict[str, str] = {"server": server}
    if parts.username:
        proxy["username"] = unquote(parts.username)
    if parts.password:
        proxy["password"] = unquote(parts.password)
    return proxy
