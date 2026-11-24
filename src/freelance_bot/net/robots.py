"""Загрузка, кеширование и соблюдение robots.txt."""

from __future__ import annotations

import asyncio
import urllib.robotparser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

Fetcher = Callable[[str], Awaitable[tuple[int, str]]]


@dataclass
class RobotsCache:
    fetcher: Fetcher
    user_agent: str = "*"
    enabled: bool = True
    _cache: dict[str, urllib.robotparser.RobotFileParser | None] = field(default_factory=dict)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    async def _parser(self, domain: str) -> urllib.robotparser.RobotFileParser | None:
        if domain in self._cache:
            return self._cache[domain]
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            if domain in self._cache:
                return self._cache[domain]
            parser: urllib.robotparser.RobotFileParser | None = None
            try:
                status, text = await self.fetcher(f"https://{domain}/robots.txt")
                if status == 200 and "html" not in text[:200].lower():
                    parser = urllib.robotparser.RobotFileParser()
                    parser.parse(text.splitlines())
            except Exception:  # noqa: BLE001 - unreachable robots means "no policy published"
                parser = None
            self._cache[domain] = parser
            return parser

    async def allowed(self, url: str) -> tuple[bool, str]:
        """Возвращает (разрешено, причина). Fail-open только при отсутствии robots.txt.
        """
        if not self.enabled:
            return True, "robots check disabled"
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        domain = parts.netloc
        parser = await self._parser(domain)
        if parser is None:
            return True, "no robots.txt"
        path = parts.path or "/"
        if parser.can_fetch(self.user_agent, path):
            return True, "allowed"
        return False, f"robots.txt disallows {path}"

    def clear(self) -> None:
        self._cache.clear()
